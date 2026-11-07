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
