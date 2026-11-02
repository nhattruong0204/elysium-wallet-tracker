#!/bin/sh
# Runs inside the postgres image. Writes /backups/tracker-<UTC>.dump every
# BACKUP_INTERVAL_S seconds, verifies it is readable, keeps BACKUP_KEEP_DAYS days.
set -eu
: "${BACKUP_INTERVAL_S:=86400}" "${BACKUP_KEEP_DAYS:=14}"
while true; do
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  tmp="/backups/.tracker-$ts.dump.partial"
  out="/backups/tracker-$ts.dump"
  if pg_dump -Fc -f "$tmp" && pg_restore --list "$tmp" > /dev/null; then
    mv "$tmp" "$out"
    echo "$(date -u +%FT%TZ) backup ok: $out ($(du -h "$out" | cut -f1))"
  else
    rm -f "$tmp"
    echo "$(date -u +%FT%TZ) BACKUP FAILED" >&2
  fi
  find /backups -name 'tracker-*.dump' -mtime +"$BACKUP_KEEP_DAYS" -print -delete || true
  sleep "$BACKUP_INTERVAL_S"
done
