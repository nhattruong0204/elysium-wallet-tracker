# PAPER trading operations — Issue #82

## Current capability

PR #83 is merged. Draft #84 adds a read-only PAPER provider; it is not deployed.
`trading.paper_enabled` remains **false by default**. No signer, approval sender or
broadcaster exists. Existing scanner/outbox delivery, PostgreSQL and Revival are unchanged.
Do not restart production bots or enable PAPER there without the reviewed release window
and explicit authorization. Passing CI is not independent security approval.

Supported pool family: single-hop HYPE ↔ ERC-20 through Agora's canonical Uniswap V2
router/factory, with exact runtime checks, canonical ABI and the verified token pair.
Other factories, curves, hooks, V3/V4, multi-hop and wrapped/native conversion refuse.
Every quote rechecks chain 99901, head freshness, code identity, pair tokens/factory,
reserves, decimals, watch-wallet balances and, for sells, allowance at one pinned block.
Limits: 1 HYPE buy cap, 1% price impact including the 0.3% pool fee, 0.5% default
slippage, 5 HYPE minimum native reserve, 30-second quote validity and three-minute session.
Nothing silently raises a cap. Unsupported decimals, missing pools, stale heads, bad code,
insufficient funds, mismatched quotes and unexpected simulation failures refuse.

The public Agora v1 builder is used for **labelled API comparisons** for the same input
and supplied watch wallet. Its returned destination/chain/value/amounts/minimum and finite
approval plan are checked. Its opaque aggregator calldata is not accepted as a verified
route: the canonical outer ABI/source identity is still unresolved. Agora fee and expected
output are API reports, possibly from a different block; they are not independent proofs.

## Local evaluation

Use `LOCAL_WSL_SETUP.md`. Do not reuse production credentials or a production database.
For a separately authorized local PAPER UI, configure a private journal and dedicated RPC:

```yaml
trading:
  paper_enabled: true        # local opt-in only; shipped default remains false
  paper_order_path: /absolute/private-directory/paper.sqlite
  paper_rpc_url: https://rpc.agoradex.app
```

The UI user must own the journal directory (0700); the regular file is 0600. Invalid
journal setup pauses PAPER while leaving normal UI initialization available. The journal
is separate from PostgreSQL and dashboard views. No new migrations are needed. The existing
IntelBot remains the only Telegram update consumer. Local handler demonstrations use a fake
sender and send no Telegram messages. Do not start a second consumer for a production bot.

Private commands / interactions:

1. `/paper_wallet ADDRESS` saves a public **watch-only** address for the caller. It proves
   no wallet ownership or spending authority. Changing it invalidates existing selections
   and reviews. The association persists privately and never appears in group alerts.
2. Open Buy/Sell from an existing alert, or `/trade TOKEN buy` / `/trade TOKEN sell`.
3. Choose 0.1/0.5/1 HYPE or a custom HYPE amount; sells offer 25/50/100% or a custom percent
   of the real wallet balance. `/paper_amount SESSION AMOUNT` accepts exact decimal input.
4. Review / refresh fetches real pool and balance reads and proposes an unsigned payload.
   The panel shows decimal amounts, minimum, pool fee, impact, wallet, timestamp/age,
   gas reserve when available, simulation state and a separately labelled Agora comparison.
5. Confirm records a hypothetical scenario only; Cancel always remains available, including
   while paused. `/pause_trading` blocks new actions until restart. `/orders` shows only the
   caller's latest ten persisted scenario details. `/paper_balances TOKEN` reads real balances;
   these are not simulated positions. No realized PnL, actual fill or live receipt is fabricated.

Quote/confirmation tokens are one-time, user-bound and rotated on refresh. Wallet/amount
changes, cancellation, expiry, pause during I/O and nested payload mutation invalidate the
review. Network I/O holds no global session lock or scanner transaction. Dedicated quote
requests fail busy rather than queue; reads have per-call timeouts and total request budgets.

## Simulation and missing allowances

BUY and already-approved SELL use the canonical fee-on-transfer-supporting methods so
`eth_call` enforces the recipient's minimum at the pinned block. `eth_estimateGas` runs
on the **same payload**, with a 20% gas-unit reserve and a 0.01 HYPE reserve cap. No state
overrides, approvals, signatures or transaction submissions are performed.

These methods return no delivered amount: the expected output remains the pool's quote,
assuming normal token transfers. Taxed/restricted tokens can differ or fail. A successful
call proves the minimum check at that block, not a guaranteed future fill or sellability.
The exact delivered amount is unavailable without an actual transaction/receipt.

If a SELL lacks allowance and its actual payload reverts, the panel explicitly offers a
**conditional PAPER scenario** (`hypothetical_requires_approval`), not a successful
simulation. It shows the finite amount/spender required, unknown gas, and transfer/tax
uncertainty. A transport failure never becomes this state. Recording the scenario sends
no approval and does not imply that a live order is ready. Other simulation failures refuse.
Larger sell presets may exceed the impact cap; choose a smaller custom percentage.

## Reproduce read-only verification and measurements

```bash
.venv/bin/python scripts/verify_trade_contracts.py --pair PAIR_ADDRESS
.venv/bin/python scripts/trade_paper_bench.py --wallet PUBLIC_WALLET \
  --token TOKEN_ADDRESS --buy 0.0001 --sell-percent 1 --samples 3
```

The verifier checks the published artifact SHA-256, ABI membership and deployed runtime,
filling only the canonical router's declared factory/wrapped-HYPE immutable references.
The bounded benchmark invokes the actual Telegram handler with a fake sender, persists
PAPER records in a private temporary journal and reports sanitized latency/outcome data.
Wallet addresses, balances and complete private quote snapshots are excluded from output.
It does not measure Telegram network delivery or user dwell. See `RPC_BENCHMARK.md`.

## Release / rollback

Keep #84 draft pending review. No live rollout is authorized. Preserve both bot processes,
DB data/checkpoints/outbox and OPS_REQUEST. An authorized later PAPER rollout needs a
private persistent journal mount, health baseline and rollback image/config. A future live
execution service needs independent wallet authentication, signer threat review, least-
privilege storage, approvals, nonce/reorg/reconciliation, backup and stop procedures from
`TRADING_SECURITY.md`, followed by explicit capped live-test and enablement approval.
