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
  `token_market_snapshots` (max age 30 min) and are labelled "stored, not live". Market cap is
  in HYPE (no USD value is stored).
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

* Market cap is in HYPE; storing `mcap_usd` in the collector would allow USD.
* The tracker connects as the Postgres superuser; splitting that into a least-privilege
  writer role is a separate hardening step.
* Revival `research_briefs` retention and the undecided BOXA WHYPE option are tracked in #44.

## Acceptance

Met on 2026-11-03: `https://onchainflow.app` serves the dashboard over HTTPS with real tracker
data; the site's DB role cannot write or read private tables; the tracker's heartbeat and cursor
stayed fresh under load; both bots kept working. See the open item about recreating the tracker
and ui containers in OPERATIONS.md ("Dashboard").
