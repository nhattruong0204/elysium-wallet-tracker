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

VPS run pending. Location, observation time, request mix/count and launch phase are in JSON
output. Before declaring a preferred provider, repeat with >=20 samples, representative
verified routes/sizes, provider budgets, actual fees/net output and both launch phases.
No globally fastest RPC is selected from these initial observations.

## Not measured / gates

Agora vs direct quote/net-output comparisons, swap gas, signed-send acknowledgment,
inclusion, confirmation and finality are unrun. No stable authorized Agora transaction
builder/SDK has been verified. ABI/permissions/soft-launch inbox behavior are unresolved;
no signing or broadcast probes are permitted. A consented tiny live test is a separate
security-reviewed operation requiring explicit limits and owner authorization.
