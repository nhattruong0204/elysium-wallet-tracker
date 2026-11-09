# Read-only routing/RPC benchmark

Run `python scripts/trade_rpc_bench.py --config /config/config.yaml --rpc
https://rpc.agoradex.app --rpc https://rpc.eleos.fun --samples 3 --location production-vps`.
The config supplies the actual Kleos/BOXA/ElysPump candidates. No premium RPC is assumed.
Each endpoint receives at most 1 + samples * (2 + workload count) requests (samples <=20,
endpoints <=8, optional workloads <=4), sequentially, with persistent HTTP connections,
at least 0.5 seconds spacing, 5 second timeouts and no retries. Stop on HTTP 429.

The default mix is chain ID followed by latest block and AgoraRouter bytecode reads.
Wrong chains are rejected before further calls. Report p50/p95/p99 per method and success/
error counts; small sample tail statistics are descriptive only. Heads are sampled at
different times, so relative lag includes sampling skew. The current script reports own-head timestamp recency separately and never qualifies
cross-provider freshness from sequential samples.
Output contains public host labels and aggregate values; custom/premium hosts are redacted
because hostnames may contain API keys. Never record URL paths, query keys, credentials,
raw RPC errors, payloads or signed bytes. Router code SHA-256 is a reproducibility aid,
not Ethereum's codehash or proof of an audited contract.

Optional `--workloads FILE` accepts a reviewed list of named `eth_call`/`eth_estimateGas`
transaction payloads. Every endpoint receives exactly the same supplied payloads. Refuse
write methods. Quote and gas results require an independently verified actual swap payload;
do not substitute a plain transfer estimate for swap gas or a token read for a swap quote.

## Measurements

VPS run [42345299584](https://github.com/nhattruong0204/elysium-wallet-tracker/actions/runs/42345299584)
completed November 7, 2026 at 13:04 GMT+7 (06:04 UTC) from the existing VPS network
(physical region not independently established). Six candidates each received 7 requests,
42 total. All returned chain 99901 and nonempty router bytecode. No timeout/rate-limit
errors occurred in this small sample. The observed router code digest matched across nodes;
this is not ABI/security verification.

| Host | Latest block p50/p95/p99 ms | Router code p50/p95/p99 ms | Own head age seconds | Requests |
| --- | --- | --- | --- | --- |
| rpc.thekleos.pro | 316.04 / 688.29 / 688.29 | 325.97 / 411.41 / 411.41 | 3 | 7 |
| rpc.thekleos.xyz | 252.73 / 319.49 / 319.49 | 341.41 / 379.81 / 379.81 | 1 | 7 |
| rpc-live.boxa.club | 294.74 / 560.94 / 560.94 | 290.95 / 376.87 / 376.87 | 3 | 7 |
| rpc-live.elyspump.com | 74.43 / 2949.63 / 2949.63 | 78.72 / 3498.23 / 3498.23 | 2 | 7 |
| rpc.agoradex.app | 211.81 / 212.9 / 212.9 | 233.6 / 238.49 / 238.49 | 1 | 7 |
| rpc.eleos.fun | 260.71 / 293.31 / 293.31 | 258.57 / 262.16 / 262.16 | 2 | 7 |

Raw sanitized results: `docs/benchmarks/rpc-2026-11-07.json`.
The phase label follows the announced launch date; it does not verify actual transaction
submission permissions, inbox behavior or sequencer readiness.
The initial script's `freshness_qualified` flag combines own head age with absolute head
differences sampled sequentially. Most flags were false even with fresh local timestamps;
time skew can explain that difference. It must not be used to select an execution endpoint.
Future pools require synchronized references and per-request freshness checks. The lowest
median also had multi-second maxima; no globally fastest provider is selected.
 Location, observation time, request mix/count and launch phase are in JSON
output. Before declaring a preferred provider, repeat with >=20 samples, representative
verified routes/sizes, provider budgets, actual fees/net output and both launch phases.
No globally fastest RPC is selected from these initial observations.

## Not measured / gates

Agora vs direct quote/net-output comparisons, swap gas, signed-send acknowledgment,
inclusion, confirmation and finality are unrun. An authorized Agora public v1 quote/builder API is now documented (see WSL evidence
below); its returned transaction semantics have not been independently verified. ABI/permissions/soft-launch inbox behavior are unresolved;
no signing or broadcast probes are permitted. A consented tiny live test is a separate
security-reviewed operation requiring explicit limits and owner authorization.

## WSL measurements — November 7, 2026

`docs/benchmarks/rpc-wsl-2026-11-07.json` was generated at 14:21:45 GMT+7 from
Ubuntu 24.04 / WSL2 local networking (physical egress region unverified). Ten samples
per method were requested; 115 requests actually ran across six candidates. All six
reported chain 99901. Five completed ten latest-block and ten code reads with matching
router code digests. ElysPump returned HTTP 429 during its ninth latest-block read;
the script immediately stopped that endpoint and did not sample its code. No retries.
A preliminary connectivity run used the earlier script; this published artifact uses
the corrected freshness/phase semantics. These runs share a network and are not
independent measurements or provider-capacity tests.

| Host | Latest block p50/p95/p99 ms | Requests | Latest-block errors | Own head age s |
| --- | --- | --- | --- | --- |
| rpc.thekleos.pro | 171.39 / 177.26 / 177.26 | 21 | 0 | 3 |
| rpc.thekleos.xyz | 172.22 / 541.67 / 541.67 | 21 | 0 | 4 |
| rpc-live.boxa.club | 218.08 / 221.51 / 221.51 | 21 | 0 | 0 |
| rpc-live.elyspump.com | 62.76 / 3287.42 / 3287.42 | 10 | 1 rate limit | 2 |
| rpc.agoradex.app | 231.37 / 337.51 / 337.51 | 21 | 0 | 3 |
| rpc.eleos.fun | 239.56 / 245.16 / 245.16 | 21 | 0 | 2 |

Ten samples do not establish p95/p99 reliability or a globally fastest provider.
Sequential head differences remain descriptive; `freshness_qualified` is now always
false and `own_head_timestamp_recent` is separate. `phase=submission-mode-unverified`
replaces calendar-based launch guesses. No synchronized head-lag qualification exists.
Provider budgets may be tighter than the script's generic spacing; honor HTTP 429 and
use a smaller sample/broader spacing in a separately scheduled run.

`docs/benchmarks/agora-quotes-wsl-2026-11-07.json` records six **indicative public API**
quotes from the [official v1 interface](https://docs.agoradex.app/developers), with the
retrieved [OpenAPI](https://api.agoradex.app/api/v1/openapi.json) hash. These are GET reads,
not `eth_call` swap workloads. Observed total quote latency ranged 260.37–1320.76 ms.
There is only one observation per token/size; no tail or direct-router latency comparison
is supported. `venues` compares outputs reported by Agora only. Transactions are null
because no recipient was supplied. Actual fee applicability, balances, swap gas and
simulation remain unverified. The new public API resolves discovery, not execution safety.


## Verified V2 PAPER workload — November 7 continuation

Canonical artifact/ABI/runtime evidence is in
`benchmarks/v2-contract-verification-2026-11-07.json`. This verifies the direct V2
router, its declared factory/wrapped-HYPE immutables, factory and AGORA pair against
published Uniswap build artifacts. It does not establish the aggregator's outer ABI.
Reproduce with `scripts/verify_trade_contracts.py --pair PAIR_ADDRESS`.

`benchmarks/paper-workflow-wsl-2026-11-07.json` measures the actual private Telegram
review and confirmation handlers with a fake sender, real read-only RPC/API requests
and a private temporary journal. The owner's supplied public wallet was used without
publishing its address, balances or private snapshots. The 0.1 HYPE preset refused when
the observed native balance was insufficient; the measured BUY size was 0.0001 HYPE.
SELL size was 1% of the verified token balance; larger presets retain the 1% impact cap.

| Workload | Samples | p50 / p95 / p99 ms |
| --- | --- | --- |
| Local Review click → panel rendered | 6 | 4379.52 / 4643.31 / 4643.31 |
| Public Agora v1 quote/builder | 6 | 311.42 / 498.30 / 498.30 |
| Direct V2 `getAmountsOut` call | 6 | 215.31 / 229.08 / 229.08 |
| RPC `eth_call` (metadata, quotes, simulations) | 69 | 209.63 / 225.56 / 456.42 |
| Actual BUY payload `estimateGas` | 3 | 216.56 / 217.02 / 217.02 |

All six reviews produced private hypothetical records. Three BUY payloads simulated
successfully; three SELL payloads required allowance, reverted as expected, and were
recorded only as `hypothetical_requires_approval`, with no gas estimate or approval sent.
The canonical supporting-fee-on-transfer methods enforce minOut at simulation time but
return no delivered amount; pool expected output still assumes normal token transfers.
No actual token balance, allowance or native balance was changed by these calls.

Agora-reported expected output differed from the direct pool quote by -50 bps for these
BUY observations and -31 bps for these SELL observations. They may observe different
blocks/routes. Those differences are not a causal fee attribution, synchronized best-
net-output proof or guaranteed fill. Fees remain API-reported for the aggregator.
Three observations per side cannot establish tail latency or provider superiority.
The handler timing excludes user dwell, Telegram network delivery and production-VPS
latency. Only Agora RPC was used for this exact workload; the earlier six-provider run
used a different read mix. These timings cannot be treated as a cross-provider swap test.

## PAPER hardening / performance follow-up

`benchmarks/paper-profile-2026-11-07.json` compares the same hardened handler, RPC,
token, watch wallet and sizes with optimization disabled/enabled, in sequential phases.
Both modes include the new final pinned-block hash recheck. Thus this is a fresh
unoptimized baseline, not a claim to reproduce the earlier 4.38 s on another network/time.
Each phase attempts three BUY and three conditional SELL reviews, after a separate
recorded cold-start request. Wallet addresses, balances and request payloads are excluded.

| Review click → render | Before p50 / p95 ms | After p50 / p95 ms |
| --- | --- | --- |
| Matched successes (3 BUY + 2 conditional SELL) | 5526.60 / 5921.24 | 3605.79 / 3844.76 |
| All successful observations (5 before / 6 after) | 5526.60 / 5921.24 | 3605.79 / 4632.33 |
| Separate cold start (one each, not quantiles) | 5560.95 | 8543.91 |

The matched sample shows 34.8% lower median and 35.1% lower descriptive p95. The full
successful after sample has a 21.8% lower descriptive p95. One before SELL refused a
stale/future head; it is retained in raw observations, excluded from latency success
quantiles, and its matching after observation is excluded only from the paired row.
This is not evidence of a reliability improvement. Cold-start variance remains high.
The samples are too small for reliable population tail estimates; sequential state,
provider and network conditions differ. User dwell/Telegram delivery remain excluded.

Before, each operation typically cost ~223–251 ms; one getAmountsOut cost ~225 ms.
The BUY path made 20 RPCs and conditional SELL 19 including the new reorg check.
Warm optimization reduces those to 13 and 12: six immutable-metadata reads are cached
and one redundant router quote is calculated locally with the canonical integer formula.
Chain, all three runtime hashes, mutable state, simulation/gas and final block-hash
checks remain. Per-quote calculation was <0.01 ms and panel rendering p50 ~0.15–0.16 ms.
The bottleneck was serial network round trips, not arithmetic or response formatting.

Raw profiles label every operation (chain/head/code/metadata/reserves/decimals/balances/
allowance/simulation/estimateGas/gasPrice/reorg check), Agora latency, rendering time,
cache state, RPC count and exact simulation status. The cache holds at most 64 immutable
pair relationships and verifies current code even on hits. Token decimals and every
mutable financial value are deliberately uncached. See `PAPER_REVIEW.md` for assumptions.

Reproduce using the approved public watch address (never a key):
`python scripts/trade_paper_bench.py --wallet ADDRESS --token TOKEN --buy 0.0001
--sell-percent 1 --samples 3 --compare`. Inspect refusals and cold-start data as well as
quantiles. No production bot/VPS or transaction submission is involved.
