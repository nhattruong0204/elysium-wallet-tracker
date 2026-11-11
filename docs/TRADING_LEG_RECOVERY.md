# Owner-audited recovery of stuck wallet legs (#95 F-02)

Status: **OFF unless wallet execution is on. Not deployed. Execution stays OFF.** Nothing in this
change signs, broadcasts, prompts a wallet, requests an approval, creates an order or leg, or
spends. It only *reads* the chain and, on an owner's confirmation, appends audited ledger rows.

## 1. The problem

A wallet answer can be lost, a hash can never appear, a transaction can be replaced, or a reorg can
drop an included swap. The reconciler (`LegReconciler`) and the receipt worker (`ReceiptReconciler`)
settle everything they can **prove**. What they cannot prove stays:

| Leg state | Effect |
|---|---|
| `ambiguous` | holds the **wallet+chain lock** (`ux_legs_wallet_active`); the order is `ambiguous` |
| `orphaned` | outside the lock, but the order is stuck `orphaned` |

Without a tool, an operator would have to edit the database. That is the worse failure: an
unproven release can let a *late-broadcast* transaction land while a second trade runs on the same
wallet. This change adds the tool with the evidence rules built in.

## 2. What it is (and is not)

* `wallet/recovery.py` — `LegRecovery`: `stuck` · `diagnose` · `prepare` · `confirm`.
* `OrderLedger.apply_leg_recovery` — the one write: the leg change + an append-only `leg_recoveries`
  row (exact evidence) + a hash-chained `LEG_RECOVERY` event + the order's cancellation, **one
  transaction**. Migration `0015`.
* Owner Telegram commands (private chat, owner only): `/wallet_recover [leg id]` (read-only) and
  `/wallet_resolve <leg id> <code> <reason…>`.
* It reuses the ledger, legs, reconciler helpers (`expected_of`, `swap_deadline`, `critical`),
  `tx_mismatches` and the read-only `ExecutionRpc`. It adds no signer, no RPC write method and no
  new chain-facing capability: `tx_count` merely accepts a hex block tag (nonce *at* a block).
* It is **not** a force-unlock. There is deliberately no command that releases a lock without proof.

## 3. Diagnosis (read-only)

`/wallet_recover <id>` shows: tx hash, final hash, nonce (and the nonce hint recorded at the
prompt), chain, wallet, prompt block, the last evidence the reconciler recorded, the leg/order
state, the anchor block, each source, and a **category**:

| Category | Meaning | Correction? |
|---|---|---|
| `confirmed` | the request is mined and matches, status 1 | **No** — the reconciler/receipt worker records it (`auto`) |
| `reverted` | mined and matches, status 0 | **No** — same |
| `replaced` | the nonce was consumed by another tx | identical payload (speed-up): No (`auto`). Different payload, ≥ `finality_confirmations` deep: **superseded** |
| `nonce_consumed_externally` | the wallet executed a *different* tx under the reported hash (final), or the hinted nonce went elsewhere | reported-hash case: **superseded**; hint-only: No |
| `rpc_unavailable` | a source did not answer | No |
| `expired` | swap past its on-chain deadline + margin, nothing matching exists | **abandoned** |
| `unknown` | nothing proves the outcome (nonce still free, tx pending, deadline not passed, approval without a deadline, sources disagree) | No — **stays blocked** with an escalation procedure |

A diagnosis never writes. Rendering is escaped and carries an evidence digest.

## 4. The evidence rule (what "independently checked" means)

All of the following, every time, for every correction:

1. **≥ 2 configured sources with distinct hosts AND distinct operators** (`TRADING_EXEC_RPC_URL` +
   `TRADING_CORROBORATION_RPC_URLS`; since F-15 this check, and every read below, is the shared
   layer in `docs/TRADING_RPC_CORROBORATION.md`). Fewer, or two hosts of one operator → diagnose
   only. The evidence is also re-checked by the ledger (`check_chain_evidence`) before any write.
2. **Every** source answers (one outage = no correction), has a head ≤ 60 s old on its own block
   clock, and the heads are within 64 blocks (`corroboration_max_head_skew_blocks`; the lowest head is the anchor).
3. At the **anchor block** (the lowest head) all sources return the **identical header hash** — same
   canonical chain. A fork or disagreement blocks.
4. Facts are re-read from **each** source and must be identical: the wallet's nonce at the anchor,
   and (for a supersession) the consuming transaction, found by bisecting the sender's nonce by
   block on every source, with the same hash/block/block-hash from all. A transaction known to one
   source only never supports a correction.
5. Only **monotone** facts count, so a late event cannot invalidate them:
   * *superseded*: a different transaction occupies the nonce and is ≥ `finality_confirmations`
     deep on the canonical chain (or the wallet's reported hash is a final tx whose payload does not
     match the reviewed request);
   * *abandoned* (swaps only): the swap's decoded on-chain deadline + ≥ 600 s has passed on the
     sources' block timestamps, the wallet has no queued tx (`pending == latest`), and every
     transaction the wallet sent since the prompt block was checked and none matches the request.
     A late broadcast could then only revert.
6. Everything else is blocked. An **approval/reset has no deadline**, so it can never be abandoned;
   it can only be superseded by a proven consumed nonce (otherwise it stays blocked).

## 5. Owner confirmation and audit

* `prepare` returns a **one-time code** (10 hex chars, 5 minutes, newest per owner+leg, in memory
  only) bound to the owner, the leg and a *decision key* (hash of the decisive facts and the leg
  version). Wrong guesses are rate-limited (5 per owner+leg).
* `confirm(leg, code, reason)` needs **a reason of 10–190 printable characters** (a bad reason does
  not burn the code), consumes the code, **re-diagnoses from scratch**, and applies only if the
  fresh diagnosis proposes the **same** decision key. Any change (a tx mined, a nonce moved, a
  source stale, a pending tx, a reorg) refuses with `evidence_changed`/the blocking code.
* The ledger independently refuses: non-owner actors, a stale leg version, a leg not in
  `ambiguous`/`orphaned`, an already-recovered leg, a swap whose order already carries a receipt,
  evidence with < 2 sources / unresolved problems / no consuming tx (supersession) / no passed
  deadline (abandonment), and a reason that is too short.
* **Append-only**: `leg_recoveries` rejects UPDATE/DELETE/TRUNCATE (trigger), grants `trading_rw`
  only SELECT/INSERT, has `UNIQUE(leg_id)` (a leg is recovered **at most once**) and
  `UNIQUE(request_key)` (idempotent replays return `applied=False`). `order_events` gets
  `LEG_RECOVERY` and `LEG_RECOVERY_REFUSED` (every refused confirmation is on the record too).
* **What changes**: only the leg's `state` (→ `superseded`/`abandoned`), `version`, `resolution`
  (`owner_recovery:<action>`) and `resolved_at`; for a swap the order goes
  (…→`ambiguous`→) `cancelled` with the owner's reason. **What never changes**: `tx_hash`,
  `final_tx_hash`, `nonce`, any receipt/block/gas field, fills (incl. rolled-back ones), attempts,
  or any history. An approval leg's order is left untouched.
* The wallet lock is released **only as a consequence of a proven terminal leg state**.

## 6. Cases that stay blocked (by design)

* Outcome unknown: nonce still free, tx pending, deadline (+margin) not passed, wallet has queued
  transactions, too many wallet txs to check (> 10).
* Approval/reset legs without a proven consumed nonce — a late confirmation in the wallet cannot be
  excluded. Escalation: verify in the wallet + explorer, then trade from **another wallet** (the lock
  is per wallet+chain).
* A replacement that is not yet final; a reported hash that is a different tx but not yet final.
* Any source down/stale/forked/disagreeing; a single configured source (F-15 still open).
* Orphaned legs without a proven nonce (the reorged tx could be re-included) unless the swap
  deadline has passed.
* The escalation text always ends with: *lifting a lock without proof is an out-of-band decision for
  the security reviewer; there is deliberately no command for it.*

## 7. Configuration

| Key | Default | Meaning |
|---|---|---|
| `trading.corroboration_rpc_urls_env` | `TRADING_CORROBORATION_RPC_URLS` | Env var holding comma-separated SECRET http(s) URLs (optionally `label=url`) of *additional* read-only endpoints. Must not equal the scanner/paper RPC; hosts AND operators must be distinct (startup is fatal otherwise). **Renamed in F-15** from `recovery_rpc_urls_env` / `TRADING_RECOVERY_RPC_URLS`. |
| `trading.finality_confirmations` | 12 | Reused (depth that makes a consuming tx final) |

The service is built in `run_ui` only when `wallet_execution_enabled` is on. Owner ids come from the
ledger (`TG_CHAT_ID` + `TRADING_OWNER_IDS`); everyone else — and any group chat — gets "Not available."

## 8. Resource use

One diagnosis at a time (`busy` otherwise). a handful of reads per source for the head/anchor/tx lookups + a nonce
bisection (window ≤ 200 000 blocks ⇒ ≤ ~18 probes) per consumed nonce, ≤ 10 nonces, over the shared
execution RPC (existing concurrency cap, timeouts and breaker apply). The worker's own per-minute budget
(`BudgetedChain`) is not consumed, but recovery shares the endpoint, so a diagnosis competes with the
worker for the same concurrency cap.

## 9. Rollback

Turn `wallet_execution_enabled` off (the service is not built) or remove the env var (diagnose-only).
Migration `0015` downgrade drops `leg_recoveries` and its indexes; take a backup first because it is
an audit record. The widened `order_events` kind CHECK intentionally stays (append-only table).

## 10. Unresolved / not done

* Evidence is only as good as the *configured* endpoints. Two endpoints from one upstream provider
  would agree while both being wrong; host and operator distinctness are checked (F-15), but true
  upstream independence and colluding operators cannot be detected in code
  (`docs/TRADING_RPC_CORROBORATION.md` §8).
* Codes live in memory: a restart voids them (safe), but there is no cross-process single-use
  guarantee beyond the database (`UNIQUE(leg_id)`, request-key idempotency, version check).
* The owner's *reason* is free text (printable, ≤ 190 chars) — not verified by the system.
* `ChainSource` is the read allowlist; a malicious endpoint could still lie consistently to every
  configured source. Not mitigated here.
* Not exercised: a real chain/RPC, a real Telegram client, a real wallet.
