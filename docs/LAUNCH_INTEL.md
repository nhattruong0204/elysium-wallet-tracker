# Launch intelligence (alerts only)

Five additions to the alert pipeline, motivated by two early plays that pumped (PERPME on a DEX
Agora does not index, VOLTA on a new launchpad). **Nothing here signs or sends a transaction.**
If execution is ever added it must simulate buy *and* sell first, cap size against pool
liquidity, cap slippage and set take-profit levels up front; that is a separate decision.

| # | Feature | Where | Config (`intel:`) |
|---|---------|-------|-------------------|
| 1 | New launchpad / new DEX pool alerts | `collector.py` (Agora launchpad names), `launchintel.LaunchWatcher` (chain `PoolCreated`) | `new_launchpad_alerts`, `launch_watch`, `launch_min_liquidity_hype`, `launch_delay_s`, `launch_factories` |
| 2 | Independent-wallet scoring and tier label | `engine._maybe_cluster`, `launchintel.find_bundle_wallets/count_independent` | `independent_min`, `independent_window_s`, `bundle_*` |
| 3 | Due-diligence block | `launchintel.DueDiligence`, rendered by `cards.dd_lines` | `dd_*` |
| 4 | Venue-aware buttons | `cards.keyboard` / `link_bar` | none |
| 5 | Paper-trading log | `paper_trades` table, `--launch-report` | `paper_enabled`, `paper_slippage_pct` |

## Scoring (2)
Wallets that keep buying the same token in the same block as other tracked wallets are one
operator splitting a buy. A wallet that took part in at least `bundle_min_hits` (5) such blocks
(3+ distinct wallets) over `bundle_lookback_days` is a *bundle wallet*; the set is refreshed
every `bundle_refresh_s`. Known bundle wallets count once, and unknown wallets that bought in the
same block as each other count once. Tiers: `N independent in M min`, `bundle entry (one operator)`.

The plain cluster alert (`cluster_min_wallets` addresses) is unchanged except for the tier line.
When `independent_min` (3) unrelated wallets bought within `independent_window_s` the **strong**
signal (`signals.kind = independent_buy`, once per token per day) fires instead, as one card
marked `✅ independent`, and opens a paper trade. Several addresses from one operator never
reach it, and no longer hide it either: the strong signal has its own cooldown.

## Due diligence (3)
Rendered at send time for clusters, launch alerts and first buys. Read-only JSON-RPC, at most
`dd_budget_s` seconds per token, cached `dd_cache_s`, and paused 60 s after any RPC failure.
Lines: pool or bonding curve and the HYPE it holds (the ceiling for sellers), router, largest
holder / owner share, wallets that sold into the pool in the last `dd_log_blocks` blocks, and
mint/pause/blacklist/upgrade functions in the (clone-followed) bytecode. A line that could not
be read is omitted and the card says so; **missing never means safe**. Not covered: who holds
the LP position (a locker contract is common) and honeypot simulation (a failed *future* sell
cannot be seen from past transfers).

## Venues (1, 4)
Agora launchpad names are remembered in `launch_venues`; the first run only seeds, later new
names alert once. The chain watcher scans `PoolCreated` (any factory unless `launch_factories`
is set), pairs the token with WHYPE, waits `launch_delay_s`, and alerts once if the pool holds at
least `launch_min_liquidity_hype` HYPE and the token is not an old Agora token. Factories seen
this way also feed the due-diligence pool lookup. For launchpads other than Kleos, BOXA and
ElysPump the Rabby/OKX/Kleos/BOXA buttons (dead ends there) are replaced by Pool/Curve, Router,
Token and Tx links on Elyscan.

## Paper trading (5)
Entry is the executed price of the buy that completed the signal, plus `paper_slippage_pct`.
Later prices come from `token_market_snapshots` (one per ~10 min), so peaks can be missed.
`maint steps=launch-report` (or `python -m agora_tracker --launch-report`) prints peak,
+1/6/24/48 h results and the share of signals now below entry. Judge it only after dozens of
signals; the manual sample that motivated it was 16 tokens over 2 days.

## Operations
Migration `0006` adds `launch_venues` and `paper_trades` (private, no `public_api` view). Because
the tracker and UI refuse to start on a schema mismatch, deploy recreates both (a few seconds of
Telegram downtime; the cursor resumes without replay). `web` is unaffected. Take a verified
backup first.
