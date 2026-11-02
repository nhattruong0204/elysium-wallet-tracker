# Elysium Intelligence Center (Telegram UI)

Interactive menu, analytics and exports over the data the tracker already stores.
Runs as its own service (`ui`), next to the unchanged `tracker`.

## Using it

Send `/start` (or `/menu`). A persistent keyboard and the home card appear:

| | | |
|---|---|---|
| 📊 Market Overview | 🧠 Smart Money | |
| 🚀 Trending Projects | 🐳 Top Traders | |
| 📈 Top Gainers | 📉 Top Losers | |
| 🔍 Search Wallet/Token | 📤 Export Report | |
| ⚙️ System Health | 🕒 Time Range | |

Commands (also in Telegram's command menu, registered with `setMyCommands`):
`/menu /market /smart /trending /traders /gainers /losers /search <q> /export /range
/health /status /help /cancel /token <addr> /wallet <addr>`.

Anything you type that looks like an address or a token symbol is treated as a search.

### Time range

* Presets: 15m, 1h, 6h, 24h (default), 7d, 30d. Presets **roll forward**: “Last 6 hours”
  always ends now.
* **Custom**: the bot asks for the start, then the end, in `Asia/Ho_Chi_Minh`
  (`2026-11-01 08:00`). Custom ranges are fixed and never moved to the present.
* Every report covers **[start, end)**: the start is included, the end excluded.
* The selection is stored per user (`ui_sessions`) and kept while you navigate and across
  restarts. Changing it returns you to the view you were on.
* Each view shows the window, its time zone, the last indexed block, the indexing lag,
  how fresh the data is, and coverage: ✅ full, ⚠️ partial (window starts before tracking
  began, or the indexer is behind), or ⛔ none.

### Export

📤 → report type → period → CSV or Excel. The file is sent to the chat with
`sendDocument`, for example `elysium_smart_money_2026-11-01_2026-11-02.xlsx`.

| Type | Sheets / tables |
|---|---|
| Market Overview | Summary, Token Flows |
| Smart Money | Smart Money, Clusters |
| Trending Projects | Trending (with every score component) |
| Gainers / Losers | Gainers, Losers |
| Top Traders | Top Traders |
| Wallet Transactions | Transactions (asks for the address) |
| Token Activity | Token Wallets, Transactions (asks for the address) |
| Complete Intelligence Report | Summary, Smart Money, Trending, Gainers, Losers, Top Traders, Transactions, Data Quality |

Excel files have styled headers, filters, frozen header rows, column widths and number
formats. A CSV of a multi-table report is a `.zip` of CSVs.

* Times in files are **UTC** (`*_utc` columns). The display zone is recorded in Summary
  and Data Quality.
* `hype_wei` and `qty_raw` are exact integers (strings). The HYPE columns are exact
  decimals in the file; Excel displays about 15 significant digits.
* Text starting with `= + - @` is prefixed with `'`, so cells cannot run as formulas.
* Limits are configurable (`ui.report_max_rows` per table, `ui.report_max_bytes` per file);
  truncated tables say so. Exports run one at a time (`ui.report_workers`) with a small
  queue, so they cannot starve the indexer. Temporary files are deleted after sending.
  Job metadata goes to `report_jobs`; files are never stored.

## What the numbers mean

| Term | Definition |
|---|---|
| Tracked wallets | Wallets the tracker followed when the trade happened: Agora's top-100 PnL leaderboard plus `watchlist.txt` plus wallets added with ⭐ in the bot. **Not chain-wide.** |
| Qualified | In the latest accepted leaderboard snapshot taken before the window ended, with Agora PnL > `ui.qualified_min_pnl`. Without a snapshot, results are labelled **unscored** and every tracked wallet counts. There is no separate wallet quality score; rank and Agora PnL are shown instead. |
| ✓ Verified | Trades whose HYPE leg was measured on-chain (`confidence = high`). |
| Estimated | HYPE inferred, not measured. Always shown separately, never added to verified totals. |
| Transfers | Never counted as buys or sells. |
| Agora PnL | Agora's own all-time accounting, in HYPE. |
| Realized | PnL this bot measured from closing trades inside the window (`realized_pnl`, status `measured`). Never mixed with Agora PnL. |
| Market data | Price, market cap and chain-wide volume come from Agora (`/api/tokens`), are labelled “live now”, and are indicative. Agora does not publish liquidity. |

Each (tx, wallet, token) leg is stored once (`uq_trades_leg`), so nothing is counted
twice. Different addresses may belong to the same owner; the bot never assumes they are
independent.

### Smart Money tabs

Net (qualified verified net buying), Buyers (distinct qualified buyers), New (bought by
wallets that entered the leaderboard during the window), Repeat (qualified buys in at
least 2 of 4 equal sub-intervals), Exits (qualified wallets net selling), Clusters (the
tracker's cluster signals).

### Gainers / Losers

Price returns come from, in order:

1. Agora's own change for exactly that rolling window (1h, 6h, 24h or 7d ending now).
   Population: top 200 tokens by that window's volume, chain-wide.
2. Stored snapshots (`token_market_snapshots`), when the first and last snapshot in the
   window fall within `max(1.5 × collector interval, 5 % of the window)` of its edges and
   there are at least `ui.min_observations` points.
3. Agora candles, for windows starting within the last 7 days: about 1-minute (1h),
   15-minute (24h) or 84-minute (7d) buckets. Tokens launched inside the window are
   excluded because they have no start price.

If none of these covers the window, the view says so. A 24h change is never stretched to
cover another window. Filters: `min_volume_hype`, `min_mcap_hype` and `min_age_h`; the
number of tokens each filter removed is shown. Other tabs: Volume (window volume against
the average rate of the longer window) and Smart flow (tracked-wallet net flow).

### Trending score (0–100)

| Component | Points |
|---|---|
| Qualified tracked buyers in the window (5+ = full) | 30 |
| Qualified verified net buying (20 HYPE+ = full) | 15 |
| Volume acceleration (window volume against the longer window's average rate; 3x+ = full) | 15 |
| Buy pressure (24h buys / trades above 50 %) | 10 |
| Trading frequency (log of 24h trades; 1,000+ = full) | 10 |
| Recency (launched under 72h ago, linear) | 10 |
| Price momentum in the matched window (+100 % = full) | 10 |
| Wash risk (average trade under 0.05 HYPE) | −10 |

Each entry lists the components that apply and flags such as thin markets or one wallet
accounting for most tracked buying. For a historical custom window, Agora's live market
statistics don't apply, so only the tracked-wallet components (max 45) are scored, and
the view says so. Holder growth is not scored because no holder history exists.

## Architecture

```
Telegram ──getUpdates──► ui service (python -m agora_tracker --ui)
                          ├─ tgui.IntelBot   poll loop, auth, sessions, routing, exports
                          │    ├─ tgviews    pure renderers (HTML + inline keyboards)
                          │    ├─ reports    read-only SQL (statement_timeout)
                          │    └─ export     CSV / XLSX
                          └─ collector       Agora snapshots every 10 min
tracker service (unchanged) ── scanner, PnL, signals, outbox ──sendMessage──► Telegram
                 PostgreSQL ◄────────── both services
```

* **One update consumer.** Only the `ui` service calls `getUpdates` (long polling, no
  webhook; an existing webhook is removed at startup). The tracker only sends alerts. The
  update offset is persisted (`service_state.tg_offset`).
* The UI never takes the indexer's advisory lock. Reports run in `READ ONLY` transactions
  with `statement_timeout = ui.query_timeout_ms`, using parameterized, time-bounded SQL
  with LIMITs. The UI writes only to `ui_sessions`, `report_jobs`, `wallet_watchlist`
  (source `telegram`), market snapshots and its heartbeat.
* **Concurrency:** `ui.workers` handler threads, serialized per user; exports on
  `ui.report_workers` threads with `ui.report_queue` waiting slots.
* **Authorization:** `TG_CHAT_ID` (your private chat id is your user id) plus
  `TG_ADMIN_IDS` (comma-separated). Anyone else gets one refusal per hour and no data.
* **Callbacks** are at most 64 bytes, validated server side, and always answered.
  Malformed or stale buttons show “expired” and the home screen. Navigation edits the
  message in place, falling back to a new message if the edit fails.
* **Restart recovery:** sessions and the offset persist. Exports that were running are
  marked failed and the user is told to rerun them.
* **Wallets watched from the bot** (⭐) are tracked within one leaderboard refresh (about
  5 min) and are not removed by `watchlist.txt` syncs.

## Deploy / rollback

Deploy as usual (`deploy` action or `docker compose up -d --build`). Migration `0003` is
additive (new tables, one index, one extra watchlist source), so the running tracker is
unaffected. After the deploy, send `/start`.

Add the optional repo secret `TG_ADMIN_IDS` to authorize more users. `TG_CHAT_ID` must be
known: the deploy keeps the VPS's existing value. Because the UI now consumes updates,
auto-detection from `getUpdates` only works before the UI's first run.

Rollback, from the least to the most invasive:

```bash
docker compose stop ui            # 1. switch the menu off; tracker and alerts keep running
git checkout <previous tag>       # 2. previous code (the tracker ignores the new tables)
docker compose up -d --build tracker
# 3. only if you also want the schema back at 0002 (drops snapshots, sessions and
#    report jobs; trades and alerts are untouched), run this with the NEW image:
docker compose run --rm --entrypoint python tracker -c \
  "from agora_tracker.db import make_engine; from agora_tracker.migrate import downgrade; downgrade(make_engine(), '0002')"
```

Disk: about 22k snapshot rows a day (top 150 tokens every 10 min), pruned after
`collector.retention_days` (30).

## Known limitations

* Wallet metrics cover tracked wallets only, starting when tracking began (see the
  coverage line). All-time wallet returns are never inferred.
* USD totals are not shown: there is no time-aligned HYPE/USD history. Token cards show
  USD at the current rate, and say so.
* Historical price returns start when the collector first runs. Before that, only Agora's
  rolling windows and candles (last 7 days, bucket precision) are available.
* Agora publishes neither liquidity nor holder history, so neither is used for ranking.
* PDF export is not implemented (CSV and XLSX are).
