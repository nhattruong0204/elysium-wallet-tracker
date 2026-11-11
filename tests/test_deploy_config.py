"""Static guards on the deployment files: the dashboard container must stay least-privilege
and the ops scripts must not be able to touch the bots or do anything destructive."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
REMOTE = (ROOT / "scripts" / "remote.sh").read_text()
MAINT = (ROOT / "scripts" / "vps-maint.sh").read_text()


def web():
    return COMPOSE["services"]["web"]


def test_web_has_no_secrets_other_than_its_narrow_db_and_owner_credentials():
    w = web()
    assert "env_file" not in w                      # no TG_* / POSTGRES_PASSWORD inside this container
    env = w["environment"]
    # DATABASE_URL, the narrow Consensus settings credentials (owner key + consensus_rw
    # password: no tracker, Telegram or admin-database secret) and, at most, the public list of
    # hidden Activity tokens: literal 0x addresses only (no ${...} interpolation).
    assert {"DATABASE_URL", "CONSENSUS_DB_PASSWORD", "CONSENSUS_ADMIN_KEY"} <= set(env)
    assert set(env) <= {"DATABASE_URL", "CONSENSUS_DB_PASSWORD", "CONSENSUS_ADMIN_KEY",
                        "WEB_ACTIVITY_HIDDEN_TOKENS"}
    if "WEB_ACTIVITY_HIDDEN_TOKENS" in env:
        assert re.fullmatch(r"0x[0-9a-f]{40}([ ,]+0x[0-9a-f]{40})*",
                            env["WEB_ACTIVITY_HIDDEN_TOKENS"])
    assert env["DATABASE_URL"].startswith("postgresql+psycopg://dashboard_ro:${DASHBOARD_DB_PASSWORD")
    # Optional owner Save opens a separate, narrowly scoped consensus_rw connection.
    # The website never receives a Telegram bot token, group ID or tracker admin URL.
    assert env["CONSENSUS_DB_PASSWORD"] == "${CONSENSUS_DB_PASSWORD:-}"
    assert env["CONSENSUS_ADMIN_KEY"] == "${CONSENSUS_ADMIN_KEY:-}"
    assert "POSTGRES_PASSWORD" not in str(w) and "TG_" not in str(w)
    assert "CONSENSUS_TG_BOT_TOKEN" not in str(w)
    assert "CONSENSUS_TG_CHAT_ID" not in str(w)


def test_group_bot_is_opt_in_and_cannot_change_existing_services():
    s = COMPOSE["services"]
    group = s["consensus-alerts"]
    assert group["profiles"] == ["consensus-alerts"]
    assert group["command"] == ["--consensus-alerts"]
    assert "ports" not in group and "env_file" not in group
    assert group["read_only"] and group["cap_drop"] == ["ALL"]
    assert "CONSENSUS_TG_BOT_TOKEN" in group["environment"]
    assert "CONSENSUS_TG_CHAT_ID" in group["environment"]
    assert "TG_BOT_TOKEN" not in group["environment"]
    assert "TG_CHAT_ID" not in group["environment"]


def test_web_is_hardened_and_loopback_only():
    w = web()
    assert w["ports"] == ["127.0.0.1:8100:8100"]
    assert w["read_only"] is True and "/tmp" in " ".join(w["tmpfs"])
    assert w["mem_limit"] == "256m"
    assert w["cap_drop"] == ["ALL"] and "no-new-privileges:true" in w["security_opt"]
    assert w["restart"] == "unless-stopped"
    assert "/healthz" in " ".join(w["healthcheck"]["test"])
    assert w["logging"]["options"]["max-size"] and w["logging"]["options"]["max-file"]
    assert w["command"][:1] == ["--web"]
    assert all(v.endswith(":ro") for v in w["volumes"])


def test_only_migrate_receives_the_dashboard_password_for_provisioning():
    assert "DASHBOARD_DB_PASSWORD" in COMPOSE["services"]["migrate"]["environment"]
    for name in ("tracker", "ui", "backup", "postgres"):
        assert "DASHBOARD_DB_PASSWORD" not in str(COMPOSE["services"][name])


def test_existing_services_are_unchanged_in_the_ways_that_matter():
    s = COMPOSE["services"]
    assert s["tracker"]["env_file"] == ".env" and s["ui"]["env_file"] == ".env"
    assert s["tracker"]["mem_limit"] == "256m" and s["ui"]["mem_limit"] == "384m"
    assert "ports" not in s["postgres"]                        # database never published


def code_only(text: str) -> str:
    """Drop comment lines so the guards inspect commands, not prose."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def action_block(name: str) -> str:
    m = re.search(rf"^  {re.escape(name)}\)\n(.*?)^    ;;\n", REMOTE, re.S | re.M)
    assert m, name
    return code_only(m.group(1))


def test_web_action_cannot_touch_the_bots_or_destroy_anything():
    body = action_block("web") + action_block("web-rollback") + action_block("loadtest")
    for forbidden in ("--force-recreate", "docker compose up -d\n", "down", "prune", "volume rm",
                      "rm -rf", "docker rm", "restart", "tracker --once", "DROP ", "--reset"):
        assert forbidden not in body, forbidden
    assert "up -d --no-deps web" in action_block("web")


def test_password_is_never_echoed():
    block = REMOTE[REMOTE.index("ensure_dashboard_password()"):REMOTE.index("psql_admin()")]
    assert 'echo "DASHBOARD_DB_PASSWORD=$pw" >> .env.tmp' in block      # only into the 0600 file
    assert "umask 077" in block
    assert not re.search(r'echo[^>\n]*\$pw[^>\n]*$', block, re.M)


def test_deploy_keeps_the_dashboard_password_across_env_rewrites():
    assert "grep -E '^DASHBOARD_DB_PASSWORD=' .env" in REMOTE[:REMOTE.index("install_config()")]


def test_caddy_steps_write_in_place_and_have_a_rollback():
    assert "sed -i" not in code_only(MAINT)                                # would swap the bind-mounted inode
    assert 'cat "$1" > "$CADDYFILE"' in MAINT
    assert "caddy validate" in MAINT and "caddy reload" in MAINT
    assert "caddy-rollback" in MAINT and "pre-elysium" in MAINT
    assert "post-check FAILED: rolling back automatically" in MAINT


def test_systemd_units_present_and_read_only_monitor():
    svc = (ROOT / "deploy" / "systemd" / "revival-health.service").read_text()
    tmr = (ROOT / "deploy" / "systemd" / "revival-health.timer").read_text()
    assert "Type=oneshot" in svc and "NoNewPrivileges=yes" in svc
    assert "OnUnitActiveSec=5min" in tmr and "WantedBy=timers.target" in tmr
    script = (ROOT / "scripts" / "revival-health.py").read_text()
    assert "mode=ro" in script and "query_only" in script.lower()
    assert "INSERT" not in script and "UPDATE " not in script and "DELETE" not in script
