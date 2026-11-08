# Wallet submission, nonce/idempotency and the execution RPC (Stage C2, issue #93)

**Status: implemented, default OFF, never enabled by this change. No funded call, no deployment.**
Builds on #92 (`docs/TRADING_WALLET_APPROVALS.md`). The user's wallet is the only signer and the
only broadcaster; nothing here can send, replace or rebroadcast a transaction.

## What is established, and what is not

| Question | Status |
|---|---|
| Does the server need any submission relay / inbox permission? | **No, in wallet-managed mode** (`eth_sendTransaction` goes wallet → the wallet's own RPC). |
| Current Elysium/Agora raw-submission relay or inbox permission and exact status | **NOT ESTABLISHED.** The build sandbox cannot reach external hosts, so nothing was probed and nothing is assumed. Raw-signed submission stays unimplemented and needs its own review (identical-byte rebroadcast only, separate approval). |
| Does the execution RPC provider honour chain 99901 / head freshness? | Enforced in code on every endpoint; the *real* endpoint is unverified. |
| Is any RPC "fastest"? | **Not claimed.** Needs same-workload measurements from the production VPS. |

## Execution RPC (`trading/wallet/rpc.py`)

* separate from the scanner/notifier/Revival RPC; the URL is a secret read only from
  `TRADING_EXEC_RPC_URL` (never config files) and must differ from the scanner/paper URLs;
* **read-only allowlist** (`eth_chainId`, `eth_blockNumber`, `eth_getBlockByNumber`,
  `eth_getTransactionByHash`, `eth_getTransactionReceipt`, `eth_getTransactionCount`, `eth_call`,
  `eth_getCode`, `eth_estimateGas`, `eth_gasPrice`). `eth_send*`, `eth_sign*`, `personal_*`,
  `wallet_*` … are refused *before* any request is made, so there is no code path to broadcast;
* chain id 99901 verified before first use and every 5 min (mismatch ⇒ hard breaker trip);
  head must be ≤ 30 s old to be trusted;
* concurrency cap (semaphore, 250 ms wait ⇒ `RpcBusy`), per-call timeout, bounded body;
* 429/503: `Retry-After` honoured (capped at 5 s) + jittered backoff, then the circuit breaker
  (5 failures ⇒ open 30 s ⇒ single half-open probe). A revert or node error is not an endpoint failure;
* redaction: exceptions, logs and metrics carry only the host name — never the URL, key or body;
* metrics: per-method ok/error/reverted/rate_limited/unavailable counters and a latency histogram.

## Durable legs (migration `0013`, `trading_private.order_legs`)

A *leg* = one thing the wallet was asked to do (`reset`, `approve`, `swap`), created **in the same
transaction** as its `WALLET_REQUEST` audit event (if the database refuses the leg, nothing is
committed). It stores the public request payload (to/value/data/gas caps), the wallet's pending
nonce at prompt time (`nonce_hint`), the prompt block, and later `tx_hash`, `nonce`,
`final_tx_hash`, timestamps. Database-enforced invariants (each tested):

* `idempotency_key` (request id) unique — a duplicate callback cannot create a second leg;
* **one unsettled leg per (wallet, chain)** — the concurrency guard: parallel orders for the same
  account serialize instead of racing a nonce (`wallet_busy`); the cost is that a stuck leg blocks
  that wallet until it is reconciled;
* **one swap leg per order, ever**; `tx_hash`/`nonce`/`final_tx_hash` are set once; legal state
  edges only; rows are never deleted; row-level security per Telegram user.

`OrderLedger.report_wallet_answer` records the wallet's answer, the leg, the execution attempt and
the order move in **one transaction**, exactly once per request id:

| wallet says | leg | swap order |
|---|---|---|
| hash | `hash_reported` | `submitted` (acknowledgment, **not** a fill) |
| rejected | `rejected` | `cancelled` (reason `wallet_rejected`) |
| error / lost answer | `ambiguous` | `ambiguous` — it may have been sent |

A different second answer is a `conflict`; a late hash after a restart or after the deadline sweep
is still accepted (evidence beats the timeout). After an error no new prompt exists: a new
prompt needs a new order, the wallet guard blocks it while the leg is unsettled, and a swap prompt
is issued at most once per order.

## Reconciliation (`trading/wallet/reconcile.py`)

Evidence only; a human or a timer never "decides". Per unsettled leg, using every configured source
(presence from any healthy source counts; **absence needs all sources healthy and agreeing**):

| evidence | verdict | effect |
|---|---|---|
| hash found, from/to/value/input/chain equal | `pending` / `mined` | nonce + `seen_at` recorded; order stays `submitted` (receipts, inclusion, reverts: #94) |
| hash found, payload differs | `mismatch` | leg + order `ambiguous`; wallet stays blocked; `log.error` |
| sources disagree about the hash | `mismatch` | same |
| hash gone, nonce known, identical tx at that nonce | `replaced_same` | speed-up: `final_tx_hash` recorded, **same** logical leg |
| hash gone, nonce known, other tx owns the nonce | `superseded` | provably cannot execute ⇒ leg `superseded`, order `cancelled` |
| no hash, nonce hint consumed, identical request found by block scan | `replaced_same` | lost answer recovered |
| nonce hint consumed by something else / scan window incomplete | `unresolved` | stays `ambiguous`; nothing guessed |
| nothing moved, swap deadline + 90 s passed | `abandoned` | a swap cannot execute after its deadline ⇒ order `cancelled` |
| nothing moved, approval older than 1 h with no hash | `abandoned` | residual risk: a wallet could still broadcast it later; it is finite, exact and router-only |
| source unreachable | `unknown` | **no change** |

Gas/fee differences are *advisory* (the wallet's own fee UI may legitimately differ); a different
sender, target, value, calldata or chain is not.
Updates are optimistic (`expected_version`): two reconcilers or a late wallet answer cannot both
win, and stale evidence is never applied.

## Measurement

`reconcile.timeline(leg, quote_ready_at=…)` reports quote→prompt, prompt→answer (user
confirmation), answer→seen, seen→included and included→settled separately (the last two are
filled by #94). `answer→seen` is *our* first observation, bounded by the polling interval.

## Definition-of-done mapping

Duplicate callbacks, restart, parallel orders, RPC timeouts, externally consumed nonce and
replacement are covered by `tests/test_wallet_reconcile.py` (real PostgreSQL), `test_wallet_rpc.py`
(scripted transport) and `test_wallet_flow.py`. An order stays `ambiguous` until evidence resolves
it, and an unreadable nonce at prompt time refuses the prompt (fail closed).

## Not done / blocked

* Receipts, inclusion/revert/finality, fills, reorgs, user-visible status: **#94**.
* Nothing starts the reconciler loop or builds the RPC at process start; `build_execution_rpc`
  and `LegReconciler.run_once` are ready for #94's worker.
* No owner tool to force-resolve a stuck leg yet (a wallet blocked by `ambiguous` needs evidence or
  a manual DB-level decision by the owner): listed as a gap for #95.
* No funded run, no real RPC, no measurement of any provider.
