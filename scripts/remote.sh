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
  for u in https://rpc.thekleos.xyz/rpc https://www.agoradex.app/traders https://api.telegram.org \
           https://registry-1.docker.io/v2/ https://pypi.org/simple/; do
    code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$u" || echo "fail")
    echo "reach $u -> $code"
  done
  chain=$(curl -s -m 10 -H 'content-type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' https://rpc.thekleos.xyz/rpc || true)
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
  umask 077
  local pg=""
  if [ -f .env ]; then
    pg="$(grep -E '^POSTGRES_PASSWORD=' .env | head -1 | cut -d= -f2- || true)"
  fi
  if [ -z "$pg" ]; then
    pg="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "generated a new POSTGRES_PASSWORD (stored only in $DIR/.env)"
  fi
  { echo "POSTGRES_PASSWORD=$pg"; cat .env.incoming; } > .env.tmp
  mv .env.tmp .env
  rm -f .env.incoming
}

install_config() {
  mkdir -p config backups
  cp deploy/config.yaml config/config.yaml
  cp deploy/watchlist.txt config/watchlist.txt
}

wait_healthy() {
  log "waiting for the tracker to report healthy (up to 5 min)"
  for _ in $(seq 1 60); do
    state="$(docker inspect -f '{{.State.Health.Status}}' elysium-wallet-tracker 2>/dev/null || echo missing)"
    case "$state" in
      healthy) echo "tracker: healthy"; return 0 ;;
      unhealthy) echo "tracker: UNHEALTHY"; return 1 ;;
    esac
    sleep 5
  done
  echo "tracker: still '$state' after 5 min"
  return 1
}

status() {
  log "containers"
  docker compose ps
  log "tracker --status"
  docker compose run --rm --no-deps tracker --status || true
  log "recent tracker logs"
  docker compose logs --no-color --tail 60 tracker || true
}

case "$ACTION" in
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
    docker compose up -d --no-deps --force-recreate tracker
    wait_healthy || true
    status
    ;;
  status)
    status
    ;;
  logs)
    docker compose logs --no-color --tail 300 tracker
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
  restart)
    docker compose restart tracker
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
