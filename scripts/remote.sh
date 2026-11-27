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
    # Read-only: Agora candles for 30-day Consensus card tokens and a sample of serial-launcher tokens (throttled).
    need_docker
    rm -rf /tmp/elysium-probe && mkdir -p /tmp/elysium-probe && chmod 755 /tmp/elysium-probe
    cat > /tmp/elysium-probe/cards.json <<'JSON_CARDS'
[["0x12b973d61cfb8aad43d6050d4365d993bc302684","2026-11-02T12:15:38+00:00",3.281213137915916e-07],["0xb251b136ad20b0c59dda3cda464f5ec1d4d6bec2","2026-11-02T14:46:26+00:00",3.5336432803030305e-08],["0x3cd6b3ec6d822b480ecfedcb4a4579c18a25275e","2026-11-02T15:47:13+00:00",7.36213562581413e-08],["0x5f471e53eda2110981b5ac6a1ee7e7ae5b62b21b","2026-11-02T15:50:29+00:00",2.7230186560826935e-07],["0x7b95669a04a82d5cf820cec4a8cf33b58e0574d4","2026-11-02T15:57:15+00:00",7.087169767819848e-08],["0xb296801a1e514e1363ee0d40f17e1dcdcb43b078","2026-11-02T15:57:32+00:00",8.103300023097779e-08],["0x74f9ad9ab68f0780adea496d10d1bf60f3950d58","2026-11-02T15:58:13+00:00",1.0519135497050088e-07],["0x45d64e20cd617e1574319f1e0803c0aa2b3524b0","2026-11-02T15:59:19+00:00",7.087169767819848e-08],["0x3dde9197211f65ef2eb351de1121bdbe26887902","2026-11-02T16:58:42+00:00",7.618920490656582e-08],["0x392ea0144a622773eaee0fa59141f058ad39eef3","2026-11-02T17:44:17+00:00",7.68648829855896e-07],["0x44d13223e2024c6f3f957d5c9ab98f8a550061f3","2026-11-02T18:15:53+00:00",9.093225819264644e-08],["0x5057d26cd6005ee39c4275f411e9a1d63fad0299","2026-11-02T18:49:52+00:00",3.657525222146423e-08],["0x1d4e52bcc2219ae6b5f3a3281e565aaf3968a034","2026-11-02T21:30:49+00:00",1.1930630950267708e-07],["0xa4d91152e453eac3d6148bc25909b214d6225560","2026-11-02T22:08:20+00:00",5.434104519144119e-07],["0x335931763b9a5c85b2e3f3ac4b513e37b71f903e","2026-11-02T23:45:35+00:00",1.0724811553027373e-07],["0x33275c7e29e58ee1864e6aec63ca4caa3b3e3abd","2026-11-02T23:46:16+00:00",3.47198326556677e-08],["0xae868c9a75db8c9d2a86145995288c096c0fe13a","2026-11-03T00:18:26+00:00",2.28100592683581e-07],["0x60c9bd4c4245ceff1473b43f9629dc7a9c17e415","2026-11-03T02:56:11+00:00",2.285538331935255e-07],["0xf1a9c70301554c7331c8916dce07fc0ea6d0573a","2026-11-03T04:54:17+00:00",1.8024497925825377e-07],["0x468f1c7d4efb3ef3ba8d24b42e439a233bfa3ec8","2026-11-03T06:19:01+00:00",1.7752239966329967e-08],["0xbde53acbc56f5de32473528a6e028a363819f7b1","2026-11-03T07:54:35+00:00",3.945670277875893e-08],["0x7a7ae0e715867fc3b0055192624982017328c60b","2026-11-03T08:29:28+00:00",4.878655940856117e-06],["0x7f10bda46c1e99fe2d64affadc1793a07d5f5aee","2026-11-03T08:38:36+00:00",2.2701947477649324e-07],["0x5e8dd354bed35a8569a172d6d55dcfb269fe7885","2026-11-03T10:07:18+00:00",2.7792399228235545e-08],["0xbb7444bf4599536e915a7fcce172133e331bf715","2026-11-03T10:10:06+00:00",4.5032333561890966e-07],["0x7aa1e7ca2ac9329ac4e19523a57c34bf449a6b4e","2026-11-03T10:11:26+00:00",5.739106274365757e-08],["0x250198e0b9124d09e74e4039cacd910ed20e8cc9","2026-11-03T10:48:04+00:00",1.1086392701926668e-07],["0xe198c1ce5634dfa02b11237628c3c905c813a45a","2026-11-03T10:55:13+00:00",5.83856886152307e-08],["0x5081f109ee7fcfb6159ae1b9349ca8de8a628d0f","2026-11-03T11:45:07+00:00",3.893425299492386e-08],["0x0d1786026728924c149687ae5edc4017674a40c0","2026-11-03T11:47:36+00:00",6.457877319210883e-08],["0x0f79931689af199e7cf868ed1107bba2c83c68f7","2026-11-03T11:53:22+00:00",4.925468043812451e-08],["0x2bb0382f2de14b965e3e6d1546487534bf584dd5","2026-11-03T12:46:48+00:00",6.495976494945449e-08],["0xe1036caa66231b2adeaa7d68781faaace66c9a92","2026-11-03T13:18:50+00:00",4.894811305878384e-08],["0x47ee5b938597ec5794471a74abaff97279eb778d","2026-11-03T14:16:29+00:00",1.9390104669321475e-07],["0x0f79931689af199e7cf868ed1107bba2c83c68f7","2026-11-03T14:20:04+00:00",6.631394096192335e-06],["0x6bd503be90d07dd4d917f885c007afee6e53b403","2026-11-03T16:28:09+00:00",5.930047868451062e-07],["0xd8bc9b34b57b21e45d6cc75879dafa886d8564b6","2026-11-03T16:40:30+00:00",6.497959454492394e-08],["0x623118d7b079b956da88b457c9b49f738ef60999","2026-11-03T16:52:39+00:00",4.7460815406040345e-08],["0x8914cc9321f5446d1864bd3d85e72502e1040999","2026-11-03T19:02:06+00:00",4.517224434765505e-08],["0x51c343ed5131f17895991849860b61c2cb0e7e7a","2026-11-03T19:04:37+00:00",1.5910007296925656e-05],["0x4a7f43111726002b98fca2136d83665c17c20999","2026-11-03T19:12:16+00:00",4.3315032366531635e-08],["0xbdcca0c2480f6f73f8ed9994dd33b1ce58a3f345","2026-11-03T22:54:14+00:00",6.549293623817694e-08],["0x3d77749c33c12b6767e0c3e7e0ee0e98f5550320","2026-11-04T04:12:58+00:00",2.1509592566425468e-08],["0x96ebda0cc687e8962ffa3121a139f6ef6e8b5d24","2026-11-04T05:44:52+00:00",1.1789151199860091e-07],["0x7b1a4fbc49d4052bcaf8b4d3185ba7e7696ca6ea","2026-11-04T06:19:30+00:00",4.9771123809195184e-08],["0xe9ba2f1258f1149728bd9a1a49a106b7446c5e08","2026-11-04T06:28:38+00:00",6.559967215691469e-08],["0xb40b9f481c1cc95b246cbbc2b7e9fc41f16f1cd1","2026-11-04T08:39:34+00:00",5.202488494658633e-07],["0x1e0cc793904f7d33fc632e97fe23b81a00a762a3","2026-11-04T12:42:49+00:00",1.4183928964834505e-05],["0x51c343ed5131f17895991849860b61c2cb0e7e7a","2026-11-04T12:51:19+00:00",1.570812147111698e-05],["0x792aa7771e4e1bd58f84631c915ae98aef45277e","2026-11-04T13:19:27+00:00",2.3937744261011506e-06],["0x8914cc9321f5446d1864bd3d85e72502e1040999","2026-11-04T14:05:45+00:00",5.797382777413295e-08],["0x3003b0433a504f29ad87a34e152fa156dce039fb","2026-11-04T14:09:14+00:00",1.2629034174767891e-07],["0x96f456000ce9f0efc4a4ed9732ac032e7e799088","2026-11-04T15:21:29+00:00",7.026628600758224e-07],["0x7e2e70332829036984fb1d337799ddd43401cdcb","2026-11-04T15:35:59+00:00",9.288359956788853e-08],["0x41a3377c2084103200749a7c138d57c550ae7a02","2026-11-04T15:36:33+00:00",2.5143255127324283e-07],["0x17a90bd323625256e1c8ab16815d5a624a073199","2026-11-04T15:38:12+00:00",7.272304907256499e-07],["0xeb05ab042b6ebb5e22fe2630f51a2b7ba1867dca","2026-11-04T15:52:19+00:00",2.4107042202009133e-07],["0xdeb08b66604958e987bd254921a1e7139dda5ac8","2026-11-04T16:18:41+00:00",5.952321880401016e-07],["0xad332ba68d5d94f042f459e4befa3467bf17fcd2","2026-11-04T16:51:10+00:00",1.161559349217074e-07],["0x8df4f39d592de2aaee15838b265c2392d19a4128","2026-11-04T16:57:50+00:00",2.6436355343802953e-06],["0xeb05ab042b6ebb5e22fe2630f51a2b7ba1867dca","2026-11-04T16:59:14+00:00",5.010833327064541e-07],["0x41a3377c2084103200749a7c138d57c550ae7a02","2026-11-04T17:01:26+00:00",7.220311348109703e-07],["0x3281787548f8315ec91e3559ad40d730bf88c83c","2026-11-04T17:12:44+00:00",1.152019913556588e-07],["0x7c4c1177ea9a3af229e8210a130a267051b445f2","2026-11-04T17:27:10+00:00",2.766585049111534e-07],["0xfffe843046aecefb6a065cb9f4d751275243f742","2026-11-04T17:31:56+00:00",5.613777861836842e-08],["0x00ad061d2f8ae66f707b742b95be2c009a96f566","2026-11-04T17:52:58+00:00",7.009535445875377e-07],["0x17a90bd323625256e1c8ab16815d5a624a073199","2026-11-04T17:58:42+00:00",3.536055746136842e-06],["0x3cc2b40573af6fb670b4c60214907061a9f2bd01","2026-11-04T18:03:24+00:00",1.2691111802909785e-07],["0x7c0843a79c66f38ceea234a082359fc15c863cb1","2026-11-04T18:06:44+00:00",6.690990676476549e-08],["0xef55f0d5b2592d8519b86ef89aa3097c59165bee","2026-11-04T18:07:55+00:00",3.9060335014541067e-07],["0x193ea6f034112491749c64556931345076d995f6","2026-11-04T18:17:33+00:00",1.3766582572815125e-07],["0xbedbb200ae382ddef5fa4293c30f1f911936bdaa","2026-11-04T18:47:37+00:00",8.734396247723193e-08],["0xd2df9f2ed128cbbde1975497db76adac8bac273f","2026-11-04T18:51:15+00:00",4.6427213444487825e-07],["0x04ccecf9f3f96c5512cae593eb8316b779d96cff","2026-11-04T19:04:43+00:00",1.2473652752905246e-07],["0xf36b828a38af6e2048b777b541bd2bc102d60999","2026-11-04T19:21:37+00:00",3.90943595348093e-08],["0x8914cc9321f5446d1864bd3d85e72502e1040999","2026-11-04T19:41:31+00:00",8.367984314582996e-08],["0x2989e7ec6875c005a52c9c03b58960e570eb5a33","2026-11-04T20:02:45+00:00",1.4845870819711134e-07],["0xf0e21193ed2e8543e5595372871b2264b1543c43","2026-11-04T22:01:42+00:00",2.433147861319838e-07],["0x9a9815af521149e8d982791703a31bca2f95f534","2026-11-04T22:25:30+00:00",2.0347555376947132e-05],["0xeb05ab042b6ebb5e22fe2630f51a2b7ba1867dca","2026-11-04T22:56:42+00:00",2.0310273084909548e-06],["0x409ae92e7a192be7c23e17c9944962fcf25ebd30","2026-11-04T23:15:00+00:00",9.939545422278911e-08],["0x9c162d7809b27444dbb55c775d60497ee0f2f80e","2026-11-04T23:17:10+00:00",1.710542642537507e-07],["0x82e01bcf942cfb1e777a04fc1e3c229687f67bb2","2026-11-04T23:32:39+00:00",4.518430750001868e-08],["0x4c7068deb569c3c763d7cc6d51b1eceb565586ea","2026-11-04T23:37:13+00:00",2.2660111792169544e-07],["0x316255f9d6210ceb71fc9fea19886f232169fea4","2026-11-05T00:41:27+00:00",5.258873196109956e-08],["0xbd453b650e84957be00e3c24fcd66e739a187804","2026-11-05T00:51:31+00:00",2.1749799368828138e-07],["0xdcdd002fd74ad1dfac10cfc44104df4d5a1c71da","2026-11-05T01:19:49+00:00",1.421824753090882e-07],["0xefc3bfafa0cdc424e1fb8610794144c539d7295d","2026-11-05T01:23:34+00:00",6.649906621025858e-08],["0x82fbc8c89286a8bbb95e552a74cef57a757d0215","2026-11-05T01:24:04+00:00",1.0624144549474634e-07],["0xedd0a6fc71c13ea606fd1eb7ebada7cc4ac29618","2026-11-05T01:34:51+00:00",1.654808110111279e-07],["0xf56587c86a359e23287fa0da43ed4283a7ebd9b5","2026-11-05T02:40:24+00:00",5.371752518786933e-08],["0x5c99e121c5bb296ae2c5230636b6de4bb44a56fb","2026-11-05T02:41:18+00:00",5.835364090301979e-08],["0x4e91ea05733b1e909c1b860c3c2d6e7a18df676b","2026-11-05T02:57:08+00:00",5.4819918680344835e-08],["0xb7a5b2d241afc8fc15476a2ff332662742ec8af2","2026-11-05T05:32:55+00:00",5.93273560753994e-07],["0xffbd13213e71a13f839a756048218dab0a4257a4","2026-11-05T10:43:43+00:00",2.7737874998497944e-07],["0x88e49f7e08c503e1d89ebc56ebc1119cd594dfb1","2026-11-05T13:55:41+00:00",5.765956202337091e-08],["0x5b53e7728a979f0dfeb61bc4ace393be356fd0c5","2026-11-05T18:04:18+00:00",2.008855558005245e-07],["0x32193afd14c53f12c45a4c04eca9f7ba98e3ec71","2026-11-05T19:10:00+00:00",2.1071059830388939e-07],["0x171103ffb79d4c8e06f4a1626c5a1adc46d18ac9","2026-11-05T19:46:23+00:00",1.1561633469568646e-06],["0x32193afd14c53f12c45a4c04eca9f7ba98e3ec71","2026-11-05T20:01:54+00:00",2.0074579666550215e-05],["0x204b942d1b9275b3a0bafe35dbd9158646b40cd9","2026-11-05T20:02:56+00:00",3.594913479779621e-08],["0xa9627c677930bd378f090797873fc2423ab363bd","2026-11-05T20:05:45+00:00",6.92056989014402e-08],["0x2efd54da84860b81b48748cfabd0b5aa632495e0","2026-11-05T20:19:19+00:00",9.095415524805496e-08],["0xcac5b6e74eb7383af0a6dc344d996e225bbf9a17","2026-11-06T00:10:35+00:00",7.561504655848019e-06],["0xe3085dc15d4697d418d1391ca9090b2576354f0d","2026-11-06T05:27:16+00:00",4.240096818568184e-07],["0x853cdedb89689923b3b7ad54e94288f389e723c1","2026-11-06T06:53:51+00:00",2.3484485575437198e-07],["0xc7481f67b12b7af92496adfffcfc7abfdfd94299","2026-11-06T07:40:31+00:00",2.587077763324584e-07],["0x6049c623decec46189ad25994f0a48046162cccc","2026-11-06T07:46:22+00:00",1.0174651984923957e-07],["0x6049c623decec46189ad25994f0a48046162cccc","2026-11-06T08:52:48+00:00",5.636210074022162e-07],["0x86a0366716dddceb6ec053b566cf946567940a4c","2026-11-06T10:14:01+00:00",9.117899256657516e-08],["0xd2a7447bc7152f9ba398ec4f937c7e65619479c1","2026-11-06T11:52:46+00:00",1.6010349589073844e-06],["0xbc3327593cdf430419bc818b002a4b5c861a0422","2026-11-06T11:52:48+00:00",5.062812918326287e-07],["0xad57414dac9d5c222356af781fa9dc62cc41d209","2026-11-06T12:21:52+00:00",9.165824093038355e-07],["0x911f19c1c39596f158ed5c314548d0fcf382003e","2026-11-06T12:25:12+00:00",5.206982560631173e-07],["0x62d72731aee06ed4de5156842caa2aa133c4e039","2026-11-06T12:57:55+00:00",3.59592668963028e-07],["0x5c61114e6125811df3086f398dc0ede9bc6d6d11","2026-11-06T13:04:59+00:00",8.674209539843334e-08],["0x14477bee1ca663ea819e06131918ab94b8136b20","2026-11-06T13:10:59+00:00",8.148999692866642e-08],["0x35fddfa5032ee8a038bfd2b5d4af26000858e53b","2026-11-06T13:16:30+00:00",7.439432994758583e-08],["0x6fe00a6a306f36407151e3b17d1346a891160d72","2026-11-06T13:48:01+00:00",5.480080753968254e-08],["0x65dd6f612fd7cb550499d859fe7a39cb33b891f9","2026-11-06T15:03:18+00:00",2.4747919294511616e-07],["0xb65a435800f5d80da8150a12561f29fdca733b57","2026-11-06T15:16:53+00:00",1.940710752889318e-07],["0xa279a738859ede80d44caf1e92fc9035c813ba53","2026-11-06T15:20:05+00:00",8.131167015670242e-08],["0xbc3327593cdf430419bc818b002a4b5c861a0422","2026-11-06T15:50:30+00:00",2.5953926622174723e-07],["0xab2bf944fa4fe183db7cf9843d0b5a6edae72c1d","2026-11-06T15:52:22+00:00",1.3443673297006057e-07],["0x83cb11d56bcbf04ca77c24c59f2c5b3f67a92831","2026-11-06T15:54:15+00:00",6.075066149700725e-08],["0xab2bf944fa4fe183db7cf9843d0b5a6edae72c1d","2026-11-06T16:58:25+00:00",2.5935945737220184e-07],["0x88e49f7e08c503e1d89ebc56ebc1119cd594dfb1","2026-11-06T17:11:55+00:00",2.4403166044084624e-06],["0xe32e27887493f017f0a4f2bdf21a253c5d679351","2026-11-06T17:13:55+00:00",5.756968139435238e-08],["0x50815b9b7a028361f6ded143ab5fc42a3842a63c","2026-11-06T17:20:10+00:00",1.6845191072939542e-07],["0x1280324a3ca2573c929f7277e77c1d1db3e93d88","2026-11-06T17:29:41+00:00",2.0493003748142634e-07],["0xe17f8b71908ba50948e5754d8889d875912994ff","2026-11-06T17:41:56+00:00",2.0248922644554233e-07],["0xf314eded192429c7386dfad76ce6dd60d1ac7378","2026-11-06T17:44:23+00:00",1.1066075294173077e-07],["0x236feecd909ff562e775318c77781f4946c33b3b","2026-11-06T17:49:12+00:00",1.0814465104902143e-10],["0x7b12b3ec877d24cbe4b04a019c0acd47599bcd16","2026-11-06T17:50:37+00:00",6.50689161186516e-08],["0x7e7384e8c7e622e0bbf5813c399ffa4adb22dc44","2026-11-06T17:56:42+00:00",9.394233230219214e-08],["0xcef1bbe5af5c1c1228b4a7f91b1a900f22776d43","2026-11-06T18:15:28+00:00",9.987312803512034e-08],["0xf314eded192429c7386dfad76ce6dd60d1ac7378","2026-11-06T18:34:29+00:00",8.727382842474977e-08],["0x6f5b6abb08b75d908f8bd3b5327b71c4d1a9b610","2026-11-06T18:41:21+00:00",4.5537626623798216e-07],["0x18a60760c899d5913d3aafceb790081b3812901c","2026-11-06T18:50:00+00:00",1.1990618080495074e-07],["0xbc3327593cdf430419bc818b002a4b5c861a0422","2026-11-06T18:58:00+00:00",6.916943210570449e-07],["0xfebe2fefbcd92f66b76fd17da504459fad1e37e8","2026-11-06T19:16:54+00:00",1.3776950946822633e-07],["0x0018f3c1010926ae3098a455a793862adfc9cece","2026-11-06T19:20:58+00:00",1.0768242871858247e-07],["0xc6088be9c99787463a5fc655091e589cb0da0c3a","2026-11-06T19:40:31+00:00",4.9652464989953055e-06],["0x260b1952320995aa52d9f465efb8d71f8e86fb8c","2026-11-06T19:49:01+00:00",1.5571652976376752e-07],["0x6f5b6abb08b75d908f8bd3b5327b71c4d1a9b610","2026-11-06T20:03:16+00:00",1.0781912817863624e-06],["0x802abcdf438ce604faae9ddf419a4a38f99dbb47","2026-11-06T20:23:59+00:00",3.3110830804508413e-07],["0x721a49268773015faf375f4a571d25a30af8432d","2026-11-06T20:43:04+00:00",1.2722760650978943e-07],["0x87f68a00d06f9f2914f6b5b91a2791baf1f768fd","2026-11-06T22:06:38+00:00",5.643655145414545e-08],["0xcc842e9fdd35a80adcfd3cda3a2a04e90b81efe9","2026-11-06T22:15:44+00:00",1.0475005650520634e-07],["0xf0e5351d2f389c2b7a24358a947be29b39bcd471","2026-11-06T22:32:41+00:00",2.600143147213076e-07],["0x62db2e20a9749aca4834e56ac3f1d8127a2abbc6","2026-11-06T22:34:24+00:00",1.3588729252691514e-07],["0xbb2f7354c76db15c0f0c51fb15caf46d7364d475","2026-11-06T22:45:24+00:00",9.857636723076056e-08],["0x654d6153722f35538d82efd40c3bd85a9170cb05","2026-11-06T22:46:54+00:00",9.529191907504102e-08],["0xbd2d13426e9fbc58ba8b19609044793bb6efbf01","2026-11-06T22:49:10+00:00",1.3233673590435764e-07],["0xb874abb9904678d85babde1ba1a5bde46effdf63","2026-11-06T22:57:55+00:00",1.798470125433837e-07],["0x2311f61f1b5d4e296480f85249b28535b3999c79","2026-11-06T22:58:56+00:00",1.03496317472184e-07],["0x33da78b95d9d048cb47c5e878819d1992e7cee2c","2026-11-06T23:00:27+00:00",8.72434787033818e-08],["0x721a49268773015faf375f4a571d25a30af8432d","2026-11-06T23:21:12+00:00",5.484547082598763e-07],["0x93807a104e60549195d981cac46bbd9bde60917a","2026-11-07T02:18:03+00:00",1.2498137287288227e-07],["0xb80229ada45b0c2764023f78872898c48105cad7","2026-11-07T03:18:53+00:00",6.215170076394321e-08],["0xc890cd53181d16140be68ffdf81876d8e5249a0a","2026-11-07T04:22:42+00:00",9.066105138290356e-07],["0xf1903979552204b0db5ae09ddd8440636c323d79","2026-11-07T04:26:52+00:00",1.8580730012450492e-07],["0xf13ee15741ea8f19da1405212b62eea0ccea448c","2026-11-07T05:04:27+00:00",2.340945934840267e-07],["0xe6a5e5905bd43186174efe143a3924c31a045d30","2026-11-07T05:17:13+00:00",6.798823514858858e-08],["0x00f582d04a452cd12ed415183ed6d956e4517017","2026-11-07T05:58:45+00:00",1.4147966622241958e-07],["0xe13ce7bcddee1151b777087d06cd61c9e4a29195","2026-11-07T06:29:31+00:00",1.180561940394559e-07],["0x32bbcba32a2e2318a471d1d95abad2309aefc330","2026-11-07T08:30:35+00:00",7.55293267698918e-08],["0xc56e42e340164db34fa6a343614b5dd3d04e249a","2026-11-07T11:26:27+00:00",1.3614776465045428e-07],["0x52c8d23eb61513d8d812bbce1bd1f85941655229","2026-11-07T12:46:19+00:00",8.697159534669046e-07],["0x911f19c1c39596f158ed5c314548d0fcf382003e","2026-11-07T13:59:53+00:00",9.917707268344817e-07],["0xc7625e25ecbe0fadca4783bbc8ffa97e4776c842","2026-11-07T14:57:26+00:00",1.0251567042794261e-07],["0x784fa844e1c19f9e9450e8f2250396a2b9023734","2026-11-07T15:29:58+00:00",4.36377938715443e-07],["0xda3040ab5ac51d9b82a439a03ce7009af4c7ff05","2026-11-07T16:04:12+00:00",1.5624721770530192e-07],["0xff6d8fdd23b8f9815497b8e60396e4c15e1c11e0","2026-11-07T16:10:10+00:00",1.261662333755955e-07],["0xedb963ed6efe231f6327c87a42ed49abe2447afa","2026-11-07T16:23:00+00:00",5.622559999301877e-06],["0xfd09ff1227d7d12aea2d0d4710bd6b1aa7c0f063","2026-11-07T16:23:18+00:00",1.76305420004044e-07],["0xf087b03b6ea7483835168e07c90a32a707ba845f","2026-11-07T16:30:59+00:00",3.4633459611280306e-08],["0x8d805e7bd6ea87c61f41065ea669369c3b4ec1a9","2026-11-07T16:32:12+00:00",9.948215043454485e-08],["0x930fbe8f449e41847b9c64b300ef4fb4296a7d15","2026-11-07T16:55:19+00:00",1.5162702058457934e-07],["0xad330616f1e0033de331b3c95abbe193a01c3554","2026-11-07T17:14:16+00:00",3.5210503941234215e-07],["0x5d4e0347f7690f948ebc4d5fc2f46bdb9f15b8dd","2026-11-07T17:29:44+00:00",8.274987540123658e-07],["0x2934d9fdb20fcc1c9fdf3409dab647f2fbfcd3e1","2026-11-07T17:34:14+00:00",4.783907656484121e-08],["0xd11345b5953100b70d4b2a93d33d8cb8d41e0e8f","2026-11-07T17:49:16+00:00",2.553230211232735e-07],["0x5a9aa8df5f69a8fd543e6838b0d4ac34624eeb3a","2026-11-07T17:58:30+00:00",8.8874572530394e-08],["0x9f0498c2db26d9674c5438cdef02c0c8f87145c7","2026-11-07T18:04:02+00:00",7.03025282531445e-07],["0x46f04359c13ad0799c98ce7571929fa4e08eeb8e","2026-11-07T18:12:34+00:00",1.0861912925789729e-07],["0xedb963ed6efe231f6327c87a42ed49abe2447afa","2026-11-07T18:20:04+00:00",8.712408427253556e-06],["0x02f4989b71e70e126e3449f883f8dc9dc0154c5b","2026-11-07T18:39:50+00:00",1.5535119497301728e-07],["0xd19ea95fd62ae6d05fb4f6e07c1d6b88f7aaea9b","2026-11-07T19:33:52+00:00",5.642635200704521e-08],["0x35cf591c9a041857b322e16e19b276693292fe47","2026-11-07T20:01:52+00:00",7.10194748599468e-08],["0x260b1952320995aa52d9f465efb8d71f8e86fb8c","2026-11-07T20:16:25+00:00",6.735564673796986e-07],["0x02f4989b71e70e126e3449f883f8dc9dc0154c5b","2026-11-07T20:27:51+00:00",1.345589426813741e-06],["0x301eb259de132d592966fe0712db75b27792a68a","2026-11-07T20:48:06+00:00",1.3153172738272499e-06],["0x8df4f39d592de2aaee15838b265c2392d19a4128","2026-11-07T20:56:56+00:00",7.206823879704686e-05],["0xd2a7447bc7152f9ba398ec4f937c7e65619479c1","2026-11-07T21:01:26+00:00",1.092289482313249e-06],["0x8df4f39d592de2aaee15838b265c2392d19a4128","2026-11-07T22:14:56+00:00",7.315844949864586e-05],["0x23cbeef6dca372b01a42d5b87a85978327c95dcb","2026-11-07T22:17:15+00:00",6.135130939115996e-08],["0x6fe2fd06aba944896b6d263512f5f0030396a47a","2026-11-07T22:19:38+00:00",7.183334717732109e-07],["0x3374f389d6e42df2e9a8050453493bc059086139","2026-11-07T22:35:53+00:00",9.187757270867625e-08],["0xb7126d3be7c594b0ad3dc68eed791082a18cc589","2026-11-07T23:09:03+00:00",7.503631412726785e-07],["0x6fe2fd06aba944896b6d263512f5f0030396a47a","2026-11-07T23:22:39+00:00",8.501353585934894e-07],["0xc6088be9c99787463a5fc655091e589cb0da0c3a","2026-11-07T23:24:40+00:00",5.225498841725877e-06],["0x79e2ab9c5aa47525a98b65515218974e17a3bfc9","2026-11-07T23:25:02+00:00",2.3645629937176833e-07],["0x22985f1b966348546f378d45862634254749ec56","2026-11-07T23:27:04+00:00",6.832052730763367e-08],["0x2743abf110c7489908e5507a51d972f1267e2e1f","2026-11-07T23:34:20+00:00",8.986405309194958e-08],["0x52d3473135334ecaab569de7cd50580ff9ea3c3f","2026-11-07T23:42:57+00:00",2.2172132368188374e-07],["0xd6274c5ccd0cc6872d3d0ecfceee6689c4928746","2026-11-07T23:56:34+00:00",6.973019645786386e-08],["0x9a08d8a8ee63abbc59aa121b25c5179db5e05ed8","2026-11-07T23:59:04+00:00",2.698087884141104e-07],["0xbfeb44111e3a4f75d432d13bc98e0789589cf6c5","2026-11-08T00:23:24+00:00",6.281542107927928e-08],["0xb6b4de050a8423193d2dffb14dc6decb052f1a66","2026-11-08T01:09:04+00:00",1.5685718208582097e-07],["0xf31811dd7087171f096f0593a6d5a2d4bb554978","2026-11-08T02:02:37+00:00",7.135773494098557e-08],["0x6f3c52684f6bbe296206f37aec42a30e9fd93833","2026-11-08T04:27:02+00:00",5.680341741119731e-07],["0xbfcf64cfe000243efa3a9f30f34f1f0da5857c1d","2026-11-08T04:57:18+00:00",2.788810532503201e-07],["0xdd521f77cc27db0f073e384e3af99c2b4dd4037c","2026-11-08T05:48:19+00:00",1.3333341094128754e-07],["0xfed1ab0a263b309a2725262ec1ac9fad0fc18da7","2026-11-08T06:03:39+00:00",1.3594399864506796e-07],["0x61e6c090fb341b5cd41a17bb0f895f0f9e3a756b","2026-11-08T06:30:06+00:00",2.241439309827316e-07],["0x2cef34ed0f6675ec4282f2801886609ae9fb2ba3","2026-11-08T09:58:02+00:00",5.974169156882203e-07],["0x714934fa7124924d6116829712fc3a44b783f6a9","2026-11-08T12:02:53+00:00",2.0957731378327712e-07],["0x578090030bc716abd502c648ee183cfe0328993d","2026-11-08T14:24:44+00:00",1.987962051126674e-07],["0x11bb1a049f05d25391285a26b407908900c072b1","2026-11-08T14:51:47+00:00",2.742712547581487e-07],["0xaacc5bc6ea24cb753e5c2d48c88f7e361a50aacc","2026-11-08T15:09:32+00:00",1.044317150411559e-05],["0x7ebb6c268881c8f2c3783889e5a235fc057154fc","2026-11-08T15:29:38+00:00",5.528172793604626e-08],["0x625c6f61bc60b6571c69c4734c22b4458539a027","2026-11-08T16:13:33+00:00",1.5640525122114825e-07],["0x50d5133f80e5321b176f46d2ecaabeaef30a9872","2026-11-08T16:27:37+00:00",1.5983170004665788e-07],["0xc8567e00872fb9fe8faa5749556a19fc91a479dd","2026-11-08T17:01:41+00:00",2.804320936584272e-06],["0x02f4989b71e70e126e3449f883f8dc9dc0154c5b","2026-11-08T17:26:07+00:00",1.8648089884636865e-06],["0xa8bdcb70451fa9b775f07b5c777704141485ec20","2026-11-08T18:11:37+00:00",2.0264326013337147e-07],["0x6522fdae3dd4c67d6479238e482a79ed01643751","2026-11-08T18:41:28+00:00",2.292864413096389e-07],["0x7e8a9d3f1b9375e125c6e4746022eee6357c418e","2026-11-08T19:21:06+00:00",3.950682443600032e-07],["0x7e8a9d3f1b9375e125c6e4746022eee6357c418e","2026-11-08T20:08:07+00:00",3.891807401309241e-07],["0x32193afd14c53f12c45a4c04eca9f7ba98e3ec71","2026-11-08T20:12:46+00:00",6.993394086280455e-05],["0x38450874cf1ad0389dcb746ce80c20beac2b0c06","2026-11-08T20:27:48+00:00",2.312990443359096e-07],["0xf31bced2ea8f160052d54c8a3fd03381fedd8e0d","2026-11-08T20:40:16+00:00",3.3495232697677984e-06],["0x3dcfe5deae9fa7bdfbf35543d6d31b38e97b757a","2026-11-08T20:59:42+00:00",5.051025534290257e-07],["0xb7126d3be7c594b0ad3dc68eed791082a18cc589","2026-11-08T21:08:37+00:00",1.5393901605608585e-06],["0x02f4989b71e70e126e3449f883f8dc9dc0154c5b","2026-11-08T21:20:22+00:00",1.2749332949300283e-06],["0x4bb51327eb190abe8372215dcfb1ce0b46837a1d","2026-11-08T21:22:49+00:00",5.911389189277814e-07],["0x2116cb10e53510d2173f898dd35583daed9ae7ab","2026-11-08T21:28:51+00:00",1.1364934636087844e-07],["0x9c2779613ec0f4b0b2b63a362b44be7db448ff21","2026-11-08T21:37:27+00:00",6.018252652978772e-08],["0x9f3de7dd2903ef704fbaf00c0b217cdd1d3d2e96","2026-11-08T21:38:18+00:00",6.035139577201968e-08],["0xd4ff820661f732855842d02ee06dfce85dae6659","2026-11-08T21:42:19+00:00",7.62019379083431e-08],["0x6652974e6b72c0a8c76b23f187fb11aad68e6299","2026-11-08T21:51:34+00:00",2.2902384694395167e-06],["0x639272e075464608cdd4f9353a95f52c90fe6729","2026-11-08T22:05:35+00:00",1.416248064979979e-07],["0x24d184aa7c91ced385db67608bcc4dbbe318b40e","2026-11-08T22:14:34+00:00",2.6937776190956206e-07],["0xfbfc7be4afd8cbd427b395493d0c8899e64ccccc","2026-11-08T22:22:40+00:00",1.045441368270408e-07],["0xe9ff7d075b8119ca337bc9e6dad7f45a67d778a9","2026-11-08T22:46:04+00:00",5.255777775730147e-07],["0x24d184aa7c91ced385db67608bcc4dbbe318b40e","2026-11-08T22:55:46+00:00",2.0003460538105177e-07],["0x95548302a15fd3163a03d9fcfe7f5dce8225721f","2026-11-09T00:00:25+00:00",1.5751263408718228e-07],["0x6b771e998ef811b52e90b09a9b628f3c5fdf422a","2026-11-09T00:40:26+00:00",1.5865349841197291e-07],["0xd5bccb149c337b750f215c032fd5aa41ede3d4ee","2026-11-09T01:20:32+00:00",2.32793261689927e-07],["0x4bb51327eb190abe8372215dcfb1ce0b46837a1d","2026-11-09T02:49:33+00:00",2.118622094295248e-06],["0xbcfd3f32d8a49c8d562bb76432f0cfe56c56bffb","2026-11-09T02:55:13+00:00",4.6799974334332535e-07],["0xe9b99c30b4b1ad19d25da5d15ef6162878e7f65c","2026-11-09T02:59:18+00:00",1.0196968920798798e-07],["0x006ec314bcd5a997b84173a2a7a3960dc89b51b6","2026-11-09T03:38:41+00:00",1.042644940103453e-07],["0xfa054fee3462525622b91721aad872c627100eff","2026-11-09T05:04:08+00:00",6.117477300552363e-07],["0x48e28fb562f927f2e06990abb0000a96f02dc485","2026-11-09T05:20:35+00:00",1.3885063872059881e-07],["0x48bad38dbd5682d9c37e91a394789409325edd4b","2026-11-09T05:53:53+00:00",1.7681801608310886e-07],["0xcfe3596d492f3ec75a95fb22d5caa92415c8b13d","2026-11-09T06:18:47+00:00",3.1824977055982395e-06],["0x412698aa5dbf9aa3b94b66b69718b9a058ac849f","2026-11-09T06:53:33+00:00",2.1106727837813618e-07],["0x6be861adad274ee41516b5b99101520a4cbb18d7","2026-11-09T08:44:38+00:00",8.838245744285866e-07],["0x7295c7a755fc658236b9e2d3cdcc4330b158d041","2026-11-09T08:57:00+00:00",3.3738910210362877e-07],["0xb877f522f683af89cfd063b55ab08f717a4e565d","2026-11-09T09:24:24+00:00",1.7310126877372238e-07],["0x70b8fb32aaf1bab8ff24706ed9a6c2c7aa9d1940","2026-11-09T10:31:57+00:00",1.7380010574030197e-07],["0x6394f09f433fccb60b1d989edc640e3cefaf7c7f","2026-11-09T11:16:50+00:00",3.125799894586775e-07],["0xda9a8a8fd776c075149c647f8b5397ba91d398e4","2026-11-09T12:20:26+00:00",6.44718249600211e-08],["0xe82db8d6719a042f9d0fdcd29c4dc112a2857908","2026-11-09T12:27:36+00:00",1.2131551224660962e-07],["0xf1b36cfba4c02fa00a900fe5ab5764849f6b81ff","2026-11-09T12:50:01+00:00",5.867808939203123e-07],["0x5cdafcac4f91ce69e00ce90aa55fb969ecbb8ded","2026-11-09T12:59:37+00:00",6.867133380965493e-07],["0x93d08212af367301b9fecc74503942aa71b61f1f","2026-11-09T13:08:46+00:00",6.741238625934627e-08],["0xbcfd3f32d8a49c8d562bb76432f0cfe56c56bffb","2026-11-09T13:09:47+00:00",4.4923635518589475e-07],["0x7701a31c0b3815fe6d09a8a313967c5977a5fa7a","2026-11-09T13:15:41+00:00",1.004370425924961e-06],["0x7b12b3ec877d24cbe4b04a019c0acd47599bcd16","2026-11-09T13:19:01+00:00",6.336067388987884e-08],["0xdd3eddf52b1fbcf14f95a189e3e260970ecf25f4","2026-11-09T13:27:49+00:00",2.3807355187522163e-07],["0xa3c1d6a8648df3978a38ce1478fbc284c75d95d7","2026-11-09T13:29:38+00:00",1.1524591782828748e-05],["0x547a2a9c5af6e4372ead794e80eaf3f42dbbaaac","2026-11-09T13:31:04+00:00",3.044294986297278e-06],["0x236feecd909ff562e775318c77781f4946c33b3b","2026-11-09T13:37:26+00:00",5.201384753405456e-09],["0x9a9815af521149e8d982791703a31bca2f95f534","2026-11-09T13:38:34+00:00",2.858463488739383e-05],["0x405caa914fd1c1408ea526c90a6cfd429ee57ebe","2026-11-09T13:39:31+00:00",7.64263692238533e-07],["0x0f79931689af199e7cf868ed1107bba2c83c68f7","2026-11-09T13:48:01+00:00",4.916497640885851e-05],["0x5dcfb191f0cdb853bac3a6a4385e160e779e377f","2026-11-09T13:51:00+00:00",2.694270941221466e-06],["0x3b3da85235148b1e279a53b67d99666e945d025a","2026-11-09T13:55:29+00:00",6.652487567610693e-07],["0x6c0eeb1c858e559e12e424d6f7cfd7561f11fa27","2026-11-09T13:58:02+00:00",1.1405058291106005e-07],["0x660a22496a4ec7bcb5f6d6c0e3b8aa2550372a45","2026-11-09T14:02:15+00:00",9.183998475892643e-07],["0xea19d0a15cc925c6cf8024122c49e50ad6e67858","2026-11-09T14:13:14+00:00",1.604146675861213e-07],["0xd4aa52a5617b0114ead787accf5200ff6506dd5a","2026-11-09T14:15:16+00:00",6.414467520662622e-07],["0x38d7e4d94378ba5cd7eaca1ff2c7c638d4516ccf","2026-11-09T14:22:33+00:00",6.177375273253244e-08],["0x3800ef86b1e40639b4ada60f33b75eedbf037354","2026-11-09T14:24:17+00:00",5.288294797999062e-08],["0x971e7b4b0c74f5aedcd5d20b07fb478d644f5599","2026-11-09T14:25:00+00:00",7.243588861057126e-08],["0xf1b36cfba4c02fa00a900fe5ab5764849f6b81ff","2026-11-09T14:27:32+00:00",4.1195699474344367e-07],["0x065625d34c2ccd5cd12a48cc135cafc64bfe4a13","2026-11-09T14:38:10+00:00",9.972407676626177e-07],["0x32193afd14c53f12c45a4c04eca9f7ba98e3ec71","2026-11-09T14:49:20+00:00",2.3971870395425e-05],["0xc506ba5d59152d2aa629bf23e9ff898c8b88c454","2026-11-09T14:59:04+00:00",1.417567960616478e-07],["0x2fee0b31666886633f924d0ddbbbc72c77232ead","2026-11-09T15:00:03+00:00",5.173657039721559e-07],["0xdc34b3a34a3f84998d2aaea9c31c9c86318b5b51","2026-11-09T15:01:31+00:00",3.1786806319551684e-07],["0xa3c1d6a8648df3978a38ce1478fbc284c75d95d7","2026-11-09T15:05:45+00:00",9.14470775134279e-06],["0x8df4f39d592de2aaee15838b265c2392d19a4128","2026-11-09T15:05:47+00:00",6.283217832460835e-05],["0x5e5df82d563fcf36b6f853c4d09f89e2d18825b4","2026-11-09T15:16:38+00:00",5.776257155493905e-06],["0xcfe3596d492f3ec75a95fb22d5caa92415c8b13d","2026-11-09T15:17:09+00:00",1.1115063909172325e-06],["0x66f46517d0c2cfa489bcbf776d127d59c79fa499","2026-11-09T15:18:31+00:00",4.416483886658683e-06],["0x4b1b84626790d57f73e270b826a1d3e3a8687b78","2026-11-09T15:29:00+00:00",4.774172244069331e-08],["0x548a0627fe04c28298541142ced99bdb88a03231","2026-11-09T15:40:59+00:00",2.178904966679615e-07],["0xea19d0a15cc925c6cf8024122c49e50ad6e67858","2026-11-09T15:45:24+00:00",1.0142053235387671e-07],["0xba959a8a1c3c4fabeff4426fc52207f0fd3b0d3b","2026-11-09T16:01:34+00:00",1.563429302277843e-07],["0x8df4f39d592de2aaee15838b265c2392d19a4128","2026-11-09T16:17:51+00:00",6.976783863919442e-05],["0x55f9ed41acf81ed327b2c7691d09caa97dad51dd","2026-11-09T16:26:35+00:00",6.681780471026308e-08],["0xae42712c56cc0781df6e4beb180bb7986260db58","2026-11-09T16:36:38+00:00",5.289559681378021e-07],["0xeb0689d030f9bbf09bb662b730c1da9183e007ba","2026-11-09T16:51:50+00:00",1.1953160914128224e-07],["0xa3c1d6a8648df3978a38ce1478fbc284c75d95d7","2026-11-09T16:56:32+00:00",3.6084412910750346e-05],["0x866be81dc10971983737c6deea0c6d4294ae1ca6","2026-11-09T17:01:43+00:00",4.907959811024691e-07],["0x51c343ed5131f17895991849860b61c2cb0e7e7a","2026-11-09T17:10:06+00:00",8.5204070765318e-06],["0xedb963ed6efe231f6327c87a42ed49abe2447afa","2026-11-09T17:29:55+00:00",2.9192347675872343e-06],["0x9507dc067cf750024d0fb8c05362df6e19829a66","2026-11-09T17:36:18+00:00",0.0003205128890421616],["0x6e09d3643927d1c54ff791f309b770bca435df27","2026-11-09T17:42:21+00:00",3.288518140707363e-07],["0xd51da1714f9117231388f74bbac35f0e5515afca","2026-11-09T17:46:09+00:00",1.7627097033126454e-06],["0x6703e9c8d0dfae9c8eff93318369f7cfcdd90d13","2026-11-09T17:58:49+00:00",1.1192802997011055e-07],["0x5dcfb191f0cdb853bac3a6a4385e160e779e377f","2026-11-09T18:02:41+00:00",4.030785363415198e-06],["0xa8a262e8358335bbbedcfe541a077605a5f3decd","2026-11-09T18:03:07+00:00",1.5521492634112275e-07],["0x0eca7b56d434f1c0b63ce19af04a7354738f03f5","2026-11-09T18:39:52+00:00",5.104847333578145e-08],["0x8df4f39d592de2aaee15838b265c2392d19a4128","2026-11-09T19:00:41+00:00",7.132821759848093e-05],["0x98a1bb40f598baa6a79b0cf653b0cb57bd1d5f69","2026-11-09T19:04:51+00:00",2.0686270848029946e-07],["0x1381a7c2fdbf59b1c2f7420c98c45c358d33499a","2026-11-09T19:42:49+00:00",4.285823852700134e-08],["0x56115088b5344cfdbb8967900b0ba311428135d2","2026-11-09T19:42:57+00:00",9.467385816197584e-08],["0x50f6ee7d9b901b5a28b00bbb206c3488fd7d79d8","2026-11-09T19:43:00+00:00",1.3795898066418588e-07],["0x5dcfb191f0cdb853bac3a6a4385e160e779e377f","2026-11-09T19:44:31+00:00",8.210481639489482e-06],["0x0c3ed3ceabe3ea6383b9deb668308e6c9cb2544a","2026-11-09T19:49:01+00:00",2.378645581928374e-07],["0x840179389fb6c6b91b4ecc256e692cc1fc0ba639","2026-11-09T20:41:13+00:00",3.46995682094219e-07],["0xa3c1d6a8648df3978a38ce1478fbc284c75d95d7","2026-11-09T20:55:42+00:00",2.4601509472053348e-05],["0x9d9a6161cb7399f7a73f47f8195c9c3ad6911c76","2026-11-09T20:58:15+00:00",4.129549454949517e-07],["0x7e4ed7dc6187225e9a005f029b5eff866ab7fba3","2026-11-09T21:18:49+00:00",1.399073971998451e-06],["0x62d5d39e2b3a2a45e743698c9995b0e48676dab8","2026-11-09T21:23:31+00:00",3.693387243343595e-07],["0x8b25672b7522dc9bc8748f06a3e292e9e97f4844","2026-11-09T21:29:47+00:00",1.458269131917838e-07],["0xe05651e01127f0198e138b42569231af1ac4d4c6","2026-11-09T22:09:00+00:00",2.330949840499114e-07],["0x24c5b179c849948a413429acbec5166b3d165e4a","2026-11-09T22:19:27+00:00",8.201315343235678e-08],["0x866be81dc10971983737c6deea0c6d4294ae1ca6","2026-11-09T22:44:58+00:00",2.2633720087500535e-06],["0x8b8606fd4343a62939137ce8c907c0a67c32534a","2026-11-09T22:59:27+00:00",5.086409004153397e-07],["0x54e323f53af93640a4912e2f9a959a16c8349674","2026-11-09T23:07:32+00:00",8.469428396230903e-08],["0x442bc7b8c87deb954592d5914853cdb47149650e","2026-11-09T23:09:04+00:00",2.6336981179494853e-07],["0xc03350a8aefdf74093d6335673b1944af962c67b","2026-11-09T23:19:37+00:00",6.56888234813182e-08],["0xa5ad38205ef2487d7037910a92760d22b4ac7446","2026-11-10T00:01:55+00:00",3.9261279794910914e-06],["0x063f7cc5339f92a5fd7baf3d22233044fda1f327","2026-11-10T00:37:09+00:00",7.4055581761844005e-06],["0xd662d66ec991a3eae4ecbda34327264c53d4cf38","2026-11-10T00:54:29+00:00",1.3217161098358295e-05],["0xb451178f46613ecf0535f4f8fb671b8978fdc382","2026-11-10T00:56:55+00:00",1.2475880924705795e-07],["0xbb47e9e3d746800bd920264eaad6ef7b0ee3fe51","2026-11-10T00:57:15+00:00",1.0269787631003928e-07],["0x2a0759f53e6ef6bc2c318628906f7156f83e3806","2026-11-10T01:16:04+00:00",1.1533606339547977e-07],["0xaac5544662132bc94182dcce2d82821ddc347283","2026-11-10T01:39:19+00:00",5.483446387482721e-07],["0x4c247c9eb7e1a19c48a0c4dc6ad4bd55b6cff25b","2026-11-10T01:40:48+00:00",5.25445616308925e-08],["0xdf5cc9995abac09b4f9d5a7ee950fcc665a99b7c","2026-11-10T02:01:15+00:00",1.4507829185914099e-07],["0x3189818b9e441bca6bd5f271567e9f58c2173b7e","2026-11-10T02:14:02+00:00",3.196671899223316e-07],["0xe6f9144bb84e2588a2fc1c8e5f1d0a0a194b62ec","2026-11-10T02:19:19+00:00",1.5522658379874576e-07],["0x8df4f39d592de2aaee15838b265c2392d19a4128","2026-11-10T02:26:31+00:00",7.661064078500183e-05],["0x0477f04dee32aee612283f6bcdb124d1e23b7f85","2026-11-10T02:44:09+00:00",2.455494083287057e-07],["0xbd453b650e84957be00e3c24fcd66e739a187804","2026-11-10T02:52:25+00:00",1.654412237732649e-07],["0x6b35f059f45b90313148315294dcf5a9c0ed564e","2026-11-10T03:36:33+00:00",7.957058529629712e-07],["0x10216000a21e6f05575b6c3d2e6b2727eb238eb7","2026-11-10T04:02:13+00:00",4.054373231695454e-08],["0x866be81dc10971983737c6deea0c6d4294ae1ca6","2026-11-10T05:50:00+00:00",6.136939123082881e-06],["0xf2d54fc881eec638f52f72cf7dadfada0a704595","2026-11-10T06:14:43+00:00",8.179512597536297e-08],["0xcde71af20925f7a06e4a9a3eaf6f26990229e080","2026-11-10T08:20:19+00:00",5.461801287873052e-08],["0x699ca19a04a3e39e8d21d9e8e59acb6ec067ae0a","2026-11-10T08:49:40+00:00",8.311853935592179e-08],["0x063f7cc5339f92a5fd7baf3d22233044fda1f327","2026-11-10T09:12:13+00:00",8.238683120868784e-05],["0x8549a4fa7f865f39298226125b9e07b724eabcde","2026-11-10T13:23:50+00:00",1.030071959605615e-06],["0x091c5925dc5addb8369bce662ada09788e2af80f","2026-11-10T13:36:16+00:00",7.988585334028865e-08],["0x3940798587ad7ebf58ac772c75ebb5fb39c8c125","2026-11-10T13:36:47+00:00",7.734634781560283e-08],["0x60c54e8b6d783c62c0aae10e1ae21628f0daf0e8","2026-11-10T14:14:15+00:00",2.3039021337554414e-08],["0x5f245389a5a8160153f4fcb768ff63826c9b035f","2026-11-10T14:46:36+00:00",5.98208118948706e-08],["0x71c346c90fc54aa6ea07f41087665f30b4a6aafd","2026-11-10T15:27:15+00:00",6.868776598290338e-07],["0x061d44bff5d9cb0c92f7ae59ccf5e414b7b59991","2026-11-10T16:49:18+00:00",5.542164491237936e-08],["0xd172611bfd1c1a8ac3e51ba93e25f7126b21e7ee","2026-11-10T17:02:53+00:00",6.487001848900627e-07],["0xb0ae33c9a05608a2d6cad984a5dae9c5ba67dcca","2026-11-10T17:10:22+00:00",2.9390066542428196e-07],["0xd515abbf0ef895cbeef7e02f8ced83903af900c9","2026-11-10T17:35:40+00:00",1.816549578495174e-07],["0x6d86046909e7dd02967985ef2effa9b47147622c","2026-11-10T18:33:39+00:00",5.4079429937800156e-08],["0xb0ae33c9a05608a2d6cad984a5dae9c5ba67dcca","2026-11-10T18:37:57+00:00",2.1012323738714088e-06],["0x91ea9ff552f55fdfff62e87911454bb74e9a788f","2026-11-10T18:44:16+00:00",2.1505239764528394e-07],["0x48ef4d5db04642164831ec83ec6707c363f7208f","2026-11-10T19:10:43+00:00",1.693823718194802e-07],["0x43b95975f5b12e9e37dfefa5c162edc14a34dcbd","2026-11-10T19:30:54+00:00",7.536633561733886e-07],["0x5c5a1106a4e2a9ca5fadab22d6a7dbed89a11bda","2026-11-10T20:04:21+00:00",5.631184190262371e-07],["0xf0394d1e25be152c9746791f5da3b287743f0e52","2026-11-10T20:20:40+00:00",3.145975674062633e-06],["0xbe133191630eabd46de93ff7c1310909973934bf","2026-11-10T20:45:29+00:00",4.376195857526438e-07],["0xd321d6a1ff55ab8051e69f475fd625b3014e090d","2026-11-10T21:11:52+00:00",6.052198926334545e-07],["0xfdbf2af8adf6f34d0ed81624e40a3d5349952115","2026-11-10T21:16:38+00:00",5.26987443000404e-08],["0x3e04be222b1b22348eb8522d68bb3cae92d4fd6e","2026-11-10T21:24:16+00:00",9.52997253554113e-08],["0x5c5a1106a4e2a9ca5fadab22d6a7dbed89a11bda","2026-11-10T21:53:52+00:00",2.6388195589630875e-06],["0x7e6f7b252b08f3c2d715cd2d859a0f150c471aa9","2026-11-10T22:19:49+00:00",1.1110139313141443e-07],["0xf71f7f8569973275a601db79a6235b1a6ba11801","2026-11-10T22:22:39+00:00",2.479127150179474e-07],["0x49a591beb2a77ddcf41758f4e99126bc0510ea40","2026-11-10T23:05:26+00:00",5.207505850953309e-07],["0x0cb21a498bcff71e61651f1c13569f83d5ee8a39","2026-11-10T23:13:28+00:00",7.48373684461648e-07],["0x5c5a1106a4e2a9ca5fadab22d6a7dbed89a11bda","2026-11-10T23:34:55+00:00",4.469928667271278e-06],["0x8007567d13f61c81caf5ea6af38c187d02699122","2026-11-10T23:45:23+00:00",2.5681609627981926e-07],["0x6583ab10e769594fc86d2e060f7eb8c0ac1ec96b","2026-11-10T23:57:18+00:00",5.276175241733008e-07],["0x063f7cc5339f92a5fd7baf3d22233044fda1f327","2026-11-11T00:04:29+00:00",7.620778511170495e-05],["0xefeccfc9c248baa97e3d8f864107f785f91b1596","2026-11-11T00:22:41+00:00",2.4147986418154065e-07],["0xc75f783646739542c1e3680fd64424a0c4088d69","2026-11-11T01:57:06+00:00",3.376995174051696e-05],["0xe6b851760c05155d49f5ae0e8d57a3f078fc6cf7","2026-11-11T02:44:50+00:00",1.307535012988029e-07],["0x1f13e294c45fdea52a9b92ba76cd9f993d58a4f9","2026-11-11T02:51:14+00:00",1.7970318406717616e-07],["0xc75f783646739542c1e3680fd64424a0c4088d69","2026-11-11T03:00:36+00:00",3.287145666614587e-05],["0xca2ee49ed15ba36ca8a01082767d9ac2a24fc33b","2026-11-11T03:17:25+00:00",6.484437670053223e-08],["0xc75f783646739542c1e3680fd64424a0c4088d69","2026-11-11T04:05:57+00:00",3.105222961822374e-05],["0xc75f783646739542c1e3680fd64424a0c4088d69","2026-11-11T05:08:52+00:00",3.0236687319692646e-05],["0x708d12de0acefc2f35b4cd0bb3ff4709d0adfcfa","2026-11-11T05:52:35+00:00",1.0651802061754036e-07],["0xac09ab7cfe8f59b1262d1d760e049d59fcc62bfa","2026-11-11T07:06:29+00:00",9.300297452907216e-08],["0xb9b0a0880029aa441ceff36643620e21d502eaa9","2026-11-11T11:13:03+00:00",1.3133523980504467e-07],["0x8082e2a716f6d706e47d1411b379977310bd7af5","2026-11-11T13:10:00+00:00",3.9295764080782373e-08],["0x892723bf41f2e1af70adb1ff19036810888473a3","2026-11-11T15:15:17+00:00",2.4933101528933163e-07],["0xd8f5c64ff9b48334f7e88336d1eba09dc53c3377","2026-11-11T15:25:48+00:00",1.1431731644063383e-07],["0x9cc39bfee8d110e723619d330814e9aaeb1f6c3e","2026-11-11T15:45:50+00:00",2.5754862234985575e-06],["0x55a16b98ef8017c33e21a3212996344c2b1dcbfc","2026-11-11T16:17:22+00:00",3.7143427044344993e-07],["0x12f9b7d0d1711e4c32144a4efcba8bdbaadf3d72","2026-11-11T16:43:40+00:00",3.134123329170796e-07],["0x396c9fb063bb4e94f0f123500129b39797e5a0e1","2026-11-11T16:57:23+00:00",2.155578145005956e-07],["0xc585acca50b29db0aff2fa12090078173aa3cccc","2026-11-11T18:57:19+00:00",2.457739187981842e-06],["0x33deec7f76e8f6435b8b53d6c1400c7ff39fc872","2026-11-11T19:34:00+00:00",5.079728792080173e-07],["0xa713a15b85cab304f9e2fbc649440b5e3ab8a8d4","2026-11-11T19:34:09+00:00",3.111393432776272e-06],["0x7d94fbe716b91d770e70d25efab912007a1df1fd","2026-11-11T20:11:25+00:00",6.045111645759785e-08],["0xfc27a2c24f4bc9a63cfcda02dca63b661c5e749e","2026-11-11T20:15:55+00:00",2.21212782589931e-07],["0xe9c952cd198ff412113a465d34ff2c233f6f4ca1","2026-11-11T21:37:39+00:00",2.4054902238550724e-06],["0x749167b345908ed46f643a1dbc1ff7bd66a28083","2026-11-11T21:49:01+00:00",5.768193849151649e-08],["0xe9c952cd198ff412113a465d34ff2c233f6f4ca1","2026-11-11T22:15:45+00:00",8.50377884565162e-06],["0x5c72f1f408e9c1cc3582f2a6391a6b609c577777","2026-11-11T22:56:35+00:00",8.364415220841104e-08],["0xdf161c5eef15f82f1071a67ce7bb7460f9b7e4bf","2026-11-11T23:37:29+00:00",1.3148027009119478e-07],["0x7acea1b69a8e70c57fdc695387b77dcc2b6f92df","2026-11-12T03:29:58+00:00",6.214828194657763e-08],["0x87860c5410492c5fa7edcf69ec743f566782b86c","2026-11-12T11:34:58+00:00",5.780405170764894e-08],["0xe9c952cd198ff412113a465d34ff2c233f6f4ca1","2026-11-12T14:48:13+00:00",7.561997243312045e-06],["0xe45cb00580090b45890bea97c69bf2149f1041af","2026-11-12T15:11:04+00:00",8.668240777069486e-08],["0x50d7899937dd6a343016d38abd2ba6f8e21b8a09","2026-11-12T16:49:50+00:00",5.603789299432003e-07],["0x5d7215d2e06087318ff97f7e425ebdd77e5282ce","2026-11-12T17:00:50+00:00",4.0790996513056275e-07],["0x445f318f75f51691b325ca1d72adef674b3b6be5","2026-11-12T17:05:58+00:00",1.0615266005335393e-07],["0x9fb6b2c2f6891dd521205fc5bae9a6a66b3f0d0c","2026-11-12T17:18:16+00:00",2.9311301608210565e-06],["0x6668aa70a1f1a9e6e096ae73efe21fe876855799","2026-11-12T20:49:12+00:00",4.1492999486951013e-07],["0xe107e5d99910f544f88ef961e3ca6d88f06f29dd","2026-11-12T20:57:24+00:00",0.03994436658969115],["0x7f4f3b9770648262fe5133b9cb8a2155173be0d4","2026-11-12T22:41:04+00:00",1.014677345190337e-06],["0x8fea5df885506a84d3287470e032c599c8dc981f","2026-11-17T10:12:55+00:00",6.526055002907336e-07],["0x1b8a75f9062a4d4fbacef32a6a6511b42d84c449","2026-11-17T11:26:25+00:00",6.526055002907336e-07],["0x5c968057d276476025f9bd93a271866c386394fd","2026-11-17T12:16:56+00:00",6.511198539861607e-07],["0xc37511ad33d84e4a54b67eddb93af7c1d8423bfb","2026-11-17T12:54:35+00:00",6.503776656911716e-07],["0x86e5c8be439b9b5a2ee75083be26ee9dc8ece636","2026-11-17T13:41:18+00:00",8.616293613059902e-08],["0xd5b982a4de32874230a8b449ab95580cf9cfef06","2026-11-17T14:41:19+00:00",6.51862465519348e-07],["0x0c4cb3d13067bd5d86a857f3344a47ecc7160c02","2026-11-17T15:15:43+00:00",2.728303332092684e-06],["0x0c4cb3d13067bd5d86a857f3344a47ecc7160c02","2026-11-17T16:29:51+00:00",2.2029088062132203e-06],["0x6290268015bea4814abd2134ce33fd44dee84b48","2026-11-17T18:52:40+00:00",6.511198539861607e-07],["0x880c92f562105087733d319a647a0ae9ed3e8888","2026-11-17T20:25:45+00:00",8.844114698392766e-08],["0xd8448b1b910d2922702dfbd0e83b0b17eed194cd","2026-11-17T21:01:22+00:00",3.1479923323205056e-07],["0xb5f2f7ce25b5ec5557e68e4cc0bdcec9f6321a9b","2026-11-18T03:44:45+00:00",5.323940605262955e-08],["0x36a6031912dee000f4fac4af902d2d06ac0aae50","2026-11-19T02:38:16+00:00",1.2129224822991394e-07],["0x231e303fc99f8ff35fb089c707981e3767d4e6b7","2026-11-19T23:02:46+00:00",7.754713965503442e-07],["0x231e303fc99f8ff35fb089c707981e3767d4e6b7","2026-11-20T00:45:45+00:00",1.022398851972163e-06],["0xeacc3dc3192d1bf3e04a6479b36bcbf4a4fad072","2026-11-22T18:55:37+00:00",1.5271325132783097e-06],["0xd12609ddb1cd18280c7c14fb2cb5091cfea37f2d","2026-11-23T19:18:20+00:00",1.4393347773204758e-05],["0x66e8fd180b4e699b773174d4551b0ab5cc728699","2026-11-23T23:41:16+00:00",7.754348408319593e-07],["0x0d442948a8748f0296af853c85ec054d875c010f","2026-11-24T19:41:33+00:00",1.0097048795137516e-06],["0x476c059333d36b19d8c53d6b83a38abcbe199a50","2026-11-25T23:21:03+00:00",7.328851812482922e-07],["0xb070442d506bea996f0ca6cf291a2a837758adc4","2026-11-26T03:48:50+00:00",9.039877276805383e-08],["0x3e2e36dd45328b99825cc10b2d3f6c7d7ef100f2","2026-11-26T13:48:03+00:00",2.9592103180692657e-07],["0x8bf8388a6512b6dc9675bc964b1298da08c5afbd","2026-11-26T14:35:11+00:00",4.265692711493539e-07],["0x07074cd461b746ff1a2ee17fc46d49bc0a2617de","2026-11-26T15:30:24+00:00",4.47856350668487e-08],["0x6c13d7fddb4a1907656cdd9d88bb523aaaed926e","2026-11-26T17:11:48+00:00",1.0619815959179208e-07],["0x6c7cf99ba0680b1b23dc3c0dc2be8413bda08a33","2026-11-26T17:44:20+00:00",1.1056107047193691e-06],["0xcf78e80d4ef0287fd8e77429ae645c2719914885","2026-11-26T17:56:51+00:00",1.0236317512085695e-07],["0x2c41ff518f0a9ea28fab7047055545c5ff3552d3","2026-11-26T18:09:25+00:00",5.106410734958331e-07],["0xe2b8bcb648a842127efe2a5a773b2628c11f0a2b","2026-11-26T19:10:15+00:00",1.8943661899813446e-07],["0x38d7e4d94378ba5cd7eaca1ff2c7c638d4516ccf","2026-11-26T19:42:37+00:00",1.5785538629591488e-07],["0xc95970f6ac22a024d4a5e14b5b5f97e379a00156","2026-11-26T19:59:36+00:00",9.01971397736202e-08],["0x00e5714f267a0c23efb90bf5a70e4945a12e3795","2026-11-26T20:04:37+00:00",1.375517234096042e-07],["0x721a49268773015faf375f4a571d25a30af8432d","2026-11-26T20:10:27+00:00",5.546330273772873e-08],["0x940097339795fb5c315a39605ef99bf81bd37c08","2026-11-26T20:18:35+00:00",3.8236586567088025e-07],["0xe2b8bcb648a842127efe2a5a773b2628c11f0a2b","2026-11-26T20:21:55+00:00",6.720576629581209e-07],["0xb0b3ac8801f6b3904f51a0ed2e2953d0d540280c","2026-11-26T20:23:29+00:00",4.526874844916802e-07],["0x3a6d21bafa830c9571ee2aa63629451255b63f6c","2026-11-26T21:04:06+00:00",1.3494091903215166e-07],["0xd136f532df4c8868eb94421926476605c17d9df8","2026-11-26T21:09:07+00:00",1.98550832784086e-07],["0x390dc336c4e050da7163b834baffbb5308333518","2026-11-26T21:22:58+00:00",1.700059060533346e-07],["0x4ca45116bd1c26a803fbeae6101ab8aaa331375c","2026-11-26T21:48:39+00:00",1.08883723863452e-07],["0x30b6e95aa22df446fc0273c7891240383cc2aeaa","2026-11-26T21:55:23+00:00",4.3020167072619454e-07],["0xff273b5ee754c4c017065de3889fa3ee06d69929","2026-11-26T23:20:19+00:00",9.015229375290181e-08],["0xb98bf4091f4642669221f46351347ae8ae73237d","2026-11-27T00:53:37+00:00",7.685414859322949e-08],["0x8db154bc557ebbc5ee7863a7c75c1acc5a1d3689","2026-11-27T00:57:02+00:00",1.2274102089725935e-07]]
JSON_CARDS
    cat > /tmp/elysium-probe/lsample.json <<'JSON_LS'
[["0x7003868d91bfdbe5ae05135a75a7ae7ab2b08eb0","2026-11-21T06:47:09+00:00","0xd576f2c5eac0a07c34c4b2f439107ae1bb9e86fe"],["0x744b914ca8585ce7b5dc4222e3abc0ec2f447cf6","2026-11-25T11:57:18+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x789fb5c91091666a57cc389a41a011584f47bed9","2026-11-20T10:29:57+00:00","0xb561cfaf3ade352f9455654cf5bb026321b24e7a"],["0x89333a5fcc70bd33a6fd0c784cbde82b411e1511","2026-11-24T04:06:58+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x13a7c40f85d749628c084554ff23ba07fbaad4a3","2026-11-21T01:54:50+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x992c15263cdaaf569b8742c911d3528945d15b80","2026-11-20T03:36:34+00:00","0x8dbe0a2f8ee46e2d0ac0530058466ae34f010838"],["0x1145c8f710d2f5e44777e67d667d340bd0753c5d","2026-11-20T08:23:32+00:00","0xb1df17cb410fd0357663c0a387bdb367ff358c34"],["0x53833f84bb1a873016ab5e8796e9f14c62e0bf1b","2026-11-20T07:45:44+00:00","0x8dbe0a2f8ee46e2d0ac0530058466ae34f010838"],["0x48f2f041d10d319534f32c39f56aeaffb30b6cb7","2026-11-23T09:36:16+00:00","0x7e9020380c608bcba4e47b6ccffa22c09387486d"],["0xe340e9c423c2813e7c112cf700c5baa420bf60a4","2026-11-20T22:00:21+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0x7961736ede5633b5ed2c738af7126b93d7191e30","2026-11-20T17:29:53+00:00","0xa6799305843b9c5bf9f9a3a295a26dedbe79bbb8"],["0x26bbdbb846d4c50ae173be15c8328ccaa556a396","2026-11-20T17:07:00+00:00","0xa6799305843b9c5bf9f9a3a295a26dedbe79bbb8"],["0x54b87ac9bd15fd5cc6539deaf3725a6a5eb1efb5","2026-11-23T21:03:35+00:00","0xf88fd850a47974b40d7bcf8fa89ec0af48b163c1"],["0xbe30e8dddf2e1d0654e22e5e69c666477d077f8d","2026-11-25T02:12:53+00:00","0xc66d9fdbee720693026234c47c63311476b97321"],["0x22da3d8723d79fcc3dac42b3368940c60ede8a07","2026-11-24T06:18:46+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x7cb530e9f1592cadaf0782ebc640b10fe1813ac4","2026-11-21T03:30:08+00:00","0xd576f2c5eac0a07c34c4b2f439107ae1bb9e86fe"],["0x7bef65dfb91aced4f30514adf34293bbc49b0621","2026-11-24T08:21:01+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x59d9fa5302ac69d921b5b195bd0a5581aef9ab2b","2026-11-21T01:33:57+00:00","0x782ffa3594d268290e9e2c93887afdaee5e7f495"],["0x986d48d36b42eb1bf98480234409da2c228805b9","2026-11-23T10:07:53+00:00","0x7e9020380c608bcba4e47b6ccffa22c09387486d"],["0xbf3ccd354dd7673d28ac31f2b7af182543673614","2026-11-22T18:39:57+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0xb9c357f54bbc21c4829eb36c587ae8fd017cd6d8","2026-11-20T02:23:15+00:00","0x8dbe0a2f8ee46e2d0ac0530058466ae34f010838"],["0x33135b83d259ef830affa3d933ff98e677a932ef","2026-11-20T20:54:03+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0xbee0fb0d69b861155c03b78ff29ff1872d7c5feb","2026-11-20T07:50:32+00:00","0x8dbe0a2f8ee46e2d0ac0530058466ae34f010838"],["0xa2a413efcff7b582ed3821beed780b86180082e9","2026-11-21T05:13:31+00:00","0xd576f2c5eac0a07c34c4b2f439107ae1bb9e86fe"],["0x36856cdd82e135834e90b36ca3c9d398bb039737","2026-11-20T05:29:35+00:00","0x8dbe0a2f8ee46e2d0ac0530058466ae34f010838"],["0x95f2cdf909e4714f491d250d76c3d03a4f9b2a15","2026-11-23T08:34:52+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xfe3eae7d8b6e348dc96f45fa631aff5cb41e04ec","2026-11-25T01:27:18+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xf84b04f8d6a9255dc7e05fec1cd3ff508580cc81","2026-11-21T05:49:15+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0x759ac418244a6e03c22dddec7b612be911e01915","2026-11-20T10:59:58+00:00","0x1d9577c440a4873e1f21c187490f33a814a024b0"],["0xe98638a91483139b0684ff22fbaaf6f66fa09d81","2026-11-26T02:27:17+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0x839632f20b43259ad7cfac75caf3d11d04c059f4","2026-11-23T11:12:17+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x97054defa8150b769e89c497235b72bf607e1367","2026-11-23T07:00:57+00:00","0xb05900da5eb138158d638b54a806749d95da879c"],["0x30b0f1754e8a925b68ab005e3401487aacc5e9c5","2026-11-23T20:53:47+00:00","0xf88fd850a47974b40d7bcf8fa89ec0af48b163c1"],["0xd58ee0ee7daff7ee1baa42847bc7087d07f4ac36","2026-11-25T11:12:08+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xcbfcaadb2ade7bd874ab4b7983f61dbe6690618d","2026-11-21T05:35:15+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0x09bb17e373691e5279f4f3fc092a7ae20222397d","2026-11-20T22:17:29+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0x1e662262d8846ad1191ffee0333007463f2e7c6b","2026-11-23T23:51:06+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xcb0924813e40662c943855be6f63ebfa37fe3a6f","2026-11-24T03:07:12+00:00","0xaeaa327a6fb6ee8ca0d97469118e8609c980c7a9"],["0xa75b7d88a0964a36dd0b9a890f373ccb2eebd2e7","2026-11-20T10:27:54+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0xed4183f73723b52d3a85ec281825925d2345e6a8","2026-11-23T10:58:53+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x41d3fc595332e64050c4d53df61dc24a18d56945","2026-11-25T02:50:31+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x031faa4328d7e7a125cce6308b59ccb429e691f1","2026-11-25T03:20:25+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xb357a473599a40aa04be1cc8cd46330804570fc8","2026-11-23T17:34:36+00:00","0x7e9020380c608bcba4e47b6ccffa22c09387486d"],["0x8373c8d1a459f38f75f6fa22dae45a6c4530bdde","2026-11-22T02:48:16+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0xa125147e6106cb0d5b2dc00f9773b51890a5603c","2026-11-25T03:06:52+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xed2a3c491a45400ac539384b581f357b0144dced","2026-11-25T01:51:16+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x435e23da639a0383ed919627ad867ac671b550f9","2026-11-25T03:21:11+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xbed03724d2e2a3b536790f01a1b108fef153ddfb","2026-11-25T01:59:52+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xc12404e08b779f66b25d793a49ae3a4288a14562","2026-11-26T01:14:11+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0x6521440f92f6af1c0b849cc5633a95b0e45f58a0","2026-11-25T20:39:25+00:00","0x134e50aafd9fe4e8525092ec0ee2b97218298f6a"],["0x2de587d19d59e1e590b53f37f984a6bc6ce917db","2026-11-20T11:05:21+00:00","0xb561cfaf3ade352f9455654cf5bb026321b24e7a"],["0x83e124bdbcef761227807e130fea38d777f3139b","2026-11-25T13:54:27+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0xb343a664f31011c4b117c0d2778003d4e7dc4c0b","2026-11-23T05:13:55+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xd4e2da2abdf2b96bece987dc1aac0c63c0fa5e8f","2026-11-24T00:47:03+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x94a0bd8326ccd974aa8b11d5e68e467df0eb16e6","2026-11-24T04:34:28+00:00","0xaeaa327a6fb6ee8ca0d97469118e8609c980c7a9"],["0xcb37dab52c5ded4bca45ee9cdfc8dd92ef203bfe","2026-11-20T17:09:51+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0x3098bc133ed5f579c6b3ca12e08b6ce17afc9e0f","2026-11-21T06:35:59+00:00","0xd576f2c5eac0a07c34c4b2f439107ae1bb9e86fe"],["0x5bc0b3f29c3b03a7e8032aba3e6da621a9c7a4bc","2026-11-25T02:30:03+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xea02defdd321802c8d61f620cb4b9b0a4e13f311","2026-11-22T01:35:45+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0xf638ae25b3a2953e8123c20088392b368afbb965","2026-11-21T19:59:02+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0x9437ca602c3446538eda6e70ed9835f1a3dc0197","2026-11-21T06:33:20+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0xa71be066c926bafe42a63f50c85dae2773045dc2","2026-11-20T11:36:37+00:00","0x1d9577c440a4873e1f21c187490f33a814a024b0"],["0x3ff21e413eed3d11f5fcd7e24a56741bff9a6b60","2026-11-24T10:21:21+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x740215fbca3dcbad79b464c8ab8ca95ce11e1b26","2026-11-20T14:31:26+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0xbbadb687f27aa7f6b76e8dabd1f47342cb00a123","2026-11-23T05:06:53+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xd88f85d6334738d0893e9662aeaa94e396c65a11","2026-11-20T14:47:59+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0x80f7ad15cd865158f04509a8a251ba03969254d4","2026-11-21T10:42:40+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0x6d0ba8e12c6efaab2632b87482cc7b21a6dfeaa2","2026-11-25T23:32:40+00:00","0x556c89cc2b3f33002576fed06873d84ca164ce62"],["0xd7ca89f3839c3bb9a8a5818c4482902361cb3a94","2026-11-22T03:40:02+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0xc978da29c8e7a750d98a58eab003357fd576d389","2026-11-21T03:12:41+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0x7bd14162400d7afd4241ba7963988f4aa2464dde","2026-11-24T06:02:06+00:00","0x4838326a8aac6f82eb8e445a5c352afcc46f7bc1"],["0xeb224627f6471b8efdd4854199d2cda7d650435e","2026-11-22T00:18:58+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0x55ea1f6afb52a932cf84baaf16b654f558c8259f","2026-11-20T17:34:32+00:00","0xa6799305843b9c5bf9f9a3a295a26dedbe79bbb8"],["0xddbc2eab795ed34ce984d89c42e85c6ca4a5d688","2026-11-25T04:40:48+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0xeac041acc81bee063fb053d654c99138573ba58a","2026-11-22T03:54:37+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0x99cbe0deb5bdc0f7305710aeba28802492d88e90","2026-11-22T16:57:16+00:00","0xb9fbb483e581b6e19d81cc47cabd5a4b8af791c9"],["0xb20f9d0f5d7f84f81919621459817934b2505c97","2026-11-21T06:28:25+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0x2010055929d756dff7b5a92e55d00805e4509971","2026-11-25T13:09:35+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0xe285f71873596cf7d28c3078e7f71c98e496aaa9","2026-11-20T06:09:28+00:00","0xb1df17cb410fd0357663c0a387bdb367ff358c34"],["0x3f2946d70b56963ccb45b4f80ee4c9ffa8cffe6d","2026-11-21T01:39:00+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0xb7d9d7f1158f01453c3de8621ad5e997137209b4","2026-11-25T08:26:53+00:00","0xc66d9fdbee720693026234c47c63311476b97321"],["0xd5eda2c711422db41650c25994d41010158b3c81","2026-11-20T04:48:45+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xc2cbdc6112eb8573be3eefc442044b5976c5f945","2026-11-25T00:50:26+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xf39c05b2071633a499bab9f22d9071d120bc1bc5","2026-11-20T21:54:22+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0x9c9896dab95cfaaf49d3e9f03c689c39c2527060","2026-11-24T21:32:11+00:00","0xd1fef0eab1eb3694c28a6403e0a63af5b5ff0f93"],["0xa4e83c1956136d16b7c1aa7dbe98374c505ac8d4","2026-11-23T19:18:34+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0x605732afd66355927e76df446eeb0121e294dd62","2026-11-25T12:06:57+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xff2586e89aed0e90cc44238d1949b8bba8eaa8aa","2026-11-25T14:26:18+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0xc74c8e3809ffbcc9e0587492ac3ebccd6dabaf9f","2026-11-22T00:15:46+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0xb6ed67ce21abf6716b57b3bb5ecfad72fc4687cc","2026-11-21T02:13:21+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0xa395b4a4b9a0d60f7ac0d1260c95157c03dc893f","2026-11-20T15:57:27+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0xc6b5ee62bb69f634d3c62524ba0a2944f7c8d945","2026-11-21T13:28:49+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0xfc6d2caa0f3981fe01eb9314013cfa9c9e5b1ba2","2026-11-21T22:18:43+00:00","0x8923a3a2dd21aa88b587ff3777ff5354f747363a"],["0x516b8e22987cc64268d7ff17ac72e4c280e7dbf5","2026-11-21T05:31:37+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0x198da0920e04ef02873db7ab7a81b8305848249d","2026-11-23T05:19:20+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x8047c089ffc11dc4a0afd428389f402d797423de","2026-11-21T03:46:16+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xde143dd9167ac638dcfe9b75cab7c479adeb96db","2026-11-25T05:20:31+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0xf2456eb9b1086c93b758c3a4d13c8a0e0d02b057","2026-11-20T13:48:51+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0xabca429f64ac742a09b853acc140e212f60094cf","2026-11-24T03:36:09+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xaa9a579510c8bb8f49e054757de9c685f0ca427c","2026-11-21T00:39:02+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0x64cda9f73e3a33319d973334fd3e94bdfefdbcc1","2026-11-23T09:46:22+00:00","0x7e9020380c608bcba4e47b6ccffa22c09387486d"],["0xa7cb065d5c82306a376af4d1facc90603bbbf19f","2026-11-20T06:41:32+00:00","0xb1df17cb410fd0357663c0a387bdb367ff358c34"],["0xf715f8384aa738abbbf96e6b1766a5f6ad54c6ed","2026-11-25T11:40:13+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x2846a52689669000f01eb3b1c96fd25534b39ee6","2026-11-20T11:29:12+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0x2a6f293b19220609e7929e2f5581b8d4ecad1cfa","2026-11-25T13:25:40+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0x19a17414df8fb3f5464cae02831a01da4e457246","2026-11-21T02:04:33+00:00","0x782ffa3594d268290e9e2c93887afdaee5e7f495"],["0xcb3502909b262a61cc57aa870cf21afd40ddbf23","2026-11-25T04:41:17+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x36490751acab70c3c21034d0f23d50d4bd461eaf","2026-11-25T07:01:50+00:00","0xc66d9fdbee720693026234c47c63311476b97321"],["0x62c30c6ce9750883f57d54f65f81847fe8c9ebab","2026-11-23T23:40:21+00:00","0x39cb0c2825e96f240d64b7a747355bf44fa49ce1"],["0x1faed84d8f5745e46147e81352f1bde0f374518c","2026-11-20T01:47:51+00:00","0x48fc6c8bd65433acc28b90b5d1e5394a1df5f728"],["0xe88476f45b0a6e1487552fa3d086235e5dab0655","2026-11-20T11:10:12+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0x4ac59106888b8c4bf2c316444ab66c03ea50d26b","2026-11-20T05:07:09+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x96e6b23cb5e94f5fb1dd4144d93d405966c4c24c","2026-11-20T16:17:26+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xf6961460d46ca3823879a28f3d28ef5848dbcd3d","2026-11-24T02:00:30+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xae193d04a845fa11d9e4b3aec0bd2b184c602c26","2026-11-21T02:52:30+00:00","0x782ffa3594d268290e9e2c93887afdaee5e7f495"],["0xb3050405aa70102362213a4e9a5c691b7ef29f2f","2026-11-21T09:55:48+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0xbdfd32a9dc8eb54df55486f9c3c2ec7e44c7dc43","2026-11-25T00:31:28+00:00","0xc66d9fdbee720693026234c47c63311476b97321"],["0xcc896075ef24214118db2747ff35f8923e0cd236","2026-11-23T11:55:50+00:00","0x7e9020380c608bcba4e47b6ccffa22c09387486d"],["0x56c7e7889187bbd69b45a53cf00035310fff5b80","2026-11-22T16:49:56+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0x84526cd4950b36e5080cc7e789f76f9d3f94072f","2026-11-23T09:00:54+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xfa038b3bf0617f0b35356b959bd236ee615af89b","2026-11-25T14:33:56+00:00","0x134e50aafd9fe4e8525092ec0ee2b97218298f6a"],["0x2f6e58054c63a7c5b83740c540e2b57195ce21ec","2026-11-21T04:46:39+00:00","0xd576f2c5eac0a07c34c4b2f439107ae1bb9e86fe"],["0x5288352c82c68d914c0e0f4cb9fbdceaf7e6065f","2026-11-24T06:34:59+00:00","0x4838326a8aac6f82eb8e445a5c352afcc46f7bc1"],["0xe149fceeb02c8bbc937a1ee1eccf456ff69db4b9","2026-11-21T14:09:30+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0xc9b0f3db18af31300a3fe2e63f4abda2fb0301c3","2026-11-24T04:21:45+00:00","0x4838326a8aac6f82eb8e445a5c352afcc46f7bc1"],["0x8e665eb7703bcf92c68570a899d2deaeb1d09707","2026-11-21T23:03:59+00:00","0x8923a3a2dd21aa88b587ff3777ff5354f747363a"],["0x42367427a6e371478f008206a802453e0c31256e","2026-11-21T03:24:35+00:00","0xd576f2c5eac0a07c34c4b2f439107ae1bb9e86fe"],["0xd7dd701216f94572df26db51d64cc9aff00e62b1","2026-11-22T00:21:54+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0x8e9557e152d6824449e2526195b0f630011bd240","2026-11-25T13:29:12+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0xcecdadb3582dbe5154ab4dbbe217e71c35b1ca0a","2026-11-20T09:55:37+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0xb58dd901ec1c986e66a7b79878ac1b3808c1826d","2026-11-25T13:12:25+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0xbd2402361760bcbceac1e91986c0409f606bc253","2026-11-21T06:27:09+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0x75328dfceb58f3fa2c7c726c43c4566fe14b0d5b","2026-11-20T08:13:01+00:00","0xb1df17cb410fd0357663c0a387bdb367ff358c34"],["0x4dc82e974cdb8ffacc0a865e2eb3ed25d6d35faa","2026-11-22T01:01:55+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0x2f79d5aa74c09439683d9bdf6b0cd4fce1056dfd","2026-11-22T13:33:42+00:00","0xad9c6bc581921ca2dc5bd1a53b679d8f94960300"],["0x5951763ecc32b6d0303b70ececa08ec8a05d19ce","2026-11-23T06:39:47+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x75881f505fd1607e2a4eb7158d4eaacc8e14fdc6","2026-11-22T13:11:39+00:00","0xd082b21c1d87bb21afdc3da53351b05d6610dc3b"],["0x759649e6ff8e8c8d5684c076054c990d4ee476de","2026-11-20T06:47:34+00:00","0x1d9577c440a4873e1f21c187490f33a814a024b0"],["0x078ce73e55cdcba0af50b32e63e5e7ae82521944","2026-11-25T03:33:53+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xf508c66771af36fc4da892e1c7608f031b791e81","2026-11-22T17:28:42+00:00","0xb9fbb483e581b6e19d81cc47cabd5a4b8af791c9"],["0x5e33388e0e4a0958363793c49a5deff98baf2a3a","2026-11-25T12:59:58+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0x8749e00321b829566debdf69bd7dd1a4691294cc","2026-11-22T00:54:56+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0x91074564a6d9f81abc8fb1078692a025dbfac47a","2026-11-20T03:43:54+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x02cffb4fcd4b45d2e9b19667cd5eb126782a7c80","2026-11-24T02:57:34+00:00","0xaeaa327a6fb6ee8ca0d97469118e8609c980c7a9"],["0x470884576a826dcf4ab0a16c48889e5f00590bfd","2026-11-25T14:56:13+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0xd0335d7cb217b75b3fbfa131f1a0d055c7591897","2026-11-25T08:55:15+00:00","0x556c89cc2b3f33002576fed06873d84ca164ce62"],["0xb9e1585208136aa7b0bca8dcde5fa9c40fc63cf9","2026-11-20T11:45:29+00:00","0xb561cfaf3ade352f9455654cf5bb026321b24e7a"],["0xa5ac8ee17f5cc7e600fdde827e8d29269c211025","2026-11-20T11:55:14+00:00","0x1d9577c440a4873e1f21c187490f33a814a024b0"],["0x3d16b6831b310d1ae3d8786684c0905bdf7ff162","2026-11-25T12:59:07+00:00","0x556c89cc2b3f33002576fed06873d84ca164ce62"],["0x2598622fa60e7506ad2462e9e81214eedbcadf90","2026-11-21T01:37:15+00:00","0x782ffa3594d268290e9e2c93887afdaee5e7f495"],["0xe386997eba920ed87cd716df1250f8f4639345ff","2026-11-25T03:18:54+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xc3230b09737d7d2eb14b3ece05217f50a34e2dd2","2026-11-25T10:09:04+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xc7eb0a87a4cbf480d81878cdbcfe2b2e8057178c","2026-11-25T12:09:59+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xc80ac28eecb369f9d8ecc4587362d82659c62525","2026-11-20T06:57:14+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xc4e67ef3972443725ebd50097af0b35fc9daaa99","2026-11-24T14:57:49+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x368f8d2dc2133aa82d8187a993db8acae1a796b2","2026-11-20T14:20:00+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0xf3628ad2cb4fee5ab2c939c9e36a267297226dc6","2026-11-24T02:48:58+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xc96da5cfa36c1c70cff4f7cb59604bca73015593","2026-11-20T12:51:32+00:00","0x1d9577c440a4873e1f21c187490f33a814a024b0"],["0x29e40810ec4dac496a25e10ccea4e5712ed0c78f","2026-11-21T00:46:14+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0x637937d47de4c834c1a69087d1a62a4bbd47e4cd","2026-11-25T12:29:50+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0x2af685890ac1f39656fcaaadb5df96e96a9d9f7c","2026-11-22T12:28:49+00:00","0xd082b21c1d87bb21afdc3da53351b05d6610dc3b"],["0x69e865a1d5f47833024d2c6a3c6465f9672d0fd7","2026-11-20T23:38:09+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xdc940abb5de086e4ac8ef38db19df9d7e9280e9a","2026-11-23T09:17:57+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x51e1e9d45e3eb07b681046db3fbe25575ff1af6f","2026-11-20T03:08:40+00:00","0x8dbe0a2f8ee46e2d0ac0530058466ae34f010838"],["0x38e213d320bf02b08ba0728072a5353d8f385af5","2026-11-24T04:54:00+00:00","0xaeaa327a6fb6ee8ca0d97469118e8609c980c7a9"],["0xac41cc848af807ba80dc35587c3eb4044320c890","2026-11-25T12:00:13+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x23a8949f4572904801d8d664726156fb533f8c95","2026-11-23T03:50:59+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x35c22badd10d7e9f17d609f54b5979216fdbbd35","2026-11-25T18:50:15+00:00","0x134e50aafd9fe4e8525092ec0ee2b97218298f6a"],["0x010a24f88866504d02d7abec466ad7e9d42aabd9","2026-11-20T11:42:39+00:00","0xf1fbf01cf165915f70d43430a5eeb7d6bac82729"],["0x359c45fca7cb06f870632bdc3a293c2552bd7ba8","2026-11-21T00:35:12+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0xb6d0ca4a121b4e34965c4820fb781ffdbb5da37c","2026-11-21T06:51:03+00:00","0xd576f2c5eac0a07c34c4b2f439107ae1bb9e86fe"],["0x12fc219c51c8f877bda2cbb6ff27c8606fbbe15f","2026-11-21T07:15:01+00:00","0xd576f2c5eac0a07c34c4b2f439107ae1bb9e86fe"],["0x2c22e534d6b5366e895141423b6d09f24b8072cd","2026-11-25T15:56:27+00:00","0x0b57a6900479eb5e0f991e2e7da43629563b2f7b"],["0x699b553ce091ed0fa0c5a69dded346d9d6a6053f","2026-11-21T01:41:00+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0xbc47d1873962910e7d656a124d630da0eea41857","2026-11-20T01:43:14+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0x4d8f849d79a471267d9449c7b63813ac6919e86d","2026-11-20T12:21:53+00:00","0xfa5844c7d9e76914307b7ad1d37347ddb31d3fdb"],["0x818cf620cd4fd3331c5716bda6e5904196b2c203","2026-11-24T03:56:07+00:00","0xaeaa327a6fb6ee8ca0d97469118e8609c980c7a9"],["0xb0b80501566d9511e80e29256087dd47175118b4","2026-11-22T00:35:43+00:00","0xd4762f0b8751b85fc594c0b5e9dceb372fe94691"],["0xf0e27f82169c00ee830dc3fdc41ee0f48e3af962","2026-11-22T10:14:12+00:00","0xad9c6bc581921ca2dc5bd1a53b679d8f94960300"],["0x3c5a78b8a3906fe8c486e28043fa85b88b4f3cba","2026-11-25T02:17:37+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x39c4754151ca496dede0956dc715c92cbe43ca99","2026-11-23T12:51:09+00:00","0x7e9020380c608bcba4e47b6ccffa22c09387486d"],["0xf65e4bb4b2325b565a9c91e540bc0ee80b08ec53","2026-11-24T07:01:40+00:00","0x4838326a8aac6f82eb8e445a5c352afcc46f7bc1"],["0xe80036baa05d8835e0866c1ab9bc2a68b1264744","2026-11-20T19:14:46+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0xf2a3fe5b7d1169772733d3be6d96e1b5b91bc005","2026-11-25T10:57:11+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0xf4e11256e48936709dc8c4f9687ff1176d41861f","2026-11-21T01:20:14+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"],["0xa0c7a3886bec254986dcf69c00a5d51aaa53c88e","2026-11-24T14:35:11+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x30425414d55b13427274a00c65f27c75bbf25d6a","2026-11-20T17:35:09+00:00","0xe065bbc60b355be9d884489ad63fcb5c663fd52e"],["0x465c49c5a386d6926c0b0a144ad35b8a8993305b","2026-11-21T14:59:10+00:00","0x4945280027ffced153401d699f1621a46b1adb46"],["0xad717944b0033c15ceeddbb9bc3b203b9cf282ca","2026-11-20T07:36:24+00:00","0x1d9577c440a4873e1f21c187490f33a814a024b0"],["0x886ce76f1c0b2604f50ce6ff1baf11974dea4855","2026-11-25T12:48:37+00:00","0x134e50aafd9fe4e8525092ec0ee2b97218298f6a"],["0xf25bcff44bc58d68fce078620319ad6768ce097f","2026-11-22T13:35:50+00:00","0xd082b21c1d87bb21afdc3da53351b05d6610dc3b"],["0x51b3afa36c1a374d5e43c1abd66ca077f74f4964","2026-11-25T16:06:20+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0x10fca561a029cca0e6e81122644db841de601026","2026-11-23T06:00:24+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x69269f3ec79abc763c12a170ceec6be49109920c","2026-11-25T18:11:50+00:00","0x134e50aafd9fe4e8525092ec0ee2b97218298f6a"],["0xb66024ffa5b0ee5707bda4b261fa4724396938ac","2026-11-22T11:24:18+00:00","0xad9c6bc581921ca2dc5bd1a53b679d8f94960300"],["0x478879870aee33fc364e2561809b53c226f33def","2026-11-20T23:47:35+00:00","0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce"],["0x13921e6994ef9d02f0dcbeddccbdc99ebf076363","2026-11-24T05:43:21+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x99b748d3688c00f1009858272b941064417eff67","2026-11-25T09:24:12+00:00","0x95fa70efcb193961d3c0a7e6889616643a0c5576"],["0x32d533b442949dcff8f12ec53d69f70f27a1dab7","2026-11-25T11:17:06+00:00","0xa98ca0cdddbb239af17d4480230abd191a89221e"],["0x8624293590242aad5847eab6ffd999912a2c0bf8","2026-11-21T11:30:45+00:00","0x6352d587293b39790f96f5ca945abc7e8881d36b"]]
JSON_LS
    cat > /tmp/elysium-probe/run.py <<'PY_RUN'
"""Read-only research pass 2 (dashboard_ro, web image): Agora candles for every 30-day Consensus card token
and for a stratified sample of tokens created by tracked serial launchers. One request at a time,
~1.3 s apart, backs off on 429 and stops after repeated 429s, so the tracker's own Agora use is not
crowded out."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import sqlalchemy as sa

UTC = timezone.utc
NOW = datetime.now(UTC)
C = 99901
API = "https://api.agoradex.app"
UA = "elysium-wallet-tracker research (+https://github.com/nhattruong0204/elysium-wallet-tracker)"
S = requests.Session()
S.headers.update({"user-agent": UA, "accept": "application/json"})
ST = {"req": 0, "429": 0, "err": 0, "stopped": False}


def out(kind, obj):
    print("DATA|" + kind + "|" + json.dumps(obj, default=str, separators=(",", ":")), flush=True)


def get(path, **params):
    for attempt in range(3):
        if ST["stopped"]:
            return None
        time.sleep(1.3)
        try:
            r = S.get(API + path, params=params, timeout=20)
            ST["req"] += 1
            if r.status_code == 429:
                ST["429"] += 1
                if ST["429"] > 8:
                    ST["stopped"] = True
                    print("stopping: rate limited", flush=True)
                    return None
                time.sleep(30)
                continue
            if r.status_code >= 500:
                time.sleep(5)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            ST["err"] += 1
            time.sleep(5)
    return None


def g(x):
    try:
        return float(f"{float(x):.6g}")
    except (TypeError, ValueError):
        return None


def emit(kind, tok, rng, j):
    if not j:
        out(kind, {"token": tok, "range": rng, "ok": False})
        return
    st = j.get("stats") or {}
    out(kind, {"token": tok, "range": rng, "ok": True, "sym": j.get("symbol"), "pad": j.get("launchpad"),
               "created": j.get("createdAt"), "holders": j.get("holders"),
               "price": g(st.get("price")), "mcap": g(st.get("mcap")), "last": st.get("lastTradeAt"),
               "candles": [[int(float(c[0])), g(c[1]), g(c[2]), g(c[3]), g(c[4]), g(c[5]) if len(c) > 5 else None]
                           for c in (j.get("candles") or []) if isinstance(c, list) and len(c) >= 5]})


cards = json.load(open("/probe/cards.json"))
lsample = json.load(open("/probe/lsample.json"))
eng = sa.create_engine(os.environ["DATABASE_URL"])
toks = sorted({c[0] for c in cards})
lt = sorted({x[0] for x in lsample})
with eng.connect() as c:
    c.execute(sa.text("SET statement_timeout = '300s'"))
    for r in c.execute(sa.text("SELECT address, symbol, launchpad, launched_at FROM token_profiles "
                               "WHERE chain_id = :c AND address = ANY(:t)"), {"c": C, "t": toks + lt}):
        out("prof", {"token": r[0], "sym": r[1], "pad": r[2], "launched_at": r[3]})
    dec = {r[0]: int(r[1]) for r in c.execute(sa.text(
        "SELECT address, decimals FROM token_metadata WHERE chain_id = :c AND address = ANY(:t)"), {"c": C, "t": lt})}
    for tok, at, creator in lsample:
        rows = c.execute(sa.text("""
            SELECT kind, sum(hype_wei), sum(qty), min(block_time), max(block_time), count(*) FROM trades
             WHERE chain_id = :c AND token = :t AND wallet = :w AND kind IN ('BUY', 'SELL') AND hype_wei > 0
             GROUP BY kind"""), {"c": C, "t": tok, "w": creator}).all()
        k = {r[0]: r for r in rows}
        sc = 10 ** (dec.get(tok, 18) - 18)
        b, s = k.get("BUY"), k.get("SELL")
        out("cr", {"token": tok, "creator": creator, "decimals": dec.get(tok),
                   "buy_hype": int(b[1]) / 1e18 if b else None, "buy_px": int(b[1]) / int(b[2]) * sc if b and int(b[2]) else None,
                   "sell_hype": int(s[1]) / 1e18 if s else None, "sell_px": int(s[1]) / int(s[2]) * sc if s and int(s[2]) else None,
                   "buy_at": b[3] if b else None, "sell_first": s[3] if s else None, "sell_last": s[4] if s else None,
                   "n_sells": s[5] if s else 0})
t0 = time.monotonic()
recent7 = {c[0] for c in cards if NOW - datetime.fromisoformat(c[1]) < timedelta(days=6.8)}
recent1 = {c[0] for c in cards if NOW - datetime.fromisoformat(c[1]) < timedelta(hours=23)}
for tok in toks:
    emit("ct", tok, "all", get(f"/api/tokens/{tok}", range="all"))
    if tok in recent7:
        emit("ct", tok, "7d", get(f"/api/tokens/{tok}", range="7d"))
    if tok in recent1:
        emit("ct", tok, "24h", get(f"/api/tokens/{tok}", range="24h"))
print(f"card tokens done in {time.monotonic() - t0:.0f}s {ST}", flush=True)
for tok in lt:
    emit("lt", tok, "all", get(f"/api/tokens/{tok}", range="all"))
print(f"done in {time.monotonic() - t0:.0f}s {ST}", flush=True)
sys.exit(0)
PY_RUN
    chmod 644 /tmp/elysium-probe/*
    docker compose run -T --rm --no-deps -v /tmp/elysium-probe:/probe:ro --entrypoint python web /probe/run.py 2>&1 | grep -v "^ Container\|Creating\|Created" || true
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
