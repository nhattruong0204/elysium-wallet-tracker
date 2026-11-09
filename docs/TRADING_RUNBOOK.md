# Trading operations runbook (Stage C2) — on-call, incidents, release, rollback

Scope: the private trading pieces inside the `--ui` service (ledger, wallet worker, ingress,
liveness alerts, owner recovery). Scanner (`tracker`), public dashboard (`web`) and the separate
Revival bot are **not** operated from here; every procedure ends with their non-regression check.
Nothing here signs, broadcasts or moves funds; the user's wallet is the only signer.

## 0. Safe state (default)

| Switch | Where | Safe value |
|---|---|---|
| `trading.ledger_enabled` | config | off (or on, harmless alone) |
| `trading.wallet_execution_enabled` | config | **off** |
| `trading.ingress_enabled` | config | **off** |
| ledger `execution_enabled` | DB `controls` (owner-only, no UI toggles it) | **off** |
| ledger `paused` | DB `controls`, `/pause_trading` / `/resume_trading` (owners) | on during incidents |

`/trading_status` (owners, private chat, read-only) prints all of these and ends with **"Wallet
requests possible: YES/NO"**. Wallet requests are possible only if config wallet execution AND
ingress AND the ledger execution flag are on and the ledger is not paused.

## 1. Emergency stop (any suspicion of wrong spending, data leak or evidence disagreement)

1. `/pause_trading` (owner, private chat) — durable, survives restarts; new wallet requests stop
   immediately (the ledger refuses them). Existing legs keep being reconciled (receipts are facts).
2. If the Mini App itself is suspect: set `trading.ingress_enabled: false` and restart **only** the
   `ui` service in an approved window → no listener exists. Remove/disable the nginx vhost.
3. Users' wallets can still hold prompts they did not answer: they expire (swap deadline) or are
   finite approvals (F-03). Tell users to reject any pending prompt.
4. Do **not** clear a pause until the cause is understood and written down; `/resume_trading` is
   owner-only and audited.

## 2. Alerts (F-18) and what to do

| Alert | First action |
|---|---|
| `ingress_down` / `ingress_degraded` | Mini App offline (fail closed). Check `/wallet_health`, logs (`docker compose logs ui`), port conflicts; restart `ui` in a window. |
| `worker_down` / `worker_stale` / `worker_failing` | Receipts/ambiguous legs not reconciled. Check DB reachability and the RPC; the worker's crash-loop breaker needs a service restart after the cause is fixed. |
| `rpc_outage` | Run `python -m agora_tracker.trading.wallet.rpcdiag --duration 60` (`docs/TRADING_RPC_DIAGNOSTICS.md`); 429 → lower budgets / ask the provider; wrong chain or stale → replace the endpoint. Nothing is recorded meanwhile (fail closed). |
| `ambiguous_legs` | `/wallet_recover` (list) → `/wallet_recover <leg>` (diagnosis, read-only). Only a corroborated, final proof yields a code for `/wallet_resolve`. Otherwise wait / escalate (§3). |
| `orphaned_legs` | A reorg removed an inclusion; the worker re-checks. If it persists, diagnose as above. |
| `receipt_lag` / `queue_growth` | Usually finality not reached (`finalized` tag missing on a source) or RPC trouble: run rpcdiag. |

## 3. Stuck order escalation

1. `/wallet_recover <leg>`: read the category and reason. `unknown` + "nonce still free" means the
   transaction **could still be mined** — never force anything.
2. Ask the user (out of band) whether the wallet shows the request pending; ask them to reject or
   to send a 0-value self-transaction with the same nonce **only if** a reviewer agrees (that makes
   supersession provable).
3. If sources disagree (`conflict` rows in `chain_evidence`), run rpcdiag; replace the bad source;
   never resolve on one source.
4. Escalate to the security reviewer if a leg stays ambiguous > 24 h or any conflict persists.

## 4. Release (only after #95 sign-off AND written owner authorization; see RELEASE_C2_CANARY.md)

1. Freeze the candidate SHA; build once; record the image **digest**; set `TRADING_RELEASE_IMAGE`
   and `POSTGRES_IMAGE` to digests (`deploy/trading-release.compose.yaml`). All app services run the
   same digest (schema head is in the image).
2. Backup: `pg_dump` of `tracker` (includes `trading_private`), copy off-site, **restore rehearsal**
   into a scratch database (`docs/BACKUP_AND_RECOVERY.md`), keep the dump for the rollback window.
3. Preflight: `python -m agora_tracker.preflight --graph-only` and with `DATABASE_URL` (before).
4. `docker compose ... run --rm migrate`; preflight again (after) — all PASS/INFO.
5. Start services with flags still OFF; `/trading_status` must say "NO"; scanner/dashboard checks
   (§7) pass.
6. Only then follow the canary plan.

## 5. Rollback

* Code: redeploy the previous image digest for **all** app services. If the schema moved past what
  the old image expects, either downgrade (`alembic downgrade <rev>`, see DATABASE.md — 0016/0014
  downgrades drop tables: archive first) or restore the pre-release dump.
* Post-rollback validation: `/trading_status` NO; `--health` / `--health-ui` OK; preflight PASS
  for the old head; §7 checks.

## 6. Evidence retention and logging controls

* Keep: `chain_evidence`, `leg_recoveries`, `order_events` (append-only, hash-chained) for the life
  of the ledger; rpcdiag JSON reports, verifier outputs and canary records with the review (#95/#96).
* Never log/paste: RPC URLs (may embed keys), the ingress secret, Telegram `initData`, cookies, SIWE
  signatures, wallet addresses in public channels. The code logs hosts only; alerts are scrubbed.
* Secrets live only in the root-owned env file (≤ 0640) and the nginx secret include (0640).

## 7. Non-regression checks (after any change)

* Scanner: `docker compose run --rm tracker --status`; scan lag below `lag_warn_blocks`; alerts
  still arriving in the alert chat.
* Dashboard: `curl -fsS http://127.0.0.1:8100/` (loopback) and the public URL.
* Revival bot: `systemctl status revival-health.timer`; its last run OK.
* Exactly one `getUpdates` consumer: no 409 "another getUpdates consumer" in `ui` logs.
* Resources: `docker stats --no-stream` within `mem_limit`s; worker RPC calls/min below
  `worker_rpc_calls_per_min`.

## 8. Release audit trail

For every change record: candidate SHA, image digest, reviewer sign-off reference, owner
authorization (date, scope), preflight outputs, backup id, verifier/rpcdiag reports, and the
`/trading_status` output before and after. Store with #96.
