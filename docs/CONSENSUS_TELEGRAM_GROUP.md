# Dedicated Consensus Telegram group (owner-controlled)

Status: **draft feature, disabled by default**. This is a second Telegram bot and a separate
supergroup destination, not a new destination for the existing ElysiumScan notification outbox.
No Telegram messages are sent until the operator configures a separate bot + group, enables
the Compose profile, and an authorized owner saves `enabled=true` on the Consensus web tab.

## Behavior

- Reuses the **same Consensus engine** as `/consensus` on onchainflow.app, including public
  leaderboard wallets only, verified BUY minus SELL, bundle/same-block independence,
  per-wallet minimum HYPE, time window, launchpad, time range, market-cap-at-trigger and
  stored USD conversions. A browser's Apply changes that browser view only.
- An **authenticated "Save filters for Telegram group"** button publishes the chosen six
  thresholds and the enable/disable flag to PostgreSQL. A separate, send-only bot reads the
  saved version every 45 seconds. No service restart is required for threshold changes.
- The bot sends a compact Consensus card: symbol/address, traders, independent count,
  bundle badge, net HYPE/approx USD, MC at threshold and now, and member summaries.
  Buttons: Fomo, Maestro, Elyscan, original trigger transaction, Consensus web page.
- The main ElysiumScan alerts, Telegram menu, updater, `getUpdates` consumer, outbox,
  token tracking, USDC suppression, and public dashboard queries are unaffected.
- Every setting revision creates an audit record. Changing or disabling the policy
  cancels **pending** messages from previous revisions. No old consensus history is blasted
  into the group: only events triggered **after the owner saved the current policy** and
  no more than 10 minutes old can be queued.
- Each event has a unique (version, token, trigger time, transaction) identity. The
  delivery log is durable across crashes and restarts, retrying transient Telegram
  errors. Sending is *at-least-once*, not exactly-once: Telegram could accept a message
  while the worker loses the response, so rare duplicates are possible.
- One Postgres advisory lock prevents competing senders; max 10 recent cards checked
  and 5 messages per cycle, 1.2 seconds minimum between sends. No RPC calls or wallet
  interactions are made by the sender.

## Authentication and least-privilege design

1. The public `web` process keeps connecting as `dashboard_ro`, with **only SELECT**
   on existing `public_api` views. There are no new public dashboard write privileges.
2. Migration `0011_consensus_group.py` creates `consensus_alerts` schema and
   `consensus_rw` role without a password. This narrowly scoped role may access only
   the private settings, audit and delivery tables. It has **no access** to tracker
   tables, `notification_outbox`, or wallet/order execution records.
3. The web process optionally opens a second connection as `consensus_rw`, but
   only when both `CONSENSUS_DB_PASSWORD` and a 32+ character `CONSENSUS_ADMIN_KEY`
   exist. Missing or partial configuration refuses to enable writes.
4. `GET /api/v1/consensus/settings` reveals only saved thresholds, enabled flag and
   version, not group/chat IDs, bot token, keys, or database credentials.
5. `POST /api/v1/consensus/settings` requires an exact `Authorization: Bearer`
   secret header, `Origin: https://onchainflow.app` (configurable by
   `CONSENSUS_ADMIN_ORIGIN` for authorized staging), trusted reverse-proxy
   `X-Forwarded-Proto: https`, JSON-only body limited to 2048 bytes, and strict
   allowlisted thresholds. A version mismatch is HTTP 409; re-read before retrying.
   **The owner key must never be stored in localStorage, sent in query parameters,
   checked into Git, or exposed in logs.** The web form clears it after a save.
6. Caddy must overwrite client-supplied forwarding headers, serve HTTPS with a single
   trusted host, avoid caching POSTs and prevent credential leakage in access logs.
   Real-proxy review is required before enabling writes.
7. The sender has a dedicated `CONSENSUS_TG_BOT_TOKEN` and **supergroup**
   `CONSENSUS_TG_CHAT_ID=-100...`. It never polls Telegram updates. Do not reuse
   `TG_BOT_TOKEN`, `TG_CHAT_ID` or a user's personal funds. The separate bot needs
   permission to post to the destination supergroup; the group chat ID is configured
   by the operator (not read from the public web UI).

## One-time staging setup (never paste credentials into chat)

Generate and store these secrets in the VPS deployment secrets manager / mode-0600
`.env` only:

- `CONSENSUS_DB_PASSWORD` — `openssl rand -hex 32` (restricted DB login)
- `CONSENSUS_ADMIN_KEY` — `openssl rand -hex 32` (owner-only web Save)
- `CONSENSUS_TG_BOT_TOKEN` — independent bot created by BotFather
- `CONSENSUS_TG_CHAT_ID` — the new supergroup's ID (negative `-100...`)

The existing `DASHBOARD_DB_PASSWORD` remains unchanged. The Compose template
injects only DB/admin values into `web`, and only the independent bot token/group
into the opt-in `consensus-alerts` container. No token is sent to the public API.

After a verified offsite backup, apply the Alembic migration using the normal
migration command with `CONSENSUS_DB_PASSWORD` set; `--migrate` provisions the
new role password. **Do not run this migration on the production VPS as part of
this draft PR**.

For staging (after explicitly approving the deployment), run just the profile:

```bash
docker compose --profile consensus-alerts up -d --no-deps consensus-alerts
docker compose ps consensus-alerts
docker compose logs --tail 60 consensus-alerts
```

With the separate bot present in the target group, visit
`https://onchainflow.app/consensus`, select filters and click **Apply** to preview.
Enter the owner key and click **Save filters for Telegram group**. The website
shows the saved version. This is the **only** action that propagates thresholds to
the bot. It does not replace browsing settings for other visitors.

To pause new notifications immediately, use the same owner Save with the
"Enable group alerts" checkbox OFF. No new queue/send work occurs after the worker
observes the new revision; old pending deliveries are cancelled transactionally.
For a hard emergency stop, stop only `consensus-alerts` (not the scanner/UI).

## Required review and testing

- Migration upgrade/downgrade and role boundary against disposable PostgreSQL.
- Owner Save: real DB role, authenticated JSON, strict Origin/HTTPS, CSRF-safe bearer
  design, wrong key, bad/oversized payload, optimistic version conflict.
- Browser Apply vs Save distinction, no admin credential in browser storage or logs.
- Settings persist across web/bot restarts, no historical blast after enabling.
- Correct same-block/bundle filtering and independent settings across web + sender.
- Group delivery: correct chat ID, proper HTML escaping, links, 429/retry, stale
  chain/dashboard data, PostgreSQL lock, concurrent workers and duplicate protection.
- Recheck public `dashboard_ro` isolation and existing USDC mute.
- Small-VPS resource and API budgets; preserve previous images for rollback.

**Migration collision:** This branch uses revision `0011` after the current `main`
revision `0010`. The unmerged Stage C trading stack separately uses revisions
`0011` onward. Before combining those branches, manually reconcile the Alembic
revision graph and rename/rebase migrations. Do not merge both heads blindly.

**Deployment remains OFF** until independent review, owner configuration, staging
and explicit authorization. No funded trades, Stage C execution flags, or Telegram
`getUpdates` calls are involved.
