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
different times, so relative lag includes sampling skew. Freshness qualification requires
chain 99901, head age <=60 seconds and sampled lag <=50; it does not authorize execution.
Output contains host labels and aggregate values, never URL paths, query keys, credentials,
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
inclusion, confirmation and finality are unrun. No stable authorized Agora transaction
builder/SDK has been verified. ABI/permissions/soft-launch inbox behavior are unresolved;
no signing or broadcast probes are permitted. A consented tiny live test is a separate
security-reviewed operation requiring explicit limits and owner authorization.
