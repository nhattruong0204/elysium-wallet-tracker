#!/usr/bin/env bash
# Approved maintenance steps for the VPS, run over SSH by the deploy workflow
# (action=maint, steps=a,b,c). Every step names its exact targets; there are no globs
# outside fixed directories, no `docker system prune`, no volume removal, and the live
# databases are never touched. Each step prints what it did and the disk before/after.
#
#   ssh vps 'bash -s -- pause-revival-backup-timer,stop-old-website' < scripts/vps-maint.sh
set -uo pipefail
export LC_ALL=C
STEPS="${1:?steps}"
REPO="$(cd "${2:-elysium-wallet-tracker}" 2>/dev/null && pwd || true)"   # code bundle uploaded by the workflow
CADDYFILE=/opt/gmgn-revival-radar/deploy/Caddyfile
CADDY_C=gmgn-revival-radar-caddy-1
CADDY_BAK="$CADDYFILE.pre-elysium"
LIVE_DB=/opt/gmgn-revival-radar/data/revival_radar.db
KEEP_DAILY=/root/onchainflow-backups/daily/daily-20261101T190022807644Z.db   # newest copy stays

say() { printf '\n=== %s\n' "$*"; }
disk() { df -B1 --output=used,avail / | awk 'NR==2{printf "disk: used %.2f GB, free %.2f GB\n", $1/1e9, $2/1e9}'; }
open_by_any_process() {  # 0 if any process holds a file under $1 open
  find /proc/[0-9]*/fd -lname "$1*" 2>/dev/null | grep -q .
}
del() {  # del <path>: remove one approved file or directory, refusing anything unexpected
  local p="$1"
  case "$p" in
    "$LIVE_DB"|"$LIVE_DB"-wal|"$LIVE_DB"-shm|"$KEEP_DAILY") echo "REFUSED (protected): $p"; return 1 ;;
    /root/onchainflow-backups/*|/root/onchainflow-releases|/root/onchainflow-stage|/opt/gmgn-revival-radar-backups|/opt/gmgn-revival-radar/data/revival_radar.before-upgrade-*) ;;
    *) echo "REFUSED (not in the approved list): $p"; return 1 ;;
  esac
  [ -e "$p" ] || { echo "already gone: $p"; return 0; }
  if open_by_any_process "$p"; then echo "SKIPPED (open by a process): $p"; return 1; fi
  local sz; sz=$(du -sb "$p" 2>/dev/null | cut -f1)
  rm -rf --one-file-system -- "$p" && awk -v b="$sz" -v p="$p" 'BEGIN{printf "deleted %8.2f GB  %s\n", b/1e9, p}'
}
healthy_bots() {
  echo "containers:"; docker ps --format '  {{.Names}}: {{.Status}}' | sort
  echo "revival bot, last scan lines:"; docker logs --since 15m gmgn-revival-radar-radar-1 2>&1 | grep -E 'scan complete' | tail -2 | sed 's/^/  /'
  echo "elysium tracker:"; docker exec elysium-wallet-tracker python -m agora_tracker --health 2>&1 | tail -1 | sed 's/^/  /'
}

disk
for step in ${STEPS//,/ }; do
  case "$step" in
    pause-revival-backup-timer)
      say "pause onchainflow-backup.timer (reversible: systemctl enable --now onchainflow-backup.timer)"
      systemctl disable --now onchainflow-backup.timer 2>&1
      systemctl reset-failed onchainflow-backup.service 2>/dev/null || true
      systemctl is-enabled onchainflow-backup.timer 2>&1; systemctl is-active onchainflow-backup.timer 2>&1
      ;;
    stop-old-website)
      say "stop the old website (container kept for rollback: docker start gmgn-revival-radar-web-1)"
      docker stop gmgn-revival-radar-web-1 2>&1
      systemctl disable --now onchainflow-health.timer 2>&1
      echo "web: $(docker inspect -f '{{.State.Status}}' gmgn-revival-radar-web-1 2>&1)"
      echo "health timer: $(systemctl is-enabled onchainflow-health.timer 2>&1)/$(systemctl is-active onchainflow-health.timer 2>&1)"
      echo "public site now: $(curl -s -o /dev/null -w '%{http_code}' -m 10 https://onchainflow.app/ || echo '?') (502 expected until the dashboard cutover)"
      healthy_bots
      ;;
    delete-copies)
      say "delete approved copies of the revival database (live DB and newest daily copy are protected)"
      [ -f "$LIVE_DB" ] || { echo "live DB missing: aborting"; exit 1; }
      [ -f "$KEEP_DAILY" ] || { echo "newest daily copy missing: aborting"; exit 1; }
      for f in daily-20261026T190024108651Z daily-20261027T190010508862Z daily-20261028T190000745550Z \
               daily-20261029T190009985200Z daily-20261030T190001060258Z daily-20261031T190015537144Z; do
        for suffix in .db .db-shm .db-wal; do del "/root/onchainflow-backups/daily/$f$suffix"; done
      done
      del /root/onchainflow-backups/daily/daily-20261102T190001246454Z.db-journal
      rm -f /root/onchainflow-backups/daily/daily-20261101T190022807644Z.db-shm /root/onchainflow-backups/daily/daily-20261101T190022807644Z.db-wal  # empty side files from the audit
      del /root/onchainflow-backups/release-20261012T130446Z
      del /root/onchainflow-releases
      del /root/onchainflow-stage
      del /opt/gmgn-revival-radar-backups
      for f in /opt/gmgn-revival-radar/data/revival_radar.before-upgrade-2026100*Z.db; do
        [ -e "$f" ] && del "$f"
      done
      echo "remaining in daily/:"; ls -la /root/onchainflow-backups/daily/
      echo "live DB intact: $(stat -c '%s bytes, modified %y' "$LIVE_DB")"
      healthy_bots
      ;;
    prune-build-cache)
      say "docker builder prune (build cache only; images, containers and volumes untouched)"
      docker builder prune -f 2>&1 | tail -1
      ;;
    remove-old-image-tags)
      say "remove superseded revival image tags (current :latest and v2-cb3401f rollback images kept)"
      for img in gmgn-revival-radar-web:website-release gmgn-revival-radar-radar:website-release \
                 gmgn-revival-radar-radar:before-website gmgn-revival-radar:before-market-report-20261010T033424Z \
                 gmgn-revival-radar:before-market-report-20261010T032731Z gmgn-revival-radar:before-helius-20261009T164648Z \
                 gmgn-revival-radar:audit-5b0f6df gmgn-revival-radar:rollback-dd14651-20261008; do
        docker image inspect "$img" >/dev/null 2>&1 || { echo "not present: $img"; continue; }
        docker rmi "$img" 2>&1 | tail -1
      done
      ;;
    cap-journald)
      say "cap the systemd journal at 200 MB"
      mkdir -p /etc/systemd/journald.conf.d
      printf '[Journal]\nSystemMaxUse=200M\nSystemKeepFree=2G\n' > /etc/systemd/journald.conf.d/50-size-cap.conf
      systemctl restart systemd-journald
      journalctl --vacuum-size=200M 2>&1 | tail -1
      journalctl --disk-usage
      ;;
    install-revival-health)
      say "install the Revival scan-health monitor (read-only; replaces the retired onchainflow-health)"
      [ -f "$REPO/scripts/revival-health.py" ] && [ -f "$REPO/deploy/systemd/revival-health.timer" ] \
        || { echo "code bundle missing under $REPO"; exit 1; }
      install -d -m 0755 /opt/elysium-ops
      install -m 0755 "$REPO/scripts/revival-health.py" /opt/elysium-ops/revival-health.py
      install -m 0644 "$REPO/deploy/systemd/revival-health.service" /etc/systemd/system/revival-health.service
      install -m 0644 "$REPO/deploy/systemd/revival-health.timer" /etc/systemd/system/revival-health.timer
      systemctl daemon-reload
      echo "scan_runs columns and newest rows (no secrets in this table):"
      python3 - "$LIVE_DB" <<'PY'
import sqlite3, sys, urllib.parse
c = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(sys.argv[1]), uri=True, timeout=5)
c.execute("PRAGMA query_only=ON")
print("  columns:", [r[1] for r in c.execute("PRAGMA table_info(scan_runs)")])
for r in c.execute("SELECT id, started, finished, duration_seconds, processed, errors, sent, sources_ok FROM scan_runs ORDER BY id DESC LIMIT 3"):
    print("  ", r)
PY
      echo "dry evaluation:"; python3 /opt/elysium-ops/revival-health.py --no-alert --json </dev/null || true
      echo "telegram delivery check:"; python3 /opt/elysium-ops/revival-health.py --test-telegram </dev/null || true
      systemctl enable --now revival-health.timer 2>&1
      systemctl start revival-health.service 2>&1 || true
      echo "timer: $(systemctl is-enabled revival-health.timer)/$(systemctl is-active revival-health.timer)"
      systemctl list-timers revival-health.timer --no-pager 2>&1 | head -3
      journalctl -u revival-health.service -n 5 --no-pager 2>&1 | sed 's/^/  /'
      echo "old website health timer: $(systemctl is-enabled onchainflow-health.timer 2>&1)/$(systemctl is-active onchainflow-health.timer 2>&1) (must stay disabled)"
      ;;
    caddy-show)
      say "Caddy: structure of the live Caddyfile (long token-like strings redacted)"
      ls -la "$CADDYFILE" "$CADDY_BAK" 2>&1 | sed 's/^/  /'
      docker inspect -f '  container: {{.State.Status}}, network={{.HostConfig.NetworkMode}}' "$CADDY_C" 2>&1
      docker inspect -f '{{range .Mounts}}  mount: {{.Source}} -> {{.Destination}} (rw={{.RW}}){{"\n"}}{{end}}' "$CADDY_C" 2>&1
      sed -E 's/[A-Za-z0-9+\/=_-]{24,}/<redacted>/g' "$CADDYFILE" | sed 's/^/  | /'
      ;;
    caddy-backup)
      say "Caddy: back up the live Caddyfile (never overwrites an existing backup)"
      if [ -e "$CADDY_BAK" ]; then echo "backup already exists: $CADDY_BAK"; else cp -p "$CADDYFILE" "$CADDY_BAK" && echo "created $CADDY_BAK"; fi
      sha256sum "$CADDYFILE" "$CADDY_BAK" | sed 's/^/  /'
      ;;
    caddy-switch|caddy-rollback)
      # caddy_apply <source-file>: validate it, write it IN PLACE (the file is a Docker bind
      # mount: replacing the inode, e.g. with `sed -i` or mv, would leave the container reading
      # the old content), confirm the container sees it, then reload (graceful, no downtime).
      caddy_apply() {
        docker exec -i "$CADDY_C" caddy validate --config /dev/stdin --adapter caddyfile < "$1" 2>&1 | tail -3 \
          && [ "${PIPESTATUS[0]}" = 0 ] || { echo "validation FAILED: live file untouched"; return 1; }
        cat "$1" > "$CADDYFILE" || return 1
        cmp -s "$1" "$CADDYFILE" && docker exec "$CADDY_C" sh -c 'cat /etc/caddy/Caddyfile' | cmp -s - "$1" \
          || { echo "container does not see the new file"; return 1; }
        docker exec "$CADDY_C" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -3
      }
      site_ok() {  # public HTTPS check from this host: status, real content, valid certificate
        local code body
        code=$(curl -s -o /tmp/.cf_body -w '%{http_code}' -m 20 https://onchainflow.app/) || return 1
        echo "  https://onchainflow.app/ -> $code (certificate verified by curl)"
        [ "$code" = 200 ] && grep -q "Elysium" /tmp/.cf_body
      }
      if [ "$step" = caddy-rollback ]; then
        say "Caddy ROLLBACK: restore $CADDY_BAK"
        [ -f "$CADDY_BAK" ] || { echo "no backup file"; exit 1; }
        caddy_apply "$CADDY_BAK" && echo "restored; upstream lines now:" && grep -nE 'reverse_proxy' "$CADDYFILE" | sed 's/^/  /'
        echo "  (the old website container is stopped: start it with 'docker start gmgn-revival-radar-web-1' for :8000)"
        continue
      fi
      say "Caddy cutover: onchainflow.app upstream 127.0.0.1:8000 -> 127.0.0.1:8100"
      [ -f "$CADDY_BAK" ] || { echo "run caddy-backup first"; exit 1; }
      for path in /healthz / /tokens /smart-money /early /scanner; do
        code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "http://127.0.0.1:8100$path" || echo 000)
        echo "  pre-check :8100$path -> $code"; [ "$code" = 200 ] || { echo "dashboard not ready: aborting before any change"; exit 1; }
      done
      n=$(grep -cE 'reverse_proxy.*:8000' "$CADDYFILE" || true)
      [ "$n" = 1 ] || { echo "expected exactly one ':8000' reverse_proxy line, found $n: aborting"; exit 1; }
      tmp=$(mktemp); sed -E '/reverse_proxy/ s/:8000/:8100/' "$CADDYFILE" > "$tmp"
      diff <(sed -E 's/[A-Za-z0-9+\/=_-]{24,}/<redacted>/g' "$CADDYFILE") <(sed -E 's/[A-Za-z0-9+\/=_-]{24,}/<redacted>/g' "$tmp") | sed 's/^/  diff /'
      if caddy_apply "$tmp"; then
        sleep 3
        if site_ok; then echo "CUTOVER OK"; else
          echo "post-check FAILED: rolling back automatically"; caddy_apply "$CADDY_BAK"; site_ok || true; rm -f "$tmp"; exit 1
        fi
      else
        echo "apply failed: restoring the backup"; caddy_apply "$CADDY_BAK" || true; rm -f "$tmp"; exit 1
      fi
      rm -f "$tmp"
      healthy_bots
      ;;
    *)
      echo "unknown step: $step"; exit 2 ;;
  esac
done
say "after"; disk
