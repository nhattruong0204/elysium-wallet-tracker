# Finality on Elysium (chain 99901) — #95 finding F-04

Status: **OPEN.** Interim policy is implemented in code and fails closed. Nothing here has been
measured against a real chain-99901 endpoint: the build environment cannot reach one. The real
numbers come from running `docs/TRADING_RPC_DIAGNOSTICS.md` on the VPS.

## 1. What is documented (sources, retrieved 2026-11-09)

| Fact | Source | Kind |
|---|---|---|
| Chain 99901 is Elysium **mainnet**; 99801 is testnet | dRPC public-endpoint PR, LI.FI deployment PR, StreamingFast Substreams docs (links §7) | third-party, consistent |
| Mainnet launch date 2026-11-09 | Bankless; Kinetiq teaser | third-party |
| Arbitrum **Orbit** chain running Nitro (ArbOS 61 on testnet) | Dwellir "What is Elysium", Kann Audits | third-party |
| Settles on **HyperEVM** (parent chain id 999), HYPE as gas | Dwellir, Kann Audits | third-party |
| **One sequencer**, operated by Conduit; priority-fee ordering; no public mempool | Dwellir, Kann Audits | third-party |
| AnyTrust DA committee, fallback to full data on HyperEVM | Dwellir, Kann Audits | third-party |
| BoLD contracts; validation **permissioned** at launch | Dwellir, Kann Audits | third-party |
| Block time target 100–200 ms (testnet measured 129–136 ms); timestamps can repeat | Dwellir, Kann Audits | third-party measurement |
| Testnet challenge period `confirmPeriodBlocks` = 10 HyperEVM blocks; mainnet not published | Dwellir | third-party |
| Orbit finality levels: `latest` = sequencer soft confirmation (reversible); `safe` / `finalized` = batch posted and the parent block safe / finalized; reorgs bounded by the unfinalized parent window, **not by a block count** | Arbitrum docs "Finality and chain reorganizations" | protocol documentation |
| HyperEVM (HyperBFT): "every committed block is final" | Dwellir Hyperliquid architecture | third-party |
| Whether HyperEVM RPC serves `safe`/`finalized`, and how Elysium nodes map them | **not documented anywhere we found** | unknown |

Kinetiq's own docs (`elysium.kinetiq.xyz/docs`) could not be fetched from this environment, so
none of the rows above is confirmed by the chain operator itself.

## 2. What that means for "12 confirmations"

At 5–10 blocks/s, `finality_confirmations: 12` is **~1.2–2.4 seconds of the sequencer's own
promise**. On a single-sequencer Orbit chain that is not finality: until the batch is posted and
the parent block is final, the sequencer (or a sequencer failover/restart) can still reorder or drop
those blocks. There is **no documented guarantee attached to any confirmation count**, and a week
without an observed reorg would not create one.

| Level | Meaning on Elysium | How the code treats it |
|---|---|---|
| Inclusion (`latest`) | sequencer executed it; reversible | recorded **provisional**; fills provisional; a reorg rolls them back (append-only) |
| N confirmations | N more soft blocks on top | necessary, **not sufficient** |
| `finalized` tag | batch posted to HyperEVM and that block final (HyperBFT) | **required** (default) to finalize a receipt, auto-supersede a leg, or propose an owner recovery |
| Settlement / withdrawal | assertion confirmed after the challenge period | not used: the bot never bridges; matters only for a user withdrawing to HyperEVM |

## 3. Policy now in code (interim, fail closed)

* `trading.finality_confirmations` stays **12** (unchanged).
* New `trading.finality_require_finalized_tag: true` (default). A block is final only if it is
  ≥ 12 deep at the shared anchor **and** at or below the **lowest** `finalized` height reported by
  **every** corroboration source (capped at the anchor). Implemented once in
  `corroboration.Session.finality()`; used by the receipt worker, the reconciler's automatic
  supersession and owner recovery. The ledger re-checks it (`OrderLedger._check_finalized`):
  finalization or supersession evidence without `finality_policy: finalized_tag+depth` and a
  `finalized.number` ≥ the block (and ≤ the anchor) is refused.
* A node that does not serve the tag, answers malformed, or reports `finalized` above its own head
  => **nothing becomes final** (fills stay provisional, locks stay held). That is the safe failure.
* `false` restores depth-only finality; that is a conscious downgrade the security reviewer must
  accept in writing (it is a separate config key so it cannot happen by accident).
* Abandonment (swap) is unaffected: it relies on the swap's on-chain deadline having passed on the
  chain's clock with no queued transaction, so a late broadcast can only revert.

Related fix (F-07): the corroboration head-skew tolerance was 3 blocks (~0.3–0.6 s), which two
honest providers on this chain exceed constantly; it is now
`corroboration_max_head_skew_blocks: 64` (~6–13 s). Safe because the LOWEST head is the anchor —
skew changes freshness, never a fact; every head must still be ≤ 60 s old. The nonce bisection
window is no longer clipped when the prompt block is known (it previously produced false
"consumed before the request" conflicts for legs older than ~200 000 blocks ≈ 6–11 h).

## 4. Required measurements before F-04 can close

Run `rpcdiag` (`docs/TRADING_RPC_DIAGNOSTICS.md`) on the VPS for ≥ 24 h at a low rate and record:

1. `finality_tags`: is `finalized` served by **every** endpoint? lag in blocks and seconds
   (expect minutes if parent-derived; ≈ 0 means it mirrors the sequencer => WARN, policy useless);
2. `finalized_progress`: does it advance steadily? max gap?
3. `same_finalized_ancestry`: same hash at the lowest finalized height on all endpoints;
4. `reorgs_observed`: any hash change at a seen height, with depth;
5. Kinetiq/Conduit statement (written) on sequencer reorg behaviour, batch-posting cadence and
   the Elysium node's parent-chain finality source.

Close F-04 only when 1–3 pass on every endpoint and 5 exists; otherwise keep the tag requirement
and keep F-04 open.

## 5. Interpretation for operators

* A swap shown **included** but not **final** is normal for minutes. Do not treat it as settled
  for accounting; PnL labels it provisional.
* If legs never become final: run `rpcdiag`; the usual cause is an endpoint without the
  `finalized` tag (`finality_tags` FAIL). Replace that endpoint; do **not** flip the policy.

## 6. Residual risks

Single sequencer (censorship / reordering before batch posting); permissioned validation; AnyTrust
committee trust; HyperEVM finality semantics as served by its RPC are unverified; no light-client
or Merkle proof is checked (agreement of operators is the strongest evidence used).

## 7. Sources

* https://www.dwellir.com/blog/what-is-elysium
* https://kannaudits.com/blog/elysium-security-kinetiq-hyperliquid-l2
* https://docs.arbitrum.io/how-arbitrum-works/reference/finality-and-reorgs
* https://www.dwellir.com/docs/hyperliquid/architecture
* https://github.com/drpcorg/public/pull/291 (chain ids 99901 / 99801)
* https://github.com/lifinance/contracts/pull/2613 (chain id 99901)
* https://docs.substreams.dev/tutorials/intro-to-tutorials/elysium (chain id 99901)
* https://www.bankless.com/read/news/kinetiqs-elysium-l2-sets-nov-9-mainnet-launch
