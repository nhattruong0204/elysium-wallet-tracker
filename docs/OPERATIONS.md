# Operations runbook

All commands run from the repository directory on the VPS.

## Services

| Service | Role | Restart |
|---|---|---|
| `postgres` | PostgreSQL 16, data in volume `pgdata`, not exposed to the host | always |
| `migrate` | Applies Alembic migrations, then exits | once per `up` |
| `tracker` | Leaderboard collector + indexer + notifier (one process) | always |
| `ui` | Telegram Intelligence Center (menu, reports, exports; the only `getUpdates` consumer) + market snapshot collector. See [TELEGRAM_UI.md](TELEGRAM_UI.md) | always |
| `backup` | Daily `pg_dump -Fc` into `./backups`, verified, pruned after 14 days | always |

## Deploying without SSH (GitHub Actions)

The `deploy` workflow connects to the VPS from GitHub's runner, so you can deploy, check
status and read logs from a phone. Credentials live only in GitHub secrets.

1. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   `VPS_HOST`, `VPS_USER`, `VPS_PASSWORD` (or better `VPS_SSH_KEY`), `TG_BOT_TOKEN`;
   optional `TG_CHAT_ID` (if unset, deploy uses the chat of the latest message sent to the
   bot, so send it `/start` first), `VPS_PORT`, `VPS_HOST_KEY`.
2. **Actions → deploy → Run workflow**, choose an action (or push a commit to the `ops`
   branch whose `deploy/OPS_REQUEST` contains `action=<name>`):

| Action | Does |
|---|---|
| `deploy` | Upload code, write `.env` (PostgreSQL password generated once on the VPS), install `deploy/config.yaml` + `deploy/watchlist.txt`, build, migrate, send a Telegram test message, start, wait for healthy |
| `status` / `logs` | `--status`, container states, recent logs |
| `discover` | Find Agora's leaderboard API; paste the result into `deploy/config.yaml` via PR, then `deploy` |
| `dry-run` | Catch up once and print alerts instead of sending |
| `test-telegram` | Send one test message |
| `test-card` | Re-send the latest tracked BUY/SELL as a full alert card with live market data |
| `ui-smoke` | Render every Intelligence Center view on live data, build a complete XLSX and send both to the owner chat (no polling) |
| `restart` / `stop` | As named (`restart` restarts tracker and ui) |
| `web` | Deploy **only** the dashboard: build, migrate, `up -d --no-deps web`, smoke + role checks. Needs `backup_run=<id of a successful off-site backup run, <= 36 h old>`. Never restarts tracker/ui/postgres |
| `web-smoke` / `loadtest` / `web-rollback` | Page and role checks on the loopback port; 5 rps x 5 min load test that aborts if the bots degrade; stop the `web` container |
| `maint` | Allow-listed steps (`steps=a,b`), now also `launch-report`, `install-revival-health`, `caddy-show`, `caddy-backup`, `caddy-switch`, `caddy-rollback` |

Bot settings are config-as-code: change `deploy/config.yaml` or `deploy/watchlist.txt` in a
PR and run `deploy`. The first run prints the server's SSH host key fingerprint; save that
line as `VPS_HOST_KEY` to pin it. Set `install_docker` only if Docker is missing and the SSH
user is root.

## Dashboard (onchainflow.app)

Design, boundary and verification record: [infra/ONCHAINFLOW_DASHBOARD.md](infra/ONCHAINFLOW_DASHBOARD.md).

* `web` is a compose service on `127.0.0.1:8100` (256 MB, read-only rootfs, no `.env` secrets).
  It logs in as `dashboard_ro` using `DASHBOARD_DB_PASSWORD`, generated once on the VPS into
  `.env` (mode 0600, never printed) and preserved by `deploy`.
* Public edge: the existing Caddy container (host network) proxies `onchainflow.app` to
  `127.0.0.1:8100`. Caddyfile backup: `/opt/gmgn-revival-radar/deploy/Caddyfile.pre-elysium`.
* **Schema changes and the bots.** The tracker and ui refuse to start when the database
  schema differs from the one their image expects (`schema at X, code expects Y`). Migration
  0004 advanced the schema, so until `tracker` and `ui` are recreated on the new image their
  Docker health reads *unhealthy* (they keep working) and **a restart of either would fail to
  start**. The `web` action deliberately does not recreate them. Bring them level with
  `action=deploy` (recreates tracker and ui, resumes from the stored cursor, no replay) at a
  moment you accept a few seconds of downtime. Any future migration needs the same step.
* **Migration 0005** (USD market cap, activity monitor) adds `hype_usd_rates`, a never-failing
  trigger on `leaderboard_snapshots`, `public_api.hype_usd_rates`, and skip-scan rewrites of
  `public_api.public_wallets` / `public_tokens`. It is additive; the tracker keeps writing
  normally. The `web` action takes a backup (`backup_run`), runs the migrate image, then
  `up -d --no-deps web`. Rollback of the schema: `alembic downgrade 0004`; of the site:
  `action=web-rollback`. Activity polling cadence: `web.activity_poll_s` (20 s) in
  `deploy/config.yaml`.
* **Recreating tracker/ui (needs explicit owner approval).** Safe procedure: confirm a fresh
  backup, `action=deploy`, then verify `heartbeat` age < 60 s, cursor advancing, `docker ps`
  healthy, and a Telegram test. Impact: a few seconds without notifications; indexing resumes from
  the stored cursor with no replay.
* External check of the public site: push `requested=<time>` to `deploy/SITE_CHECK` on `ops`.
* Revival scan-health monitor: `revival-health.timer` (every 5 min) runs
  `/opt/elysium-ops/revival-health.py`, read-only on the Revival SQLite file. Alerts via
  Telegram (6 h cooldown per condition, recovery notice) for: no finished scan within 20 min,
  all sources down for 3 scans, DB unreadable, disk low (< 15% or < 8 GB), container down.
  Zero candidates is healthy. `systemctl list-timers revival-health.timer`,
  `journalctl -u revival-health.service -n 20`. It replaces the retired `onchainflow-health`,
  which stays disabled.

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
| "RPC … N blocks behind; skipping it" | `docker compose logs tracker \| grep RPC` | Expected now and then; the node is used again once it catches up. If a node is always behind, remove it from `chain.rpc_urls`. |
| "pinned endpoint … failed" | Same | One node failed mid-range; the range is retried on the next node. Only a concern if it repeats for every node. |
| Many `estimated` / `unknown` trades | `--status` → `trades_by_confidence` | The RPC may not serve historical state (`eth_getBalance` at block N-1). Try another provider. |
| Menu doesn't respond | `docker compose logs ui` | "409": something else polls the bot (another deployment, a webhook). Only one `ui` may run per bot token. "not authorized": add your user id to `TG_ADMIN_IDS`. |
| Gainers/Losers say "no snapshots" | `docker compose logs ui \| grep collector` | History builds from the collector's first run; until then only Agora's rolling windows work. |
| Alerts not arriving | `--status` → `outbox` | `dead` > 0: inspect `SELECT dedup_key, last_error FROM notification_outbox WHERE status='dead'`. Check `TG_*` in `.env`. |
| Leaderboard "degraded" / "failed" | `SELECT captured_at, status, request_meta FROM leaderboard_snapshots ORDER BY id DESC LIMIT 5` | Endpoint changed or blocked: rerun `--discover` or re-import the cURL. Watchlist tracking continues meanwhile. |

Database shell: `docker compose exec postgres psql -U tracker -d tracker`.

## Wallet universe and scan cost

**Policy.** The scanner follows `tracked = every wallet ever in an accepted top list ∪ manual
watchlist ∪ Telegram watchlist` (`tracking.cumulative: true`). "Accepted" means a snapshot with
status `ok`/`imported` **and** `complete` (the collector marks a response with fewer than
`top_n` rows incomplete; a response that shrank by half is `degraded`; errors are `failed`).
Only accepted lists are evidence: incidental senders, recipients and pools never become
tracked. Leaving the Top 100 flips `wallet_enrollment.in_top` and records an EXITED event; it
does not change tracking eligibility. Contracts are the one explicit exclusion
(`status='excluded'`, reason `contract`) and are still counted in the registry.

Three different facts, three records: **membership** (`in_top`, `leaderboard_membership_events`),
**eligibility** (`wallet_enrollment.status`), **coverage** (`wallet_scan_coverage`: the block
intervals in which a wallet was really scanned). A wallet that is enrolled today was not scanned
yesterday, and that gap is stored. Trades in a gap are **not recorded**; history older than the
first interval is unknown. Nothing in the registry says otherwise. (Re-fetching old blocks is not
done automatically: positions and PnL are computed in block order, so inserting old trades after
newer ones would corrupt cost basis. Tracked in #5.)

```
python -m agora_tracker --config /config/config.yaml --tracking-report      # counts only
python -m agora_tracker --config /config/config.yaml --tracking-backfill    # rebuild registry first (idempotent)
```

The report prints: snapshots accepted / not accepted, wallets ever in the list, current, former,
manual, manual-only, scanned now, enrolled-but-not-scanned, excluded by policy (with reasons),
history incomplete (never scanned, gaps, or first scanned after their first appearance),
re-entered. The tracker also runs the rebuild at start-up when the registry is behind.

**Rollback.** `tracking.cumulative: false` in `config.yaml` and restart the tracker: the scan set
is again `latest snapshot ∪ watchlists`. Evidence keeps being recorded; nothing is deleted.
Migration 0008 is additive; `alembic downgrade 0007` drops the three new tables and two views.

**Scan cost.** Per range the filtered strategy issues `2 × ceil(wallets / wallet_chunk)`
`eth_getLogs` calls (Transfer from-side and to-side per chunk) no matter how short the range is,
and the tracker polls every `poll_interval_s`. The full strategy issues **one** unfiltered
Transfer query and filters locally, so its cost does not depend on the wallet count (the response
grows with the chain's Transfer volume instead). `scan_mode: auto` stays filtered until it would
need more than `full_scan_above_calls` calls and then switches; if a node refuses unfiltered
queries the tracker falls back to filters for 10 minutes. Node limits are learned at run time:
a topic OR-list the node rejects ("Too many topic alternatives") halves `wallet_chunk`; a block
range it rejects halves the span (floor `min_block_range`) and doubles back after 20 clean ranges.

`service_state.scan_stats` (public, aggregates only) holds the cost of the last range: HTTP
requests, getLogs calls, seconds, lag in blocks, scan mode, leaderboard-wallet count, and two
flags: `over_capacity` (wallets above `tracking.capacity_warn`) and `falling_behind` (lag above
`tracking.lag_warn_blocks`). Neither flag drops a wallet: they are the signal to add RPC capacity
or move to a dedicated node. `scripts/scan_bench.py` and the `scan-bench` / `scan-density` /
`tracking-baseline` maintenance steps measure the providers (bounded, read-only).

## RPC endpoints

`chain.rpc_urls` is an ordered list. The first healthy node is used; on failure the tracker
moves to the next one and stays there.

* **One node per block range.** The head, logs, transactions and receipts for a range all
  come from the same node. If that node fails partway through, the whole range is retried
  on the next node. Nodes can be a few blocks apart, so mixing them inside one range could
  skip trades.
* **Head-lag guard.** Every `head_check_s` the tracker compares all nodes' heads and skips
  any node more than `max_head_lag` blocks behind (or unreachable) until it catches up.
* **Batch limits** are learned per node; BOXA accepts at most 20 calls per batch.
* Every node's chain id is checked at startup; a node on another chain is never used.

## Delivery semantics (Telegram)

At-least-once. Telegram has no transaction shared with PostgreSQL: if Telegram accepts a
message but the response is lost, or the process dies before the commit, the alert is sent
again. Every attempt is recorded in `notification_deliveries`. Losses are not possible
short of deleting rows; duplicates are possible but rare.

## Telegram-only token mute (USDC, mainnet hotfix 2026-11-09)

`alerts.telegram_mute_tokens` (config) and `ALERTS_TELEGRAM_MUTE_TOKENS` (environment,
comma-separated, set for the **tracker service only** in `docker-compose.yml`) list exact token
contracts whose **Telegram alerts** are not sent: BUY, SELL, TRANSFER_IN/OUT, SWAP/MULTI legs
of that token, and its cluster / independent-buy cards. Everything else is unchanged: the token is
indexed (trades, raw evidence, positions, PnL, cursor), still part of cluster/smart-money
**signals** and paper trades, and still shown on the website. A trade of another token that used
it as the quote asset still alerts (filtering is on the reported token, never `counter_token`),
and look-alike tokens with the same symbol still alert (exact address match only).

It differs from `mute_tokens` (also hides the token on the Activity tab and keeps it out of
cluster signals) and from `ignore_tokens` (dropped before indexing). Neither is used for USDC.

**Muted on mainnet:** `0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65` (`0xb47c4C8F73F009CC0C0d3f92B388d13E293F0B65`)
— Elysium's bridged USDC (shown as USDC / USDC.e, 6 decimals): the Arbitrum-Orbit-gateway
representation of Circle's HyperEVM USDC `0xb88339CB7199b77E23DB6E890353E22632Ba630f`.
Elysium has no native Circle USDC/CCTP. Evidence (2026-11-09):

* Circle's USDC contract list gives HyperEVM USDC = `0xb88339…630f` and does not list Elysium.
* Hyperlane registry PR #1792 reads on chain (`l1Address()` / both gateways) that the Elysium
  token for that parent is `0xb47c4C8F…F0B65` (6 decimals).
* LI.FI data-types PR #287 (`USDC.e`, chain 99901), ShapeShift web PR #12832 ("HyperEVM USDC
  bridged through the Orbit gateway"), MetaMask contract-metadata PR #2056 (merged icon for
  `eip155:99901/erc20:0xb47c4C8F…F0B65`).
* Kinetiq's docs name WHYPE and USDC as the canonical tokens and warn about look-alikes; their
  contract page had not yet published mainnet addresses when checked.

Operator re-check on the VPS before deploying (read-only):

```bash
R=https://rpc.elysium.kinetiq.xyz; T=0xb47c4C8F73F009CC0C0d3f92B388d13E293F0B65
call() { curl -s -H 'content-type: application/json' -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_call\",\"params\":[{\"to\":\"$T\",\"data\":\"$1\"},\"latest\"]}" $R; echo; }
call 0x95d89b41   # symbol()    -> "USDC" or "USDC.e"
call 0x313ce567   # decimals()  -> 6
call 0xc2eeeebd   # l1Address() -> ...b88339cb7199b77e23db6e890353e22632ba630f (Circle HyperEVM USDC)
```

**Already-queued alerts.** On every drain the notifier closes still-pending (queued or retrying)
outbox rows whose payload token is muted: `status = 'dead'`, `last_error = 'suppressed: not sent
(alerts.telegram_mute_tokens)'`, plus a `notification_deliveries` row (`ok = false`, same text).
Nothing is deleted, nothing is marked `sent`, other rows are untouched, and rows a concurrent
notifier holds are skipped and caught next time. Private health/`--status` counts them as
`suppressed`; the public dashboard's message-free view still counts them under "failed" (changing
that view needs a migration, deliberately not part of a hotfix).

Count them (read-only) on the VPS:

```sql
SELECT status, last_error LIKE 'suppressed:%' AS suppressed, count(*)
FROM notification_outbox
WHERE lower(payload->>'token') = '0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65'
GROUP BY 1, 2 ORDER BY 1, 2;
```

**Deploy only the tracker** (GitHub Actions > deploy > `tracker`, or `deploy/OPS_REQUEST`
`action=tracker`): uploads the code, rebuilds the image, recreates **only** the tracker
container (no config install, no migration, no postgres/ui/web restart; their containers keep
their current image), tags the previous image `elysium-wallet-tracker:pre-tracker-hotfix`, and
prints the muted queue counts and tracker/ui health before and after.

**Rollback:** action `tracker-rollback` (re-tags the previous image and recreates the tracker).
Suppressed rows stay closed (they are not re-sent). To stop muting without a rollback, remove the
address from `ALERTS_TELEGRAM_MUTE_TOKENS` and run `tracker` again.

## Maestro quick-trade handoff (2026-11-11)

The first row of eligible token alerts is **⚡ Quick Buy / Sell · Maestro**, pointing to the
official [@Maestro](https://t.me/Maestro) Telegram bot. Contract addresses are public and
normalized to lowercase; no tracked-wallet identity, owner details, credentials, or referral
parameters are passed.

- The tracker **does not** place a trade, sign, broadcast, import keys or read Maestro's balance.
  Maestro independently owns its wallet setup, HYPE funding, buy presets and sell controls.
- Normal link: `https://t.me/Maestro?start=<contract-address>`. The owner confirmed
  on a physical iPhone that the official @Maestro token link opens the expected Elysium
  Montra token (2026-11-11). This is a manual smoke test of token handoff, not a full
  security or trading approval. Users should verify the selected chain and contract.
- IMPORTANT: Maestro may automatically buy a pasted/deep-linked token if auto-buy is enabled
  in that wallet's settings. Validate the link first with auto-buy **disabled** and a
  non-funded/small test account. No user funds or transaction can be accessed by our tracker.
- If the start payload opens the wrong token, wrong chain, or only the generic bot menu:
  set `MAESTRO_DEEPLINK=off` **on the tracker service** and recreate only tracker. This
  opens the official Maestro chat, where the user manually pastes the CA shown in the alert.
- `MAESTRO_BUTTON=off` removes the external-trade button altogether after recreating tracker.
  Existing already-delivered Telegram messages keep their old BasedBot buttons; no sent
  message is retroactively edited.
- The **second row** on the same token alerts is **📱 Open in Fomo**. It uses the public Fomo
  share-link shape `https://fomo.family/coin?address=<lowercase_CA>&chainId=99901`
  (no referral or other parameters). Fomo's official docs confirm Elysium support and
  describe opening shared coin links in the iOS app. The precise Elysium token-address link
  on iPhone was **confirmed by the owner** using the Montra Elysium link (2026-11-11);
  this is confirmation of the handoff only, not execution approval.
  If Fomo opens the wrong asset, a generic page, or fails to hand off to iOS, set
  `FOMO_BUTTON=off` in the tracker service and recreate **only** tracker.
  The token contract is always visible in the original Telegram alert and can be copied
  into Fomo Search manually as a fallback.
- The web Activity tab includes a **Fomo** column immediately after Launchpad. It links to the
  same Elysium coin URL in both server-rendered rows and live/incremental Activity rows.
  The link is constructed only from an exact Elysium contract address, never a token symbol.
  No changes to market analytics, pagination, alert suppression, DB schema or trading flags.
- Trade alert cards now show **💰 MC** immediately below the BUY/SELL headline, reusing the
  existing Agora market cap and HYPE/USD conversion. If no market cap exists, show unavailable;
  do not invent $0. The original MC in the lower link bar is omitted on BUY/SELL alerts to
  avoid duplication; other alert types retain their existing layout.
- Fomo uses its own account/wallet/balance; it does **not** share Maestro's funded wallet.
  The scanner never has access to either wallet and does not execute trades.
- Alerts are rendered at send-time. USDC Telegram suppression and all other alert,
  indexing, cursor and outbox behavior are unchanged. No Stage C execution flags change.
- Manual iPhone acceptance: tap a fresh tracker alert, confirm official @Maestro opens,
  Elysium chain is selected, exact contract is resolved, expected funded Maestro wallet
  appears, and BUY presets / SELL controls are available. Do not execute a funded trade as
  part of the handoff acceptance.

## Automated-wallet filter (2026-11-10)

Some leaderboard wallets are machines (launch snipers, volume farms, arbitrage loops). They are
indexed like any wallet, but their own Telegram trade / transfer alerts are not sent, and they
never count as an independent buyer in the strong signal.

- **Rule** (`IntelConfig.bot_*`, defaults in code): in the last 7 days the wallet sent at least
  100 transactions and at least 15% of the gaps between consecutive transactions were under
  2 seconds. Since 2026-11-11 it must also be transfer-only: at most 5% of its legs decode as a
  real BUY/SELL (`bot_max_trade_share`), so a fast wallet that really trades keeps alerting. Re-judged every 15 minutes from our own `trades` table, so a wallet that slows down
  alerts again once the fast history ages out. The verdict is logged whenever the set changes
  (`automated-wallet filter: N wallet(s) not alerted: ...`).
- **Unchanged:** indexing, positions, PnL, the website, cluster signals in the database, the
  USDC mutes. Cluster alerts for a token still fire; a flagged wallet just counts as "one
  operator" together with bundle wallets.
- **Dry run on production, 2026-11-10:** 5 of 381 wallets flagged (fast share 26-73%); the next
  wallet with enough history sits at 13%. Those 5 produced 4,259 of 25,089 trade alerts in 7 days.
- **Off switch:** `ALERTS_BOT_FILTER=off` in the tracker's environment, then recreate the tracker.
- **Limits:** timing only. A slow bot is not caught; a person firing many transactions through a
  trading bot in bursts could be. Two of the five flagged wallets do make real buys and sells.

Reference case: `0xfffff389…f7d9` (744 transactions, 358 tokens, 26% of gaps under 2 s) trades
through the private contract `0xf045fae5…296e` together with two sibling `0xffff…` wallets that
buy on its behalf; none of it decodes as BUY/SELL because the buys are paid by another address.

## Blocked wallets (operator block list, 2026-11-11)

`TRACKING_BLOCKED_WALLETS` on the tracker service (`docker-compose.yml`; also
`tracking.blocked_wallets`) removes exact addresses from tracking altogether: they leave the
scan set whatever list they are on (current top, former top, manual, Telegram), get no trade,
transfer or leaderboard alerts, and are marked `excluded` / `blocked` in `wallet_enrollment` so
reports show why. Nothing stored is deleted; from then on no new trades, positions or PnL are
recorded for them, and the website shows their history only.

Blocked on 2026-11-11 (owner decision, the five wallets the automated-wallet dry run flagged):
`0xf70da978…dbef`, `0xf8fe0956…24e6`, `0x00c18c85…592e`, `0x75d2ae08…4dfb08`, `0xfffff389…f7d9`.

To reinstate one: delete its line, recreate the tracker (`action=tracker`). It is re-enrolled on
the next leaderboard refresh; the blocks it missed are a recorded coverage gap, not back-filled.

## Airdrop spam filter (2026-11-11)

Anyone can mint a token and send it to every leaderboard wallet; each receipt used to produce a
Telegram alert (197 alerts in 12 minutes for `0x6f0954d9…8af6` on 2026-11-11).

- **Unsolicited receipt:** a `TRANSFER_IN` leg whose wallet did not send the transaction.
- **Rule** (`IntelConfig.airdrop_*`, defaults in code): once 5 or more tracked wallets received
  unsolicited transfers of one token within 24 hours, and the tracked wallets' real BUY/SELL legs
  of that token are at most 10% of that number, further unsolicited receipts of the token are
  not sent to Telegram. The check runs per indexed block range and counts the range itself, so
  a burst is silenced from its first range; a slow drip leaks at most the first four alerts.
- **Still alerted:** buys, sells, and transfers a wallet sends itself (including selling or
  moving a spam token), and receipts of tokens that are really traded.
- **Unchanged:** every leg is still stored; positions, the website and the other mutes are as
  before. The tracker logs each newly flagged token once (`airdrop filter: ...`).
- **Dry run on production, 2026-11-11 (7 days):** 23 tokens, 3,180 receipts out of 26,844 trade
  alerts; every flagged token had one or two senders and at most two real trades.
- **Off switch:** `ALERTS_AIRDROP_FILTER=off` in the tracker's environment.
- **Limits:** Telegram only; the website's Activity feed still lists these rows (its read-only
  views do not expose who sent a transaction). A genuine airdrop of a real token is muted the
  same way until tracked wallets trade it.

### 2026-11-12: launch cohort (16 wallets)

Blocked: 16 Agora-leaderboard wallets (ranks 6-79) that entered 18 new tokens in ~8 hours, a
new token roughly every 29 minutes, 9-16 of them within seconds of each other on every launch,
all through the same private router `0x3e328152…28d4` (used by no other tracked wallet), buying
from the Uniswap v4 pool without paying from the wallet itself, then selling in small pieces
over ~15 minutes. No direct transfers between them were found. Addresses are in
`docker-compose.yml` (`TRACKING_BLOCKED_WALLETS`).

## Launch-cohort detector (2026-11-12)

`agora_tracker/cohorts.py` finds groups of tracked wallets that keep entering new tokens together:

1. **Launch burst** of a token: the wallets whose first trade of it is within 10 s of the first
   tracked trade of it, if at least 5 wallets did so.
2. **Linked wallets:** in the same burst on at least 5 tokens, which is at least 60% of the
   less active wallet's bursts and at least 30% of the busier one's (keeps a sniper that enters
   every launch out of whichever group it happens to share launches with).
3. **Cohort:** a connected group of at least 5 linked wallets (7-day lookback, refreshed every
   15 minutes). Thresholds: `IntelConfig.cohort_*`.

Effect (no database migration; every view computes it from the trades it already reads):

| Where | What happens |
|---|---|
| Telegram trade alerts | a cohort wallet's own alerts are not sent; alerts by others on a token 2+ cohort wallets traded carry a warning line |
| Cluster / strong-signal alerts | cohort wallets count as one operator; the card carries the warning |
| Token page / `GET /api/v1/tokens/{a}` | red banner listing the cohort wallets; `cohort` field |
| `GET /api/v1/cohorts` | every cohort, its wallets and launches, and the rule: the exclusion list for your own trading or bots |
| Consensus page | `⚠️ launch cohort ×N` badge on the card |
| Consensus Telegram channel | a card with 2+ cohort wallets among its traders is not posted |

Still indexed and on the website like any wallet. Off switch: `ALERTS_COHORT_FILTER=off` (tracker
and web read the same setting). A cohort is a flag, not a block: to stop tracking a cohort's
wallets entirely, add them to `TRACKING_BLOCKED_WALLETS`.

Production dry run, 2026-11-12: 64 tokens with a launch burst, 2 cohorts. (1) 14 of the 16
wallets blocked the same morning, 21 launches. (2) 7 active wallets (ranks 25-99), 34 launches,
5-7 of them in the same block on most launches, a contract no other tracked wallet calls, and
13 token transfers between them; unlike (1) they really trade, so this may be one operator or
clients of one copy-trading bot.

## Wallet labels on Wallet intelligence (2026-11-12)

`agora_tracker/web/labels.py` (`labels-v1`) gives every tracked wallet labels on
https://onchainflow.app/leaderboard: what kind of wallet it is, not only how it performed. All
labels are rule-based, computed by the website from the read-only `public_api` views over a fixed
**30-day lookback** (independent of the page's window), refreshed every 15 minutes with the
cohorts, and each one carries its evidence (hover on the page, `label_reasons` in the CSV). No
database migration, no new table, nothing written.

| Family | Values |
|---|---|
| `label_status` (one per wallet, first match wins) | `blocked` (operator block list) · `contract` · `cohort` (launch-cohort detector) · `bundler` (5+ same-block buys on 3+ tokens with the **same** other tracked wallet) · `farm` (5+ same-transaction transfers with 2+ tracked wallets, 10%+ of its legs) · `automated` (the tracker's machine-timing rule) · `dormant` (no action of its own; airdrop receipts do not count) · `unknown` (active, but < 8 decoded buys/sells: no verdict) · `organic` (enough trading seen, no coordination evidence) |
| `label_flags` | every coordination flag that applies (a cohort wallet is usually a bundler too) |
| `relationship` | `launch_cohort` · `bundle_pair` · `transfer_linked` · `none_detected` · `unknown` (too little to say; never the same as none detected) |
| `style` | `sniper` (first buy within 60 s of the token's first tracked trade on 50%+ of 5+ observed launches) · `scalper` (median hold <= 15 min) · `swing` (15 min to 3 days) · `holder` (<= 0.25 sells per buy, or > 3 days) · `mover` (20+ transfers, few decoded trades) · `mixed` · `thin` (< 8 decoded buys/sells) |
| `style_tags` | `high_frequency` (40+ legs per active day, about the top 6% of wallets) · `high_churn` (20+ round trips, median hold <= 5 min, sells about equal to buys: consistent with volume farming, not proof) · `accumulator` (3+ buys per token, few sells) · `early_buyer` · `concentrated` · `diversified` · `linked` |
| `performance_tier` | measured closes in the lookback only, never Agora's PnL: `consistent_winner` (10+ closes, positive, 50%+ wins, profit factor 1.5+) · `emerging` (3-9 closes, positive, 50%+ wins) · `persistent_loser` (10+ closes, negative, < 40% wins) · `mixed` · `insufficient_history` (< 3 closes) |
| `label_confidence` | `confirmed` (operator or chain fact) · `high` · `medium` · `low` · `insufficient`: a heuristic from evidence count, capped at `low` when data quality is `gaps`/`unscanned`. Not a probability. |
| other tiers | `size_tier` by median verified buy (<1 / 1-5 / 5-25 / 25-100 / 100+ HYPE) · `activity` (own leg within 7 d / 30 d / none) |

Page: a label strip (mutually exclusive counts, click to filter), a summary line (classified vs
insufficient evidence, cohorts, same-block pairs, flag-free consistent winners, style mix),
filters `?label= ?style= ?flag= ?tier= ?confidence= ?cohort=` (each accepts several values,
comma-separated = any of them; different filters AND together; `flag=clean` = no coordination
flag), Style / Cohort-flags / Conf. columns, a Labels section with partners and evidence on each
wallet page, and a page per launch cohort at `/cohorts/{id}` (`/api/v1/cohorts/{id}`): members,
their labels, shared launches. Member PnL is never summed.

CSV: **Export CSV** = the filtered wallets across all pages; **Export all wallets** = every
tracked wallet. Both keep the 32 earlier columns and append the label columns, the `lb_*`
metrics behind each rule, and `chain_id`, `label_confidence`, `flag_confidence`,
`style_confidence`, `relationship`, `bundle_partner_wallets`, `label_lookback_days`,
`labels_as_of`, `exported_at`. Lists are `;`-separated; unknown is empty, never 0.

The web service now also receives `TRACKING_BLOCKED_WALLETS` (the same YAML anchor as the
tracker: a public list of addresses, no secret) so blocked wallets are labelled as such.

Production, 2026-11-12 (610 wallets, 0.9 s): 228 classified, 382 insufficient evidence
(206 unknown, 176 dormant); 181 organic; 2 launch cohorts (21 wallets); 25 wallets in same-block
pairs, 18 of them outside any cohort, forming seven small groups (two triangles of high-churn
snipers holding 1-3 minutes); 34 consistent winners, 23 without any flag; 30 persistent losers.
The median wallet has 9 legs in 30 days and 1 decoded buy/sell; the rest are transfers
(including the 0x6f0954 airdrop spam), which is why receipts never count as activity or style.

Deliberately not labelled, because stored rows cannot support them yet: momentum (no entry-time
market history per trade), diamond hands (history starts 2026-11-02), shared funders (native-coin
funding transfers are not indexed), insider and wash-trading labels. Not built in this version
(need a reviewed migration): stored label history ("what was it 7 days ago"), hysteresis,
manual review/override, persistent cluster ids. Cohort ids are the lowest member address and can
change when membership changes. Private (manual/Telegram) wallets are outside `public_api` and
are not classified here by design.

Thresholds live in `LabelRules`; changing one changes what a label means, so bump `VERSION`.
Flags are inferences from on-chain patterns, not proof of common control or misconduct.
