# Backup and recovery (VPS: Elysium tracker + Revival Radar bot)

No credentials appear in this document. The VPS has a single 50 GB disk, so off-site
backups are **streamed** to a GitHub Actions runner and never staged on the VPS.

## What is backed up

| Data | Owner | Engine / location | Method | Consistency |
|---|---|---|---|---|
| Elysium tracker DB (`tracker`) | elysium stack | PostgreSQL 16, volume `elysium_pgdata` | `pg_dump -Fc` over SSH | one MVCC snapshot |
| Revival Radar DB | revival bot (shared with the old website) | SQLite `/opt/gmgn-revival-radar/data/revival_radar.db` (WAL) | logical dump (`iterdump`) inside **one read transaction**, gzip on the VPS, streamed over SSH | one WAL snapshot; writers are not blocked |
| Configuration | both | compose files, `config/`, systemd units, `.env` files | `tar` over SSH | point in time |

Secrets (`.env`) are only stored inside the encrypted archive.

## Off-site backup (`backup` action)

Run it with **Actions → deploy → Run workflow → `backup`**, or push `action=backup`
to `deploy/OPS_REQUEST` on the `ops` branch. It requires the repository secret
`BACKUP_PASSPHRASE` (at least 20 characters). **Keep a copy of the passphrase in your
password manager: without it the backups cannot be decrypted.**

The runner then:

1. streams each database to its own disk (nothing is written on the VPS);
2. **restore-tests** each one:
   * PostgreSQL: `pg_restore` into a scratch `postgres:16`. Same table set, Alembic
     revision present, key tables non-empty.
   * SQLite: rebuilt with `sqlite3`. `PRAGMA integrity_check = ok`, `user_version`
     equal, and **every table's row count identical** to the counts taken inside the
     dump's own read transaction;
3. encrypts each file (`openssl enc -aes-256-cbc -pbkdf2 -iter 300000`);
4. uploads `*.enc`, `SHA256SUMS` and `manifest.json` (sizes, counts, verification
   results; no secrets) as the artifact `offsite-backup-<UTC stamp>`, kept for **90 days**.

The run fails, and no artifact is stored, if any restore check fails.

> 90-day artifacts are an interim off-site copy. For long-term retention, add a durable
> destination such as Backblaze B2 or Cloudflare R2 via `rclone`, with credentials as
> repository secrets. See "Next steps".

## Restore

Download the artifact from the workflow run page (or `gh run download <run-id>`), then:

```bash
sha256sum -c SHA256SUMS
dec() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 300000 -pass env:BACKUP_PASSPHRASE -in "$1" -out "${1%.enc}"; }
export BACKUP_PASSPHRASE='…'          # from your password manager
dec elysium-tracker.pgdump.enc; dec revival-radar.sql.gz.enc; dec config.tar.gz.enc
```

### Elysium tracker (PostgreSQL)

```bash
cd /root/elysium-wallet-tracker
docker compose stop tracker ui
docker compose exec -T postgres pg_restore -U tracker -d tracker --clean --if-exists --no-owner \
  < elysium-tracker.pgdump
docker compose run --rm migrate            # no-op unless the dump is older than the code
docker compose start tracker ui
docker compose run --rm tracker --status   # cursor, trades, outbox
```

The tracker resumes from the checkpoint inside the dump and re-scans forward. Replay is
idempotent.

### Revival Radar (SQLite)

Needs free space of about twice the database size (about 3.5 GB today).

```bash
cd /opt/gmgn-revival-radar
docker compose stop radar web                          # nothing may write during the swap
mv data/revival_radar.db data/revival_radar.db.pre-restore   # keep until verified
gzip -dc revival-radar.sql.gz | sqlite3 data/revival_radar.db
sqlite3 data/revival_radar.db 'PRAGMA journal_mode=WAL; PRAGMA integrity_check;'
chown 10001:10001 data/revival_radar.db
docker compose start radar                              # web stays retired
```

### Configuration

```bash
tar -xzf config.tar.gz -C /tmp/restore   # inspect, then copy the needed files into place
```

## Local (on-VPS) backups that exist today

* `elysium-backup` container: daily `pg_dump -Fc` into `/root/elysium-wallet-tracker/backups`
  (verified with `pg_restore --list`, 14 days kept, about 44 KB each).
* `onchainflow-backup.timer` (revival DB): full SQLite copy to
  `/root/onchainflow-backups/daily`, 7 kept. **This job writes the new 3.4 GB copy
  before deleting the oldest one. It failed with "disk is full" on 2026-11-03 and will
  fill the disk on every run until it is fixed.** See the infrastructure audit.

## Rollback of the retirement work

* Stopped containers or services: `docker compose start <service>` or
  `systemctl start <unit>`.
* Caddy route change: restore the previous Caddyfile, then
  `docker exec <caddy> caddy reload --config /etc/caddy/Caddyfile`.
* Deleted files: restore from the off-site artifact as above. Only files listed and
  approved at Approval Gate B are deleted.

## Next steps

* Durable off-site target (B2/R2/S3 via `rclone`), scheduled weekly.
* Replace the revival copy-before-prune job with "check free space, prune first" or with
  the off-site stream.
