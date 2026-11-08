# Security review package — wallet-managed execution (Stage C2, issue #95)

> **This is an AI-prepared review package. It is NOT an independent security approval.**
> A human reviewer who did not write the code must read it, reproduce the evidence and sign the
> block at the end. Until then the state of this review is **BLOCKED on human sign-off**, and the
> separately authorized live test (#96) **must not start**. Nothing here enables execution, funds a
> wallet, deploys or restarts anything.

## 1. Scope and frozen heads

| Issue | PR (draft) | Branch | Head under review |
|---|---|---|---|
| #92 finite approvals, wallet-managed Buy/Sell | #104 | `feat/trading-c2-wallet-approvals` | `f769b42` (CI green) |
| #93 nonce/idempotency, execution RPC, reconciliation | #105 | `feat/trading-c2-nonce-idempotency` | `e6c4fcd` (CI green) |
| #94 receipts, reorgs, verified fills, status | #106 | `feat/trading-c2-receipt-reconciliation` | `de3bdc1` (CI green) |
| #95 F-01 supervised worker lifecycle | #108 | `feat/trading-c2-worker-lifecycle` | `8985c1f` (CI green; head of integration PR #109) |
| #95 F-16 Mini App / wallet ingress | #110 | `feat/trading-c2-ingress` | `80e12a6` (CI green: docker, lint, test) |

Stack base: `feat/trading-c1-unsigned-plans` (auth, plans, ledger) — **`main` contains only #100;
#101–#103 are not on `main` yet**, so these PRs cannot merge to `main` until that chain does.
PRs #100–#103 were not modified.

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
| Scanner/Postgres/web/Revival unaffected | additive migrations 0012–0014 confined to the `trading_private` schema (no `public`/scanner table altered); wallet code is imported by nothing in the collector/engine/web/Revival paths. The only touch of a shared module is `tgui.py` (a ledger-gated, exception-safe text section; returns `""` when the ledger is off) and `config.py` (one validated field) | `git diff --stat`; `test_migration_*` round-trip tests; `test_telegram_section_never_raises_into_the_command_handler` |

## 4. Threat model (STRIDE-oriented)

Assets: user funds (via the user's wallet), approvals, order/audit integrity, the owner execution
flag, the execution-RPC secret, the Telegram identity binding.

| # | Threat | Actor | Mitigation | Residual |
|---|---|---|---|---|
| T1 | Server asks the wallet for something other than the reviewed trade | compromised server / bug | Request is derived from the plan and digest-bound; browser re-checks target/spender/sender/chain/finite amount; **the wallet's own confirmation screen is the final control** | A server that also serves tampered JS defeats the in-page check (see F-11) |
| T2 | Replay/steal a request id | other user, session thief | request bound to user+session+wallet+order+digest+expiry; unknown/foreign ids are indistinguishable 404 | session theft is #101's boundary |
| T3 | Double prompt / double order | double click, retry, two tabs, crash | DB-enforced keys and partial unique indexes; single-flight JS; ambiguous never re-prompts | none known |
| T4 | Wallet sends, answer lost | network, crash | leg `ambiguous`; wallet stays blocked until **chain evidence** resolves it | needs operator tool if evidence never arrives (F-02) |
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
"Status": **open** = must be resolved or consciously accepted by a human before #96; **accepted** =
documented trade-off; **unverified** = could not be tested in this environment.

| ID | Sev | Finding | Status | Recommendation |
|---|---|---|---|---|
| F-01 | Med | Nothing started the reconciler / receipt worker or built the execution RPC at process start. | **mitigated in code (worker-lifecycle PR); awaiting human verification** | `docs/TRADING_WORKER_LIFECYCLE.md`: flag-gated, fail-closed startup, one active worker (advisory lock) + ledger-level exactly-once, bounded retries/budgets, pause-independent, private health. Evidence: `tests/test_wallet_worker.py` (47 tests incl. real PostgreSQL and the real `run_ui` wiring). Residual: see F-16 and the lifecycle doc's *Unresolved*. |
| F-16 | Med | No process entrypoint mounted the wallet/Mini App routes (`miniapp.create_app(wallet=…)` had no caller, and could not be given a matching `PlanBook` from outside), so the user-facing flow could not run end to end; found while wiring F-01. | **mitigated in code (ingress PR); awaiting human verification** | `docs/TRADING_INGRESS.md`: in-process loopback listener in `--ui`, default OFF, fail-closed startup, ingress-secret/Host/https guard, resource limits, wallet routes behind a second flag, one `getUpdates` consumer. Evidence: `tests/test_wallet_ingress.py` (54, incl. the real `run_ui` wiring and real PostgreSQL ledger) and `tests/test_wallet_ingress_e2e.py` (13, real signed Telegram/SIWE session over the real listener). Not exercised: a real proxy/TLS, browser, Telegram client or RPC (F-17, F-18). |
| F-17 | Med | The reverse proxy is a trusted component: it terminates TLS and injects the shared ingress secret, `X-Forwarded-Proto` and the client IP (which feeds the per-IP bootstrap rate limit). Anyone holding the secret who can reach loopback bypasses the edge controls (they still need Telegram identity, SIWE ownership and CSRF); rotation needs a restart. Proxy/TLS/DNS config is outside this repository and unreviewed. | open | Human review of the real vhost against `docs/TRADING_INGRESS.md` §4 (overwriting headers, single host, body/time limits, secret file mode). |
| F-18 | Low | Listener liveness is pull-only: if the ingress thread dies the bot keeps running and the Mini App is silently unavailable until `/wallet_health` is read; no auto-restart or alert push. | open | Add an alert/automatic restart before #96, or accept explicitly. |
| F-02 | Med | No owner force-resolve tool for a leg stuck `ambiguous`/`orphaned`; the (wallet, chain) pair stays blocked until chain evidence appears. | open | Owner-only, audited resolve command (requires entering evidence), never a re-prompt. |
| F-03 | Low | An approval/swap the wallet holds but did not broadcast could be sent after we abandoned the leg (approval: 1 h; swap: after deadline+margin). Swaps expire by their on-chain deadline; approvals are finite and bound to the router. | accepted | Keep amounts finite; consider revoking via a new reviewed zero-approval prompt. |
| F-04 | Med | Finality (12 confirmations) is an assumption; no finality gadget queried; reorg depth on chain 99901 unmeasured. | open | Measure on the production RPC; set `finality_confirmations` from data. |
| F-05 | Info | Telegram's in-app WebView usually has no injected wallet provider; users must open the page in a wallet browser. | accepted | UX only; the page explains instead of faking. |
| F-06 | High (blocking if used) | The current Elysium/Agora raw-submission relay / inbox permission and its exact status are **NOT ESTABLISHED** (no network in the build environment). | unverified | Not needed in wallet-managed mode. Raw-signed submission stays unimplemented and needs its own review. |
| F-07 | Med | The real execution RPC was never exercised: chain-id behaviour, head freshness, rate limits, `Retry-After`, historical `eth_call`/`eth_getBalance` support (needed for fills). | unverified | Run the read-only smoke list in §8 against the real endpoint. |
| F-08 | Med | Only a mocked EIP-1193 provider was exercised; real wallets differ (fee fields, `from` casing, rejection codes, nonce handling). | unverified | Manual matrix on the wallets the owner actually uses, with no funds. |
| F-09 | Info | Fills are `unknown` whenever another transaction from/to the wallet shares the block, for fee-on-transfer/rebasing mismatches, or when state is pruned. PnL then reads unknown. | accepted | Intended fail-closed behaviour. |
| F-10 | Low | Fill rows are immutable; a wrong `unknown` cannot be corrected in place. | accepted | Add a compensating-entry mechanism if it becomes common. |
| F-11 | Med | The in-page re-check cannot protect against a server that also serves modified JS (same origin). | accepted | Wallet confirmation screen is the control; consider SRI/CSP hash pinning of `wallet.js`. |
| F-12 | Info | `main` lacks #101–#103; #104/#105/#94 are stacked drafts. The paused clustering PR's migration number must be renumbered (now 0015). | process | Owner decides merge order. |
| F-13 | Low | Telegram status section swallows all exceptions (logs a warning). | accepted | Acceptable for a read-only display. |
| F-14 | Low | Migration 0014 downgrade drops `order_fills`. | accepted | Backup before downgrade (documented). |
| F-15 | Med | Facts about a transaction come from one execution RPC (reconciliation can use several, receipts use one). A dishonest endpoint could fabricate a receipt, a balance or a block. | open | Corroborate receipts/balances from a second independent endpoint before real funds; treat disagreement as `unknown`. |

No critical or high finding was identified *in the code*, but F-02, F-04, F-15, F-17 and F-18 are open (F-01 and F-16 are mitigated in code, unverified by a human) and
F-06/F-07/F-08 are unverifiable here. **These are the reasons the sign-off is blocked.**

## 6. What this review did NOT do

* No independent reviewer, no penetration test, no fuzzing, no dependency/CVE audit (offline), no
  formal verification, no review of the router/token contracts, no load test.
* Static checks were limited to `ruff` and `mypy`; dynamic evidence is the pytest suite.
* Nothing was run against a real chain, RPC, wallet or funded account.
* Local environment lacked `eth_keys`/`eth_abi`/`eth_hash`; the modules depending on them (the #101
  authenticator tests) ran only in CI.

## 7. Reproducing the evidence

```bash
git fetch origin && git checkout de3bdc1          # or each frozen head in §1
ruff check agora_tracker tests
mypy agora_tracker/trading
export TEST_DATABASE_URL=postgresql+psycopg://…/tracker_test     # disposable database only
pytest tests/test_wallet_*.py tests/test_trading_ledger.py -q   # includes test_wallet_ingress*.py (F-16)
# no key material in the wallet/ledger packages (expect no hits besides docs/comments):
grep -rniE "private_key|mnemonic|seed phrase|keystore|sign_transaction" agora_tracker/trading/wallet agora_tracker/trading/ledger
# no send/sign RPC in the allowlist:
python - <<'PY'
from agora_tracker.trading.wallet.rpc import READ_METHODS, FORBIDDEN_PREFIXES
assert not any(m.startswith(FORBIDDEN_PREFIXES) for m in READ_METHODS); print(sorted(READ_METHODS))
PY
git diff --stat main...HEAD -- agora_tracker/web agora_tracker/collector.py agora_tracker/engine.py agora_tracker/rpc.py   # expect nothing
```
CI (docker, lint, test) was green for `f769b42`, `e6c4fcd` and `de3bdc1` when written. The human reviewer should re-run everything on the exact heads.

## 8. Read-only smoke list for the real endpoint (no funds, no flag)

`eth_chainId == 0x1863d`; `eth_getBlockByNumber latest` is < 30 s old; `eth_getBlockByNumber N-1000`
and `eth_getBalance`/`eth_call` at a block 1000 back succeed (historical state); 429 behaviour with
`Retry-After`; a concurrent burst stays within the semaphore; observed reorg depth over a week.

## 9. Preconditions for #96 (NOT started; each needs explicit owner authorization)

1. A human reviewer signs §10 after verifying F-01 and F-16 and closing or consciously accepting F-02, F-04, F-15, F-17, F-18 and
   resolving F-06/F-07/F-08 by evidence.
2. Owner provisions the dedicated execution RPC (and ideally a second source) and approves a funded
   test wallet with a hard cap far below the policy ceilings.
3. Worker wiring (F-01, implemented in the worker-lifecycle PR) reviewed and merged; operator alerting on ambiguous legs / breaker state / `failed` worker defined (health is pull-only today).
4. Owner turns the flags on themselves (config flag + durable owner-only execution flag), with a
   written rollback (pause + flag off) and an observer present.
5. Written test plan: one buy, one finite approval, one sell; expected receipts; stop conditions.

## 10. Sign-off — BLOCKED

| Role | Name | Date | Heads reviewed | Decision |
|---|---|---|---|---|
| Independent human security reviewer | _pending_ | _pending_ | _pending_ | **NOT SIGNED** |
| Owner (authorizes #96 separately) | _pending_ | _pending_ | — | **NOT AUTHORIZED** |
