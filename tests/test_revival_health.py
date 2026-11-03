"""Revival scan-health monitor: pure logic + a temporary SQLite shaped like scan_runs."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "revival-health.py"
spec = importlib.util.spec_from_file_location("revival_health", SCRIPT)
rh = importlib.util.module_from_spec(spec)
sys.modules["revival_health"] = rh
spec.loader.exec_module(rh)

NOW = 1_800_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_db(path: Path, rows: list[tuple]) -> str:
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE scan_runs (id INTEGER PRIMARY KEY, started TEXT, finished TEXT,
                 duration_seconds REAL, processed INT, errors INT, sent INT,
                 potential_alerts INT, sources_ok INT)""")
    c.executemany("INSERT INTO scan_runs(started, finished, duration_seconds, processed, errors,"
                  " sent, potential_alerts, sources_ok) VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return str(path)


def args_for(db: str, tmp_path: Path, *extra: str):
    return rh.build_parser().parse_args(
        ["--db", db, "--state", str(tmp_path / "state.json"), "--container", "",
         "--min-free-pct", "0", "--min-free-gb", "0", *extra])


def row(finished_ago: float | None, sources_ok: int = 4, processed: int = 30, sent: int = 0):
    fin = None if finished_ago is None else iso(NOW - finished_ago)
    return (iso(NOW - (finished_ago or 0) - 20), fin, 20.0, processed, 0, sent, 0, sources_ok)


def conds(findings):
    return {f.condition for f in findings}


# ---- timestamps ------------------------------------------------------------------------------
@pytest.mark.parametrize("v,expect", [
    (1_800_000_000, 1_800_000_000.0),
    (1_800_000_000_000, 1_800_000_000.0),            # epoch ms
    ("1800000000", 1_800_000_000.0),
    ("2027-01-15T08:00:00Z", datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp()),
    ("2027-01-15 08:00:00", datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp()),
    ("2027-01-15T15:00:00+07:00", datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp()),
])
def test_parse_ts(v, expect):
    assert rh.parse_ts(v) == expect


@pytest.mark.parametrize("v", [None, "", "garbage"])
def test_parse_ts_invalid(v):
    assert rh.parse_ts(v) is None


# ---- scan checks -----------------------------------------------------------------------------
def test_recent_scan_is_healthy_even_with_zero_candidates(tmp_path):
    db = make_db(tmp_path / "r.db", [row(600), row(300, processed=0, sent=0), row(40, processed=0)])
    assert rh.evaluate(args_for(db, tmp_path), NOW) == []


def test_stale_scan_is_reported(tmp_path):
    db = make_db(tmp_path / "r.db", [row(4000), row(3700)])
    f = rh.evaluate(args_for(db, tmp_path), NOW)
    assert conds(f) == {"scan-stale"} and "min ago" in f[0].message


def test_boundary_is_inclusive_of_the_limit(tmp_path):
    db = make_db(tmp_path / "r.db", [row(1200)])
    assert rh.evaluate(args_for(db, tmp_path), NOW) == []
    db2 = make_db(tmp_path / "r2.db", [row(1201)])
    assert conds(rh.evaluate(args_for(db2, tmp_path), NOW)) == {"scan-stale"}


def test_unfinished_newest_run_does_not_mask_staleness(tmp_path):
    db = make_db(tmp_path / "r.db", [row(5000), row(None)])
    f = rh.evaluate(args_for(db, tmp_path), NOW)
    assert conds(f) == {"scan-stale"} and "unfinished" in f[0].message


def test_unfinished_newest_run_with_recent_finish_is_healthy(tmp_path):
    db = make_db(tmp_path / "r.db", [row(200), row(None)])
    assert rh.evaluate(args_for(db, tmp_path), NOW) == []


def test_empty_table_is_stale(tmp_path):
    db = make_db(tmp_path / "r.db", [])
    assert conds(rh.evaluate(args_for(db, tmp_path), NOW)) == {"scan-stale"}


def test_all_sources_failing(tmp_path):
    db = make_db(tmp_path / "r.db", [row(500, 0), row(300, 0), row(60, 0)])
    assert conds(rh.evaluate(args_for(db, tmp_path), NOW)) == {"sources-down"}


def test_partial_source_failure_is_not_an_outage(tmp_path):
    db = make_db(tmp_path / "r.db", [row(500, 0), row(300, 2), row(60, 0)])
    assert rh.evaluate(args_for(db, tmp_path), NOW) == []


def test_missing_database_is_unreadable_not_a_crash(tmp_path):
    f = rh.evaluate(args_for(str(tmp_path / "nope.db"), tmp_path), NOW)
    assert "db-unreadable" in conds(f)


def test_connection_is_read_only(tmp_path):
    db = make_db(tmp_path / "r.db", [row(10)])
    conn = rh.open_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM scan_runs")
    conn.close()
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM scan_runs").fetchone()[0] == 1
    c.close()


def test_schema_without_finished_column_is_reported(tmp_path):
    p = tmp_path / "r.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE scan_runs (id INTEGER PRIMARY KEY, started TEXT)")
    c.commit()
    c.close()
    assert "db-unreadable" in conds(rh.evaluate(args_for(str(p), tmp_path), NOW))


def test_disk_low(tmp_path):
    f = rh.check_disk(str(tmp_path), min_free_pct=101.0, min_free_gb=0)
    assert conds(f) == {"disk-low"}
    assert rh.check_disk(str(tmp_path), min_free_pct=0, min_free_gb=0) == []


def test_container_check_skipped_without_name():
    assert rh.check_container("") == []


# ---- alert decisions -------------------------------------------------------------------------
COOL = 6 * 3600.0


def test_alert_once_then_cooldown_then_repeat_then_recovery():
    f = [rh.Finding("scan-stale", "x")]
    msgs, st = rh.decide({}, f, 1000.0, COOL)
    assert len(msgs) == 1 and "scan-stale" in msgs[0]
    msgs, st = rh.decide(st, f, 1300.0, COOL)
    assert msgs == []                                              # inside cooldown
    msgs, st = rh.decide(st, f, 1000.0 + COOL + 1, COOL)
    assert len(msgs) == 1                                          # reminder after cooldown
    msgs, st = rh.decide(st, [], 1000.0 + COOL + 400, COOL)
    assert len(msgs) == 1 and "recovered" in msgs[0]
    msgs, st = rh.decide(st, [], 1000.0 + COOL + 800, COOL)
    assert msgs == [] and st["conditions"] == {}


def test_db_unreadable_needs_two_consecutive_runs():
    f = [rh.Finding("db-unreadable", "locked")]
    msgs, st = rh.decide({}, f, 1000.0, COOL)
    assert msgs == []
    msgs, st = rh.decide(st, f, 1300.0, COOL)
    assert len(msgs) == 1
    # a blip that clears before confirmation never alerts nor "recovers"
    _, st = rh.decide({}, f, 1000.0, COOL)
    msgs, st = rh.decide(st, [], 1300.0, COOL)
    assert msgs == []


def test_conditions_are_independent():
    msgs, st = rh.decide({}, [rh.Finding("scan-stale", "a")], 1000.0, COOL)
    msgs, st = rh.decide(st, [rh.Finding("scan-stale", "a"), rh.Finding("disk-low", "b")],
                         1300.0, COOL)
    assert len(msgs) == 1 and "disk-low" in msgs[0]


# ---- end to end ------------------------------------------------------------------------------
def write_env(path: Path, **kv):
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))


def test_main_unhealthy_alerts_once_and_recovers(tmp_path, monkeypatch, capsys):
    db = make_db(tmp_path / "r.db", [row(5000)])
    env = tmp_path / ".env"
    write_env(env, TELEGRAM_BOT_TOKEN="TOKEN-SECRET", TELEGRAM_CHAT_ID="42")
    sent = []
    monkeypatch.setattr(rh, "send_telegram", lambda tok, chat, text, **k: sent.append((chat, text)) or True)
    argv = ["--db", db, "--state", str(tmp_path / "s.json"), "--container", "",
            "--min-free-pct", "0", "--min-free-gb", "0", "--env-file", str(env)]
    assert rh.main(argv, now=NOW) == 1
    assert rh.main(argv, now=NOW + 300) == 1
    assert len(sent) == 1 and sent[0][0] == "42"
    # scanner recovers
    c = sqlite3.connect(db)
    c.execute("INSERT INTO scan_runs(started, finished, sources_ok) VALUES (?,?,4)",
              (iso(NOW + 590), iso(NOW + 600)))
    c.commit()
    c.close()
    assert rh.main(argv, now=NOW + 700) == 0
    assert len(sent) == 2 and "recovered" in sent[1][1]
    out = capsys.readouterr()
    assert "TOKEN-SECRET" not in out.out + out.err


def test_failed_send_is_retried_next_run(tmp_path, monkeypatch):
    db = make_db(tmp_path / "r.db", [row(5000)])
    env = tmp_path / ".env"
    write_env(env, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1")
    results = iter([False, True])
    calls = []
    monkeypatch.setattr(rh, "send_telegram", lambda *a, **k: calls.append(1) or next(results))
    argv = ["--db", db, "--state", str(tmp_path / "s.json"), "--container", "",
            "--min-free-pct", "0", "--min-free-gb", "0", "--env-file", str(env)]
    rh.main(argv, now=NOW)
    rh.main(argv, now=NOW + 300)
    assert len(calls) == 2                                          # second run retried


def test_no_alert_mode_sends_nothing_and_writes_no_state(tmp_path, monkeypatch, capsys):
    db = make_db(tmp_path / "r.db", [row(5000)])
    monkeypatch.setattr(rh, "send_telegram", lambda *a, **k: pytest.fail("must not send"))
    st = tmp_path / "s.json"
    code = rh.main(["--db", db, "--state", str(st), "--container", "", "--min-free-pct", "0",
                    "--min-free-gb", "0", "--no-alert", "--json"], now=NOW)
    assert code == 1 and not st.exists()
    assert json.loads(capsys.readouterr().out)["findings"][0]["condition"] == "scan-stale"


def test_missing_credentials_is_an_error_not_a_crash(tmp_path):
    db = make_db(tmp_path / "r.db", [row(5000)])
    code = rh.main(["--db", db, "--state", str(tmp_path / "s.json"), "--container", "",
                    "--min-free-pct", "0", "--min-free-gb", "0",
                    "--env-file", str(tmp_path / "absent.env")], now=NOW)
    assert code == 1


def test_credentials_fallback_order(tmp_path):
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    write_env(a, UNRELATED="1")
    write_env(b, TG_BOT_TOKEN="tt", TG_CHAT_ID="9")
    assert rh.telegram_credentials([str(a), str(b)]) == ("tt", "9")
    write_env(a, TELEGRAM_BOT_TOKEN="rt", TELEGRAM_CHAT_ID="8")
    assert rh.telegram_credentials([str(a), str(b)]) == ("rt", "8")
    assert rh.telegram_credentials([str(tmp_path / "none")]) is None
