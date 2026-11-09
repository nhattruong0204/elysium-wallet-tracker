# Corroborated RPC evidence (Stage C2, #95 finding F-15)

Status: **mitigated in code, awaiting independent verification.** Real-provider independence has
**not** been demonstrated; see §8 and §9. Execution flags stay OFF. Nothing here signs, approves or
broadcasts.

## 1. The problem

Before F-15 the receipt worker read the receipt, the block and the balances from one execution RPC,
and a fill was `verified` when two *accounting methods* (transfer logs vs. balance delta) agreed.
One dishonest or broken provider can return a fabricated receipt **and** consistent fabricated logs
**and** consistent fabricated balances; method agreement alone cannot detect that. Owner recovery and
the reconciler had their own, different multi-source checks. F-15: nothing may be finalized,
certified as a fill, or allowed to release a wallet lock because one provider says so.

## 2. One shared layer

`agora_tracker/trading/wallet/corroboration.py` is used by the receipt worker, the auto-reconciler
and owner recovery. There is no second implementation.

* `Corroborator`: stateless factory over >= 2 configured sources.
* `Session`: one unit of work (a worker pass or one diagnosis). Holds a **call budget** (default 120,
  `trading.corroboration_calls_per_pass`) and a **wall-time budget** (20 s). Not thread-safe.
* Errors: `Unverifiable` (cannot tell: missing/late/rate-limited/malformed/unsupported) and
  `Conflict` (sources positively disagree). Both are `TradeRefused`; both change **nothing** in the
  ledger except that a conflict appends a `chain_evidence` row of kind `conflict`.

### What a session verifies, in order

1. **Independence** (configuration): >= 2 sources, distinct host names AND distinct *operators*
   (registrable domain, or an explicit label), each with declared and verified `chain_id == 99901`.
   Otherwise `sources_not_independent` / `needs_two_independent_sources`.
2. **Heads**: every source answers; each head is <= 60 s old on the *block clock* (not the local
   clock) and heads are within `corroboration_max_head_skew_blocks` (64; Elysium makes 5–10 blocks/s) of each other (`stale_head`, `heads_diverge`).
3. **Anchor**: the lowest head. Every source must return the **same header hash at the anchor**
   (`sources_disagree_on_chain` otherwise: fork or lying provider).
4. **Same-state reads**: every fact is read at a block <= anchor, so a provider that is merely
   ahead or behind is never misread as disagreeing. A transaction mined above the anchor is
   `ahead` (retry later), not a conflict.
5. **Unanimity**: tx identity (`hash, from, to, value, input, nonce`), receipt (status, block hash and
   number, gas, logs core), inclusion block, canonical hash of that block, wallet nonce at block
   heights, balances at N-1 and N. One differing source is a `Conflict`.
6. Historical state: a source that cannot answer `eth_call`/balance at a past block, errors, or
   returns a malformed value makes the evidence `Unverifiable` (`historical_state_unavailable`).

## 3. Method agreement vs. source agreement (fills)

A fill is `verified` only if **both** hold, and the ledger checks both:

| Property | Meaning | Recorded as |
|---|---|---|
| Method agreement | logs == balance delta; native == tx value; withdrawal == native delta + gas | `FillRecord.method_agreement`, `chain_evidence.method_agreement` |
| Source agreement | every input of both methods read through a corroborated `Session` (>= 2 independent operators identical at one anchor) | `Session.corroborated`, `evidence.fill.inputs_source_agreed`, `source_agreement` |

`compute_fill` returns `unknown` for any source object that is not a corroborated session.
Balance evidence that cannot be read is retried (3 passes) and then recorded as `unknown`, never
guessed. The numbers of an `unknown` fill are NULL.

## 4. What the ledger enforces (not just the callers)

`trading/ledger/evidence.py::check_chain_evidence` runs inside the ledger transaction for
`apply_receipt`, `apply_reorg`, and `resolve_leg` (terminal states, or whenever a `final_tx_hash`
is recorded) and for owner recovery:

* schema 1, `chain_id` 99901, an anchor, >= 2 sources with distinct names **and** operators,
  `source_agreement is True`, no `failed`/`problems`, `taken_at` <= 300 s old (30 s future skew),
  integer `finality >= 1`, <= 24 000 bytes;
* facts must name this leg, its **current version** and the outcome being written (no replay onto
  another leg or an older state), and for receipts match block number/hash/status/tx hash;
* finalization requires `confirmations >= evidence.finality` at the shared anchor. The finality
  depth is unchanged (default **12**, `trading.finality_confirmations`);
* `superseded` needs a consuming transaction with `confirmations >= finality` and non-empty request
  mismatches; `abandoned` needs an empty wallet queue and the **chain's** timestamp past the swap
  deadline + margin (>= 60 s), or the "approval never sent" policy with age >= 600 s.

Storage: `trading_private.chain_evidence` (migration 0016), append-only (trigger + no
UPDATE/DELETE/TRUNCATE; `trading_rw` has SELECT, INSERT only; RLS). Kinds `inclusion | final | reorg |
resolution | conflict`. Conflicts are deduplicated per (leg, code, hour) and capped at 50 per leg.
`order_fills.evidence_id` + `ck_fill_verified_evidence` (NOT VALID, so old rows are untouched): the
database itself refuses a new `verified` fill without evidence.

## 5. Behaviour table

| Situation | Result |
|---|---|
| One source down / rate-limited / malformed | `unverifiable`; nothing recorded; retried next pass |
| One provider behind the others | anchor = lowest head; tx above it is `pending` (not conflict) |
| Providers on different forks | `sources_disagree_on_chain`; nothing recorded |
| One provider hides or alters a receipt | `Conflict`: `conflict` evidence row, state untouched |
| Two hosts of one operator | `sources_not_independent`; nothing recorded or released |
| Historical balance unsupported by a source | 3 retries, then `unknown` fill; inclusion still recorded |
| One provider fabricates consistent balances | never `verified` (`sources_disagree_on_balance`) |
| Reorg corroborated by all sources | `orphaned`; the verified fill is kept `rolled_back` (append-only) |
| One provider says "reorged" | conflict only; never orphans |
| Receipt missing everywhere but the block is unchanged | conflict (`receipt_missing_in_canonical_block`) |
| Re-inclusion after a reorg | a **new** fill row (keyed by block hash); the old one stays |
| Restart / retries / 6 concurrent workers | one inclusion, one evidence row, no new leg or attempt, no resubmission |

## 6. Bounds

Each worker pass uses one session: <= 120 calls and 20 s (config 10..2000 calls). Each source also
keeps its own per-minute budget (`worker_rpc_calls_per_min`) so one noisy source cannot starve the
others or the shared VPS. Cost is roughly **2x** the previous RPC use (every fact from two sources)
plus a few header reads. A budget overrun is `unverifiable` and records nothing.

## 7. Configuration

```yaml
trading:
  execution_rpc_url_env: TRADING_EXEC_RPC_URL
  execution_rpc_operator: ""                         # optional label; default = registrable domain
  corroboration_rpc_urls_env: TRADING_CORROBORATION_RPC_URLS   # replaces recovery_rpc_urls_env
  corroboration_calls_per_pass: 120
```

`TRADING_CORROBORATION_RPC_URLS` is a comma-separated list of SECRET urls, each optionally
`label=url` (e.g. `bravo=https://...`). Startup is fatal for: the scanner/paper RPC, a duplicate
url, a duplicate host, a duplicate operator. Fewer than two sources is allowed (diagnosis still
works) but nothing safety-critical will ever be recorded: the system fails closed. **Breaking
rename:** `recovery_rpc_urls_env` / `TRADING_RECOVERY_RPC_URLS` (F-02) -> `corroboration_rpc_urls_env`
/ `TRADING_CORROBORATION_RPC_URLS`.

## 8. Provider-independence assumptions (what code cannot prove)

* Distinct host and distinct operator are *configuration-level* checks. Two differently named
  operators can run on the same cloud, front the same upstream node, or buy from the same
  aggregator. **Providers that are colluding, or that share an upstream, cannot be detected** by this code; if both
  return the same fabricated chain, the evidence is accepted. A label (`label=url`) is the
  deployer's attestation that the operator is independent and is not verified.
* Operator identity uses the registrable-domain heuristic (last two labels); multi-part public
  suffixes (e.g. `co.uk`) need an explicit label.
* There are no cryptographic inclusion proofs (no light-client / Merkle-Patricia receipt proofs).
  Agreement of providers on the header hash is the strongest check.
* Mitigation: use >= 2 operators that you have reason to believe run separate infrastructure; keep
  a third honest source in mind for disputes; run your own node when stakes justify it.

## 9. Situations that must stay `unknown`

* Fewer than two independent sources, or any source failing, stale, forked, or rate-limited.
* Sources disagree on anything (identity, receipt, balances, nonces, queue state).
* Historical state unsupported (non-archive nodes, pruned state, a node ignoring the block
  parameter is **not** detectable except by disagreement with the other source).
* Other transactions touching the wallet in the same block (balance delta not attributable).
* Wallet queue not provably empty on every source, or too many wallet nonces to scan (> 10).
* Evidence older than 300 s at write time, or facts about an older leg version.

## 10. Provenance and freshness

Every accepted write stores: anchor number/hash/timestamp, each source's name, operator, head, chain
id, reads performed (up to 64), call count, `taken_at`, finality, facts, and the SHA-256 digest of
the canonical evidence. Evidence is only valid for 300 s and only for the leg version it names.

## 11. Residual risks / not done

* Real-provider independence, archive support, and "ignores block parameter" behaviour untested on
  live endpoints (see the validation checklist in the PR).
* `finality_confirmations` (12) now also gates automatic supersession by the reconciler (a
  consuming transaction must be that deep at the shared anchor); the depth itself is unchanged.
* F-04, F-17, F-18 and #96 are not part of this change.

## 12. Finality and fast blocks (F-04 / F-07 follow-up)

* Finality is `Session.finality()`: ≥ `finality_confirmations` (12, unchanged) at the anchor AND,
  by default, at or below the lowest `finalized` tag of every source (`docs/TRADING_FINALITY.md`).
  Evidence records `finality_policy` and `finalized`; the ledger re-checks both.
* Head skew tolerance is 64 blocks (was 3, which two honest providers on a 5–10 blocks/s chain
  exceed all the time, making the system fail closed permanently).
* Provider measurements and the independence inventory: `docs/TRADING_RPC_DIAGNOSTICS.md`.
