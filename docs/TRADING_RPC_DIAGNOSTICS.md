# Read-only RPC diagnostics and provider inventory — #95 F-07, F-04, F-15

Status of real-endpoint validation: **BLOCKED** — the build environment has no route to any chain-99901
endpoint and holds no credentials. The tool and its adversarial tests are complete; the numbers
below must be produced by the operator on the VPS. No result in this repository is a real
measurement.

## 1. The tool

`python -m agora_tracker.trading.wallet.rpcdiag` (`agora_tracker/trading/wallet/rpcdiag.py`).

* Sends **only** read methods (execution allowlist + `web3_clientVersion`/`net_version`); anything
  that sends, signs or administers raises before any I/O (tested).
* Reads endpoints from **environment variables only** (default `TRADING_EXEC_RPC_URL` and
  `TRADING_CORROBORATION_RPC_URLS`, `label=url` supported). The report contains host and operator
  label, never the URL, path or key (tested).
* Hard per-provider call budget (`--max-calls`, default 400) and rate ceiling (`--max-rps`, default
  4/s); concurrency probe off unless `--concurrency N` (max 8).
* Exit code 0 = PASS/WARN, 1 = FAIL, 2 = BLOCKED (no usable endpoint).

Adversarial behaviour is tested only against `tests/rpcmock.py`, a loopback mock (lying hashes,
ignored block parameter, stale head, pruned state, missing `finalized`, 429 + `Retry-After`,
malformed bodies, timeouts, wrong chain, reorg, identical fingerprints). Never point adversarial
traffic at real providers.

## 2. Operator procedure (VPS, no trading flags needed)

```bash
# secrets come from the same root-owned env file the service uses; nothing is echoed
set -a; . /etc/elysium/trading.env; set +a
python -m agora_tracker.trading.wallet.rpcdiag --duration 600 --interval 5 --max-rps 2 \
    --out /var/lib/elysium/rpcdiag-$(date +%F).json --markdown /var/lib/elysium/rpcdiag.md
# optional, once, low: --concurrency 4  (only within each provider's published limits)
```

Run it at least: once now; once over 24 h (`--duration 3600` hourly from cron, `--max-calls 300`);
and after any provider change. Attach the JSON to the security review (#95).

## 3. What each check proves

| Check | PASS means | Blocks release if |
|---|---|---|
| `chain_id` | answers 99901 | any endpoint not 99901 |
| `parent_hash_continuity` | 20 blocks link by `parentHash`, timestamps non-decreasing | any break |
| `finality_tags` | `finalized` served, below latest, trailing by > 2 s | not served (finality never reached) |
| `finalized_progress` | finalized advanced during the window | stuck for the whole window |
| `historical_block_param` | nonce at N-1 ≤ tx nonce < nonce at N | node ignores the block parameter |
| `historical_balance_n_minus_1` | balance differs across the sender's own tx | WARN only |
| `historical_eth_call` | Multicall3 `getBlockNumber()` at a past block returns that block | FAIL (fills would be wrong) |
| `archive_depth` | `eth_getBalance` works ≥ 1 000 000 blocks back | < 10 000 blocks |
| `tx_receipt_consistency` | tx / receipt / block agree | any mismatch |
| `head_freshness` | head ≤ 10 s old, advancing | stalled or > 60 s |
| `reorgs_observed` | no hash changed at a seen height | any (investigate; F-04) |
| `transport` | no timeouts/429/5xx; p50/p95/p99 per method | sustained failures |
| `same_canonical_chain` / `same_historical_state` / `same_finalized_ancestry` | providers agree | any disagreement |
| `head_skew` | ≤ 64 blocks between providers | persistent > 64 |
| `upstream_indicators` | fingerprints differ | WARN only — never proof either way |

## 4. Provider inventory (fill from the reports; never paste URLs)

| Provider | Operator | Endpoint ID | Hosting / upstream evidence | Archive support | Historical accuracy | Limits | Independence assessment |
|---|---|---|---|---|---|---|---|
| _execution RPC_ | _label_ | `host` only | `client_version`, `fingerprint_headers`, provider's written statement | `archive_depth` | `historical_*` | plan, rps, Retry-After seen | **NOT ESTABLISHED** until written evidence |
| _corroboration #1_ | | | | | | | |

Known public context (2026-11-09, unverified): the sequencer is operated by **Conduit**; Conduit
also hosts an RPC hostname for Elysium mainnet; Kinetiq advertises `rpc.elysium.kinetiq.xyz` (seen
only in third-party posts). A Kinetiq-branded and a Conduit-branded endpoint may be **the same
infrastructure** — treat them as one operator unless Kinetiq/Conduit state otherwise in writing.
Every RPC ultimately replays the same single sequencer feed: corroboration protects against a lying
or broken **RPC**, not against the sequencer.

Independence can only be **accepted** by a human on written evidence (separate companies, separate
node infrastructure, not one reselling the other). The tool can only find evidence of dependence.

## 5. Budgets on the shared VPS

The diagnostic runs alone, sequentially per provider, at ≤ `--max-rps` per provider. The worker's
own ceilings are unchanged: `worker_rpc_calls_per_min` (240) per source and
`corroboration_calls_per_pass` (120) per pass. Finality adds ONE `finalized` read per source per
pinned snapshot.
