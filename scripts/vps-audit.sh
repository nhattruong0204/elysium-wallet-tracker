#!/usr/bin/env bash
# READ-ONLY infrastructure audit of the VPS. Changes nothing: no restarts, no writes to
# application data, no cleanup. Secrets are never printed: environment variables are
# listed by NAME (values only for non-secret connection hints, with passwords redacted),
# config files are passed through `redact`. The deploy workflow encrypts this output
# before it reaches the (public) Actions log.
#
#   ssh vps 'bash -s' < scripts/vps-audit.sh            (run as root for full visibility)
set -u
export LC_ALL=C
SLOW="nice -n 19 ionice -c3"
T() { timeout "${1}" "${@:2}"; }

sec() { printf '\n\n######## %s\n' "$*"; }
sub() { printf '\n---- %s\n' "$*"; }
# passwords in URLs, and VALUES of secret-looking keys (KEY=..., key: ..., "key": "...")
redact() {
  sed -E \
    -e 's#(://[^:/@ ]+:)[^@ ]+@#\1***@#g' \
    -e 's#((PASS|PASSWD|PASSWORD|SECRET|TOKEN|API_?KEY|PRIVATE|MNEMONIC|SEED|COOKIE|SALT|AUTH|CREDENTIAL|BEARER)[A-Za-z0-9_]*["]?[[:space:]]*[:=][[:space:]]*["]?)[^"[:space:],}]+#\1***#Ig' \
    -e 's#(Authorization:[[:space:]]*)[^"]+#\1***#Ig'
}

sec "HOST"
date -u +"audit time (UTC): %F %T"
hostnamectl 2>/dev/null | grep -E "Operating System|Kernel|Architecture|Virtualization" || cat /etc/os-release
uptime
echo "cpus: $(nproc)"; free -h; swapon --show 2>/dev/null

sec "DISK"
df -hT -x tmpfs -x devtmpfs -x overlay
sub "inodes"; df -i -x tmpfs -x devtmpfs -x overlay
sub "block devices"; lsblk -f -o NAME,FSTYPE,SIZE,FSUSE%,MOUNTPOINTS 2>/dev/null || lsblk -f
sub "largest top-level dirs on / (same filesystem)"
T 900 $SLOW du -xh --max-depth=1 / 2>/dev/null | sort -h | tail -20
for d in /var /var/lib /var/lib/docker /root /opt /home /srv /var/log /tmp; do
  [ -d "$d" ] || continue
  sub "du $d (depth 1)"; T 900 $SLOW du -xh --max-depth=1 "$d" 2>/dev/null | sort -h | tail -15
done
sub "files >= 500 MB (same filesystem, paths only)"
T 900 $SLOW find / -xdev -type f -size +500M -printf '%s\t%TY-%Tm-%Td %TH:%TM\t%p\n' 2>/dev/null \
  | sort -rn | awk -F'\t' '{printf "%8.2f GB  %s  %s\n", $1/1073741824, $2, $3}' | head -50
sub "SQLite / WAL files >= 1 MB"
T 900 $SLOW find / -xdev -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \
  -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*-wal' -o -name '*.db-journal' \) -size +1M \
  -printf '%s\t%TY-%Tm-%Td %TH:%TM\t%p\n' 2>/dev/null | sort -rn \
  | awk -F'\t' '{printf "%10.1f MB  %s  %s\n", $1/1048576, $2, $3}' | head -60
sub "deleted-but-open files still holding space"
T 60 find /proc/[0-9]*/fd -lname '*(deleted)' -printf '%l\n' 2>/dev/null | sort | uniq -c | sort -rn | head -15

sec "DOCKER"
docker version --format 'docker {{.Server.Version}}' 2>&1
sub "daemon.json"; cat /etc/docker/daemon.json 2>/dev/null | redact || echo "(none: default json-file logging, NO rotation)"
sub "docker info"; docker info 2>/dev/null | grep -E "Storage Driver|Docker Root Dir|Logging Driver|Containers|Running|Images"
sub "compose projects"; docker compose ls -a 2>&1
sub "containers"
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>&1
sub "per container: project, restart, health, mounts, networks, env NAMES"
for c in $(docker ps -aq); do
  docker inspect "$c" --format '
[{{.Name}}] image={{.Config.Image}} state={{.State.Status}} started={{.State.StartedAt}} restarts={{.RestartCount}}
  compose: project={{index .Config.Labels "com.docker.compose.project"}} service={{index .Config.Labels "com.docker.compose.service"}} dir={{index .Config.Labels "com.docker.compose.project.working_dir"}} files={{index .Config.Labels "com.docker.compose.project.config_files"}}
  restart={{.HostConfig.RestartPolicy.Name}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}} network_mode={{.HostConfig.NetworkMode}} log={{.HostConfig.LogConfig.Type}} {{.HostConfig.LogConfig.Config}}
  cmd={{json .Config.Cmd}} entrypoint={{json .Config.Entrypoint}}{{range .Mounts}}
  mount {{.Type}} {{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}} -> {{.Destination}} rw={{.RW}}{{end}}{{range $k, $v := .NetworkSettings.Networks}}
  net {{$k}} ip={{$v.IPAddress}}{{end}}' 2>/dev/null
  # env: names for everything; values only for connection hints, passwords redacted
  docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | while IFS= read -r kv; do
    k="${kv%%=*}"; v="${kv#*=}"
    if echo "$k" | grep -qiE 'PASS|SECRET|TOKEN|KEY|PRIVATE|MNEMONIC|SEED|AUTH|COOKIE|SALT|CRED'; then
      echo "  env $k=***"
    elif echo "$k" | grep -qiE 'DATABASE|_DB$|^DB_|POSTGRES_DB|POSTGRES_USER|PGHOST|PGDATABASE|PGUSER|REDIS|SQLITE|_URL$|_URI$|_PATH$|_DIR$|_HOST$|_PORT$|DOMAIN'; then
      echo "  env $k=$(printf '%s' "$v" | redact)"
    else
      echo "  env $k"
    fi
  done
done
sub "docker system df"; T 600 docker system df 2>&1
sub "docker system df -v (volumes / images)"; T 900 $SLOW docker system df -v 2>&1 | sed -n '/VOLUME NAME/,/^$/p;/REPOSITORY/,/^$/p' | head -80
sub "volumes"; docker volume ls 2>&1
sub "networks"; docker network ls 2>&1
sub "container json logs >= 10 MB"
for f in $(find /var/lib/docker/containers -name '*-json.log' -size +10M 2>/dev/null); do
  id=$(basename "$(dirname "$f")"); n=$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null || echo "(removed)")
  printf '%10s  %s\n' "$(du -h "$f" | cut -f1)" "$n"
done
sub "compose files of every project (redacted)"
for f in $(docker ps -a --format '{{.Label "com.docker.compose.project.config_files"}}' | tr ',' '\n' | sort -u); do
  [ -f "$f" ] || continue
  echo "== $f"; redact < "$f"
done
sub ".env files next to compose projects (KEY names only)"
for d in $(docker ps -a --format '{{.Label "com.docker.compose.project.working_dir"}}' | sort -u); do
  for e in "$d"/.env "$d"/.env.*; do
    [ -f "$e" ] || continue
    echo "== $e ($(stat -c '%a %U' "$e"))"; grep -vE '^\s*(#|$)' "$e" | cut -d= -f1 | sed 's/^/  /'
  done
done

sec "DATABASES: PostgreSQL (read-only SQL)"
for c in $(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /postgres|timescale|postgis/ {print $1}'); do
  docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^PGHOST=' && continue  # a client (e.g. backup sidecar), not a server
  sub "postgres container $c"
  docker exec "$c" sh -c 'echo "PGDATA=${PGDATA:-/var/lib/postgresql/data}"; du -sh "${PGDATA:-/var/lib/postgresql/data}" "${PGDATA:-/var/lib/postgresql/data}/pg_wal" 2>/dev/null'
  q() { docker exec "$c" sh -c 'psql -X -A -F" | " -v ON_ERROR_STOP=0 -U "${POSTGRES_USER:-postgres}" -d "$1" -c "SET default_transaction_read_only = on;" -c "$2" 2>&1' -- "$1" "$2"; }
  q postgres "SELECT version();"
  q postgres "SELECT datname, pg_size_pretty(pg_database_size(datname)) size, pg_database_size(datname) bytes FROM pg_database WHERE datistemplate = false ORDER BY 3 DESC;"
  q postgres "SELECT rolname, rolcanlogin, rolsuper FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1;"
  q postgres "SELECT datname, usename, application_name, client_addr, state, count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY 1,2,3,4,5 ORDER BY 1,6 DESC;"
  q postgres "SELECT slot_name, slot_type, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) retained FROM pg_replication_slots;"
  q postgres "SELECT name, setting, unit FROM pg_settings WHERE name IN ('max_wal_size','wal_keep_size','archive_mode','archive_command','max_connections','shared_buffers');"
  for db in $(docker exec "$c" sh -c 'psql -X -A -t -U "${POSTGRES_USER:-postgres}" -d postgres -c "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> '"'"'postgres'"'"'"' 2>/dev/null); do
    sub "database $db in $c: schemas, extensions, top tables"
    q "$db" "SELECT nspname, pg_size_pretty(sum(pg_total_relation_size(c.oid))::bigint) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','m','p') GROUP BY 1 ORDER BY sum(pg_total_relation_size(c.oid)) DESC;"
    q "$db" "SELECT extname, extversion FROM pg_extension ORDER BY 1;"
    q "$db" "SELECT schemaname||'.'||relname tbl, pg_size_pretty(pg_total_relation_size(relid)) total, n_live_tup live, n_dead_tup dead, n_tup_ins ins, n_tup_upd upd, n_tup_del del, greatest(last_autovacuum,last_vacuum) vacuumed FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 25;"
  done
done
if systemctl is-active --quiet postgresql 2>/dev/null; then
  sub "host PostgreSQL service is ACTIVE"
  sudo -u postgres psql -X -A -c "SET default_transaction_read_only=on;" -c "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database ORDER BY pg_database_size(datname) DESC;" 2>&1
fi

sec "DATABASES: SQLite (read-only header facts, cheap)"
for f in $(T 600 $SLOW find / -xdev -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) -size +50M 2>/dev/null); do
  sub "$f"
  ls -la --time-style=+%F_%T "$f" "$f-wal" "$f-shm" 2>/dev/null
  T 120 python3 - "$f" <<'PY' 2>&1
import sqlite3, sys
p = sys.argv[1]
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
g = lambda q: c.execute(q).fetchone()[0]
ps, pc, fl = g("PRAGMA page_size"), g("PRAGMA page_count"), g("PRAGMA freelist_count")
print(f"journal_mode={g('PRAGMA journal_mode')} page_size={ps} pages={pc} -> {ps*pc/1e9:.2f} GB, free pages={fl} ({ps*fl/1e9:.2f} GB reclaimable by VACUUM)")
print("tables:", ", ".join(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")))
PY
  sub "processes holding $f open"; T 30 fuser -v "$f" 2>&1 | tail -n +1; T 30 lsof "$f" 2>/dev/null | awk 'NR>1{print $1, $2, $3}' | sort -u
done

sec "DATABASES: Redis"
for c in $(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /redis|valkey|keydb/ {print $1}'); do
  sub "redis $c"; docker exec "$c" sh -c 'redis-cli INFO memory | grep -E "used_memory_human|maxmemory_human"; redis-cli INFO keyspace' 2>&1
done

sec "SYSTEMD"
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | awk '{print $1}' | grep -vE '^(systemd-|dbus|getty|serial-getty|user@|ssh|cron|rsyslog|snapd|polkit|udisks|ModemManager|multipathd|networkd|unattended|irqbalance|qemu-guest|chrony|packagekit|fwupd)'
sub "failed units"; systemctl --failed --no-pager --no-legend 2>/dev/null
for u in $(systemctl --failed --no-pager --no-legend --plain 2>/dev/null | awk '{print $1}'); do
  sub "last log lines of failed $u"; journalctl -u "$u" -n 25 --no-pager -o short-iso 2>/dev/null | redact
done
sub "timers"; systemctl list-timers --all --no-pager 2>/dev/null | head -40
for u in $(systemctl list-unit-files --no-legend --no-pager 2>/dev/null | awk '{print $1}' | grep -iE 'radar|onchain|elysium|revival|caddy|nginx|traefik|backup|gmgn|tracker|web'); do
  sub "unit $u ($(systemctl is-enabled "$u" 2>/dev/null)/$(systemctl is-active "$u" 2>/dev/null))"
  systemctl cat "$u" 2>/dev/null | redact
done

sec "SCHEDULED JOBS"
for u in $(cut -d: -f1 /etc/passwd); do crontab -l -u "$u" 2>/dev/null | grep -vE '^\s*(#|$)' | redact | sed "s/^/[$u] /"; done
ls -la /etc/cron.d 2>/dev/null; for f in /etc/cron.d/*; do [ -f "$f" ] && { echo "== $f"; grep -vE '^\s*(#|$)' "$f" | redact; }; done

sec "REVERSE PROXY / TLS / DNS"
for p in nginx caddy traefik apache2 httpd haproxy; do
  pgrep -x "$p" >/dev/null && echo "process running: $p"
done
sub "listening sockets"; ss -ltnp 2>/dev/null | awk 'NR==1 || /LISTEN/' | sed -E 's/users:\(\("([^"]+)".*/\1/'
command -v nginx >/dev/null && { sub "nginx -T (redacted)"; T 30 nginx -T 2>&1 | redact | grep -vE '^\s*#' | head -300; }
for f in /etc/caddy/Caddyfile $(docker ps -a --format '{{.Label "com.docker.compose.project.working_dir"}}' | sort -u | sed 's#$#/deploy/Caddyfile#'); do
  [ -f "$f" ] && { sub "Caddyfile $f"; redact < "$f"; }
done
sub "certificates"
command -v certbot >/dev/null && T 60 certbot certificates 2>&1 | grep -E "Certificate Name|Domains|Expiry"
for v in $(docker volume ls -q | grep -i caddy); do
  mp=$(docker volume inspect -f '{{.Mountpoint}}' "$v"); find "$mp" -name '*.crt' 2>/dev/null | sed "s#$mp#[$v]#"
done
for d in onchainflow.app www.onchainflow.app; do
  sub "$d"
  echo "A: $(getent ahostsv4 "$d" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
  T 15 bash -c "echo | openssl s_client -servername $d -connect $d:443 2>/dev/null | openssl x509 -noout -issuer -enddate 2>/dev/null"
  T 15 curl -s -o /dev/null -w "HTTPS %{http_code} in %{time_total}s\n" "https://$d/"
done
echo "this host's public IPv4: $(T 10 curl -s -4 https://api.ipify.org || echo '?') ; interfaces: $(ip -4 -o addr show scope global | awk '{print $4}' | tr '\n' ' ')"

sec "APPLICATION DIRECTORIES (git checkouts)"
for g in $(T 300 $SLOW find / -xdev -maxdepth 5 -type d -name .git -not -path '*/node_modules/*' 2>/dev/null); do
  d=$(dirname "$g")
  echo "== $d  [$(du -sh --exclude=.git "$d" 2>/dev/null | cut -f1)]"
  echo "   remote: $(git -C "$d" remote get-url origin 2>/dev/null | redact)  branch: $(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "   head:   $(git -C "$d" log -1 --format='%h %cI %s' 2>/dev/null | cut -c1-110)"
  echo "   dirty:  $(git -C "$d" status --porcelain 2>/dev/null | wc -l) changed files"
done

sec "LOGS AND BACKUPS"
journalctl --disk-usage 2>/dev/null; grep -E '^\s*(SystemMaxUse|MaxRetentionSec)' /etc/systemd/journald.conf 2>/dev/null || echo "journald: default limits"
sub "backup-looking directories"
T 600 $SLOW find / -xdev -maxdepth 4 -type d \( -iname '*backup*' -o -iname '*dump*' -o -iname '*snapshot*' \) -not -path '/proc/*' -not -path '/usr/*' -not -path '/var/lib/docker/overlay2/*' 2>/dev/null | while read -r d; do
  printf '%8s  %5s files  newest %s  %s\n' "$(du -sh "$d" 2>/dev/null | cut -f1)" "$(find "$d" -type f 2>/dev/null | wc -l)" \
    "$(find "$d" -type f -printf '%TY-%Tm-%Td\n' 2>/dev/null | sort | tail -1)" "$d"
done
echo; echo "audit complete"
