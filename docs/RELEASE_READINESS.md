# Stage C trading — release readiness (issues #82 / #85 / #95 / #96)

Prepared 2026-11-09 from the live repository state (GitHub API + `git merge-base`), not from issue
text. **This is not a security approval and not a release authorization.**

## 1. GO / NO-GO

| Decision | Verdict | Why |
|---|---|---|
| Code ready for **independent security review** (#95) | **GO** | Complete, stacked, CI-green implementation with tests for every in-code finding; one integration candidate; review package and reproduction commands ready. |
| **Security sign-off** | **NO-GO (blocked)** | No independent reviewer yet; F-04 (High) open; F-07/F-08 blocked on real endpoints/devices; F-15 independence, F-17 VPS proxy, F-19 Docker networking, F-20 wallet RPC unresolved; "accepted" items lack a named approver. |
| **Funded canary** (#96) | **NO-GO** | Needs the signed review for the exact SHA AND a separate written owner authorization (wallet, token, amounts, gas, window). |
| **Production trading enablement** | **NO-GO** | A third, separate decision after the canary. |

Code readiness ≠ authorization readiness: nothing here may be read as permission to deploy, flip a
flag or spend.

## 2. Scorecard

| Area | Status | Basis |
|---|---|---|
| Engineering (features, fail-closed design) | **PASS** | C1+C2 implemented; F-01/02/15/16/18/21/22 fixed in code with tests. |
| Security | **BLOCKED** | Independent review not started; F-04 High open. |
| Infrastructure (RPC, proxy, deployment) | **BLOCKED / FAIL** | Real RPC never exercised (F-07); proxy unverified (F-17); ingress not deployable in the shipped compose (F-19). |
| Wallet / device compatibility | **BLOCKED** | Emulation only (F-08). |
| Data integrity (ledger, migrations, evidence) | **PASS (in code)** | Append-only evidence, RLS, preflight, lock order fixed; awaits verification on the production database. |
| Operations (alerts, runbook, rollback) | **PASS (in code) / BLOCKED (drills)** | Liveness + runbook + canary plan exist; drills and real alert delivery not performed. |

## 3. Release readiness matrix

| Finding | Severity | Requirement | Current status | Evidence | Responsible party | Next action | Blocking release? |
|---|---|---|---|---|---|---|---|
| #88 Mini App | – | Physical iPhone/Telegram acceptance | Implemented (#100 on `main`); device evidence missing | `TRADING_MINIAPP.md`, browser tests (emulated) | Owner/tester | Run `TRADING_DEVICE_ACCEPTANCE.md` | Yes |
| #89 initData + SIWE | – | Independent auth review | Implemented (#101, stack base only) | auth tests (CI) | Reviewer | Review with #95 | Yes |
| #90 immutable plans | – | Review | Implemented (#102) | plan tests | Reviewer | Review with #95 | Yes |
| #91 ledger/pause | – | Review | Implemented (#103) | ledger tests | Reviewer | Review | Yes |
| #92 approvals | – | Finite, router-bound | Implemented (#104) | approval tests | Reviewer | Review | Yes |
| #93 nonce/idempotency | – | Exactly-once | Implemented (#105) | reconcile tests | Reviewer | Review | Yes |
| #94 receipts/fills | – | Truthful accounting | Implemented (#106) + F-15/F-04 | receipts/fills tests | Reviewer | Review | Yes |
| F-01 worker lifecycle | Med | Supervised worker | Mitigated in code | #108 | Reviewer | Verify | Yes (until verified) |
| F-02 owner recovery | Med | No lock release without proof | Mitigated in code (+finality, lock order) | #111, #113, #115 | Reviewer | Verify | Yes |
| F-03 unsent approval auto-abandon | Low | Explicit acceptance | Open | review §5 | Reviewer + owner | Accept by name or disable auto policy | Yes |
| F-04 finality | **High** | Documented/measured finality | Open; interim finalized-tag policy in code | `TRADING_FINALITY.md`, #113 | Operator + Kinetiq/Conduit | 24 h rpcdiag; written finality statement | **Yes** |
| F-05 WebView provider | Info | – | Accept (approver pending) | – | Owner | Name approver | No |
| F-06 raw submission | High if used | – | Not applicable | wallet-managed only | – | – | No |
| F-07 real RPC | Med | Measured behaviour | Blocked (tool done) | `rpcdiag`, #113 | Operator | Run on VPS | **Yes** |
| F-08 real wallets | Med | Device matrix | Blocked | `TRADING_DEVICE_ACCEPTANCE.md` | Owner/tester | Run matrix | **Yes** |
| F-09/F-10/F-11/F-13/F-14 | Info–Med | Accept | Accept (approver pending) | review §5 | Reviewer | Name approver | Yes (until named) |
| F-12 stack / migrations | Info | One candidate, no conflicts | Mitigated | §4 below, preflight | Owner | Close #109 in favour of the candidate | No |
| F-15 corroboration | Med | ≥ 2 independent operators | Mitigated in code; independence unproven | #112, inventory | Operator | Written operator evidence | **Yes** |
| F-16 ingress | Med | Entry point | Mitigated in code | #110 | Reviewer | Verify | Yes |
| F-17 proxy | Med | Verified vhost | Open (template + verifier + CI rehearsal) | #115 | Operator | Run verifier `--local` on VPS | **Yes** |
| F-18 liveness | Low | Push alerts | Mitigated in code | #114 | Owner | One real alert delivered | Yes (drill) |
| F-19 Docker ingress networking | Med | Reachable loopback | Open | `deploy/trading-release.compose.yaml` | Owner | Choose host network / systemd | **Yes** |
| F-20 wallet RPC suggestion | Low | Official RPC for users | Open | add-chain uses `rpc.agoradex.app` | Owner | Confirm official endpoint | Yes |
| F-21 ledger lock order | Med | No deadlocks | Mitigated in code | #115 | Reviewer | Verify | Yes |
| F-22 fast-block skew/bisection | Med | Works on 5–10 blocks/s | Mitigated in code | #113 | Reviewer | Verify | Yes |
| F-23 no UI for execution flag | Info | Intentional friction | Accept (approver pending) | `/trading_status` | Owner | Name approver | No |
| #87 PAPER pilot | – | Reviewed UI-only pilot | Blocked on an authorized window | `PAPER_PILOT.md` | Owner | Authorize window | No (independent) |
| #96 canary | – | Authorized tiny BUY/SELL | Prepared, not executed | `RELEASE_C2_CANARY.md` | Owner + reviewer | Two approvals | – |
| #97 performance | P2 | Read-only measurement | Not started (must not delay safety work) | – | – | Later | No |
| #98 launchpad adapters | P2 | Out of scope | Not started | – | – | Do not broaden | No |
| #99 maintainer docs | P2 | Handoff + runbook | Done in the release package | `CODEX_HANDOFF.md`, `TRADING_RUNBOOK.md` | – | Keep current | No |

## 4. Integration candidate

* One branch carries the whole stack: `release/trading-c2-rc1` = the release-package head, which
  contains every commit of #100–#115 (verified ancestry; #101–#106 merge commits are
  tree-identical and need not be replayed). It merges into `main` without conflicts
  (`git merge-tree`).
* The integration PR targets `main` as a **draft**; it must not be merged without the owner's
  explicit approval and review. #109 (head `8985c1f`) is superseded and should be closed by the owner.
* Merging code alone changes nothing at runtime: every trading switch defaults OFF
  (`ledger_enabled`, `wallet_execution_enabled`, `ingress_enabled`; ledger execution flag off), no
  listener is bound, no wallet request can be created, and nothing can send a transaction. Tests
  assert each of these (`test_run_ui_starts_no_listener_by_default`,
  `test_disabled_by_default_builds_nothing_even_with_no_configuration_at_all`,
  `test_default_state_says_no_wallet_requests_are_possible`, read-only RPC allowlists).
* Migrations: linear `0001…0016` (preflight PASS). The paused clustering branch must renumber its
  `0011` to `0017` (parent `0016`) after rebasing — 0017 is unused on every branch.

## 5. Remaining blockers (exact)

1. **Independent human security reviewer** for the exact candidate SHA (#95 §10).
2. **Real RPC evidence**: rpcdiag 24 h on the production execution + corroboration endpoints
   (F-04, F-07); written operator-independence evidence (F-15); Kinetiq/Conduit finality statement.
3. **VPS proxy**: verifier remote + `--local` PASS; reviewed redacted `nginx -T` (F-17).
4. **Deployment decision** for ingress networking (F-19) and the wallet RPC (F-20).
5. **Device matrix** on the wallet(s) actually used (F-08).
6. **Named approvers** for every accepted residual risk; decision on F-03.
7. **Drills**: one real liveness alert; backup + restore rehearsal; rollback to previous digest.
8. **Owner authorization** for the specific canary (separate from 1).

## 6. Owner action checklist (minimal)

- [ ] Ask an independent reviewer to take the package (`SECURITY_REVIEW_C2.md`) at the candidate SHA.
- [ ] Provide/confirm ≥ 2 independently operated chain-99901 RPC endpoints; run rpcdiag on the VPS
      (`TRADING_RPC_DIAGNOSTICS.md` §2) for 24 h; attach the JSON.
- [ ] Get a written statement from Kinetiq/Conduit on finality/`finalized` tag semantics.
- [ ] Decide F-19 (host network vs systemd) and F-20 (wallet RPC); decide F-03.
- [ ] Render/install the nginx template on a **staging** host; run `verify_ingress_proxy.py` (remote
      + `--local`); then the same on production when authorized.
- [ ] Run the device matrix with an unfunded wallet.
- [ ] Close #109 in favour of the integration PR (when you agree).
- [ ] Only after all of the above: written authorization for the canary (`RELEASE_C2_CANARY.md`).

## 7. CI evidence

| Head | PR | lint | test | docker | ingress-proxy |
|---|---|---|---|---|---|
| `bd6d32c` | #112 | ✅ | ✅ | ✅ | – |
| `013e861` | #113 | ✅ | ✅ | ✅ | – |
| `a66a9d0` | #114 | ✅ | ✅ | ✅ | – |
| `7e5f95e` | #115 | pending | pending | pending | pending |

(The release-package and integration heads are recorded in their PR descriptions; a final
docs-only commit updates this table.)

Local environment note: `tests/test_trading_auth_http.py`, `test_trading_auth_browser.py`,
`test_verified_paper.py`, `test_miniapp*.py`, `test_unsigned_plan*.py`, `test_paper_hardening.py`,
`test_activity_browser.py` fail here identically on the #111 base (no `eth_abi`/WebKit, self-signed
TLS) and pass in CI.
