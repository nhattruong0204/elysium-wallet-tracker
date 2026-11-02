# Operations runbook

All commands run from the repository directory on the VPS.

## Services

| Service | Role | Restart |
|---|---|---|
| `postgres` | PostgreSQL 16, data in volume `pgdata`, not exposed to the host | always |
| `migrate` | Applies Alembic migrations, then exits | once per `up` |
| `tracker` | Leaderboard collector + indexer + notifier (one process) | always |
| `backup` | Daily `pg_dump -Fc` into `./backups`, verified, pruned after 14 days | always |

## Deploying without SSH (GitHub Actions)

The `deploy` workflow connects to the VPS from GitHub's runner, so you can deploy, check
status and read logs from a phone. Credentials live only in GitHub secrets.

1. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   `VPS_HOST`, `VPS_USER`, `VPS_PASSWORD` (or better `VPS_SSH_KEY`), `TG_BOT_TOKEN`,
   `TG_CHAT_ID`; optional `VPS_PORT`, `VPS_HOST_KEY`.
2. **Actions → deploy → Run workflow**, choose an action (or push a commit to the `ops`
   branch whose `deploy/OPS_REQUEST` contains `action=<name>`):

| Action | Does |
|---|---|
| `deploy` | Upload code, write `.env` (PostgreSQL password generated once on the VPS), install `deploy/config.yaml` + `deploy/watchlist.txt`, build, migrate, send a Telegram test message, start, wait for healthy |
| `status` / `logs` | `--status`, container states, recent logs |
| `discover` | Find Agora's leaderboard API; paste the result into `deploy/config.yaml` via PR, then `deploy` |
| `dry-run` | Catch up once and print alerts instead of sending |
| `test-telegram` | Send one test message |
| `restart` / `stop` | As named |

Bot settings are config-as-code: change `deploy/config.yaml` or `deploy/watchlist.txt` in a
PR and run `deploy`. The first run prints the server's SSH host key fingerprint; save that
line as `VPS_HOST_KEY` to pin it. Set `install_docker` only if Docker is missing and the SSH
user is root.

## First deployment (manual, over SSH)

```bash
cp .env.example .env            # set POSTGRES_PASSWORD (openssl rand -hex 24), TG_*
mkdir -p config && cp config.example.yaml config/config.yaml && cp watchlist.example.txt config/watchlist.txt
docker compose build
docker compose up -d postgres
docker compose run --rm migrate
docker compose run --rm tracker --once --dry-run   # catch up once, print alerts
docker compose up -d
docker compose logs -f tracker
```

## Upgrading from the SQLite version (v0.1 / v0.2)

The old database is in the `tracker-data` volume at `/data/tracker.db`. The import is a
single PostgreSQL transaction, validated row by row with exact integer comparison.

```bash
git pull
docker compose build
docker compose stop tracker                       # nothing may write the SQLite file now
docker compose up -d postgres && docker compose run --rm migrate
# 1. rehearse: validates everything, then rolls back
docker compose run --rm tracker --import-sqlite /data/tracker.db --dry-run
# 2. import for real, keep the report
docker compose run --rm tracker --import-sqlite /data/tracker.db --report /data/import-report.json
docker compose up -d
```

What it guarantees:
* `/data/tracker.db` is opened read-only; a backup copy `/data/tracker.db.bak-<UTC>` is
  written first.
* Delivered alerts stay delivered (never re-sent), pending stay pending, dead stay dead.
* The scan cursor is carried over, so the indexer resumes where SQLite stopped.
* Old trades are labelled `legacy` (or `estimated` if v0.1 flagged them approximate). They
  are never upgraded to `high`.
* Duplicate legs caused by the v0.1 replay bug are listed in the report; the first is kept.
* Any mismatch → exit code 1 and nothing is committed.
* Re-running is safe (rows already present are counted, not duplicated).

### Rolling back the PostgreSQL migration

The SQLite file is never modified, so rolling back means running the previous release:

```bash
docker compose down                       # keeps volumes
git checkout <previous release tag>       # e.g. the last SQLite commit
docker compose up -d --build              # uses /data/tracker.db as before
```

Alerts delivered by the PostgreSQL version after the import are not in SQLite; the old
version may re-send a few of those once. To discard the PostgreSQL data entirely as well:
`docker volume rm <project>_pgdata` (take a backup first, see below).

## Backups

The `backup` service writes `./backups/tracker-<UTC>.dump` daily. Each dump is verified with
`pg_restore --list` before it replaces the partial file. Check it is working:

```bash
docker compose logs backup | tail        # "backup ok: ..." lines
ls -lh backups/
```

On-demand backup: `docker compose exec backup sh -c 'pg_dump -Fc -f /backups/manual-$(date -u +%Y%m%dT%H%M%SZ).dump'`

Copy `./backups` off the VPS (e.g. `rclone` or `rsync` from cron); a backup on the same disk
does not survive losing the disk.

### Restore

```bash
./scripts/restore.sh backups/tracker-20261102T000000Z.dump
```

It stops the tracker, runs `pg_restore --clean --if-exists`, re-applies migrations (no-op
unless the dump is older than the code), and starts the tracker. The tracker resumes from
the checkpoint inside the dump and re-scans forward; replay is idempotent. The procedure is
exercised in CI by `test_backup_and_restore_round_trip`.

## Day to day

```bash
docker compose run --rm tracker --status          # cursor, watchlist, trades by confidence, outbox
docker compose ps                                 # health column
git pull && docker compose up -d --build          # update (migrate runs automatically)
docker compose run --rm tracker --reset-cursor 190000   # replay from a block (safe)
```

## Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `tracker` keeps restarting | `docker compose logs tracker` | Read the last error. Common: RPC unreachable at startup, all RPC endpoints on the wrong chain ("QUARANTINED"), database not migrated. |
| "another indexer already holds the lock" | Is a second tracker running (`docker ps`)? | Stop the duplicate. Only one indexer per chain is allowed by design. |
| `unhealthy` in `docker compose ps` | `docker compose exec tracker python -m agora_tracker --health` | "schema at X, code expects Y": run `docker compose run --rm migrate`. "heartbeat age": the loop is stuck or erroring; see logs. |
| Cursor not moving | `--status` → `cursor` vs chain head; logs | "no wallets tracked; holding cursor": configure leaderboard or watchlist. Pre-launch the chain itself only moves per batch (~3 h). |
| Many `estimated` / `unknown` trades | `--status` → `trades_by_confidence` | The RPC may not serve historical state (`eth_getBalance` at block N-1). Try another provider. |
| Alerts not arriving | `--status` → `outbox` | `dead` > 0: inspect `SELECT dedup_key, last_error FROM notification_outbox WHERE status='dead'`. Check `TG_*` in `.env`. |
| Leaderboard "degraded" / "failed" | `SELECT captured_at, status, request_meta FROM leaderboard_snapshots ORDER BY id DESC LIMIT 5` | Endpoint changed or blocked: rerun `--discover` or re-import the cURL. Watchlist tracking continues meanwhile. |

Database shell: `docker compose exec postgres psql -U tracker -d tracker`.

## Delivery semantics (Telegram)

At-least-once. Telegram has no transaction shared with PostgreSQL: if Telegram accepts a
message but the response is lost, or the process dies before the commit, the alert is sent
again. Every attempt is recorded in `notification_deliveries`. Losses are not possible
short of deleting rows; duplicates are possible but rare.
