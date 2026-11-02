#!/bin/sh
# Restore a backup into the running stack:  ./scripts/restore.sh backups/tracker-XXXX.dump
# Stops the tracker first so nothing writes during the restore, then starts it again.
set -eu
dump="${1:?usage: $0 backups/tracker-<UTC>.dump}"
[ -f "$dump" ] || { echo "no such file: $dump" >&2; exit 1; }
docker compose stop tracker
docker compose exec -T postgres pg_restore --clean --if-exists --no-owner \
  -U tracker -d tracker < "$dump"
docker compose run --rm migrate          # no-op unless the dump predates a migration
docker compose start tracker
echo "restored $dump"
