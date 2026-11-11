# onchainflow.app: Elysium research dashboard (design and implementation handoff)

Status: **implemented and live (2026-11-03).** `https://onchainflow.app` is served by the
`web` service of this repository (PRs #48-#51, tracking issue #44). The old Revival Radar
website was stopped; the domain, DNS, Caddy and the Let's Encrypt certificate are unchanged
and reused. Sections below keep the original design; "As built" at the end records what was
verified and how to operate and roll back.

This document is self-contained so the site can be built independently. Infrastructure
facts come from `VPS_AUDIT.md`.

## Constraints

* 1 vCPU / 1.9 GB RAM VPS shared with two bots. The site must be light: one process,
  `mem_limit: 256m`, response caching.
* Data source: the Elysium tracker's PostgreSQL DB (`tracker`, about 50 MB). **Read-only**;
  it must never slow down the indexer's writes.
* No fabricated metrics. Every number comes from stored data, or from Agora live with a
  label. Empty and partial coverage are shown honestly (reuse `reports.coverage()`).
* Public site: private data stays private (see "Public data boundary").

## Architecture (simplest maintainable)

```
Internet ─► Caddy (existing, host network, :443, auto-TLS)
               └─► 127.0.0.1:8100  web  (new service in the elysium compose project)
                                     │  same image as tracker/ui, `python -m agora_tracker --web`
                                     │  Starlette + Jinja2 server-rendered pages + small JSON API
                                     └─► PostgreSQL `tracker` as role `dashboard_ro`
                                           (SELECT on the public_api schema only, read-only,
                                            statement_timeout 5 s, connection limit 5)
```

* **Code:** new package `agora_tracker/web/` in this repository. Reuse
  `agora_tracker/reports.py`, which already provides read-only, time-bounded, parameterized
  analytics (market overview, smart money, top traders, wallet/token detail, trending,
  gainers/losers, coverage and health). The Telegram views prove those queries.
  `reports.py` queries the base tables; for the web, point it at the `public_api` views
  (same column names), or add a `schema` parameter.
* **Dependencies:** `starlette`, `uvicorn` (one worker), `jinja2`. No Node build step:
  static CSS plus a few hundred lines of vanilla JS for filters and pagination.
* **Caching:** 30 s TTL per (page, filters) in-process; Agora live calls through the
  existing `AgoraMarket` cache.
* **Compose service:**
  ```yaml
  web:
    image: elysium-wallet-tracker:latest
    container_name: elysium-web
    command: ["--web", "--host", "0.0.0.0", "--port", "8100"]
    environment:
      DATABASE_URL: postgresql+psycopg://dashboard_ro:${DASHBOARD_DB_PASSWORD}@postgres:5432/tracker
    ports: ["127.0.0.1:8100:8100"]     # loopback only; Caddy is the public entry
    read_only: true
    tmpfs: [/tmp]
    mem_limit: 256m
    healthcheck: GET /healthz
    restart: unless-stopped
    logging: *logging
  ```
* **DB role** (Alembic migration `0004_dashboard_role`; the password is generated on the
  VPS like `POSTGRES_PASSWORD`, stored only in `.env` as `DASHBOARD_DB_PASSWORD`):
  ```sql
  CREATE SCHEMA public_api;
  CREATE ROLE dashboard_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 5;
  ALTER ROLE dashboard_ro SET default_transaction_read_only = on;
  ALTER ROLE dashboard_ro SET statement_timeout = '5s';
  REVOKE ALL ON SCHEMA public FROM dashboard_ro;
  GRANT USAGE ON SCHEMA public_api TO dashboard_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA public_api TO dashboard_ro;
  ```
  Note: the tracker's own role (`tracker`) is currently a superuser (it is the image's
  bootstrap user). Splitting it into an owner role is a separate hardening task.

## Public data boundary

| Data | Public? | How |
|---|---|---|
| Trades of leaderboard wallets (Agora top-100 is public) | yes | `public_api.trades`: trades whose wallet was in a leaderboard snapshot |
| Trades of wallets watched only via `watchlist.txt` / Telegram ⭐ | **no** | excluded by the view (private watchlist) |
| Leaderboard snapshots and entries | yes | `public_api.leaderboard` (no `raw_payload`) |
| Token profiles, market snapshots | yes | as is |
| Signals (cluster buys) | yes | only signals whose wallets are all public |
| Positions and realized PnL of public wallets | yes | views filtered like trades |
| Health: cursor, heads, freshness, counts | yes | `public_api.health` (no hostnames, no RPC URLs with keys) |
| `notification_outbox`/`deliveries`, `ui_sessions`, `report_jobs`, `service_state` internals, Telegram ids | **no** | not granted |

## Pages

All pages have a time-range selector (15m / 1h / 6h / 24h / 7d / 30d / custom,
`[start, end)`), a freshness and coverage banner, and honest empty and error states.

1. **Overview:** tracker health, last indexed block and lag, last trade, tracked wallets,
   trades in the window (verified buy/sell, estimated shown separately), tokens, signals,
   recent activity feed. The population label is always visible ("tracked wallets").
2. **Projects (tokens):** searchable table: symbol, name, launchpad, contract, first seen,
   last tracked activity, qualified buyers/sellers, verified net flow, MC (Agora, labelled
   "live"). Detail page: market card, price change over the window (snapshots or candles,
   labelled), tracked buyers, trade timeline, links (Agora, Kleos, BOXA, Elyscan, 𝕏).
3. **Smart money:** rankings from `reports.smart_money` (net, buyers, newcomers, repeat,
   exits, clusters) and top traders (Agora PnL and measured realized PnL, never mixed).
   Wallet detail: activity, tokens, realized PnL by confidence, recent trades, Elyscan and
   Agora links.
4. **Early plays:** tokens first seen by tracked wallets in the window, rising activity,
   trending score with its components (`reports.trending`). A clear banner says these are
   statistical observations, not recommendations.
5. **Scanner health:** indexer heartbeat, cursor vs head per RPC endpoint (lagging flag),
   leaderboard refresh status history, market snapshot freshness, alert outbox counts
   (pending/dead), coverage warnings. This is the page that catches silent failures.

Dark theme, responsive tables (horizontal scroll on mobile), monospace addresses with copy
buttons, pagination of 25 rows.

## As built

### Public database interface (migration 0004)

* Role `dashboard_ro`: LOGIN only; no superuser/createdb/createrole/replication; connection
  limit 5; `default_transaction_read_only=on`; `statement_timeout=5s`; `lock_timeout=2s`;
  `idle_in_transaction_session_timeout=15s`; `search_path=public_api`; no privilege on schema
  `public`; TEMPORARY revoked from PUBLIC. Created **without a password**: `--migrate` sets
  it (scram-sha-256) from `DASHBOARD_DB_PASSWORD` (24-128 chars of `[A-Za-z0-9_-]`).
* Views in schema `public_api` (same names as the base tables, so `reports.py` runs
  unchanged): `trades`, `wallets`, `wallet_watchlist` (leaderboard rows only, label/reason
  always NULL), `wallet_positions`, `realized_pnl`, `signals`, `leaderboard_snapshots`
  (no raw payload/request meta), `leaderboard_snapshot_entries` (no `extra`),
  `token_metadata`, `token_profiles`, `token_market_snapshots`, `indexer_checkpoints`,
  `indexed_blocks`, `service_state` (keys `rpc_heads`, `heartbeat`, `ui_heartbeat`,
  `collector_last` only), `outbox_status` (status counts only). Helper views
  `public_wallets` / `public_tokens` are not granted.
* **Public wallet** = appears in an accepted (`ok`/`imported`) leaderboard snapshot.
  **Public token** = at least one public trade. Wallets followed only via `watchlist.txt` or
  the Telegram menu are private: invisible in trades, positions, PnL, signals, counts, tokens
  and search. A signal is public only if every wallet involved is public.
* The web process refuses to start if its role is superuser/createdb/createrole or can read
  raw `public` tables.
* Adding a column to a base table does **not** expose it; a later migration must add it to
  the view deliberately.

### Runtime behaviour

* Responses are built from stored data only; market numbers come from stored
  `token_market_snapshots` (max age 30 min) and are labelled "stored, not live". Market caps are
  shown in USD (see "Activity monitor and USD market cap" below); `mcap_hype` stays as evidence.
* Cache: 30 s TTL, single-flight per key, 256 entries, stale-on-error up to 300 s; rolling
  presets end on a 30 s grid so visitors share cache entries. At most 3 page builds run at
  once (8 s wait, then 503 + `Retry-After`). Per-IP token bucket (10 rps, burst 40; loopback
  without `X-Forwarded-For` exempt).
* Time ranges: `range=15m|1h|6h|24h|7d|30d` or `start`+`end` (UTC, max 30 days), half-open
  `[start, end)`. Invalid input returns 400.
* Security headers: CSP `default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:`,
  `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, HSTS behind TLS.
  No inline script or style, no third-party requests.
* JSON API under `/api/v1/` (`meta`, `overview`, `tokens`, `tokens/{addr}`, `smart-money`,
  `wallets/{addr}`, `early`, `health`, `search`); `/healthz` for the container healthcheck.
* Configuration: the `web:` block of `deploy/config.yaml` (`WebConfig` in `config.py`).

### Local development

```bash
docker compose up -d postgres && docker compose run --rm migrate
DATABASE_URL=postgresql+psycopg://tracker:...@localhost:5432/tracker python -m agora_tracker --migrate
# give the read-only role a password, then serve as that role
DASHBOARD_DB_PASSWORD=$(openssl rand -hex 24) python -m agora_tracker --migrate
DATABASE_URL=postgresql+psycopg://dashboard_ro:$DASHBOARD_DB_PASSWORD@localhost:5432/tracker \
  python -m agora_tracker --web --port 8100
```

Tests: `TEST_DATABASE_URL=postgresql+psycopg://... pytest` (the DB-boundary tests log in as the
real role; the tests provision its password like production does).

### Verification record (2026-11-03)

| Check | Result |
|---|---|
| Staging pages on the VPS loopback | all 200; cold render 0.37 s worst (Overview), `/healthz` 4 ms |
| Role boundary, real login | read-only default on, `statement_timeout 5s`, `search_path public_api`; raw `public.trades` and `notification_outbox` denied; INSERT denied; temp tables denied |
| Data parity | `trades` 2,089 raw = 2,089 via view; watchlist 100 = 100 (no private wallets present today); 154 public wallets, 167 public tokens |
| View cost (EXPLAIN ANALYZE) | `public_wallets` 14 ms, `trades` view 17 ms on production data |
| Load test, 5 rps x 5 min, loopback | 1,501 requests, 0 server errors, 0 rate-limited; p50 12 ms, p95 0.81 s, p99 1.58 s, max 2.75 s (the tail is cold-cache builds of the heaviest pages, e.g. Early plays p95 1.8 s) |
| Resources under load | web 55 MB idle, 92 MB peak of 256 MB, 13% CPU peak; dashboard_ro connections 3 of 5; host load1 1.63 on 1 vCPU, >= 967 MB available; disk 18.45 GB used / 35.16 GB free |
| Bots during load | tracker heartbeat never older than 10 s, cursor advanced throughout |
| Cutover | one-line diff (`reverse_proxy` 8000 -> 8100), `caddy validate` ok, graceful reload, no DNS or certificate change |
| External check (GitHub runner, Chromium) | Let's Encrypt chain valid, expires 2027-01-10; http -> https 308; CSP, nosniff, DENY, HSTS, no-referrer, gzip; 8 pages x 2 viewports: 200, no console errors/failed/third-party requests, no horizontal overflow, data rendered |

Re-run the external check any time: push a new `requested=` line to `deploy/SITE_CHECK` on the
`ops` branch (workflow `site-check`, read-only, screenshots kept as an artifact).

### Cutover and rollback (no DNS change)

Executed through `maint` steps (`caddy-show`, `caddy-backup`, `caddy-switch`,
`caddy-rollback`). The Caddyfile (`/opt/gmgn-revival-radar/deploy/Caddyfile`) is a single-file
Docker bind mount, so edits are written **in place**; `sed -i` or `mv` would swap the inode and
Caddy would keep reading the old content. `caddy-switch` pre-checks the dashboard, validates
the new file inside the Caddy container, writes it, confirms the container sees it, reloads,
checks `https://onchainflow.app/` and **rolls back automatically** if that check fails.

Rollback options, fastest first:

1. `action=maint steps=caddy-rollback`: restores `Caddyfile.pre-elysium`, validates, reloads.
   (The old site's container is stopped; `docker start gmgn-revival-radar-web-1` revives it on :8000.)
2. `action=web-rollback`: stops only the `web` container (then Caddy answers 502).
3. Database: nothing to roll back for the site to work; migration 0004 is additive. To remove
   it entirely: `alembic downgrade 0003` (drops `public_api` and the role, or disables login if
   another database in the cluster still holds grants).

### Troubleshooting

* **502 from the site:** `web` is down or unhealthy. `action=status` / `web-smoke`, then
  `docker logs elysium-web`. A startup error naming the role means the DB password or
  privileges are wrong (the process refuses an over-privileged role by design).
* **503 "busy" / "database unavailable":** the 3 concurrent builds are saturated, or Postgres
  is down; the page serves a stale copy for up to 5 min when it has one.
* **Numbers look stale:** every page shows "data computed N s ago" and the snapshot age;
  Scanner health shows which input is stale.
* **`dashboard_ro` login fails after a downgrade/upgrade:** the role was recreated without a
  password; run `--migrate` with `DASHBOARD_DB_PASSWORD` set (the `web` action does).

### Known limits and follow-ups

* USD market cap is derived from the stored Agora `hypeUsd` series (not an independent price
  feed); see the methodology section for limits.
* The tracker connects as the Postgres superuser; splitting that into a least-privilege
  writer role is a separate hardening step.
* Revival `research_briefs` retention and the undecided BOXA WHYPE option are tracked in #44.

## Activity monitor and USD market cap (migration 0005)

### Wallet Intelligence: `/leaderboard`, `/leaderboard/{wallet}`, `/api/v1/leaderboard...`

Population: **every wallet that has ever been in an accepted, complete Top-100 snapshot**
(`public_api.wallet_enrollment`), not only today's list. Private manual / Telegram wallets cannot
appear: every source is a `public_api` view (migration 0009 adds `public_api.wallet_scan_coverage`).

* **Row** (`GET /api/v1/leaderboard`): status (`current` = on the latest accepted list, else
  `former`), current / best / last rank, snapshot appearances, entries (re-entries), first / last
  time on the list; **Agora** PnL / worth / volume (latest known value, Agora's accounting);
  window activity (trades, buys, sells, transfers, tokens, bought / sold HYPE from *verified*
  amounts only, estimated legs counted apart); **measured** closes, win rate and PnL (only sells
  whose cost basis is known); last trade; `quality` + `tracking_basis` + `tracked_since`.
  Unknown is `null`, never 0 (no measured close -> `measured_pnl: null`; no Agora PnL -> null).
  Agora and measured figures are never combined.
* **Data quality:** `complete` (scanned without gaps since first appearing on the list),
  `partial` (first scanned later, or only *derived* coverage), `gaps`, `unscanned`, `excluded`.
* **Filters:** `window` 24h|7d|30d|all (activity metrics), `membership` all|current|former,
  `quality`, `active=1`, `min_trades`, `min_rank` / `max_rank` (current rank; former members have
  none), `min_pnl` (latest known Agora PnL; unknown never matches), `q` (address fragment),
  `sort` rank|pnl|worth|volume|legs|buy|realized|last_trade|appearances|best_rank|last_top|first_top,
  `dir`, `limit` 1-100, `page`. Rows with no value for the sort key always go last. KPIs count the
  whole tracked set, not the page. Page-based pagination is deliberate: the set is bounded
  (<= 20,000 rows, held in the page cache) and sorts are arbitrary.
* **Wallet profile** (`/api/v1/leaderboard/{wallet}`, plus `/performance`, `/rank-history`,
  `/relationships`): performance for 24h / 7d / 30d / all, rank history (`step` snapshot|hour|day,
  last observation per bucket), membership events, scan coverage, tokens traded, and
  **relationship evidence** = same-transaction token transfers between two tracked wallets. That
  is evidence of a transfer, not of common control; no accusation is made anywhere.
* **CSV:** `GET /api/v1/leaderboard.csv` (same filters, fixed columns), `.../{wallet}/rank-history.csv`.
  Text cells starting with `= + - @ TAB CR` get a leading apostrophe (formula injection); numbers
  stay numbers. `Cache-Control: private, no-store`.

#### Wallet scores (`scoring:` config, migration 0010)

An explainable 0-100 number per wallet, shown in the table (`score`, `score_quality`, `min_score`,
`sort=score`) and broken down on the wallet page / `GET /api/v1/leaderboard/{wallet}/score`.
Five components in [0, 1], each a documented function of stored facts: `win_rate_lb` (Wilson
lower bound of measured wins), `return_on_cost` (measured PnL / cost basis, clamped), `recency`
(half-life decay since the last trade), `list_persistence` (share of accepted lists containing
the wallet), `rank_strength` (mean position when listed). Weighted mean over the components
that **have data**; the share of weight that had data is reported (`weight_available`), a
component without evidence is `available: false` (never 0) and below `min_weight_available` there
is no score. Quality: `sufficient` (both measured components, >= 80% of the weight), `limited`
(no measured trade evidence; capped at `limited_cap`), `insufficient` (no score).

* **Point in time.** Every input is strictly older than the row's `as_of`; wallets are scored only
  from the moment they first appeared on the list. A test adds trades, losses, a list and a wallet
  after `as_of` and asserts the earlier score is bit-identical. `--score-backfill DAYS` replays
  00:00 UTC points through the same code.
* **Immutable versions.** `score_versions` stores the full configuration and its hash; running a
  version with different weights/parameters is refused. Hourly rows are thinned to daily after 48 h.
* **Not a confidence.** There is no probability or confidence percentage anywhere; `weight_available`
  is the share of the model's weight that had data, nothing more.

### Activity monitor: `/activity`, `GET /api/v1/activity`

Nav: Overview | Activity | Projects | Smart Money | Early Plays | Scanner Health. Overview keeps
the newest-15 preview with a "View All Activity" link (range carried over, 6h maps to 12h).

* **Rows** are individual `trades` legs (never aggregated): timestamp (UTC, optional local time),
  wallet (link + copy), token (link), action, HYPE amount when known, basis
  (verified / estimated / unknown), tx hash, block, explorer link. Transfers are never shown as
  buys or sells; several legs of one tx stay separate rows.
* **Filters (server-side):** `action` = all | buy | sell | transfer_in | transfer_out | swap |
  multi_other; `range` = 15m | 1h | 4h | 12h | 24h | 7d | 30d | all, or `start`+`end` (UTC,
  half-open); `basis`; `q` (token address, symbol, wallet address, tx hash; exact or partial, hex
  prefixes need >= 6 digits; LIKE wildcards are escaped and everything is bound); `wallet`,
  `token`; `sort` = newest | oldest | largest_buy | largest_sell. Invalid input returns 400, an
  exhausted rate limit 429 (JSON with `Retry-After` under `/api/`).
* **HYPE amount filter:** `min_hype` / `max_hype` (both optional, inclusive). Plain decimal text,
  up to 10 integer and 18 fractional digits, at most 1,000,000,000; no sign, exponent, separators
  or non-ASCII digits (else 400). `min_hype <= max_hype`. Parsed with `Decimal`, converted to wei
  exactly and compared as `hype_wei >= :min_wei AND hype_wei <= :max_wei` on `NUMERIC(78,0)`, so
  `0.1` matches 100000000000000000 wei and not 99999999999999999. **Only rows with a known amount
  match:** `hype_wei IS NOT NULL` and confidence `high` (verified) or `estimated`; transfers,
  unknown and legacy amounts are excluded and NULL is never read as zero (a stored `0` is a
  known amount). It composes with action, range, basis, search, sort, keyset cursors, the total
  count and `since_id` polling (the watermark still advances past rows the filter hides). The
  response echoes the canonical bounds in `filters.min_hype` / `max_hype` / `amount_filter`; the
  cache keys include them. UI: Min / Max inputs, preset chips 0.1, 0.5, 1, 5, 10, 25, 50, 100,
  an active-filter chip, "clear amount" and Reset.
* **Pagination:** keyset, never OFFSET. Cursor = opaque base64 JSON bound to the sort:
  `(block_time, id)` for newest/oldest, `(hype_wei, id)` for largest_*. `dir=next|prev`, size 50
  (max 100); the query fetches size+1 for `has_next` / `has_prev`. Total is counted with a cap
  of 10,000 (`total_capped`). Coverage (first/last indexed time in the filter) is returned.
* **Auto-update:** `static/activity.js` polls `GET /api/v1/activity?since_id=<watermark>`
  every `activity_poll_s` (default 20 s, config `web.activity_poll_s`, hidden tab
  `activity_hidden_poll_s` = 120 s, 0 pauses). Single in-flight request, 10 s abort timeout,
  exponential backoff with jitter, `Retry-After` honoured, `ETag` / `If-None-Match` (304).
  The watermark is the insertion id, so late-indexed rows from old blocks are caught; if more
  than `activity_since_limit` (100) rows are new the response sets `overflow` and the UI shows
  "N+ new". New rows never move the viewport: a fixed "N new - Show newest" pill appears and
  flushes only on click. ON/OFF toggle (kept in localStorage), last refresh, error state.
  Only text nodes are written to the DOM (CSP has no inline script or eval).
* **Freshness is shown separately:** browser refresh time, scanner heartbeat, last indexed
  block, last observed tx. `scanner_status` = live | delayed | stale | unknown from heartbeat
  age (> 120 s delayed, > 300 s stale), checkpoint age (> 300 s delayed, > 900 s stale) and lag
  (> 120 blocks); the page never shows a green "Live" unless the scanner is live.
* **Caching:** short-TTL (5 s) single-flight cache separate from the page cache; responses are
  `Cache-Control: private, no-cache` with an ETag.

### Default visibility: hidden tokens (web hotfix 2026-11-10, Elysium mainnet USDC)

**Why:** after mainnet launch, USDC (`0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65`, chain 99901,
identity evidence in `docs/OPERATIONS.md` → "Telegram-only token mute") flooded the public
Activity feed. PR #118 muted it for Telegram only, through `ALERTS_TELEGRAM_MUTE_TOKENS` on the
*tracker* service; the website takes its hidden list from `alerts.mute_tokens` (WHYPE, KNTQ in
`deploy/config.yaml`), so USDC stayed visible.

**What:** `web.activity_hidden_tokens`, also read from `WEB_ACTIVITY_HIDDEN_TOKENS` (comma/space
separated, exact addresses, lower-cased, a symbol is rejected), set in `docker-compose.yml` on the
`web` service only. It is merged with `alerts.mute_tokens` into `PublicReports.hidden_tokens`.

| Surface | Hidden token |
|---|---|
| `/activity`, `GET /api/v1/activity` default page, every sort and action, cursor pages, `total` | hidden |
| live poll `since_id=` | hidden (the returned `head_id` still advances past hidden rows) |
| overview "Recent activity" preview, wallet page / `GET /api/v1/wallets/{a}` trade list | hidden |
| `token=<address>` filter, full-address search `q=<address>` | **shown** |
| `/tokens/{address}`, `GET /api/v1/tokens/{address}` | **shown** |
| symbol search `q=usdc` | the hidden contract stays hidden; look-alikes at other addresses are shown |
| token tables, smart money, early plays, overview totals and top tokens, leaderboard, PnL | **unchanged** (rankings and financial figures keep USDC) |
| consensus | excluded, as for `alerts.mute_tokens` since #66 |
| stored rows, positions, PnL, alerts, the Telegram mute | unchanged |

Rankings are deliberately not filtered: removing a token there changes totals and rank order.
The in-process caches start empty with the new container and the Activity ETag covers the item
ids, so no pre-hotfix page is served after the restart (`Cache-Control: private, no-cache`).

**Deploy (web only):** `deploy/OPS_REQUEST` on the `ops` branch with `action=web-hotfix`
(or Actions → deploy → `web-hotfix`). It records the running image and settings, tags the running
dashboard image `elysium-wallet-tracker:pre-web-hotfix`, builds, recreates **only** `web`
(`--no-deps --force-recreate`, no migration, `config/` untouched), then runs
`scripts/web_activity_check.py --expect-hidden` on the loopback port and through
`https://onchainflow.app` (cache-busting GETs). Unhealthy or a failed loopback check rolls back
automatically.

**Rollback (web only):** `action=web-hotfix-rollback`. It serves `pre-web-hotfix` through a
generated `docker-compose.web-rollback.yml` override, so `:latest` (and with it the tracker's and
ui's image) is not moved; no database restore is involved. Trigger it if the site fails health
checks, non-USDC activity disappears, paging breaks, database errors rise or the container nears
its 256 MiB limit. Removing the variable from `docker-compose.yml` and re-running `web-hotfix`
also restores the previous visibility.

### USD market cap

* **Units:** Agora quotes everything in HYPE and attaches `hypeUsd` (USD per 1 HYPE) to each
  leaderboard payload. Trade sizes and net flows stay in HYPE and are never relabelled.
* **Rate store:** `hype_usd_rates(observed_at, hype_usd numeric(24,8), source, UNIQUE(source,
  observed_at), CHECK hype_usd > 0)`. Filled by (1) an idempotent backfill from
  `leaderboard_snapshots.raw_payload` in the migration and (2) trigger
  `trg_leaderboard_hype_usd` (AFTER INSERT/UPDATE OF raw_payload) which catches every exception,
  so a bad payload can never break the tracker's insert; rates must be > 0 and < 1e6. No
  collector, tracker or ui change is needed, hence no container recreation. Nothing calls a price
  provider from the request path.
* **Derivation at read time:** `market_cap_usd = mcap_hype x nearest rate` in `Decimal`
  (quantised to 0.01), via two index probes (one rate on each side of the snapshot time). It is
  the rate valid at the snapshot's own time, not today's.
* **Provenance** by gap between snapshot and rate: `exact` <= 60 s, `aligned` <= 600 s,
  `stale` <= 3600 s (value produced, flagged), `unavailable` beyond that or with no rate (no value,
  never `$0`). `web.fx_exact_s` / `fx_aligned_s` / `fx_max_gap_s` configure the bounds.
* **API fields** (units explicit): `market_cap_usd`, `market_cap_hype`, `market_cap_source`
  (`stored_snapshot`), `market_cap_as_of`, `market_cap_status` (valid | stale | unavailable |
  invalid), `hype_usd`, `hype_usd_as_of`, `hype_usd_gap_s`, `hype_usd_quality`,
  `snapshot_age_s`. `/api/v1/meta` has a `units` block. The `public_api.hype_usd_rates` view
  exposes only `observed_at, hype_usd, source`.
* **Formatting / sorting:** one `format_usd` ($830, $12.5K, $1.28M, $3.45B; `-` when missing;
  rollover at unit edges); the token table sorts mcap by USD; sparklines use USD.
* **Limits:** the rate is Agora's own `hypeUsd`, not an independent oracle; if Agora stops
  sending it, USD turns `stale` then `unavailable` rather than being extrapolated; market cap
  itself is Agora's stored snapshot (<= 30 min old or flagged).

### Performance notes (1M-trade synthetic DB, 1 vCPU)

`public_wallets` and `public_tokens` were `SELECT DISTINCT` over large inputs (~160 ms / ~500 ms
at 1M trades). Migration 0005 replaces them with recursive skip-scans returning the same rows
(~6 ms / ~15 ms). Activity page p50 6-8 ms for any range / filter, tx search 3-8 ms, symbol
search ~15 ms, `since_id` poll 1.5-8 ms; the only slow case is `largest_buy` over all history
(~220 ms, 5 s timeout). At ~2,100 trades/day 1M rows is ~1.3 years away. Symbols are cached in
process (10 min). Downgrade of 0005 restores the previous views.

## Acceptance

Met on 2026-11-03: `https://onchainflow.app` serves the dashboard over HTTPS with real tracker
data; the site's DB role cannot write or read private tables; the tracker's heartbeat and cursor
stayed fresh under load; both bots kept working. See the open item about recreating the tracker
and ui containers in OPERATIONS.md ("Dashboard").

Historical score eligibility uses first Top-100 appearance, independent of today's scanner
exclusion status. Excluding a contract from scanning must not erase its earlier research
score. The eligibility rule is included in the immutable model fingerprint. If an earlier
build already registered the same scoring version without this rule, select a new scoring
version; do not overwrite the stored configuration or historical scores. Historical replay
uses event-time observations currently stored and is not proof that late-ingested evidence
was available to the scanner at the original time.
