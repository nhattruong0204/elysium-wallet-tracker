#!/usr/bin/env bash
# Runs ON THE VPS, invoked by .github/workflows/deploy.yml over SSH:
#   ssh vps 'bash -s -- <action> <deploy_dir> <install_docker>' < scripts/remote.sh
# The code bundle and the secrets file are uploaded by the workflow before this runs.
# Never prints secret values.
set -euo pipefail

ACTION="${1:?action}"
DIR="${2:?deploy dir}"
INSTALL_DOCKER="${3:-false}"
export COMPOSE_PROJECT_NAME=elysium

if [ "$ACTION" = preflight ]; then
  # Read-only inspection of the VPS; changes nothing.
  echo "user: $(id -un) (uid $(id -u)), groups: $(id -Gn)"
  echo "os: $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") / $(uname -m)"
  echo "cpu: $(nproc) cores; memory: $(free -h | awk '/Mem:/{print $2" total, "$7" available"}')"
  echo "disk (home): $(df -h "$HOME" | awk 'NR==2{print $4" free of "$2}')"
  if command -v docker >/dev/null; then
    echo "docker: $(docker --version 2>&1)"
    echo "compose: $(docker compose version 2>&1)"
    docker info >/dev/null 2>&1 && echo "docker daemon: reachable" || echo "docker daemon: NOT reachable by this user"
  else
    echo "docker: not installed"
  fi
  for u in https://rpc.thekleos.pro/rpc https://www.agoradex.app/traders https://api.telegram.org \
           https://registry-1.docker.io/v2/ https://pypi.org/simple/; do
    code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$u" || echo "fail")
    echo "reach $u -> $code"
  done
  chain=$(curl -s -m 10 -H 'content-type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' https://rpc.thekleos.pro/rpc || true)
  echo "kleos eth_chainId: $chain"
  [ -d "$DIR" ] && echo "deploy dir: exists" || echo "deploy dir: not created yet"
  exit 0
fi

cd "$DIR"

log() { printf '\n=== %s\n' "$*"; }

need_docker() {
  if docker compose version >/dev/null 2>&1; then
    return 0
  fi
  if [ "$INSTALL_DOCKER" = "true" ] && [ "$(id -u)" = "0" ]; then
    log "installing Docker (official convenience script)"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    docker compose version
    return 0
  fi
  echo "Docker with the compose plugin is not available for user $(id -un)." >&2
  echo "Re-run with install_docker=true as root, or add this user to the docker group." >&2
  exit 1
}

write_env() {
  # .env.incoming holds TG_* from GitHub secrets. POSTGRES_PASSWORD is generated once on
  # the VPS and kept: changing it would lock the app out of the existing database volume.
  # The umask is scoped to this subshell so it only affects .env, not later files.
  (
    umask 077
    pg=""
    if [ -f .env ]; then
      pg="$(grep -E '^POSTGRES_PASSWORD=' .env | head -1 | cut -d= -f2- || true)"
    fi
    if [ -z "$pg" ]; then
      pg="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
      echo "generated a new POSTGRES_PASSWORD (stored only in $DIR/.env)"
    fi
    # Values left empty in .env.incoming keep their current value: the Telegram UI consumes
    # bot updates, so the deploy workflow can no longer auto-detect the chat id each time.
    : > .env.tmp
    echo "POSTGRES_PASSWORD=$pg" >> .env.tmp
    # the dashboard's read-only DB password (created by the `web` action) must survive a deploy
    if [ -f .env ] && grep -qE '^DASHBOARD_DB_PASSWORD=.+' .env; then
      grep -E '^DASHBOARD_DB_PASSWORD=' .env | head -1 >> .env.tmp
    fi
    # the Consensus group settings (created by the `consensus` action) must survive a deploy
    if [ -f .env ]; then grep -E '^CONSENSUS_[A-Z_]+=.+' .env >> .env.tmp || true; fi
    while IFS= read -r line; do
      key="${line%%=*}"; val="${line#*=}"
      if [ -z "$val" ] && [ -f .env ]; then
        val="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true)"
      fi
      echo "${key}=${val}" >> .env.tmp
    done < .env.incoming
    if ! grep -qE '^TG_CHAT_ID=.+' .env.tmp; then
      echo "TG_CHAT_ID is unknown: set the TG_CHAT_ID secret (your Telegram user id)" >&2
      rm -f .env.tmp .env.incoming
      exit 1
    fi
    mv .env.tmp .env
    rm -f .env.incoming
  )
}

install_config() {
  # Read by the container's non-root user (uid 10001): world-readable, no secrets inside.
  mkdir -p config backups
  install -m 0644 deploy/config.yaml config/config.yaml
  install -m 0644 deploy/watchlist.txt config/watchlist.txt
  chmod 0755 config
}

wait_healthy() {
  # usage: wait_healthy [container] [label]
  c="${1:-elysium-wallet-tracker}"; name="${2:-tracker}"
  log "waiting for the $name to report healthy (up to 5 min)"
  for _ in $(seq 1 60); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)"
    case "$state" in
      healthy) echo "$name: healthy"; return 0 ;;
      unhealthy) echo "$name: UNHEALTHY"; return 1 ;;
    esac
    sleep 5
  done
  echo "$name: still '$state' after 5 min"
  return 1
}

ensure_dashboard_password() {
  # Generated once on the VPS, kept in .env (0600), never printed. `--migrate` applies it to
  # the dashboard_ro role (scram-sha-256); the web container reads it from the environment.
  (
    umask 077
    if grep -qE '^DASHBOARD_DB_PASSWORD=.{24,}' .env 2>/dev/null; then
      echo "DASHBOARD_DB_PASSWORD already present in .env (kept)"
    else
      pw="$(openssl rand -hex 24)"
      grep -vE '^DASHBOARD_DB_PASSWORD=' .env > .env.tmp 2>/dev/null || : > .env.tmp
      echo "DASHBOARD_DB_PASSWORD=$pw" >> .env.tmp
      mv .env.tmp .env
      echo "generated DASHBOARD_DB_PASSWORD (stored only in $DIR/.env, mode 0600)"
    fi
    chmod 600 .env
  )
}

ensure_consensus_env() {
  # CONSENSUS_DB_PASSWORD is generated once on the VPS (never leaves it, never printed).
  # The bot token, group id and owner key arrive from GitHub secrets in .env.consensus (0600);
  # an empty value keeps what .env already has. Prints names only, never values.
  (
    umask 077
    touch .env
    if ! grep -qE '^CONSENSUS_DB_PASSWORD=[A-Za-z0-9_-]{32,}' .env; then
      pw="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
      grep -vE '^CONSENSUS_DB_PASSWORD=' .env > .env.tmp || : > .env.tmp
      echo "CONSENSUS_DB_PASSWORD=$pw" >> .env.tmp; mv .env.tmp .env
      echo "generated CONSENSUS_DB_PASSWORD (stored only in $DIR/.env)"
    fi
    if [ -f .env.consensus ]; then
      while IFS= read -r line; do
        key="${line%%=*}"; val="${line#*=}"
        case "$key" in CONSENSUS_TG_BOT_TOKEN|CONSENSUS_TG_CHAT_ID|CONSENSUS_ADMIN_KEY) ;; *) continue ;; esac
        [ -n "$val" ] || continue
        grep -vE "^${key}=" .env > .env.tmp || : > .env.tmp
        echo "${key}=${val}" >> .env.tmp; mv .env.tmp .env
      done < .env.consensus
      rm -f .env.consensus
    fi
  )
  missing=""
  for key in CONSENSUS_TG_BOT_TOKEN CONSENSUS_TG_CHAT_ID CONSENSUS_ADMIN_KEY CONSENSUS_DB_PASSWORD; do
    grep -qE "^${key}=.+" .env || missing="$missing $key"
  done
  if [ -n "$missing" ]; then
    echo "missing:$missing - add them as GitHub repository secrets and run this action again" >&2
    return 1
  fi
  grep -qE '^CONSENSUS_TG_CHAT_ID=-100[0-9]{5,20}$' .env || { echo "CONSENSUS_TG_CHAT_ID must be a supergroup id (-100...)" >&2; return 1; }
  grep -qE '^CONSENSUS_ADMIN_KEY=.{32,}$' .env || { echo "CONSENSUS_ADMIN_KEY must be at least 32 characters" >&2; return 1; }
  if [ "$(grep -E '^CONSENSUS_TG_BOT_TOKEN=' .env | cut -d= -f2-)" = "$(grep -E '^TG_BOT_TOKEN=' .env | cut -d= -f2-)" ]; then
    echo "CONSENSUS_TG_BOT_TOKEN must be a different bot from the existing alert bot" >&2; return 1
  fi
  echo "consensus settings present: bot token, group id, owner key, database password (values not shown)"
}

consensus_state() {
  echo "  containers: $(docker ps --filter name=elysium-consensus-alerts --format '{{.Names}} {{.Status}}' | paste -sd' ' - || true)"
  psql_admin -Atc "SELECT '  schema: ' || coalesce((SELECT version_num FROM alembic_version LIMIT 1), '?') || ', consensus tables: ' || (SELECT count(*) FROM information_schema.tables WHERE table_schema = 'consensus_alerts')" || true
  psql_admin -Atc "SELECT '  saved settings: enabled=' || enabled || ' version=' || version || ' traders=' || min_traders || ' window=' || window_min || 'm min_net=' || min_net_hype || ' range=' || period || ' independent=' || independent FROM consensus_alerts.settings" 2>/dev/null || echo "  saved settings: (table not created yet)"
  psql_admin -Atc "SELECT '  deliveries: ' || coalesce(string_agg(status || '=' || n, ' '), 'none') FROM (SELECT status, count(*) n FROM consensus_alerts.deliveries GROUP BY 1) x" 2>/dev/null || true
  psql_admin -Atc "SELECT '  dashboard_ro can read consensus settings: ' || has_schema_privilege('dashboard_ro', 'consensus_alerts', 'USAGE') || ' (must be false); consensus_rw can read trades: ' || has_table_privilege('consensus_rw', 'public.trades', 'SELECT') || ' (must be false)'" 2>/dev/null || true
}

psql_admin() { docker compose exec -T postgres psql -U tracker -d tracker -X -v ON_ERROR_STOP=0 "$@"; }

web_smoke() {
  log "dashboard smoke test on the loopback port (status, seconds, bytes)"
  local p out code t size
  for p in /healthz / "/?range=7d" /tokens /smart-money /early /scanner /about /robots.txt \
           /api/v1/meta /api/v1/overview /api/v1/tokens /api/v1/early /api/v1/health /static/app.css; do
    out=$(curl -s -o /dev/null -w '%{http_code} %{time_total} %{size_download}' -m 30 "http://127.0.0.1:8100$p" || echo "000 0 0")
    printf '  %-22s %s\n' "$p" "$out"
  done
  log "security headers on /"
  curl -sI -m 10 http://127.0.0.1:8100/ | tr -d '\r' | grep -iE '^(content-security-policy|x-content-type-options|x-frame-options|referrer-policy|cache-control):' | cut -c1-160 | sed 's/^/  /'
  log "public health document (public by design; sanitised)"
  curl -s -m 20 http://127.0.0.1:8100/api/v1/health | python3 -c 'import json,sys; d=json.load(sys.stdin).get("health", {}); print("  status:", d.get("status"), "| reasons:", d.get("reasons")); print("  checks:", [(c.get("name"), c.get("state")) for c in d.get("checks", [])][:12])' 2>&1 || true
}

web_db_checks() {
  log "dashboard_ro role attributes and boundary (admin view)"
  psql_admin -c "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolconnlimit, (rolpassword IS NOT NULL) AS has_password FROM pg_roles WHERE rolname='dashboard_ro'" 2>&1 | sed 's/^/  /'
  psql_admin -c "SELECT (SELECT count(*) FROM public.trades) AS trades_all, (SELECT count(*) FROM public_api.trades) AS trades_public, (SELECT count(*) FROM public.wallet_watchlist WHERE status='active') AS watchlist_all, (SELECT count(*) FROM public_api.wallet_watchlist WHERE status='active') AS watchlist_public, (SELECT count(*) FROM public_api.public_wallets) AS public_wallets, (SELECT count(*) FROM public_api.public_tokens) AS public_tokens" 2>&1 | sed 's/^/  /'
  log "public_wallets view cost on production-size data (EXPLAIN ANALYZE)"
  psql_admin -c "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) SELECT count(*) FROM public_api.public_wallets" 2>&1 | grep -E 'Execution Time|Planning Time' | sed 's/^/  /'
  psql_admin -c "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) SELECT count(*) FROM public_api.trades" 2>&1 | grep -E 'Execution Time' | sed 's/^/  trades view: /'
  log "real login as dashboard_ro from inside the web container (no secrets printed)"
  docker exec -i elysium-web python - <<'PY' 2>&1 | sed 's/^/  /'
import os, sqlalchemy as sa
e = sa.create_engine(os.environ["DATABASE_URL"])
def run(label, sql):
    try:
        with e.connect() as c:
            r = c.execute(sa.text(sql)).fetchall()
            print(f"{label}: OK {r[0][0] if r and len(r[0])==1 else ''}")
    except Exception as ex:
        print(f"{label}: DENIED ({type(ex.orig).__name__ if hasattr(ex, 'orig') else type(ex).__name__})")
run("current_user", "select current_user")
run("read-only default", "show default_transaction_read_only")
run("statement_timeout", "show statement_timeout")
run("search_path", "show search_path")
run("select public_api.trades", "select count(*) from trades")
run("select RAW public.trades (must be denied)", "select count(*) from public.trades")
run("select RAW notification_outbox (must be denied)", "select count(*) from public.notification_outbox")
run("select service_state (whitelisted keys only)", "select count(*) from service_state")
run("insert (must be denied)", "insert into trades(id) values (1)")
run("create temp table (must be denied)", "create temp table x(a int)")
PY
}

tracker_alive() {
  # Never aborts the action (set -e): this is a report. Judge the bots by behaviour (cursor and
  # heartbeat advancing), not only by the container flag.
  log "tracker + ui health (must stay healthy)"
  echo "  docker health: tracker=$(docker inspect -f '{{.State.Health.Status}}' elysium-wallet-tracker 2>&1) ui=$(docker inspect -f '{{.State.Health.Status}}' elysium-ui 2>&1)"
  docker exec elysium-wallet-tracker python -m agora_tracker --health 2>&1 | tail -1 | sed 's/^/  tracker --health: /' || true
  docker exec elysium-ui python -m agora_tracker --health-ui 2>&1 | tail -1 | sed 's/^/  ui --health-ui: /' || true
  psql_admin -Atc "SELECT 'cursor block ' || block_number || ', checkpoint updated ' || to_char(updated_at, 'HH24:MI:SS') || ' UTC (now ' || to_char(now(), 'HH24:MI:SS') || ')' FROM indexer_checkpoints ORDER BY updated_at DESC LIMIT 1" | sed 's/^/  /' || true
  psql_admin -Atc "SELECT 'heartbeat age ' || round(extract(epoch FROM now() - updated_at)) || ' s' FROM service_state WHERE key = 'heartbeat'" | sed 's/^/  tracker /' || true
  docker ps --format '  {{.Names}}: {{.Status}}' | sort || true
}

resources() {
  log "resources"
  free -m | awk 'NR<=3{print "  " $0}'
  echo "  load: $(cut -d' ' -f1-3 /proc/loadavg) on $(nproc) cpu"
  df -B1 --output=used,avail / | awk 'NR==2{printf "  disk: used %.2f GB, free %.2f GB\n", $1/1e9, $2/1e9}'
  docker stats --no-stream --format '  {{.Name}}: mem {{.MemUsage}} cpu {{.CPUPerc}}' elysium-web elysium-wallet-tracker elysium-ui elysium-postgres 2>&1
}

status() {
  log "containers"
  docker compose ps
  log "tracker --status"
  docker compose run --rm --no-deps tracker --status || true
  log "leaderboard top 10"
  docker compose run --rm --no-deps tracker --top 10 || true
  log "recent tracker logs"
  docker compose logs --no-color --tail 60 tracker || true
  log "recent ui logs"
  docker compose logs --no-color --tail 30 ui || true
}

muted_queue() {
  # Read-only: queued/retrying vs suppressed alerts for the Telegram-muted tokens (no text shown).
  local toks
  toks=$(docker compose config 2>/dev/null | sed -n 's/^ *ALERTS_TELEGRAM_MUTE_TOKENS: *//p' | tr -d '"' | head -1)
  [ -n "$toks" ] || { echo "  no ALERTS_TELEGRAM_MUTE_TOKENS in the compose file"; return 0; }
  local list
  list=$(echo "$toks" | tr ', ' '\n\n' | grep -E '^0x[0-9a-fA-F]{40}$' | tr 'A-F' 'a-f' | sed "s/.*/'&'/" | paste -sd, -)
  psql_admin -Atc "SELECT 'queued (pending or retrying): ' || count(*) FILTER (WHERE status = 'pending') || ', suppressed: ' || count(*) FILTER (WHERE status = 'dead' AND last_error LIKE 'suppressed:%') || ', sent before the mute: ' || count(*) FILTER (WHERE status = 'sent') FROM notification_outbox WHERE lower(payload->>'token') IN ($list)" | sed 's/^/  muted-token alerts: /' || true
  psql_admin -Atc "SELECT 'all pending: ' || count(*) FROM notification_outbox WHERE status = 'pending'" | sed 's/^/  /' || true
}

web_hidden_tokens() {
  # the web-only hidden list as docker compose resolves it (exact addresses, no secrets)
  docker compose config 2>/dev/null | sed -n 's/^ *WEB_ACTIVITY_HIDDEN_TOKENS: *//p' | tr -d '"' | head -1
}

web_state() {
  # Read-only: what the dashboard runs and with which visibility settings. Prints only the
  # WEB_ACTIVITY_* variable of the container environment (DATABASE_URL holds a password).
  echo "  web container: $(docker inspect -f 'image {{.Image}} started {{.State.StartedAt}} health {{.State.Health.Status}}' elysium-web 2>&1)"
  echo "  web env: $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' elysium-web 2>/dev/null | grep -E '^WEB_ACTIVITY_' | paste -sd' ' - || true)"
  echo "  :latest image: $(docker image inspect -f '{{.Id}} created {{.Created}}' elysium-wallet-tracker:latest 2>&1)"
  echo "  tracker/ui images: $(docker inspect -f '{{.Name}}={{.Image}}' elysium-wallet-tracker elysium-ui 2>&1 | paste -sd' ' -)"
  echo "  config/config.yaml sha256 $(sha256sum config/config.yaml 2>/dev/null | cut -c1-16), deploy/config.yaml sha256 $(sha256sum deploy/config.yaml 2>/dev/null | cut -c1-16)"
  echo "  alerts.mute_tokens in the mounted config (hidden on Activity since #65):"
  sed -n '/^ *mute_tokens:/,/^ *[a-z_]*:/p' config/config.yaml 2>/dev/null | grep -E '0x[0-9a-fA-F]{40}' | sed 's/^/    /' || true
}

db_stats() {
  psql_admin -Atc "SELECT 'db: backends ' || numbackends || ', commits ' || xact_commit || ', rollbacks ' || xact_rollback || ', deadlocks ' || deadlocks || ', temp files ' || temp_files FROM pg_stat_database WHERE datname = 'tracker'" | sed 's/^/  /' || true
  psql_admin -Atc "SELECT 'web db sessions: ' || count(*) || ' (' || count(*) FILTER (WHERE state = 'active') || ' active)' FROM pg_stat_activity WHERE application_name = 'elysium-web'" | sed 's/^/  /' || true
  echo "  postgres log errors (last 15 min): $(docker logs --since 15m elysium-postgres 2>&1 | grep -cE 'ERROR|FATAL' || true)"
}

web_rollback_to_pre_hotfix() {
  # Serves the previous dashboard image WITHOUT moving :latest (the tracker and ui keep theirs).
  docker image inspect elysium-wallet-tracker:pre-web-hotfix >/dev/null 2>&1 || { echo "no pre-web-hotfix image" >&2; return 1; }
  printf 'services:\n  web:\n    image: elysium-wallet-tracker:pre-web-hotfix\n' > docker-compose.web-rollback.yml
  docker compose -f docker-compose.yml -f docker-compose.web-rollback.yml up -d --no-deps --force-recreate web
  wait_healthy elysium-web "dashboard (rolled back)" || true
  web_state
}

case "$ACTION" in
  web-hotfix)
    # Code/config hotfix for the DASHBOARD container only (web hotfix 2026-11-10: the default
    # Activity feed hides WEB_ACTIVITY_HIDDEN_TOKENS). No migration, no install_config (the
    # mounted config/ is not changed), no restart of postgres, tracker, ui, Caddy or Revival.
    need_docker
    [ -f .env ] || { echo ".env missing: run the normal deploy first" >&2; exit 1; }
    grep -E '^(action|commit)=' deploy/OPS_REQUEST 2>/dev/null | sed 's/^/  request: /' || true
    hid="$(web_hidden_tokens)"
    echo "$hid" | grep -qE '^0x[0-9a-f]{40}$' || { echo "WEB_ACTIVITY_HIDDEN_TOKENS missing or not one exact address in docker-compose.yml" >&2; exit 1; }
    echo "  web-only hidden token: $hid"
    log "before"
    web_state
    prev=$(docker inspect -f '{{.Image}}' elysium-web 2>/dev/null || true)
    [ -n "$prev" ] || { echo "no running elysium-web container: use the web action" >&2; exit 1; }
    # The running image id is not always taggable (image record pruned while the container
    # keeps running); then fall back to the current :latest (the previous deploy's build), as
    # the tracker action does.
    if docker tag "$prev" elysium-wallet-tracker:pre-web-hotfix 2>/dev/null; then
      echo "  rollback image: elysium-wallet-tracker:pre-web-hotfix = $prev"
    elif docker tag elysium-wallet-tracker:latest elysium-wallet-tracker:pre-web-hotfix 2>/dev/null; then
      echo "  WARNING: running dashboard image not taggable; rollback image = previous :latest"
    else
      echo "no rollback image could be tagged: refusing to deploy" >&2; exit 1
    fi
    db_stats
    tracker_alive
    muted_queue
    log "before: Activity visibility on the loopback port (report only)"
    python3 scripts/web_activity_check.py --base http://127.0.0.1:8100 --hidden "$hid" 2>&1 | sed 's/^/  /' || true
    log "build the image (retags :latest; tracker/ui keep running their current image)"
    docker compose build migrate
    echo "  new image: $(docker image inspect -f '{{.Id}}' elysium-wallet-tracker:latest)"
    log "recreate the dashboard only (no migration)"
    docker compose up -d --no-deps --force-recreate web
    if ! wait_healthy elysium-web "dashboard"; then
      docker logs --tail 60 elysium-web 2>&1 | sed 's/^/  web| /'
      echo "dashboard not healthy: rolling back" >&2
      web_rollback_to_pre_hotfix || true
      exit 1
    fi
    log "after"
    web_state
    docker logs elysium-web 2>&1 | grep -E 'default Activity hides' | tail -1 | sed 's/^/  web log| /' || true
    log "after: Activity visibility on the loopback port (must pass)"
    if ! python3 scripts/web_activity_check.py --base http://127.0.0.1:8100 --hidden "$hid" --expect-hidden 2>&1 | sed 's/^/  /'; then
      echo "loopback verification failed: rolling back" >&2
      web_rollback_to_pre_hotfix || true
      exit 1
    fi
    log "after: through the public domain (Caddy + TLS)"
    echo | openssl s_client -servername onchainflow.app -connect onchainflow.app:443 2>/dev/null | openssl x509 -noout -subject -enddate 2>/dev/null | sed 's/^/  tls: /' || true
    curl -s -o /dev/null -m 20 -w '  https://onchainflow.app/healthz -> %{http_code} (%{ssl_verify_result} = ssl verify)\n' https://onchainflow.app/healthz || true
    docker ps --filter name=caddy --format '  {{.Names}}: {{.Status}}' || true
    python3 scripts/web_activity_check.py --base https://onchainflow.app --hidden "$hid" --expect-hidden 2>&1 | sed 's/^/  /' || echo "  (public-domain check failed: see above)"
    web_smoke
    web_db_checks
    db_stats
    docker logs --since 5m elysium-web 2>&1 | grep -ciE 'traceback|error' | sed 's/^/  web log error lines (5 min): /' || true
    muted_queue
    tracker_alive
    resources
    ;;
  web-hotfix-check)
    # Read-only follow-up after web-hotfix: visibility, database errors, bots, resources.
    # Postgres lines are reduced to their ERROR/FATAL message (no STATEMENT/DETAIL text, which
    # could quote query parameters): the Actions log of this public repository is public.
    need_docker
    hid="$(web_hidden_tokens)"
    web_state
    python3 scripts/web_activity_check.py --base https://onchainflow.app --hidden "$hid" --expect-hidden 2>&1 | sed 's/^/  /' || true
    db_stats
    log "postgres ERROR/FATAL messages, last 60 min (UTC time, message only)"
    docker logs --since 60m elysium-postgres 2>&1 | grep -E ' (ERROR|FATAL): ' | sed -E 's/^([0-9-]+ [0-9:]+)[^ ]* [A-Z]+ \[[0-9]+\] /\1 /' | cut -c1-160 | sed 's/^/  pg| /' | tail -40 || true
    log "web errors, last 60 min"
    docker logs --since 60m elysium-web 2>&1 | grep -iE 'traceback|error' | grep -vE '" [23][0-9][0-9] ' | cut -c1-200 | tail -20 | sed 's/^/  web| /' || true
    curl -s -m 20 http://127.0.0.1:8100/api/v1/health | python3 -c 'import json,sys; d=json.load(sys.stdin).get("health", {}); print("  health:", d.get("status"), d.get("reasons"))' 2>&1 || true
    tracker_alive
    resources
    ;;
  web-hotfix-rollback)
    need_docker
    log "before"; web_state
    web_rollback_to_pre_hotfix
    tracker_alive
    ;;
  consensus)
    # Bring the dedicated Consensus Telegram group live in one step: secrets -> .env, database
    # dump, migration 0011 (adds schema consensus_alerts + role consensus_rw only), dashboard
    # recreated with the owner-settings endpoint, sender container started. Alerts stay OFF
    # until the owner saves settings with "enabled" on the website. Tracker, ui and postgres
    # are not restarted.
    need_docker
    [ -f .env ] || { echo ".env missing: run the normal deploy first" >&2; exit 1; }
    ensure_consensus_env
    log "before"; consensus_state; tracker_alive
    log "database dump before the migration (kept in backups/, newest 3)"
    mkdir -p backups
    free_kb=$(df -k --output=avail . | tail -1)
    [ "$free_kb" -gt 3000000 ] || { echo "less than 3 GB free: refusing to dump and migrate" >&2; exit 1; }
    dump="backups/pre-consensus-$(date -u +%Y%m%dT%H%M%SZ).dump"
    docker compose exec -T postgres pg_dump -U tracker -Fc tracker > "$dump"
    [ "$(stat -c %s "$dump")" -gt 100000 ] || { echo "dump looks empty: refusing to migrate" >&2; exit 1; }
    chmod 600 "$dump"; echo "  $dump $(du -h "$dump" | cut -f1)"
    ls -1t backups/pre-consensus-*.dump | tail -n +4 | xargs -r rm -f
    docker tag "$(docker inspect -f '{{.Image}}' elysium-web)" elysium-wallet-tracker:pre-web-hotfix 2>/dev/null \
      && echo "  rollback image for the dashboard: elysium-wallet-tracker:pre-web-hotfix" || echo "  WARNING: could not tag the running dashboard image"
    log "build"
    docker compose build migrate
    log "migrate (adds only schema consensus_alerts and role consensus_rw; sets its password)"
    docker compose run --rm migrate
    log "recreate the dashboard only (owner-settings endpoint)"
    docker compose up -d --no-deps --force-recreate web
    if ! wait_healthy elysium-web "dashboard"; then
      docker logs --tail 60 elysium-web 2>&1 | sed 's/^/  web| /'
      echo "dashboard not healthy: rolling the dashboard back (the new schema is additive and stays)" >&2
      printf 'services:\n  web:\n    image: elysium-wallet-tracker:pre-web-hotfix\n' > docker-compose.web-rollback.yml
      docker compose -f docker-compose.yml -f docker-compose.web-rollback.yml up -d --no-deps --force-recreate web || true
      wait_healthy elysium-web "dashboard (rolled back)" || true
      exit 1
    fi
    log "start the Consensus sender"
    docker compose --profile consensus-alerts up -d --no-deps --force-recreate consensus-alerts
    sleep 25
    docker logs --tail 30 elysium-consensus-alerts 2>&1 | grep -vE 'api\.telegram\.org' | cut -c1-200 | sed 's/^/  sender| /' || true
    [ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' elysium-consensus-alerts 2>/dev/null)" = "true 0" ] \
      || { echo "the Consensus sender is not running cleanly (see its log above)" >&2; exit 1; }
    log "after"; consensus_state
    echo "  settings endpoint: $(curl -s -o /dev/null -m 15 -w '%{http_code}' http://127.0.0.1:8100/api/v1/consensus/settings) (200 expected)"
    echo "  anonymous save:    $(curl -s -o /dev/null -m 15 -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{}' http://127.0.0.1:8100/api/v1/consensus/settings) (401 expected)"
    web_smoke
    web_db_checks
    tracker_alive
    resources
    ;;
  consensus-off)
    # Stop the group sender. Settings, history and the dashboard are left as they are.
    need_docker
    docker compose --profile consensus-alerts stop consensus-alerts || true
    docker compose --profile consensus-alerts rm -f consensus-alerts || true
    consensus_state
    tracker_alive
    ;;
  consensus-status)
    need_docker
    consensus_state
    docker logs --since 30m elysium-consensus-alerts 2>&1 | grep -vE 'api\.telegram\.org' | tail -20 | cut -c1-200 | sed 's/^/  sender| /' || true
    tracker_alive
    ;;
  tracker)
    # Code-only hotfix for the TRACKER container (indexer + alert notifier). Does not install
    # config/, does not migrate, does not touch postgres, ui (Telegram menu) or web. The running
    # ui/web containers keep their current image. The previous tracker image is tagged
    # elysium-wallet-tracker:pre-tracker-hotfix for `tracker-rollback`.
    need_docker
    [ -f .env ] || { echo ".env missing: run the normal deploy first" >&2; exit 1; }
    log "before"
    prev=$(docker inspect -f '{{.Image}}' elysium-wallet-tracker 2>/dev/null || true)
    echo "  running tracker image: ${prev:-none}"
    for c in elysium-ui elysium-web; do
      echo "  $c image: $(docker inspect -f '{{.Image}}' "$c" 2>/dev/null || echo none)"
    done
    echo "  current :latest: $(docker image inspect -f '{{.Id}}' elysium-wallet-tracker:latest 2>/dev/null || echo none)"
    # Keep a rollback image. The running container's image id is not always taggable (e.g. an
    # image record pruned while the container keeps running); then fall back to the current
    # :latest, which is what any recreate would have used before this hotfix.
    if [ -n "$prev" ] && docker tag "$prev" elysium-wallet-tracker:pre-tracker-hotfix 2>/dev/null; then
      echo "  rollback image: the running tracker image"
    elif docker tag elysium-wallet-tracker:latest elysium-wallet-tracker:pre-tracker-hotfix 2>/dev/null; then
      echo "  WARNING: running tracker image not taggable; rollback image = previous :latest"
    else
      echo "no rollback image could be tagged: refusing to continue" >&2
      exit 1
    fi
    muted_queue
    tracker_alive
    log "build the image (retags :latest; ui/web keep running their current image)"
    docker compose build migrate
    log "schema head must be unchanged (no migration in a tracker hotfix)"
    docker compose run --rm --no-deps tracker --health >/dev/null 2>&1 && echo "  schema OK for the new code" || echo "  (health check before start is advisory)"
    log "recreate the tracker only"
    docker compose up -d --no-deps --force-recreate tracker
    if ! wait_healthy; then
      docker logs --tail 80 elysium-wallet-tracker 2>&1 | sed 's/^/  tracker| /'
      echo "tracker not healthy: run action tracker-rollback" >&2
      exit 1
    fi
    log "after (give the notifier one drain cycle)"
    sleep 20
    muted_queue
    tracker_alive
    docker compose logs --no-color --since 2m tracker | grep -iE "closed .* queued alert|error|traceback" | tail -20 | sed 's/^/  log| /' || true
    resources
    ;;
  tracker-rollback)
    need_docker
    docker image inspect elysium-wallet-tracker:pre-tracker-hotfix >/dev/null 2>&1 || { echo "no pre-tracker-hotfix image" >&2; exit 1; }
    docker tag elysium-wallet-tracker:pre-tracker-hotfix elysium-wallet-tracker:latest
    docker compose up -d --no-deps --force-recreate tracker
    wait_healthy || true
    tracker_alive
    ;;
  deploy)
    need_docker
    write_env
    install_config
    log "build"
    docker compose build --pull
    log "start database, migrate"
    docker compose up -d postgres
    docker compose run --rm migrate
    log "telegram check"
    docker compose run --rm --no-deps tracker --test-telegram
    log "start everything"
    docker compose up -d
    # config/ is a bind mount: recreate so a config-only change is picked up too
    docker compose up -d --no-deps --force-recreate tracker ui
    wait_healthy || true
    wait_healthy elysium-ui "telegram ui" || true
    status
    ;;
  web)
    # Deploy ONLY the dashboard: tracker, ui and postgres are not restarted or recreated.
    need_docker
    [ -f .env ] || { echo ".env missing: run the normal deploy first" >&2; exit 1; }
    ensure_dashboard_password
    install_config
    log "disk before"; df -B1 --output=used,avail / | awk 'NR==2{printf "  used %.2f GB, free %.2f GB\n", $1/1e9, $2/1e9}'
    log "build the image (retags :latest; running containers keep their current image until recreated)"
    docker compose build migrate
    log "migrate: adds schema public_api + role dashboard_ro (idempotent), applies its password"
    docker compose run --rm migrate
    log "start the dashboard only"
    docker compose up -d --no-deps web
    wait_healthy elysium-web "dashboard" || { docker logs --tail 60 elysium-web 2>&1 | sed 's/^/  web| /'; exit 1; }
    web_smoke
    web_db_checks
    tracker_alive
    resources
    ;;
  web-smoke)
    web_smoke
    web_db_checks
    tracker_alive
    resources
    ;;
  loadtest)
    tracker_alive
    log "load test: ~5 req/s for 5 minutes against http://127.0.0.1:8100 (open loop, aborts if the bots degrade)"
    rc=0
    python3 scripts/loadtest.py --base http://127.0.0.1:8100 --rps 5 --duration 300 || rc=$?
    tracker_alive
    resources
    exit $rc
    ;;
  web-rollback)
    # Stop serving the dashboard (e.g. after a Caddy rollback). Tracker/ui/postgres untouched;
    # the dashboard_ro role and views stay (harmless, additive).
    docker compose stop web
    docker compose ps web
    ;;
  status)
    status
    ;;
  logs)
    docker compose logs --no-color --tail 300 tracker
    log "ui"
    docker compose logs --no-color --tail 150 ui
    ;;
  discover)
    need_docker
    docker compose run --rm --no-deps tracker --discover
    ;;
  dry-run)
    docker compose stop tracker || true
    docker compose run --rm tracker --once --dry-run --log-level INFO || true
    docker compose start tracker || true
    ;;
  test-telegram)
    docker compose run --rm --no-deps tracker --test-telegram
    ;;
  test-card)
    docker compose run --rm --no-deps tracker --test-card
    ;;
  ui)
    # Recreate ONLY the Telegram menu bot (the single getUpdates consumer) on the current image,
    # e.g. after a migration made its old image refuse the schema. No build, no migration;
    # tracker, web, postgres and the Consensus sender are not touched.
    need_docker
    [ -f .env ] || { echo ".env missing: run the normal deploy first" >&2; exit 1; }
    log "before"
    echo "  ui image $(docker inspect -f '{{.Image}}' elysium-ui 2>&1 | cut -c1-19), :latest $(docker image inspect -f '{{.Id}}' elysium-wallet-tracker:latest | cut -c1-19)"
    docker exec elysium-ui python -m agora_tracker --health-ui 2>&1 | sed 's/^/  /' || true
    if docker tag "$(docker inspect -f '{{.Image}}' elysium-ui)" elysium-wallet-tracker:pre-ui-hotfix 2>/dev/null; then
      echo "  rollback image: elysium-wallet-tracker:pre-ui-hotfix"
    else
      # Happens when the image was untagged/pruned while the container kept running. Not a
      # reason to stop: an image that predates the current schema cannot restart anyway.
      echo "  WARNING: the running ui image cannot be tagged; no ui rollback image is kept"
    fi
    docker compose up -d --no-deps --force-recreate ui
    wait_healthy elysium-ui "telegram menu" || { docker logs --tail 60 elysium-ui 2>&1 | sed 's/^/  ui| /'; exit 1; }
    log "after"
    docker exec elysium-ui python -m agora_tracker --health-ui 2>&1 | sed 's/^/  /' || true
    docker logs --since 2m elysium-ui 2>&1 | grep -iE "error|traceback|conflict|getupdates" | tail -5 | cut -c1-200 | sed 's/^/  ui| /' || true
    tracker_alive
    resources
    ;;
  consensus-diag)
    # Why does Telegram refuse the Consensus group's messages? Asks Telegram about the bot and
    # the group and retries the newest refused message ONCE. Prints only Telegram's answers;
    # the bot token and the group id never reach the (public) log.
    need_docker
    ROW_ID=$(psql_admin -Atc "SELECT id FROM consensus_alerts.deliveries WHERE status='dead' ORDER BY id DESC LIMIT 1")
    psql_admin -Atc "SELECT json_build_object('body', body, 'buttons', buttons) FROM consensus_alerts.deliveries WHERE id = ${ROW_ID:-0}" > /tmp/consensus-row.json
    chmod 600 /tmp/consensus-row.json
    TOK="$(grep -E '^CONSENSUS_TG_BOT_TOKEN=' .env | cut -d= -f2-)" CHAT="$(grep -E '^CONSENSUS_TG_CHAT_ID=' .env | cut -d= -f2-)" \
    python3 - /tmp/consensus-row.json > /tmp/consensus-diag.out <<'PY' || true
import json, os, sys, urllib.request, urllib.error
tok, chat = os.environ["TOK"], os.environ["CHAT"]
def call(method, payload):
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}",
                                 data=json.dumps(payload).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": type(e).__name__}
me = call("getMe", {})
print("bot:", me.get("ok"), (me.get("result") or {}).get("username"), me.get("description", ""))
ch = call("getChat", {"chat_id": chat})
res = ch.get("result") or {}
print("group:", ch.get("ok"), res.get("type"), repr(res.get("title")), ch.get("description", ""))
if me.get("ok") and ch.get("ok"):
    m = call("getChatMember", {"chat_id": chat, "user_id": me["result"]["id"]})
    mr = m.get("result") or {}
    print("bot in group:", m.get("ok"), mr.get("status"), "can_post_messages=", mr.get("can_post_messages"), m.get("description", ""))
# Chats the bot was added to (its own update queue; nothing else consumes this bot's updates).
# Prints shape only: the id itself stays off the public log.
up = call("getUpdates", {"timeout": 0, "allowed_updates": ["my_chat_member", "message"]})
seen = {}
for u in up.get("result") or []:
    c = ((u.get("my_chat_member") or u.get("message") or {}).get("chat") or {})
    if c.get("type") in ("group", "supergroup", "channel"):
        seen[c["id"]] = (c.get("type"), c.get("title"))
print("chats in the bot's recent updates:", len(seen), up.get("description", ""))
for cid, (typ, title) in seen.items():
    sid = str(cid)
    print(f"  - {typ} {title!r}: id starts -100={sid.startswith('-100')}, digits={len(sid.lstrip('-'))},"
          f" equals configured={sid == chat}, configured is this with -100 added={chat == '-100' + sid.lstrip('-')}")
print(f"configured id: starts -100={chat.startswith('-100')}, digits={len(chat.lstrip('-'))}")
raw = open(sys.argv[1]).read().strip()
if not raw:
    print("no refused message to retry"); sys.exit(0)
row = json.loads(raw)
for line in row["buttons"]["inline_keyboard"]:
    print("buttons:", [(b["text"], b["url"]) for b in line])
print("body chars:", len(row["body"]))
r = call("sendMessage", {"chat_id": chat, "text": row["body"], "parse_mode": "HTML",
                         "disable_web_page_preview": True, "reply_markup": row["buttons"]})
print("retry send:", r.get("ok"), r.get("description", ""))
if r.get("ok"):
    print("SENT_OK")
PY
    sed 's/^/  /' /tmp/consensus-diag.out
    if grep -q '^SENT_OK' /tmp/consensus-diag.out && [ -n "$ROW_ID" ]; then
      psql_admin -Atc "UPDATE consensus_alerts.deliveries SET status='sent', sent_at=now(), last_error=NULL, attempts=attempts+1 WHERE id=$ROW_ID AND status='dead'" | sed 's/^/  marked delivered: /'
    fi
    rm -f /tmp/consensus-row.json /tmp/consensus-diag.out
    consensus_state
    ;;
  consensus-restart)
    # Rebuild and recreate ONLY the Consensus sender (no migration, web/tracker/ui untouched).
    need_docker
    docker compose build migrate
    docker compose --profile consensus-alerts up -d --no-deps --force-recreate consensus-alerts
    sleep 30
    docker logs --tail 20 elysium-consensus-alerts 2>&1 | grep -vE 'api\.telegram\.org' | cut -c1-200 | sed 's/^/  sender| /' || true
    [ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' elysium-consensus-alerts 2>/dev/null)" = "true 0" ] \
      || { echo "the Consensus sender is not running cleanly" >&2; exit 1; }
    docker exec elysium-consensus-alerts python -c "import sys; from agora_tracker.consensus_delivery import healthy; print('  heartbeat healthy:', healthy())" 2>&1 || true
    consensus_state
    tracker_alive
    ;;
  wallet-probe)
    # Read-only: how tracked wallets made money (all history), analysed on the host.
    need_docker
    rm -rf /tmp/elysium-probe && mkdir -p /tmp/elysium-probe && chmod 700 /tmp/elysium-probe
    psql_admin -At -c "COPY (SELECT id, wallet, token, kind, confidence, coalesce(hype_wei::text,''), qty::text, extract(epoch from block_time)::bigint, block_number, chain_id FROM trades WHERE block_time IS NOT NULL AND kind IN ('BUY','SELL')) TO STDOUT WITH CSV" > /tmp/elysium-probe/trades.csv 2> /tmp/elysium-probe/trades.err || true
    echo "ROWS|trades|$(wc -l < /tmp/elysium-probe/trades.csv)|$(head -c 200 /tmp/elysium-probe/trades.err)"
    psql_admin -At -c "COPY (SELECT trade_id, coalesce(pnl_wei::text,''), coalesce(cost_basis_wei::text,''), proceeds_wei::text, status FROM realized_pnl) TO STDOUT WITH CSV" > /tmp/elysium-probe/realized.csv 2> /tmp/elysium-probe/realized.err || true
    echo "ROWS|realized|$(wc -l < /tmp/elysium-probe/realized.csv)|$(head -c 200 /tmp/elysium-probe/realized.err)"
    psql_admin -At -c "COPY (SELECT address, coalesce(symbol,''), coalesce(launchpad,''), coalesce(extract(epoch from launched_at)::bigint::text,'') FROM token_profiles) TO STDOUT WITH CSV" > /tmp/elysium-probe/profiles.csv 2> /tmp/elysium-probe/profiles.err || true
    echo "ROWS|profiles|$(wc -l < /tmp/elysium-probe/profiles.csv)|$(head -c 200 /tmp/elysium-probe/profiles.err)"
    psql_admin -At -c "COPY (SELECT date_trunc('day', observed_at)::date, avg(hype_usd) FROM hype_usd_rates GROUP BY 1 ORDER BY 1) TO STDOUT WITH CSV" > /tmp/elysium-probe/rates.csv 2> /tmp/elysium-probe/rates.err || true
    echo "ROWS|rates|$(wc -l < /tmp/elysium-probe/rates.csv)|$(head -c 200 /tmp/elysium-probe/rates.err)"
    psql_admin -At -c "COPY (SELECT c.chain_id, c.name, count(t.id), min(t.block_time), max(t.block_time) FROM chains c LEFT JOIN trades t ON t.chain_id = c.chain_id GROUP BY 1, 2) TO STDOUT WITH CSV" > /tmp/elysium-probe/chains.csv 2> /tmp/elysium-probe/chains.err || true
    echo "ROWS|chains|$(wc -l < /tmp/elysium-probe/chains.csv)|$(head -c 200 /tmp/elysium-probe/chains.err)"
    psql_admin -At -c "COPY (SELECT source, status, count(*) FROM wallet_watchlist WHERE removed_at IS NULL GROUP BY 1, 2 ORDER BY 3 DESC) TO STDOUT WITH CSV" > /tmp/elysium-probe/sources.csv 2> /tmp/elysium-probe/sources.err || true
    echo "ROWS|sources|$(wc -l < /tmp/elysium-probe/sources.csv)|$(head -c 200 /tmp/elysium-probe/sources.err)"
    psql_admin -At -c "COPY (SELECT e.address, e.rank, coalesce(e.pnl_usd::text,''), coalesce(e.volume_usd::text,'') FROM leaderboard_snapshot_entries e WHERE e.snapshot_id = (SELECT id FROM leaderboard_snapshots WHERE status IN ('ok','imported') ORDER BY captured_at DESC LIMIT 1)) TO STDOUT WITH CSV" > /tmp/elysium-probe/leaderboard.csv 2> /tmp/elysium-probe/leaderboard.err || true
    echo "ROWS|leaderboard|$(wc -l < /tmp/elysium-probe/leaderboard.csv)|$(head -c 200 /tmp/elysium-probe/leaderboard.err)"
    psql_admin -At -c "COPY (WITH g AS (SELECT tt.chain_id, tt.from_address, tt.token, count(*) AS n, count(DISTINCT tt.to_address) AS w, sum(tt.amount) AS amt FROM token_transfers tt JOIN trades t ON t.chain_id = tt.chain_id AND t.tx_hash = tt.tx_hash AND t.wallet = tt.to_address AND t.token = tt.token AND t.kind = 'TRANSFER_IN' GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 15) SELECT g.from_address, coalesce(p.symbol, ''), g.n, g.w, round(g.amt / 1e18, 2), coalesce(round(g.amt / 1e18 * ls.price_hype, 2)::text, '') FROM g LEFT JOIN token_profiles p ON p.chain_id = g.chain_id AND p.address = g.token LEFT JOIN LATERAL (SELECT s.price_hype FROM token_market_snapshots s WHERE s.chain_id = g.chain_id AND s.token = g.token AND s.price_hype IS NOT NULL ORDER BY s.captured_at DESC LIMIT 1) ls ON true ORDER BY g.n DESC) TO STDOUT WITH CSV" > /tmp/elysium-probe/rewards.csv 2> /tmp/elysium-probe/rewards.err || true
    echo "ROWS|rewards|$(wc -l < /tmp/elysium-probe/rewards.csv)|$(head -c 200 /tmp/elysium-probe/rewards.err)"
    cat > /tmp/elysium-probe/an.py <<'PY_AN'
"""Read-only: how tracked wallets made money on Elysium (all history). Reads CSV extracts."""
import csv, sys, statistics as st, bisect
from collections import defaultdict, Counter
D = sys.argv[1]
WEI = 1e18
def rows(name):
    with open(f"{D}/{name}.csv", newline="") as f:
        return list(csv.reader(f))
def fz(x):
    try: return float(x)
    except Exception: return None
def s8(a): return a[:10]
# ---- load
prof = {}
for a, sym, lp, la in rows("profiles"):
    prof[a] = (sym or a[:8], lp or "unknown", int(la) if la else None)
T = []       # BUY/SELL high-confidence with hype
for i, w, tok, k, conf, hw, q, ts, blk, cid in rows("trades"):
    if k in ("BUY", "SELL") and conf == "high" and hw and fz(hw) and fz(q):
        T.append((int(i), w, tok, k, float(hw) / WEI, float(q), int(ts), int(blk), cid))
T.sort(key=lambda r: (r[6], r[0]))
byid = {r[0]: r for r in T}
R = {}
for tid, pnl, cost, proc, stt in rows("realized"):
    R[int(tid)] = (stt, fz(pnl), fz(cost), fz(proc))
rate = {d: float(v) for d, v in rows("rates")}
avg_rate = st.mean(rate.values()) if rate else None
print("META|trades_bs_high=%d tokens=%d wallets=%d first=%s last=%s avg_hype_usd=%.2f" % (
    len(T), len({r[2] for r in T}), len({r[1] for r in T}),
    __import__("datetime").datetime.utcfromtimestamp(T[0][6]).isoformat()[:16],
    __import__("datetime").datetime.utcfromtimestamp(T[-1][6]).isoformat()[:16], avg_rate or 0))
for r in rows("chains"):
    print("CHAIN|" + "|".join(r))
for r in rows("sources"):
    print("SRC|" + "|".join(r))
sells = [r for r in T if r[3] == "SELL"]
meas = [r for r in sells if r[0] in R and R[r[0]][0] == "measured" and R[r[0]][1] is not None]
print("COVER|sells=%d measured=%d (%.0f%%)" % (len(sells), len(meas), 100 * len(meas) / max(1, len(sells))))
# ---- token first trade, per (wallet, token) positions
t0 = {}
for r in T:
    t0.setdefault(r[2], r[6])
pos = defaultdict(lambda: {"fb": None, "ls": None, "pnl": 0.0, "cost": 0.0, "proc": 0.0, "n": 0, "buy": 0.0})
for r in T:
    p = pos[(r[1], r[2])]
    if r[3] == "BUY":
        if p["fb"] is None: p["fb"] = r[6]
        p["buy"] += r[4]
    elif r[0] in R and R[r[0]][0] == "measured" and R[r[0]][1] is not None:
        st_, pnl, cost, proc = R[r[0]]
        p["pnl"] += pnl / WEI; p["cost"] += (cost or 0) / WEI; p["proc"] += (proc or 0) / WEI
        p["n"] += 1; p["ls"] = r[6]
def creator(w, tok, fb):
    la = prof.get(tok, (None, None, None))[2]
    return la is not None and fb is not None and abs(fb - la) <= 2 and fb <= t0[tok] + 2
launches = Counter()
for (w, tok), p in pos.items():
    if creator(w, tok, p["fb"]):
        launches[w] += 1
serial = {w for w, n in launches.items() if n >= 3}
def klass(w, tok, p):
    la = prof.get(tok, (None, None, None))[2]
    if p["fb"] is None: return "no buy seen"
    if creator(w, tok, p["fb"]): return "1 launch own token"
    if la is None: return "6 no launch time"
    age = p["fb"] - la
    if age < 60: return "2 snipe <60s"
    if age < 600: return "3 early 1-10m"
    if age < 6 * 3600: return "4 momentum 10m-6h"
    return "5 late >6h"
def hold(p):
    if p["fb"] is None or p["ls"] is None: return "?"
    h = p["ls"] - p["fb"]
    return "a <2m" if h < 120 else "b 2-15m" if h < 900 else "c 15m-2h" if h < 7200 else "d 2h-1d" if h < 86400 else "e >1d"
closed = {k: p for k, p in pos.items() if p["n"] > 0}
tot = sum(p["pnl"] for p in closed.values())
print("TOTAL|positions=%d pnl=%.0f HYPE (+%.0f / %.0f) win=%.0f%% wallets=%d" % (
    len(closed), tot, sum(p["pnl"] for p in closed.values() if p["pnl"] > 0),
    sum(p["pnl"] for p in closed.values() if p["pnl"] < 0),
    100 * sum(p["pnl"] > 0 for p in closed.values()) / max(1, len(closed)), len({k[0] for k in closed})))
def table(tag, keyf):
    g = defaultdict(list)
    for k, p in closed.items():
        g[keyf(k, p)].append((k, p))
    for name in sorted(g):
        v = g[name]; ps = [p["pnl"] for _, p in v]
        cost = sum(p["cost"] for _, p in v)
        print("%s|%s|pos=%d wallets=%d pnl=%.0f +%.0f %.0f win=%.0f%% med=%.2f roi=%.0f%%" % (
            tag, name, len(v), len({k[0] for k, _ in v}), sum(ps), sum(x for x in ps if x > 0),
            sum(x for x in ps if x < 0), 100 * sum(x > 0 for x in ps) / len(ps), st.median(ps),
            100 * sum(ps) / cost if cost else 0))
table("CLASS", lambda k, p: klass(k[0], k[1], p))
table("CLASS_NOSERIAL", lambda k, p: ("serial launcher wallet" if k[0] in serial else klass(k[0], k[1], p)))
table("HOLD", lambda k, p: hold(p) if klass(k[0], k[1], p) != "1 launch own token" else "launch (any)")
table("PAD", lambda k, p: prof.get(k[1], (0, "unknown", 0))[1])
import datetime as dt
table("WEEK", lambda k, p: dt.datetime.utcfromtimestamp(p["ls"]).strftime("%G-W%V") + " " + ("launch" if klass(k[0], k[1], p).startswith("1") else "snipe" if klass(k[0], k[1], p).startswith("2") else "other"))
# ---- wallets
wp = defaultdict(float); wcls = defaultdict(lambda: defaultdict(float)); wn = Counter(); wt = defaultdict(set)
for (w, tok), p in closed.items():
    wp[w] += p["pnl"]; wcls[w][klass(w, tok, p)[:1]] += p["pnl"]; wn[w] += p["n"]; wt[w].add(tok)
pos_w = sorted((v for v in wp.values() if v > 0), reverse=True)
print("WALLETS|n=%d up=%d down=%d top1=%.0f%% top10=%.0f%% top50=%.0f%% of gains; serial_launchers=%d (pnl %.0f)" % (
    len(wp), len(pos_w), sum(v < 0 for v in wp.values()),
    100 * sum(pos_w[:1]) / sum(pos_w), 100 * sum(pos_w[:10]) / sum(pos_w), 100 * sum(pos_w[:50]) / sum(pos_w),
    len(serial), sum(wp[w] for w in serial)))
lb = {a: (int(r), fz(p)) for a, r, p, v in rows("leaderboard")}
for w, v in sorted(wp.items(), key=lambda kv: -kv[1])[:25]:
    c = wcls[w]; tt = sum(abs(x) for x in c.values()) or 1
    mix = " ".join("%s:%.0f" % (k, c[k]) for k in sorted(c) if abs(c[k]) / tt > 0.05)
    print("TOPW|%s|pnl=%.0f|closes=%d|tokens=%d|serial=%s|agora_rank=%s agora_pnl_usd=%s|mix %s" % (
        s8(w), v, wn[w], len(wt[w]), w in serial, (lb.get(w) or ("-",))[0],
        "%.0f" % lb[w][1] if w in lb and lb[w][1] is not None else "-", mix))
# ---- best positions (not own launches)
best = sorted(((p["pnl"], k, p) for k, p in closed.items() if not klass(k[0], k[1], p).startswith("1")), key=lambda x: -x[0])[:15]
for pnl, (w, tok), p in best:
    la = prof.get(tok, (None, None, None))[2]
    print("BESTPOS|%s|%s|%s|entry_age=%s|hold=%s|cost=%.1f|proc=%.1f|x%.1f|pnl=%.0f" % (
        s8(w), prof.get(tok, (tok[:8],))[0], klass(w, tok, p)[2:], "%.0fm" % ((p["fb"] - la) / 60) if la and p["fb"] else "?",
        "%.0fm" % (((p["ls"] or 0) - (p["fb"] or 0)) / 60) if p["fb"] and p["ls"] else "?", p["cost"], p["proc"],
        p["proc"] / p["cost"] if p["cost"] else 0, pnl))
# tokens where tracked wallets made the most (non-creator)
tk = defaultdict(lambda: [0.0, 0, 0])
for (w, tok), p in closed.items():
    if klass(w, tok, p).startswith("1"): continue
    tk[tok][0] += p["pnl"]; tk[tok][1] += p["pnl"] > 0; tk[tok][2] += 1
for tok, (v, up, n) in sorted(tk.items(), key=lambda kv: -kv[1][0])[:12]:
    print("BESTTOK|%s|%s|pnl=%.0f|winners=%d of %d" % (prof.get(tok, (tok[:8],))[0], prof.get(tok, (0, "?"))[1], v, up, n))
# ---- walk-forward copy-trading backtest
by_tok = defaultdict(list)
for r in T:
    by_tok[r[2]].append(r)
tok_ts = {k: [r[6] for r in v] for k, v in by_tok.items()}
def px(r): return r[4] / r[5]
def first_after(tok, ts, kind, within):
    L = by_tok[tok]; i = bisect.bisect_left(tok_ts[tok], ts)
    while i < len(L) and L[i][6] <= ts + within:
        if kind is None or L[i][3] == kind: return L[i]
        i += 1
    return None
DAY = 86400
first_day = T[0][6] // DAY * DAY
# daily trailing rank (14d), excluding own launches and snipes and serial launchers
pnl_events = []   # (ts, wallet, token, pnl)
for r in meas:
    w, tok = r[1], r[2]
    k = klass(w, tok, pos[(w, tok)])
    if k[0] in "12" or w in serial: continue
    pnl_events.append((r[6], w, tok, R[r[0]][1] / WEI))
def board(day):
    agg = defaultdict(lambda: [0.0, 0, set()])
    for ts, w, tok, v in pnl_events:
        if day - 14 * DAY <= ts < day:
            a = agg[w]; a[0] += v; a[1] += 1; a[2].add(tok)
    ranked = sorted((w for w, a in agg.items() if a[1] >= 5 and len(a[2]) >= 2 and a[0] > 0), key=lambda w: -agg[w][0])
    return {w: i + 1 for i, w in enumerate(ranked)}
boards = {}
last_day = T[-1][6] // DAY * DAY
d = first_day + 14 * DAY
while d <= last_day:
    boards[d] = board(d); d += DAY
FEE = 0.01
for TOPN in (10, 30):
    for DELAY in (15, 60):
        res = []; lead = []; opened = Counter()
        for (w, tok), p in pos.items():
            if p["fb"] is None: continue
            day = p["fb"] // DAY * DAY
            rk = boards.get(day, {}).get(w)
            if not rk or rk > TOPN: continue
            k = klass(w, tok, p)
            if k[0] in "12": continue
            lb_buy = next(r for r in by_tok[tok] if r[1] == w and r[3] == "BUY")
            ent = first_after(tok, p["fb"] + DELAY, "BUY", 600)
            if ent is None: opened["no entry"] += 1; continue
            ls = next((r for r in by_tok[tok] if r[1] == w and r[3] == "SELL" and r[6] > p["fb"]), None)
            if ls is None:
                ex = by_tok[tok][-1]; opened["leader still holds"] += 1
            else:
                ex = first_after(tok, ls[6] + DELAY, "SELL", 600) or first_after(tok, ls[6] + DELAY, None, 3600)
                if ex is None: opened["no exit price"] += 1; continue
                lead.append(px(ls) / px(lb_buy) - 1)
            res.append(px(ex) / px(ent) * (1 - FEE) ** 2 - 1)
        if res:
            print("COPY|top%d delay=%ds|trades=%d win=%.0f%% mean=%+.1f%% median=%+.1f%% p90=%+.0f%% sum_per_unit=%+.1f|leader_mean=%+.1f%% (n=%d)|%s" % (
                TOPN, DELAY, len(res), 100 * sum(x > 0 for x in res) / len(res), 100 * st.mean(res), 100 * st.median(res),
                100 * sorted(res)[int(0.9 * (len(res) - 1))], sum(res), 100 * st.mean(lead) if lead else 0, len(lead), dict(opened)))
# ---- transfer-ins (rewards / airdrops)
for r in rows("rewards")[:15]:
    print("RECV|" + "|".join(x[:12] if x.startswith("0x") else x for x in r))
PY_AN
    python3 /tmp/elysium-probe/an.py /tmp/elysium-probe 2>&1 | cut -c1-320 | sed 's/^/AN|/' || true
    rm -rf /tmp/elysium-probe
    ;;
  ui-smoke)
    docker compose run --rm --no-deps ui --ui-smoke
    ;;
  restart)
    docker compose restart tracker ui
    wait_healthy || true
    status
    ;;
  stop)
    docker compose stop tracker
    docker compose ps
    ;;
  *)
    echo "unknown action: $ACTION" >&2
    exit 2
    ;;
esac
