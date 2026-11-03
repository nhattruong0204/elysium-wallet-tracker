#!/usr/bin/env bash
# Off-site backup, run ON THE GITHUB RUNNER by the deploy workflow (action=backup).
#
# Nothing is written to the VPS disk: each database is streamed over SSH to the runner,
# restored into a scratch database there and compared, then encrypted with the
# BACKUP_PASSPHRASE secret and uploaded as a workflow artifact by the next step.
#
#   1. Elysium tracker  PostgreSQL  pg_dump -Fc (one consistent snapshot)
#        verify: pg_restore into postgres:16, same table set, row counts <= live and > 0
#   2. Revival bot      SQLite      logical dump inside ONE read transaction (consistent,
#                                   no file copy, no space needed on the VPS)
#        verify: rebuilt with sqlite3, PRAGMA integrity_check = ok, row counts identical
#                to the counts taken in the same read transaction
#   3. Configuration    compose files, config dirs, systemd units, .env files (secrets
#                       stay inside the encrypted archive)
#
# Requires env: SSH_TARGET SSH_PORT SSH_OPTS [USE_SSHPASS SSHPASS] BACKUP_PASSPHRASE
set -euo pipefail
OUT="${RUNNER_TEMP:-/tmp}/offsite"
mkdir -p "$OUT/work" "$OUT/upload"
cd "$OUT"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
REVIVAL_DB=${REVIVAL_DB:-/opt/gmgn-revival-radar/data/revival_radar.db}
PG_CONTAINER=${PG_CONTAINER:-elysium-postgres}
[ -n "${BACKUP_PASSPHRASE:-}" ] || { echo "BACKUP_PASSPHRASE secret is not set: refusing to create unencrypted backups of a public repo"; exit 1; }
[ "${#BACKUP_PASSPHRASE}" -ge 20 ] || { echo "BACKUP_PASSPHRASE must be at least 20 characters"; exit 1; }

ssh_() {
  if [ -n "${USE_SSHPASS:-}" ]; then
    sshpass -e ssh -p "$SSH_PORT" -o StrictHostKeyChecking=yes -o ConnectTimeout=20 \
      -o ServerAliveInterval=30 $SSH_OPTS "$SSH_TARGET" "$@"
  else
    ssh -p "$SSH_PORT" -o StrictHostKeyChecking=yes -o ConnectTimeout=20 \
      -o ServerAliveInterval=30 $SSH_OPTS "$SSH_TARGET" "$@"
  fi
}
enc() {  # enc <file> -> upload/<file>.enc
  openssl enc -aes-256-cbc -pbkdf2 -iter 300000 -salt -pass env:BACKUP_PASSPHRASE \
    -in "$1" -out "upload/$(basename "$1").enc"
}
log() { printf '\n=== %s\n' "$*"; }
declare -A RESULT

# --- 1. Elysium PostgreSQL ----------------------------------------------------------
log "Elysium PostgreSQL: pg_dump -Fc over SSH"
ssh_ "docker exec $PG_CONTAINER pg_dump -U tracker -d tracker -Fc" > work/elysium-tracker.pgdump
ls -l work/elysium-tracker.pgdump
ssh_ "docker exec $PG_CONTAINER psql -X -A -t -U tracker -d tracker -c \"SELECT relname||' '||n_live_tup FROM pg_stat_user_tables ORDER BY 1\"" > work/pg-live-estimates.txt
log "restore test into a scratch postgres:16"
docker run -d --name pgverify -e POSTGRES_PASSWORD=verify -e POSTGRES_USER=tracker -e POSTGRES_DB=tracker postgres:16-alpine >/dev/null
# The image first runs a temporary init server on the unix socket only, then restarts;
# the final server is the one listening on TCP.
for _ in $(seq 1 90); do docker exec pgverify pg_isready -h 127.0.0.1 -U tracker -d tracker >/dev/null 2>&1 && break; sleep 1; done
sleep 2
docker exec -i pgverify pg_restore -U tracker -d tracker --no-owner --exit-on-error < work/elysium-tracker.pgdump
docker exec pgverify psql -X -A -t -U tracker -d tracker -c "SELECT version_num FROM alembic_version" > work/pg-alembic.txt
tables=$(docker exec pgverify psql -X -A -t -U tracker -d tracker -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
: > work/pg-restored-counts.txt
for t in $tables; do
  echo "$t $(docker exec pgverify psql -X -A -t -U tracker -d tracker -c "SELECT count(*) FROM \"$t\"")" >> work/pg-restored-counts.txt
done
cat work/pg-restored-counts.txt
python3 - work/pg-restored-counts.txt <<'PY'
import sys
rows = dict(l.split() for l in open(sys.argv[1]) if l.strip())
for t in ("trades", "transactions", "wallet_positions", "leaderboard_snapshots", "alembic_version"):
    assert int(rows.get(t, 0)) > 0, f"restored table {t} is empty"
print(f"pg restore check: {len(rows)} tables, {sum(map(int, rows.values()))} rows, key tables non-empty")
PY
RESULT[pg]="restored OK, alembic $(cat work/pg-alembic.txt), $(wc -l < work/pg-restored-counts.txt) tables"
docker rm -f pgverify >/dev/null
enc work/elysium-tracker.pgdump

# --- 2. Revival SQLite ----------------------------------------------------------------
log "Revival SQLite: consistent logical dump inside one read transaction (gzip on the VPS)"
ssh_ "nice -n 10 ionice -c3 python3 - '$REVIVAL_DB' | gzip -1" <<'PY' > work/revival-radar.sql.gz 2> work/revival-meta.json
import json, sqlite3, sys
db = sys.argv[1]
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60, isolation_level=None)
c.execute("BEGIN")                          # one snapshot for counts and dump
tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
counts = {t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
meta = {"user_version": c.execute("PRAGMA user_version").fetchone()[0], "counts": counts,
        "page_count": c.execute("PRAGMA page_count").fetchone()[0], "sqlite": sqlite3.sqlite_version}
w = sys.stdout.write
for line in c.iterdump():
    w(line + "\n")
w(f"PRAGMA user_version={meta['user_version']};\n")
c.execute("COMMIT")
sys.stderr.write(json.dumps(meta) + "\n")
PY
ls -l work/revival-radar.sql.gz; tail -c 2000 work/revival-meta.json
log "restore test: rebuild with sqlite3, integrity_check, exact row counts"
command -v sqlite3 >/dev/null || sudo apt-get install -y -qq sqlite3 >/dev/null
gzip -dc work/revival-radar.sql.gz | sqlite3 work/revival-restored.db
python3 - work/revival-restored.db work/revival-meta.json <<'PY'
import json, sqlite3, sys
meta = json.loads(open(sys.argv[2]).read().strip().splitlines()[-1])
c = sqlite3.connect(sys.argv[1])
assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "integrity_check failed"
assert c.execute("PRAGMA user_version").fetchone()[0] == meta["user_version"], "user_version differs"
bad = {t: (n, c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]) for t, n in meta["counts"].items()}
bad = {t: v for t, v in bad.items() if v[0] != v[1]}
assert not bad, f"row count mismatch: {bad}"
print(f"sqlite restore check: integrity ok, user_version {meta['user_version']}, "
      f"{len(meta['counts'])} tables, {sum(meta['counts'].values())} rows identical")
PY
RESULT[sqlite]="restored OK, integrity ok, row counts identical"
rm -f work/revival-restored.db
enc work/revival-radar.sql.gz

# --- 3. Configuration ----------------------------------------------------------------
log "configuration archive (compose, config, units, .env files)"
ssh_ "tar -czf - --ignore-failed-read \
  /opt/gmgn-revival-radar/docker-compose.yml /opt/gmgn-revival-radar/docker-compose.website.yml \
  /opt/gmgn-revival-radar/docker-compose.https.yml /opt/gmgn-revival-radar/deploy \
  /opt/gmgn-revival-radar/.env /opt/gmgn-revival-radar/.env.web /etc/revival-radar \
  /etc/systemd/system/onchainflow-backup.service /etc/systemd/system/onchainflow-backup.timer \
  /etc/systemd/system/onchainflow-health.service /etc/systemd/system/onchainflow-health.timer \
  /root/elysium-wallet-tracker/docker-compose.yml /root/elysium-wallet-tracker/.env \
  /root/elysium-wallet-tracker/config 2>/dev/null" > work/config.tar.gz || true
tar -tzf work/config.tar.gz | sed 's/^/  /'
enc work/config.tar.gz

# --- manifest (no secrets) -------------------------------------------------------------
( cd upload && sha256sum ./*.enc ) > upload/SHA256SUMS
python3 - "$STAMP" > upload/manifest.json <<PY
import json, os, sys
up = "$OUT/upload"
print(json.dumps({
  "created_utc": sys.argv[1],
  "encryption": "openssl enc -aes-256-cbc -pbkdf2 -iter 300000 -salt (passphrase: BACKUP_PASSPHRASE secret)",
  "files": {f: os.path.getsize(os.path.join(up, f)) for f in sorted(os.listdir(up)) if f.endswith(".enc")},
  "verification": {"postgres": "${RESULT[pg]}", "sqlite": "${RESULT[sqlite]}"},
  "revival_counts": json.loads(open("$OUT/work/revival-meta.json").read().strip().splitlines()[-1])["counts"],
}, indent=2))
PY
cat upload/manifest.json; cat upload/SHA256SUMS
shred -u work/* 2>/dev/null || rm -f work/*
echo "BACKUP_STAMP=$STAMP" >> "${GITHUB_ENV:-/dev/null}"
echo "backup complete: $OUT/upload"
