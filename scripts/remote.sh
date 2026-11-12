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
    # Read-only: tracked wallets with transfers but no decoded buy/sell in 30 days, and how they group.
    need_docker
    python3 - <<'PY_RUN'
import csv, io, json, time, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:8100/api/v1/leaderboard.csv?window=all&_cb={time.time()}", timeout=60) as r:
    rows = list(csv.DictReader(io.StringIO(r.read().decode())))
keep = "wallet status rank best_rank agora_pnl_hype legs tokens label_status label_flags style style_tags activity quality lb_legs lb_trade_legs lb_transfer_share lb_legs_per_active_day lb_internal_transfers lb_transfer_peers lb_bot_txs lb_bot_fast_share first_seen_at last_trade_at".split()
for x in rows:
    if x["lb_trade_legs"] in ("0", "") and int(x["lb_legs"] or 0) >= 20:
        print("DATA|mover|" + json.dumps({k: x[k] for k in keep if x.get(k, "") != ""}))
PY_RUN
    psql_admin -At -F'|' <<'SQL'
SET default_transaction_read_only = on; SET statement_timeout = '90s';
WITH m AS (SELECT wallet FROM trades WHERE chain_id = 99901 AND block_time > now() - interval '30 days'
       GROUP BY wallet HAVING count(*) FILTER (WHERE kind IN ('BUY','SELL')) = 0 AND count(*) >= 20),
x AS (SELECT t.wallet, coalesce(tx.to_address, '?') AS dest, count(DISTINCT t.tx_hash) AS n,
             count(DISTINCT t.tx_hash) FILTER (WHERE tx.from_address = t.wallet) AS own
        FROM trades t JOIN m USING (wallet)
        LEFT JOIN transactions tx ON tx.chain_id = t.chain_id AND tx.tx_hash = t.tx_hash
       WHERE t.chain_id = 99901 AND t.block_time > now() - interval '30 days' GROUP BY 1, 2),
r AS (SELECT *, row_number() OVER (PARTITION BY wallet ORDER BY n DESC) AS k,
             sum(n) OVER (PARTITION BY wallet) AS total FROM x)
SELECT 'DATA', 'dest', wallet, dest, n, own, total FROM r WHERE k <= 2 ORDER BY wallet, k;
WITH m AS (SELECT wallet FROM trades WHERE chain_id = 99901 AND block_time > now() - interval '30 days'
       GROUP BY wallet HAVING count(*) FILTER (WHERE kind IN ('BUY','SELL')) = 0 AND count(*) >= 20),
e AS (SELECT DISTINCT t.wallet, t.token, t.block_time FROM trades t JOIN m USING (wallet)
       WHERE t.chain_id = 99901 AND t.block_time > now() - interval '30 days'),
n AS (SELECT wallet, count(DISTINCT token) AS toks FROM e GROUP BY 1)
SELECT 'DATA', 'pair', a.wallet, b.wallet, count(DISTINCT a.token), count(*), na.toks, nb.toks
  FROM e a JOIN e b ON a.token = b.token AND a.wallet < b.wallet
   AND b.block_time BETWEEN a.block_time - interval '10 seconds' AND a.block_time + interval '10 seconds'
  JOIN n na ON na.wallet = a.wallet JOIN n nb ON nb.wallet = b.wallet
 GROUP BY a.wallet, b.wallet, na.toks, nb.toks HAVING count(DISTINCT a.token) >= 3;
WITH m AS (SELECT wallet FROM trades WHERE chain_id = 99901 AND block_time > now() - interval '30 days'
       GROUP BY wallet HAVING count(*) FILTER (WHERE kind IN ('BUY','SELL')) = 0 AND count(*) >= 20)
SELECT 'DATA', 'kind', t.wallet, count(*) FILTER (WHERE kind = 'TRANSFER_IN'), count(*) FILTER (WHERE kind = 'TRANSFER_OUT'),
       count(DISTINCT token), count(DISTINCT date_trunc('day', block_time)), min(block_time)::date, max(block_time)::timestamp(0)
  FROM trades t JOIN m USING (wallet) WHERE t.chain_id = 99901 AND t.block_time > now() - interval '30 days' GROUP BY 1;
SQL
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
