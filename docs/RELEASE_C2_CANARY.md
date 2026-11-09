# #96 release candidate checklist and canary plan — PREPARED, NOT EXECUTED

Nothing in this document has been run. No funded test, approval, deployment, restart or flag
change is authorized by it. Two separate written approvals are required **before** step C1, and a
third, separate one before production enablement (§E).

1. **Security approval** (#95): an independent human reviewer who did not implement the code signs
   `docs/SECURITY_REVIEW_C2.md` §10 for the **exact** candidate SHA below.
2. **Owner authorization for this specific funded test**: wallet, token, amounts, gas, time window.

## A. Candidate (fill at freeze time)

| Item | Value |
|---|---|
| Code SHA | `________` (integration PR head after review; see RELEASE_READINESS.md) |
| Image digest | `elysium-wallet-tracker@sha256:________` (built once from that SHA) |
| Rollback digest | `elysium-wallet-tracker@sha256:________` (current production) |
| Postgres image | `postgres:16-alpine@sha256:________` |
| Schema head | `0016` (preflight PASS before/after) |
| Reviewer sign-off | link / date |
| Owner authorization | link / date / scope |

## B. Preconditions (all must be checked)

- [ ] F-04: `rpcdiag` 24 h run: `finalized` served by every source, trailing latest by > 2 s,
      advancing; same finalized ancestry. Kinetiq/Conduit finality statement on file.
- [ ] F-07: rpcdiag PASS on the exact execution + corroboration endpoints (historical block param,
      archive depth ≥ 10 000, no ignored block parameter, transport healthy at worker budgets).
- [ ] F-15: written evidence that the ≥ 2 operators are independent (provider inventory complete).
- [ ] F-17: `verify_ingress_proxy.py` remote + `--local` PASS against the real vhost; human review
      of the redacted `nginx -T`.
- [ ] F-19: ingress network decision applied (host networking or host systemd).
- [ ] F-08: device matrix rows 1–16 PASS for the wallet actually used.
- [ ] F-18: one real alert delivered to an owner's private chat (e.g. by stopping the test ingress).
- [ ] Backup + restore rehearsal done within 24 h; preflight PASS.
- [ ] Liveness, `/wallet_health`, `/trading_status` checked; scanner/dashboard/Revival healthy.

## C. Canary (only with both approvals; one owner at the keyboard; one tester's wallet)

| Parameter | Proposed value (owner to confirm) |
|---|---|
| Wallet | dedicated, low-value, noncustodial test EOA used for nothing else |
| Pair | one explicitly allowlisted, verified canonical V2 HYPE↔ERC-20 pair with deep liquidity |
| Per-trade notional | ≤ 1 HYPE equivalent (owner sets exact) |
| Daily notional cap | ≤ 2 HYPE equivalent (ledger risk policy) |
| Slippage | ≤ 1 % |
| Max gas | ledger/plan gas ceiling; abort if the wallet shows higher |
| Approval | finite, exact sell amount, router-bound — only if the SELL needs it |
| Time window | one pre-agreed 2-hour window |

Steps (stop on the first surprise):
1. C1 Config for the window only: `wallet_execution_enabled: true`, `ingress_enabled: true`;
   ledger execution flag ON by the owner (audited); `/trading_status` → YES.
2. C2 **BUY**: from a private alert → Mini App → review exact plan → the wallet shows exactly the
   reviewed `to`/`value`/`data`/chain → owner confirms in the wallet. Record tx hash.
3. C3 Verify: receipt status, inclusion block, `chain_evidence` inclusion row with ≥ 2 operators,
   fill `verified` (method + source agreement), actual token received vs quote, gas paid; wait for
   the leg to become **final** (finalized tag) and record the time it took.
4. C4 **Approval** (if needed): exact finite amount, spender = router; record; allowance after.
5. C5 **SELL**: same as BUY; record HYPE received, fees; final.
6. C6 Reconcile independently: balances before/after from two explorers/RPCs; accounting matches
   the ledger to the wei (fees separate). Allowance after SELL = 0 or the expected remainder.
7. C7 Turn OFF: ledger execution flag off, `/pause_trading`, config flags off at the window end;
   `/trading_status` → NO.

**Stop conditions (immediate `/pause_trading`, flags off, incident note):** any wallet prompt that
differs from the review; any spend above the cap; any `conflict` evidence row; any fill not
`verified` for a mined swap; finality not reached within 30 min; any alert other than expected;
scanner lag rising; any data in logs that should not be there; a second `getUpdates` consumer.

## D. Drills (mock fixtures / non-money, before C1)

Crash of `ui` mid-flow (restart; no duplicate leg, pause preserved); RPC ambiguity (stop one
corroboration source → nothing recorded); duplicate Telegram callback; cancel and pause during a
pending prompt; delayed inclusion; rollback to the previous digest and post-rollback validation.

## E. After the canary

Production enablement is a **separate** owner decision after reviewing the canary record and the
reviewer's assessment of whether any change since sign-off needs re-review. Copy-trading,
unattended trading and server-side signing remain out of scope.
