# Supervised reconciliation worker (Stage C2, security finding F-01)

**Status: implemented, default OFF, never enabled by this change. No signing, approval, broadcast,
funded call, deployment or restart.** Closes the *code* gap in F-01 of
`docs/SECURITY_REVIEW_C2.md` ("nothing starts the reconciler / receipt worker"); the finding stays
subject to human verification (see "Evidence" and "Unresolved").

## What it is

`wallet/supervisor.py` runs the existing `LegReconciler` (#93) and `ReceiptReconciler` (#94)
unchanged, in one background thread of the **`--ui` service** (the process that already owns the
Telegram `getUpdates` consumer, the ledger and the collector). It adds only the lifecycle:

```
run_ui ─ build_worker()  ── flag off → None (nothing built)            ┐ before polling
                          └ flag on  → ledger/RPC missing → SystemExit  ┘ starts
      ─ worker.start()   ── thread "wallet-reconciler" (nice +10)
      ─ bot.run(stop)    ── the ONE getUpdates consumer (untouched)
      ─ finally: worker.stop() → bot.shutdown() → engine.dispose()
```

Per cycle: `LegReconciler.run_once` → `ReceiptReconciler.run_once` → `ledger.worker_stats()` (three
cheap counts). An idle cycle makes **zero RPC calls**.

## Gates and fail-closed startup

| Condition | Result |
|---|---|
| `trading.wallet_execution_enabled: false` (default) | no worker, no RPC object, no thread; `/wallet_health` says "Not available." |
| enabled, no ledger | `SystemExit: wallet reconciliation worker misconfigured` **before** polling starts |
| enabled, `TRADING_EXEC_RPC_URL` missing / not http(s) / same as scanner or paper RPC | same (message names the variable, never the URL) |
| enabled but the durable owner flag is OFF, or trading is **paused** | worker still runs (see below) |

The config flag is the deployment switch for the worker; the **durable owner-only execution flag**
and `/pause_trading` control *new prompts*. Turning the config flag off stops monitoring of
already-sent requests, so for an incident use pause / the durable flag, not the config flag.

## One active worker (no duplicate reconciliation)

* A PostgreSQL **session advisory lock** (`pg_try_advisory_lock(0x656C7973, 0x7265636E)`) on its own
  one-connection engine (the ledger pool used by Telegram handlers is not reduced). The second
  process is a **hot standby**: it does nothing, retries every `worker_standby_retry_s`, and takes
  over when the leader exits or its session dies (Postgres releases the lock with the session;
  `held()` is checked before every cycle, so a lost lease stops work immediately).
* The lock is an efficiency/RPC-budget measure, **not** the correctness mechanism. Correctness is
  the ledger's: optimistic `expected_version`, `UNIQUE(leg_id, block_hash)`, set-once hash/nonce and
  legal-edge triggers. A test runs three workers *without any lease* concurrently and asserts one
  inclusion, one fill, one state change.
* Within a process, `start()` twice on one supervisor is refused.

## Bounded retries, crashes, shutdown, restart

* **Failure** of a cycle = an exception from a reconciler or the stats query, or *every* RPC call
  in the cycle failed. Backoff: `interval × 2^n` (n capped at 10) up to `worker_max_backoff_s`,
  ±20 % jitter; first success resets. One reconciler failing does not stop the other.
* **Crash** (an exception in the supervisor itself): logged, counted, retried with `2^k` s backoff;
  `10` crashes within `10 min` ⇒ state **`failed`**, thread exits, `log.critical`, lease released, and
  health shows it. Transient DB/RPC outages are cycle failures, never crashes, so they cannot trip it.
* **Shutdown**: SIGINT/SIGTERM set the process stop event; the worker checks it between legs (each
  leg is its own atomic ledger transaction), sleeps in ≤1 s slices, releases the lease and exits;
  `stop()` joins with a timeout and reports whether the thread is gone.
* **Restart**: no in-memory state is authoritative. The next start re-reads candidates from the
  database and continues; a test restarts mid-flight and asserts no duplicate fills or events.
  (Per-inclusion "evidence unavailable" retry counters are in memory and reset on restart: at worst
  three more retries before an `unknown` fill is recorded.)

## Pause and the execution flag

The worker never reads either. Receipts, reorgs and leg resolution continue while trading is
paused and while the durable execution flag is OFF, and the worker never resumes, enables or
changes either. Tested with pause + flag-off + a mined transaction.

## Resource and RPC budgets (shared VPS)

| Bound | Default | Effect |
|---|---|---|
| threads | 1, sequential, `nice +10` (best effort) | RPC I/O wait dominates; no parallel calls |
| legs per pass | `worker_batch` 50 per reconciler | oldest first, so none starves |
| wall time per cycle | `worker_pass_budget_s` 20 | pass ends between legs, remainder next cycle |
| RPC calls | `worker_rpc_calls_per_min` 240 shared by both reconcilers (+ #94's per-pass budget 120) | over budget raises `RpcBusy`; both reconcilers treat it as "no evidence now": nothing changes |
| RPC object | dedicated `ExecutionRpc` (own URL, semaphore, breaker) | never the scanner's `RpcClient`; scanner and Telegram sends are not on this path |
| chain handle | `BudgetedChain`: exact allowlist of 8 read methods | no `rpc()`, `call()`, send or sign reachable (test enumerates) |
| cadence | 10 s with work, 30 s idle | idle = 3 SQL counts |

The worker never imports or calls Telegram (test: source scan, and only `tgui.py` calls
`getUpdates`).

## Private health

`WorkerSupervisor.snapshot()` (no DB access; refreshed each cycle) and the **owner-only** private-chat
command `/wallet_health` (everyone else: "Not available."). Not on any HTTP route.

| Metric | Field |
|---|---|
| running state | `state` (`stopped`/`standby`/`running`/`failed`), `running`, `thread_alive` |
| last successful cycle | `last_success_at`, `seconds_since_success` |
| processing lag | `queues.oldest_wait_s` (age of the oldest unsettled leg) |
| errors | `errors_total`, `consecutive_failures`, `crashes`, `last_error` (class name only), `pass_overruns` |
| ambiguous legs | `queues.ambiguous_legs`, `queues.orphaned_legs` |
| RPC failures | `rpc.failures`, `rpc.calls`, `rpc.budget_refusals`, `rpc.calls_last_minute`, `rpc.breaker` |
| queue depth | `queues.evidence_queue`, `queues.receipt_queue` |
| lease | `lease_losses`, `state == standby` |

An INFO summary line is logged every 5 minutes. `healthy()` = running and a success within
`3 × idle + pass budget`. Alert on: `ambiguous_legs > 0`, `oldest_wait_s` growing,
`state in (standby on all nodes, failed)`, `consecutive_failures ≥ 3`.

## Security assumptions

* The execution RPC is trusted for chain facts (finding F-15 stands: a second source is recommended).
* The Postgres role is `trading_rw` (enforced by `open_ledger`); the lease needs no extra privilege.
* A pgbouncer in *transaction* pooling mode in front of the ledger URL would break the session lock;
  use a direct connection (the lease logs and stays in standby if it cannot connect).
* Log lines and health carry exception **class names** only — no messages, URLs, hashes or user ids.

## Unresolved (not part of this change)

* F-02 owner force-resolve tool, F-04 finality depth, F-15 single RPC, F-06/F-07/F-08 unverified.
* The wallet/Mini App HTTP routes were not mounted by any entrypoint when this was written (F-16);
  the follow-up ingress change (`docs/TRADING_INGRESS.md`) adds the flag-gated, loopback-only,
  fail-closed listener in the same `--ui` process and shares this worker's execution RPC.
* No alert delivery: health is pull (owner command / log); pushing alerts to Telegram would add a
  second sender and was left out.
* Real-process behaviour (signals under the real supervisor/Docker, real Postgres failover) is
  covered only by in-process tests.

## Tests

`tests/test_wallet_worker.py` (47): fail-closed startup matrix, config bounds, read-only chain
handle and sliding RPC budget, lifecycle (start/stop/prompt shutdown/external stop/restart/double
start), standby and lease loss, failure isolation and backoff, RPC outage, DB trouble, wall-time and
overrun bounds, crash and crash-loop, health fields and escaping, Telegram isolation, owner-only
command, and with real PostgreSQL: lease exclusivity and session death, provisional→final through
the supervisor, pause + flag-off continuity, restart recovery, three leaseless workers
exactly-once, leader/standby takeover, RPC outage and budget exhaustion fail-closed, stats,
and the real `run_ui` wiring (off by default; fatal without RPC before polling; one worker beside the
single consumer and stopped on exit).
