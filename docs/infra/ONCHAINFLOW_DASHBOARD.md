# onchainflow.app: Elysium research dashboard (design and implementation handoff)

Status: **approved design, not yet implemented.** The old Revival Radar website was
stopped on 2026-11-03 (`onchainflow.app` returns 502 until cutover). The domain, DNS, Caddy
and the Let's Encrypt certificate are unchanged and are reused.

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

## Validation before cutover

1. `docker compose up -d web`, then on the VPS:
   `curl -s 127.0.0.1:8100/healthz` and `curl -s 127.0.0.1:8100/ | grep -c <expected>`.
2. Verify the role is read-only:
   `INSERT` as `dashboard_ro` fails, and `SELECT` on `notification_outbox` is denied.
3. Real data: the trade count on Overview equals `SELECT count(*) FROM public_api.trades`
   for the window.
4. The tracker keeps writing: its heartbeat is fresh and its cursor advances during a 5-minute
   load test of the site (about 5 rps).

## Cutover (no DNS change)

The Caddyfile is bind-mounted from `/opt/gmgn-revival-radar/deploy/Caddyfile`. Cutover is
a `maint` step:

```
cp Caddyfile Caddyfile.pre-elysium                 # rollback copy
sed 's#reverse_proxy 127.0.0.1:8000#reverse_proxy 127.0.0.1:8100#' -i Caddyfile
docker exec gmgn-revival-radar-caddy-1 caddy reload --config /etc/caddy/Caddyfile
curl -sI https://onchainflow.app | head -1          # expect 200 with a valid certificate
```

Rollback: restore `Caddyfile.pre-elysium` and run `caddy reload` (or
`docker start gmgn-revival-radar-web-1` to bring back the old site, which is still on disk).
Later, Caddy ownership can move to the elysium compose project, reusing the
`gmgn-revival-radar_caddy_data` volume so the certificate is kept.

## Acceptance

`https://onchainflow.app` serves the dashboard over HTTPS with real tracker data. The site's
DB role cannot write or read private tables. The tracker's heartbeat and cursor stay fresh
during load. Both bots stay healthy.
