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
    *)
      echo "unknown step: $step"; exit 2 ;;
  esac
done
say "after"; disk
