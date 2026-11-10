# Receipt reconciliation, verified fills and truthful status (Stage C2, issue #94)

**Status: implemented, default OFF, never enabled by this change. No funded call, no deployment.**
Builds on #92 (wallet requests) and #93 (legs, nonce/idempotency). The server still never signs,
broadcasts or rebroadcasts; it only *reads* the chain and records what it can prove.

## The rule

> A wallet-reported hash is an acknowledgment. Only a validated on-chain receipt moves an order on,
> and an amount is shown only when two independent on-chain measurements agree. Otherwise it is
> **unknown** — never zero, never an estimate, never the quote.

## What the worker does (`wallet/receipts.py`)

Per leg that has a transaction hash and is not final (`hash_reported`, `included`, `reverted`,
`orphaned`), oldest first, each pass:

1. `eth_getTransactionReceipt` + `eth_getTransactionByHash` + canonical header at the receipt height.
2. **Validate** the pair against the leg (`fills.validate`): receipt hash, `from` = the wallet, `to`
   = the reviewed target, no contract creation, receipt/tx agree on block hash and number, status is
   0x0/0x1, and the transaction's target/value/calldata equal the reviewed request. Wallet-chosen
   gas limit / fee cap are advisory (a wallet may legitimately change them). A pair that fails is
   **never accounted** (`invalid`); the #93 reconciler decides what that means for the order.
3. **Canonical ancestry**: the header at that height must be the receipt's block. No header yet ⇒
   `pending` (node lag is not proof). A different block ⇒ `not_canonical`.
4. Record the inclusion — leg `included`/`reverted`, swap order `included`/`reverted` — with its fill,
   **provisional** until `head − block + 1 ≥ trading.finality_confirmations` (default 12), then
   **final** (order `finalized`). The head used is the execution RPC's freshness-checked head.
5. **Reorgs** (`orphaned`): a recorded block that is no longer canonical *and* whose receipt is gone
   rolls the provisional fill back, marks leg and order `orphaned`, and leaves the transaction to the
   #93 reconciler (it usually returns to the mempool). A receipt in a *different* block is a new
   inclusion: new fill row, old row `rolled_back`. No header **and** no receipt together is node lag,
   not a reorg, and changes nothing.

Operational rules: the worker **runs while trading is paused** (pausing stops new prompts, not the
recording of what already happened); every pass has a hard RPC-call budget (`rpc_budget`, default
120) so a backlog cannot starve the execution RPC; a failing pass never ends the loop
(`receipts.serve`, exponential backoff); two workers racing record exactly one inclusion
(optimistic `expected_version`, `UNIQUE(leg_id, block_hash)`); a restart loses nothing (all state is
in the database).

**Approvals** never produce a fill and never change their order: the approval leg becomes
`included`, the order stays `requires_approval`, and the status reads *"Approval confirmed — refresh
quote required"*. The swap is always a new order/plan.

## Fill accounting (`wallet/fills.py`)

Verified only when **both** measures agree; isolation required.

| | amount from logs | cross-check | 
|---|---|---|
| BUY native spent | `tx.value` (exact) | wrapped-native `Deposit` log of the router equals it |
| BUY tokens received | net ERC20 `Transfer` logs to the wallet | `balanceOf(wallet)` at block N minus N−1 |
| SELL tokens sent | net `Transfer` logs from the wallet | `balanceOf` delta |
| SELL native received | `Withdrawal` log of the router | wallet native balance delta **+ gas** |

* **Isolation**: any *other* transaction from/to the wallet in the same block makes balance deltas
  ambiguous ⇒ fill `unknown` (receipt still recorded). 
* **Fee-on-transfer / rebasing**: logs and balance delta must agree on what the wallet really
  received; a hidden tax or rebase makes them disagree ⇒ `unknown`. A visible tax (extra
  `Transfer` out of the wallet) nets out consistently ⇒ verified at the net amount.
* ERC721 transfers (4 topics) and other tokens' events are ignored.
* **Reverted** swap ⇒ verdict `reverted`: gas only (`gasUsed × effectiveGasPrice`), no amounts.
* Unreadable balance evidence ⇒ the pass *retries* (nothing recorded); after 3 failed passes for that
  inclusion the fill is recorded as `unknown`, because a fill row is immutable and a transient RPC
  error must not become a permanent number.
* Event topics are re-derived with a reference Keccak in CI (`tests/keccak_ref.py`); they are not
  trusted from memory.

## Database (migration 0014)

* `order_legs` + `block_number, block_hash, receipt_status, gas_used, gas_cost_wei, included_at,
  finalized_at`; new leg state `orphaned` (not part of the one-unsettled-leg index).
* **A finalized leg is immutable** (trigger): state, block, status and `finalized_at` cannot change.
* `order_fills`: one row per inclusion (`UNIQUE(leg_id, block_hash)`); CHECKs forbid a `verified`
  row without amounts and amounts on non-verified rows; rows are never deleted/truncated; only
  `status` may change (`provisional → final | rolled_back`); RLS per user; `trading_rw` gets
  SELECT/INSERT and UPDATE(status, updated_at) only.
* Downgrade drops `order_fills` — **take a backup first**; `orphaned` legs revert to `ambiguous`.

## Status and positions (`wallet/status.py`)

| Situation | Label |
|---|---|
| hash reported, no receipt | *Submitted — not filled* |
| approval mined | *Approval confirmed — refresh quote required* |
| swap mined, < finality | *Included on chain — provisional* |
| revert | *Reverted on chain — no tokens moved* (+ gas paid) |
| reorg | *Block reorganized — re-checking* |
| amounts not verifiable | receipt shown, **no fill, no PnL** |

Positions use **average cost** with exact integer (`Fraction`) arithmetic, from `verified` fills
only; `rolled_back` and `reverted` rows are excluded; `provisional` is flagged. Quantity / basis /
realized PnL are `null` (unknown) when any fill for that wallet+token is unverified or a sell has no
verified basis. Gas is reported separately and is not netted into trade PnL, so the number is
reproducible from the fills. There is no unrealized PnL (that needs a price source — not provided).

Surfaces: `/wallet/api/status` returns `status` (label, legs with explorer links, fill, gas);
`wallet.html` has a Receipt card with *Refresh status*; Telegram `/orders` and `/positions` append a
*Wallet-managed orders* section from the ledger (private chat only, per user via RLS, best-effort,
HTML-escaped). The existing PAPER text is unchanged.

## Configuration

`trading.finality_confirmations` (1–256, default 12). **This is an assumption about reorg depth on
chain 99901, not a protocol guarantee** — no finality gadget was queried. Until it is reached a fill
is provisional. Measure observed reorg depth on the production RPC before relying on 12. If the
provider cannot serve `eth_getBalance`/`eth_call` at historical blocks (pruned state), fills end up
`unknown` — receipts and gas are still recorded.

## Not done / not established

* **Nothing starts the worker.** `receipts.serve` exists and is tested but no process wiring calls it,
  and `build_execution_rpc` is not invoked at startup (deliberately dormant; the flag stays OFF).
* Historical-state (`eth_call`/`eth_getBalance` at block N−1) support of the real endpoint: **unverified**.
* Reorg depth and real finality behaviour of the chain: **unverified** (see above).
* Unrealized PnL, USD valuation, fees paid to the router in-protocol: not provided.
* No owner force-resolve tool for a leg stuck `ambiguous`/`orphaned` (planned operator tooling).
* Real wallet/device behaviour: only mocked EIP-1193 providers were exercised.

## Tests

`tests/test_wallet_fills.py` (no DB; accounting, positions, labels, config, keccak-derived topics),
`tests/test_wallet_receipts.py` (real PostgreSQL: provisional→final, revert, approval-only, taxed
and unreadable evidence, reorg + re-inclusion, restart, racing workers, pause, budget, node down,
cross-user isolation, DB immutability/CHECKs, migration round-trip, Telegram section),
plus additions to `test_wallet_api.py` / `test_wallet_browser.py`.
