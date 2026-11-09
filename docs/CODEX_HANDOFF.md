# Handoff (Stage C trading) — keep this file current at the END of every session

Start of a session: compare the PR/branch table below with `gh api repos/:owner/:repo/pulls` and
`git log`; fix drift first. End of a session: update "Exact next step" and "Blockers".

## State (2026-11-09)

* `main` = #100 only. Everything else is a stacked draft chain:
  #107 → #108 (F-01) → #110 (F-16) → #111 (F-02) → #112 (F-15) → #113 (F-04/F-07) → #114 (F-18)
  → #115 (F-21/F-17/preflight) → #116 (release package) → **#117** integration PR to `main`
  (draft, branch `release/trading-c2-rc1`).
  #101–#106 were merged into their stack bases, not into `main`. #109 is stale (`8985c1f`).
* Production flags: all trading switches OFF; nothing deployed from this work; no funded test.
* Migrations: `0001…0016` linear (`python -m agora_tracker.preflight --graph-only`). Clustering
  branch must become `0017` on `0016`.
* #95 NOT signed; #96 NOT authorized.

## Where things are

| Topic | Doc | Code | Tests |
|---|---|---|---|
| Review package / findings | `SECURITY_REVIEW_C2.md` | – | – |
| Readiness, GO/NO-GO, owner checklist | `RELEASE_READINESS.md` | – | – |
| Canary plan (not executed) | `RELEASE_C2_CANARY.md` | – | – |
| On-call / incidents / rollback | `TRADING_RUNBOOK.md` | `/trading_status`, `/wallet_health` | `test_trading_status.py` |
| Finality (F-04) | `TRADING_FINALITY.md` | `wallet/corroboration.py` `Session.finality` | `test_wallet_corroboration*.py` |
| RPC diagnostics (F-07) | `TRADING_RPC_DIAGNOSTICS.md` | `wallet/rpcdiag.py` | `test_wallet_rpcdiag.py`, `rpcmock.py` |
| Corroboration (F-15) | `TRADING_RPC_CORROBORATION.md` | `wallet/corroboration.py`, `ledger/evidence.py` | as above |
| Liveness (F-18) | `TRADING_LIVENESS.md` | `trading/liveness.py` | `test_trading_liveness.py` |
| Ingress + proxy (F-16/F-17) | `TRADING_INGRESS.md` | `trading/ingress.py`, `deploy/nginx/`, `scripts/verify_ingress_proxy.py` | `test_wallet_ingress*.py`, `test_verify_ingress_proxy.py` |
| Owner recovery (F-02) | `TRADING_LEG_RECOVERY.md` | `wallet/recovery.py` | `test_wallet_recovery.py` |
| Devices (F-08) | `TRADING_DEVICE_ACCEPTANCE.md` | – | browser tests (emulation only) |
| Migrations | `DATABASE.md` | `agora_tracker/preflight.py` | `test_preflight.py` |

## Commands

```bash
ruff check . && mypy agora_tracker
python -m agora_tracker.preflight --graph-only
TEST_DATABASE_URL=postgresql+psycopg://…/tracker_test pytest -q   # full suite; CI runs browsers too
```

## Exact next step

Owner: hand `SECURITY_REVIEW_C2.md` + the #117 head SHA to an independent reviewer, and run
`rpcdiag` on the VPS (24 h). Engineering: none required before that evidence exists; do not start
#96, #97 optimizations or #98.

## Blockers

Independent review; real RPC evidence (F-04/F-07/F-15); VPS proxy verification (F-17); ingress
networking decision (F-19); wallet RPC decision (F-20); device matrix (F-08); named approvers.

## Rules for any session

One issue/finding per session; focused tests while editing, full CI at the PR; never enable flags,
deploy, restart, sign, approve or spend; never paste secrets, RPC URLs, wallet addresses or logs
with user data; AI review is not independent approval.
