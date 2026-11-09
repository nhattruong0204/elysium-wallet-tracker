# Security review package — wallet-managed execution (Stage C2, issue #95)

> **This is an AI-prepared review package. It is NOT an independent security approval.**
> A human reviewer who did not write the code must read it, reproduce the evidence and sign the
> block at the end. Until then the state of this review is **BLOCKED on human sign-off**, and the
> separately authorized live test (#96) **must not start**. Nothing here enables execution, funds a
> wallet, deploys or restarts anything.

## 1. Scope and frozen heads

State fetched from GitHub on 2026-11-09 (re-fetch before reviewing; nothing here is frozen until
the reviewer records the exact SHA in §10).

| Issue / finding | PR (draft unless noted) | Branch | Head |
|---|---|---|---|
| #88 Mini App | #100 (**merged to `main`**) | `feat/trading-c1-miniapp` | `8a46e53` |
| #89 Telegram initData + SIWE | #101 (merged into `feat/trading-c1-miniapp`, not `main`) | `feat/trading-c1-auth` | `3d15431` |
| #90 immutable V2 plans | #102 (merged into its stack base) | `feat/trading-c1-unsigned-plans` | `ad6f9c6` |
| #91 order ledger, pause | #103 (merged into its stack base) | `feat/trading-c2-order-ledger` | `893938a` |
| #92 finite approvals | #104 (merged into its stack base) | `feat/trading-c2-wallet-approvals` | `f769b42` |
| #93 nonce/idempotency | #105 (merged into its stack base) | `feat/trading-c2-nonce-idempotency` | `e6c4fcd` |
| #94 receipts/fills | #106 (merged into its stack base) | `feat/trading-c2-receipt-reconciliation` | `de3bdc1` |
| #95 review package | #107 | `feat/trading-c2-security-review` | `4695d9e` |
| F-01 worker lifecycle | #108 | `feat/trading-c2-worker-lifecycle` | `8985c1f` |
| F-16 ingress | #110 | `feat/trading-c2-ingress` | `2a0a1f4` (code `80e12a6`) |
| F-02 owner recovery | #111 | `feat/trading-c2-leg-recovery` | `a67f1ab` (code `026277c`) |
| F-15 corroboration | #112 | `feat/trading-c2-corroboration` | `bd6d32c` (CI green) |
| F-04 / F-07 / F-22 finality + RPC diagnostics | #113 | `feat/trading-c2-finality-rpc` | `013e861` (CI green) |
| F-18 liveness | #114 | `feat/trading-c2-liveness` | `a66a9d0` (CI green) |
| F-21 lock order, F-17 proxy, preflight | #115 | `feat/trading-c2-release-hardening` | `7e5f95e` |
| release package (this document, runbook, matrices, `/trading_status`) | #116 | `feat/trading-c2-release-package` | `e1e561e` + docs-only follow-up |
| **integration candidate → `main`** | **#117** (supersedes #109) | `release/trading-c2-rc1` | same as #116 (record the exact SHA in §10 when reviewing) |

Ancestry (verified with `git merge-base --is-ancestor`): every branch above is contained in the next
one; the merge commits of #101–#106 only exist on their stack bases and carry no extra content
(tree-identical). `main` contains only #100. The old integration PR **#109 points at `8985c1f`
(F-01) and is stale**: it lacks F-16, F-02, F-15 and everything after. The single integration
candidate is described in `docs/RELEASE_READINESS.md`.

Out of scope / not reviewed: the scanner/collector, Postgres tuning, the web app and the independent Revival
bot (the change set does not touch them — verify with `git diff --stat`; `tgui.py` is the one shared module touched, see §3), the Telegram login/SIWE
code from #101 (separately reviewed), any smart contract (none is deployed or modified), real
wallets/devices, the real RPC endpoint.

## 2. Security model in one paragraph

The user's own wallet is the only signer and the only broadcaster (EIP-1193 `eth_sendTransaction`).
The server (a) builds an exact request from a verified plan, (b) binds it to user, session, wallet,
order, digest and expiry, (c) records that it asked, (d) records what the wallet answered, and
(e) later *reads* the chain to record what happened. It holds no key, seed or signature, has no
send/sign RPC method, and cannot rebroadcast. A hash from the wallet is an acknowledgment, never a
fill. Anything uncertain fails closed (no prompt, no state change, amounts `unknown`).

## 3. Hard restrictions → where enforced → evidence

| Restriction (from the issues) | Enforcement | Evidence |
|---|---|---|
| Real-execution flag stays OFF | `trading.wallet_execution_enabled=False` default; routes mounted only when on **and** ledger + secure planner exist; durable owner-only flag in the ledger also default OFF and re-checked on every prompt | `test_flag_off_means_no_wallet_routes_in_the_miniapp`, `test_wallet_runtime_requires_the_secure_planner`, `test_config_flag_defaults_off_and_requires_the_ledger`, `test_approval_prompt_is_gated_by_the_execution_flag` |
| No keys/seeds on server, Telegram, .env, DB, logs | no key material exists in the wallet package (grep below); RPC URL secret only from env and redacted; wallet error text never stored (fixed reason allowlist) | `grep` in §7; `test_wallet_error_text_is_never_sent_to_the_server_or_audit`; `test_wallet_rpc.py` redaction tests |
| No signer service / no server broadcast | RPC allowlist is read-only; `eth_send*`, `eth_sign*`, `personal_*`, `wallet_*`, `debug_*`, … refused before any I/O | `test_wallet_rpc.py` (forbidden-method enumeration) |
| No unlimited approvals | amount must equal the reviewed input exactly and be `< 2**128`; spender is the frozen V2 router (code hash re-checked); allowance ≥ 2**128 is refused; nonstandard `approve` return refused; browser re-checks independently | `test_wallet_requests.py`, `test_wallet_approvals.py`, `test_wallet_browser.py` (tamper cases), `test_browser_constants_match_the_server_constants` |
| No unverified Agora outer-router calldata / arbitrary destination | swap target must equal the frozen router; calldata is re-derived and compared byte-for-byte; a conditional SELL never becomes a swap | `test_wallet_requests.py` |
| No auto/copy trading | no scheduler/signal path creates orders; every order needs an authenticated user, a reviewed plan and a wallet confirmation | structure; `test_user_cancel_before_the_wallet_only` |
| Exactly-once | durable leg per request, unique idempotency key, one unsettled leg per wallet+chain, one swap per order, set-once hash/nonce, legal-edge trigger | `test_wallet_reconcile.py`, `test_wallet_ledger_gate.py`, `test_wallet_receipts.py` (racing workers, restart) |
| Truthful receipts | validated receipt + canonical block + two-measure fills; reorg rollback; finalized rows immutable | `test_wallet_fills.py`, `test_wallet_receipts.py` |
| Mini App / wallet routes not publicly reachable unless explicitly configured (F-16) | `ingress_enabled` default false (no listener); loopback-only bind validated in config; `IngressGuard` refuses anything not forwarded by the configured HTTPS proxy (secret, Host, https); wallet routes need `wallet_execution_enabled` as a second opt-in; public dashboard is a separate service and untouched | `tests/test_wallet_ingress.py`, `tests/test_wallet_ingress_e2e.py` (see `docs/TRADING_INGRESS.md` §2) |
| Stuck legs are never released without proof (F-02) | owner-only, private-chat, two-step (one-time code + mandatory reason); ≥ 2 distinct-host sources, all healthy/fresh/same-fork; only monotone facts (final consumed nonce, passed swap deadline + margin); fresh re-diagnosis must match the prepared decision; append-only `leg_recoveries` + hash-chained audit events; receipts/fills/hashes/nonces never rewritten; no signing, no broadcast, no wallet request | `tests/test_wallet_recovery.py` (see `docs/TRADING_LEG_RECOVERY.md` §4–§6) |
| Nothing is finalized, certified as a fill or unlocked on one provider's word (F-15) | shared corroboration layer (≥ 2 distinct-host AND distinct-operator sources, same canonical anchor, same-state reads, identical facts, bounded calls/time, fail closed); ledger-side `check_chain_evidence` on every inclusion/final/reorg/supersede/abandon; verified fill = method agreement AND source agreement; append-only `chain_evidence`; finality depth unchanged | `test_wallet_corroboration.py`, `test_wallet_corroboration_ledger.py`; provider independence itself is NOT proven by tests |
| Finality is more than a confirmation count (F-04) | `Session.finality()`: ≥ 12 confirmations AND ≤ lowest `finalized` tag of every source; ledger `_check_finalized` on finalize/supersede/recovery; tag missing → nothing final | `test_wallet_corroboration*.py` finality cases, recovery test 14 |
| Operators are told when things break (F-18) | liveness monitor: private, deduplicated, capped alerts; bounded listener restart then degraded mode; never touches ledger/pause/flags/polling | `tests/test_trading_liveness.py` |
| Proxy cannot be fooled by client headers (F-17) | template sets every forwarded header; verifier + CI rehearsal against the real guard | `tests/test_verify_ingress_proxy.py`, CI `ingress-proxy` |
| Ledger writers never deadlock (F-21) | order row before the audit-chain lock everywhere | `test_event_writers_take_the_order_row_before_the_audit_chain_lock` |
| Scanner/Postgres/web/Revival unaffected | additive migrations 0012–0016 confined to the `trading_private` schema (no `public`/scanner table altered); wallet code is imported by nothing in the collector/engine/web/Revival paths. The only touch of a shared module is `tgui.py` (a ledger-gated, exception-safe text section; returns `""` when the ledger is off) the owner-only `/wallet_recover` / `/wallet_resolve` commands (F-02; "Not available." for everyone else) and `config.py` (validated fields) | `git diff --stat`; `test_migration_*` round-trip tests; `test_telegram_section_never_raises_into_the_command_handler`, `test_telegram_commands_are_owner_private_chat_only` |

## 4. Threat model (STRIDE-oriented)

Assets: user funds (via the user's wallet), approvals, order/audit integrity, the owner execution
flag, the execution-RPC secret, the Telegram identity binding.

| # | Threat | Actor | Mitigation | Residual |
|---|---|---|---|---|
| T1 | Server asks the wallet for something other than the reviewed trade | compromised server / bug | Request is derived from the plan and digest-bound; browser re-checks target/spender/sender/chain/finite amount; **the wallet's own confirmation screen is the final control** | A server that also serves tampered JS defeats the in-page check (see F-11) |
| T2 | Replay/steal a request id | other user, session thief | request bound to user+session+wallet+order+digest+expiry; unknown/foreign ids are indistinguishable 404 | session theft is #101's boundary |
| T3 | Double prompt / double order | double click, retry, two tabs, crash | DB-enforced keys and partial unique indexes; single-flight JS; ambiguous never re-prompts | none known |
| T4 | Wallet sends, answer lost | network, crash | leg `ambiguous`; wallet stays blocked until **chain evidence** resolves it | owner-audited recovery tool (F-02, `docs/TRADING_LEG_RECOVERY.md`): corrects only with corroborated, final chain evidence; otherwise stays blocked |
| T5 | Hash for someone else's tx / different payload | malicious client | payload (from/to/value/input) compared before any accounting; mismatch ⇒ `invalid`/ambiguous, never "the reviewed trade" | — |
| T6 | Fake or wrong receipt/balances | malicious or buggy RPC | validation + two-measure fills + canonical-header check | **single execution RPC** is trusted for facts (F-04/F-17) |
| T7 | Reorg after "included" | chain | provisional until N confirmations; rollback keeps history; finalized immutable | N is an assumption (F-04) |
| T8 | Approval exploited later | spender compromise | finite, exact, router only; zero-reset policy; allowance re-read after receipt (`cap_mismatch`) | abandoned approvals may still land late (F-03) |
| T9 | Cross-user data read | other user | RLS on legs/fills/orders; owner-bound reads; tests with two users | RLS depends on the app role being `trading_rw` |
| T10 | Audit/log leak | insider, log reader | fixed reason allowlist; URL/body never logged; only host name | — |
| T11 | Pause bypass | bug | gate re-checks pause + durable flag + policy before every prompt; receipts recorded while paused by design | — |
| T12 | RPC overload / DoS of the scanner | traffic spike | separate RPC, semaphore, breaker, per-pass budget | real endpoint limits unknown (F-07) |
| T13 | Telegram text injection | hostile token symbol/etc. | all dynamic text HTML-escaped; JS uses `textContent` | — |

## 5. Findings register

Severity is the reviewer-style estimate of *impact if the feature were enabled with funds*.
Status vocabulary (#95): **OPEN** · **MITIGATED IN CODE — AWAITING VERIFICATION** · **VERIFIED BY
INDEPENDENT REVIEWER** · **ACCEPTED RESIDUAL RISK — WITH NAMED APPROVER** · **NOT APPLICABLE —
WITH JUSTIFICATION** · **BLOCKED** (needs external access/evidence). Nothing below is "verified":
no independent reviewer has seen this code. Earlier "accepted" items are AI-proposed acceptances
and still need a **named human approver**.

| ID | Sev | Finding | Status | Evidence / next action |
|---|---|---|---|---|
| F-01 | Med | Nothing started the reconciler / receipt worker at process start. | MITIGATED IN CODE — AWAITING VERIFICATION | `docs/TRADING_WORKER_LIFECYCLE.md`; `tests/test_wallet_worker.py`. |
| F-02 | Med | No owner tool for a leg stuck `ambiguous`/`orphaned`. | MITIGATED IN CODE — AWAITING VERIFICATION | `docs/TRADING_LEG_RECOVERY.md`; `tests/test_wallet_recovery.py` (now also: finalized-tag finality, lock-order, transient-busy code retention). Owner recovery never abandons approvals and never releases a lock while the request's nonce is free. |
| F-03 | Low | An approval the wallet holds but never sent can be broadcast after the automatic reconciler abandons it (`approval_never_sent`, ≥ 600 s ledger minimum, 3600 s default); swaps are protected by their on-chain deadline. | OPEN — needs a named approver to ACCEPT, or set the auto policy off | Finite, router-bound approvals limit the impact; owner recovery itself never abandons approvals. Reviewer decides. |
| F-04 | **High** (was Med) | Elysium is a single-sequencer Arbitrum Orbit chain with ~100–200 ms blocks: 12 confirmations ≈ 2 s of the sequencer's promise, no documented guarantee. | OPEN (interim policy in code) | `docs/TRADING_FINALITY.md`: final = 12 confirmations AND ≤ lowest `finalized` tag of every source (ledger re-checks; fail closed). Needs 24 h rpcdiag on real endpoints + Kinetiq/Conduit statement. |
| F-05 | Info | Telegram iOS WebView usually has no injected wallet provider. | ACCEPTED RESIDUAL RISK — approver pending | UX only. |
| F-06 | High (if used) | Raw-signed submission / relay permission not established. | NOT APPLICABLE — wallet-managed mode never submits raw transactions; any raw path needs its own review | — |
| F-07 | Med | Real execution RPC behaviour never exercised (chain id, freshness, 429/Retry-After, historical state). | BLOCKED (no endpoint access here); tooling complete | `python -m agora_tracker.trading.wallet.rpcdiag` + `docs/TRADING_RPC_DIAGNOSTICS.md`; 18 adversarial tests on a loopback mock. |
| F-08 | Med | Only mocked EIP-1193 providers exercised. | BLOCKED (no physical device/wallet) | `docs/TRADING_DEVICE_ACCEPTANCE.md` (16-row matrix, steps, evidence form). |
| F-09 | Info | Fills `unknown` when other txs touch the wallet in the block, fee-on-transfer, pruned state. | ACCEPTED RESIDUAL RISK — approver pending | Intended fail-closed. |
| F-10 | Low | Fill rows immutable; a wrong `unknown` cannot be corrected in place. | ACCEPTED RESIDUAL RISK — approver pending | Compensating entries if it becomes common. |
| F-11 | Med | Same-origin JS could be modified by a compromised server. | ACCEPTED RESIDUAL RISK — approver pending | Wallet confirmation screen is the control; consider SRI/CSP hashes. |
| F-12 | Info | Stacked branches; `main` has only #100; #109 head (`8985c1f`) predates F-16/F-02/F-15. Clustering migration `0011` collides. | MITIGATED IN CODE — AWAITING VERIFICATION | One integration candidate PR (`RELEASE_READINESS.md`); preflight graph check in CI; clustering must become `0017` on `0016` (free on every branch). |
| F-13 | Low | Telegram status section swallows exceptions. | ACCEPTED RESIDUAL RISK — approver pending | Read-only display. |
| F-14 | Low | Migration 0014 downgrade drops `order_fills`. | ACCEPTED RESIDUAL RISK — approver pending | Backup before downgrade (DATABASE.md). |
| F-15 | Med | Facts came from one execution RPC. | MITIGATED IN CODE — AWAITING VERIFICATION; provider independence **not established** | `docs/TRADING_RPC_CORROBORATION.md`; inventory in `TRADING_RPC_DIAGNOSTICS.md` §4 (Kinetiq- and Conduit-branded endpoints may be one operator). |
| F-16 | Med | No process entrypoint mounted the Mini App/wallet routes. | MITIGATED IN CODE — AWAITING VERIFICATION | `docs/TRADING_INGRESS.md`; ingress tests. See F-19 for Docker. |
| F-17 | Med | Reverse proxy is trusted and outside the repository. | OPEN (template, verifier and CI rehearsal done; real VPS proxy unverified) | `deploy/nginx/*.template`, `scripts/verify_ingress_proxy.py`, CI job `ingress-proxy`. Operator runs remote + `--local` checks; human reviews redacted `nginx -T`. |
| F-18 | Low | Liveness was pull-only. | MITIGATED IN CODE — AWAITING VERIFICATION (real Telegram delivery untested) | `docs/TRADING_LIVENESS.md`; `tests/test_trading_liveness.py`. |
| F-19 | Med (blocking for ingress in Docker) | In the shipped compose file `ui` runs in a bridge network; its loopback-only ingress is unreachable from a host nginx, and the config (correctly) refuses a non-loopback bind — fails closed, but the ingress cannot be deployed as-is. | OPEN — owner decision | `deploy/trading-release.compose.yaml`: host networking for `ui` or `--ui` under systemd. |
| F-20 | Low | The Mini App's add-chain request proposes `https://rpc.agoradex.app` (a third-party RPC) as the user's wallet RPC; that endpoint would see and could censor/delay users' transactions. | OPEN — owner decision | Confirm the official endpoint with Kinetiq; record wallets' stored RPC in the device matrix. |
| F-21 | Med | Lock-order inversion: refusal-audit / wallet-result events took the audit-chain lock before the order row → deadlocks; the winning recovery confirmation was refused `busy` (seen in CI). | MITIGATED IN CODE — AWAITING VERIFICATION | Fixed in the hardening PR; deterministic test fails on the old code. |
| F-22 | Med | Corroboration tolerated only 3 blocks of head skew (≈ 0.5 s here) and clipped nonce bisection at ~6–11 h: on this chain the F-15 layer would fail closed permanently / raise false conflicts. | MITIGATED IN CODE — AWAITING VERIFICATION | Skew 64 blocks (lowest head stays the anchor); bisection unclipped when the prompt block is known. |
| F-23 | Info | The durable ledger execution flag has no UI; turning it on needs a deliberate owner action on the database/code. | ACCEPTED RESIDUAL RISK — approver pending (intentional friction) | `/trading_status` shows it read-only. |

**Summary.** No unresolved critical issue was found *in the code*. **F-04 (High) is OPEN**, as are
F-03, F-17, F-19 and F-20; F-07 and F-08 are BLOCKED on real endpoints/devices; F-15's provider
independence is not established. Every "accepted" item needs a named human approver. These are the
reasons sign-off is blocked.


## 6. What this review did NOT do

* No independent reviewer, no penetration test, no fuzzing, no dependency/CVE audit (offline), no
  formal verification, no review of the router/token contracts, no load test.
* Static checks were limited to `ruff` and `mypy`; dynamic evidence is the pytest suite.
* Nothing was run against a real chain, RPC, wallet or funded account (rpcdiag ran only against an
  isolated loopback mock; the nginx template only on a disposable CI runner).
* Chain facts in `docs/TRADING_FINALITY.md` come from third-party sources; Kinetiq's own docs could
  not be fetched from the build environment.
* Local environment lacked `eth_keys`/`eth_abi`/`eth_hash`; the modules depending on them (the #101
  authenticator tests) ran only in CI.

## 7. Reproducing the evidence

```bash
git fetch origin && git checkout de3bdc1          # or each frozen head in §1
ruff check agora_tracker tests
mypy agora_tracker/trading
export TEST_DATABASE_URL=postgresql+psycopg://…/tracker_test     # disposable database only
pytest tests/test_wallet_*.py tests/test_trading_*.py tests/test_preflight.py tests/test_verify_ingress_proxy.py -q
python -m agora_tracker.preflight --graph-only                  # single linear migration head
# no key material in the wallet/ledger packages (expect no hits besides docs/comments):
grep -rniE "private_key|mnemonic|seed phrase|keystore|sign_transaction" agora_tracker/trading/wallet agora_tracker/trading/ledger
# no send/sign RPC in the allowlist:
python - <<'PY'
from agora_tracker.trading.wallet.rpc import READ_METHODS, FORBIDDEN_PREFIXES
assert not any(m.startswith(FORBIDDEN_PREFIXES) for m in READ_METHODS); print(sorted(READ_METHODS))
PY
git diff --stat main...HEAD -- agora_tracker/web agora_tracker/collector.py agora_tracker/engine.py agora_tracker/rpc.py   # expect nothing
```
CI results per head are in §1 and `docs/RELEASE_READINESS.md`. The human reviewer should re-run
everything on the exact candidate SHA (full `pytest -v -rs` in CI includes the browser suites).

## 8. Real-endpoint checks (no funds, no flag)

`python -m agora_tracker.trading.wallet.rpcdiag` on the VPS with the real execution and
corroboration endpoints (`docs/TRADING_RPC_DIAGNOSTICS.md`): chain id, freshness, continuity,
`finalized`/`safe` tags, historical block-parameter honouring, archive depth, receipt consistency,
reorgs, 429/Retry-After, latency percentiles, cross-provider agreement. Proxy:
`scripts/verify_ingress_proxy.py --local`. Devices: `docs/TRADING_DEVICE_ACCEPTANCE.md`.

## 9. Preconditions for #96 (NOT started; each needs explicit owner authorization)

The full checklist, canary parameters, stop conditions and drills are in
`docs/RELEASE_C2_CANARY.md` (prepared, not executed). In short: this review signed for the exact
SHA; F-04 closed or explicitly accepted by a named approver on real measurements; F-07/F-08/F-15
evidence attached; F-17/F-19/F-20 resolved; a written owner authorization naming wallet, token,
amounts, gas and window.

## 10. Sign-off — BLOCKED

| Role | Name | Date | Heads reviewed | Decision |
|---|---|---|---|---|
| Independent human security reviewer | _pending_ | _pending_ | candidate SHA: _pending_ | **NOT SIGNED** |
| Owner (authorizes #96 separately) | _pending_ | _pending_ | — | **NOT AUTHORIZED** |

Re-review rule: any change after sign-off to `agora_tracker/trading/**`, the migrations, the
nginx templates or the RPC/finality configuration is security-sensitive; the reviewer decides
whether it needs re-review before #96.
