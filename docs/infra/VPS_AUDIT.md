# VPS audit, dependency map and retirement plan

Audit date: 2026-11-03 (read-only; `scripts/vps-audit.sh`, scopes `full` and `deep`).
The server address and secrets are deliberately omitted (public repository).

## Host

| | |
|---|---|
| OS | Ubuntu 24.04.2 LTS, kernel 6.8, KVM |
| CPU / RAM / swap | 1 vCPU / 1.9 GiB / 3.8 GiB swap file |
| Disk | one 50 GB XFS root: **47 GB used, 3.2 GB free (94%)**; inodes 4% |
| Docker | 29.8, json-file logging rotated per service (10 MB × 3) |
| Reverse proxy | Caddy 2 (container, host network) on :80/:443 → `127.0.0.1:8000` |
| TLS | Let's Encrypt for `onchainflow.app`, valid until 2027-01-10, auto-renewed by Caddy |
| DNS | `onchainflow.app` A record points at this VPS; `www` has no record |

## Services and data ownership

| Component | Runtime | Database / storage | Size | Clients (verified) | Keep? |
|---|---|---|---|---|---|
| **Elysium tracker** | compose project `elysium` (`/root/elysium-wallet-tracker`): `tracker`, `ui`, `postgres`, `backup`, one-shot `migrate` | PostgreSQL 16 DB `tracker`, volume `elysium_pgdata` | 46 MB (101 MB on disk) | only `elysium-wallet-tracker` and `elysium-ui` (from `pg_stat_activity`) | **Yes** |
| **Revival Radar bot** | compose project `gmgn-revival-radar` (`/opt/gmgn-revival-radar`): `radar` | SQLite `data/revival_radar.db` (WAL), bind mount `./data` | 3.4 GB + 56 MB WAL | `radar` (writer) **and** `web` (reader + owner commands) | **Yes** |
| **OnchainFlow website** | same compose project: `web` (127.0.0.1:8000) + `caddy`; host timers `onchainflow-health`, `onchainflow-backup` | **no database of its own**: reads the bot's SQLite file | – | public via Caddy | **Retire `web`** (Caddy is reused) |

The two stacks are isolated: different compose projects, networks
(`elysium_default` and `gmgn-revival-radar_default`) and databases. The website **shares
the revival bot's database file**. Nothing may be deleted from inside that file without
changing the bot.

### What is in the shared SQLite file

Measured on the newest static daily copy (2026-11-01, 3.42 GB):

| Table | Size | Writer | Readers | Website-only? |
|---|---|---|---|---|
| `research_briefs` (+ publications) | 1.63 GB (48%) | bot, inside its evaluation transaction | website API; bot (dedupe, provenance) | No: entangled with the bot's write path |
| `state` | 1.12 GB (33%) | bot | bot, website | No |
| `token_snapshots` | 0.57 GB (17%) | bot | bot, website | No |
| `evaluations` | 0.09 GB | bot | bot, website | No |
| `web_sessions`, `web_previews`, `web_commands` | ≈0 (4 rows) | website | bot (owner commands) | Yes, but negligible |

**Conclusion:** the website has no separable data worth deleting. The space problem is
copies of this file.

## Where the 47 GB is

| Path | Size | Content | Proposed action |
|---|---|---|---|
| `/root/onchainflow-backups/daily` | 20.4 GB | 7 nightly full copies of the revival DB (2.5→3.3 GB) | keep newest 1 locally until off-site backups are routine; delete 6 (**17.1 GB**) |
| `/root/onchainflow-backups/release-20261012T130446Z` | 1.5 GB | Oct 12 release bundle + restore check (+ old `.env` copies) | delete |
| `/root/onchainflow-releases` | 6.6 GB | Oct 13–21 release rehearsal DBs, logs (+ old `.env` copies) | delete |
| `/root/onchainflow-stage` | 0.75 GB | Oct 12 staging production copy | delete |
| `/opt/gmgn-revival-radar-backups` | 1.7 GB | Oct 9–10 pre-change copies | delete |
| `/opt/gmgn-revival-radar/data/*.before-upgrade-*.db` | 0.42 GB | Oct 7 pre-upgrade copies (live DB untouched) | delete |
| Docker build cache | 3.3 GB (2.1 GB reclaimable) | build layers | `docker builder prune` (build cache only) |
| Old revival image tags | ~0.3 GB unique | `before-*`, `rollback-*`, `audit-*`, `website-release`, `v2-*` | remove by exact tag |
| journald | 0.74 GB | system logs | cap at 200 MB (`SystemMaxUse`) |
| Live revival DB + WAL | 3.4 GB | **production** | **keep, untouched** |
| Elysium Postgres | 0.1 GB | **production** | **keep, untouched** |

**Estimated reclaim: about 31–34 GB**, from 3.2 GB free to about 35 GB free (≈30% used).

## Urgent risk found

`onchainflow-backup.timer` (19:00 UTC daily) copies the full 3.4 GB DB **before**
pruning the oldest copy. With 3.2 GB free it **ran the disk to 100% on 2026-11-03 and
failed**. The partial copy was removed, and both bots logged no errors. It will repeat
every night and grows by about 120 MB/day. Mitigation (needs approval): pause the timer
until the copies are cleaned and off-site backups exist, then re-enable it with
prune-first and a free-space check.

## Staged retirement plan (deletions only after Approval Gate B)

1. Off-site backup (`backup` action): both DBs + configuration, restore-tested and encrypted.
2. Pause `onchainflow-backup.timer` (reversible).
3. Delete the copies listed above, by exact path, and measure the disk.
4. Stop `web` (`docker compose stop web`; the image and container are kept for rollback);
   disable `onchainflow-health.timer` (it checks the website). The bot keeps running;
   its `web:worker` control loop is harmless without the website.
5. New Elysium dashboard on `127.0.0.1:8100`, validated locally, then the Caddy upstream
   is switched with `caddy reload` (no DNS change, same certificate).
6. After a week of stable operation: remove the `web` container and old image tags.

## Not changed

Nothing has been modified on the VPS so far. The audit only reads, with one exception:
the first audit opened the SQLite copies read-only (not `immutable`), which created
**empty** `-shm`/`-wal` side files next to them. Later reads use `immutable=1`. These
files go away with the copies.
