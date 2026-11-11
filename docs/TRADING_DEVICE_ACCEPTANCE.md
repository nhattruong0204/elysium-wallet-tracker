# Physical device and wallet acceptance (#95 F-08, #88)

Status: **BLOCKED — no physical device, Telegram account or real wallet is available to the build
environment.** Everything below is the procedure and the evidence form the owner (or a delegated
tester) must complete. Nothing in CI counts: Playwright Chromium/WebKit with a mocked EIP-1193
provider is **emulation**, not iOS, not Telegram's WebView and not a real wallet.

## 1. Rules for this test

* Unfunded test identities only: a fresh wallet with **zero** balance (or dust for nothing), a test
  Telegram account that is an allowed bot user, a non-production HTTPS host behind the reviewed
  proxy (`docs/TRADING_INGRESS.md` §4a). Production services and all execution flags unchanged.
* Allowed wallet actions: **connect, account selection, switch/add chain 99901, `personal_sign` of
  the server-issued SIWE message.** Forbidden: any ERC-20 approval, any transaction request, any
  signature other than the SIWE message. If a wallet ever shows a transaction or approval prompt,
  **reject it, stop, and record a FAIL** (it would be a defect).
* Record exact versions; take screenshots of each wallet prompt (blur addresses before sharing).

## 2. Matrix to fill

Status per cell: PASS / FAIL / N-A (with reason) / BLOCKED. "Evidence" = screenshot id or note.

| # | Scenario | iPhone · Telegram WebView · injected provider | iPhone · Telegram → external wallet browser (MetaMask) | iPhone · Telegram → external wallet browser (Rabby/OKX/other actually used) | Android · Telegram WebView (if supported) |
|---|---|---|---|---|---|
| 1 | Launch from the private alert; server-derived token/chain/expiry shown; `initData` bootstrap succeeds | | | | |
| 2 | No provider: honest explanation + copy-link / `openLink` fallback; no secrets in the link | | | | |
| 3 | Connect: account list, select account; displayed account = selected | | | | |
| 4 | Chain: switch to 99901; add-chain when unknown (note the RPC URL the page proposes) | | | | |
| 5 | SIWE: message shows domain, chain 99901, nonce, expiry; signature accepted once | | | | |
| 6 | Replay: reuse the same signature/nonce → refused | | | | |
| 7 | Expired session (wait > 120 s) → re-authentication required | | | | |
| 8 | Account switch in the wallet → old review cleared, re-auth required | | | | |
| 9 | Chain switch away from 99901 → review cleared, prompt to switch back | | | | |
| 10 | Background Telegram / switch to wallet app and back → state consistent, no duplicate prompt | | | | |
| 11 | Reject the connect / switch / SIWE prompt → clear message, recoverable | | | | |
| 12 | Interrupted browser session (kill app mid-flow) → fresh start, nothing pending | | | | |
| 13 | Cookies: `__Host-` session cookie set; SameSite=Strict does not break the return from the wallet (if it does: FAIL with details) | | | | |
| 14 | Unsupported EIP-1193 behaviour (missing `wallet_addEthereumChain`, odd error codes) handled without a stuck UI | | | | |
| 15 | BUY and SELL **unsigned review** only (presets, custom, refresh, slippage); signing stays disabled | | | | |
| 16 | No wallet ever displayed an approval or transaction request | | | | |

Header for each column: device model · iOS/Android version · Telegram version · wallet app +
version · date · tester.

## 3. Step-by-step (per column)

1. Prepare: non-production host up, verifier `scripts/verify_ingress_proxy.py` PASS against it;
   `trading.ingress_enabled: true`, `wallet_execution_enabled: false` (unsigned review only).
2. From the test Telegram account open a private alert → the Mini App button. Rows 1–2.
3. Connect the test wallet; select an account; accept/deny chain switching. Rows 3–4, 9, 11.
4. Authenticate (SIWE). Then copy the signed message/signature from the wallet history if the
   wallet exposes it and attempt a second submission via a fresh tab (row 6) — expect refusal.
5. Wait 3 minutes; continue → re-auth required (row 7). Switch accounts in the wallet (row 8).
6. Background/foreground, kill and reopen (rows 10, 12). Inspect cookies where the browser allows
   (row 13).
7. Walk BUY and SELL reviews (row 15); confirm the page shows signing disabled and the wallet shows
   nothing to sign (row 16).
8. File results here (table) and attach screenshots to #88; any FAIL blocks #96.

## 4. What is already verified (emulation only)

`tests/test_miniapp_browser.py`, `tests/test_trading_auth_browser.py`,
`tests/test_unsigned_plan_browser.py`: Chromium desktop/mobile viewport and WebKit iPhone-13
profile with a **mock** provider: connect/switch/add-chain, wrong chain, account change, rejection,
expiry, injection, delayed responses. These prove the page logic, not device or wallet behaviour.

## 5. Known compatibility questions to answer with the matrix

* Telegram iOS WebView usually injects **no** provider (F-05): the external-browser path is the
  realistic one; rows 2, 10, 13 matter most.
* The add-chain request proposes `https://rpc.agoradex.app` as the wallet's RPC. Whether that is the
  right RPC for users' wallets (vs. the operator-confirmed official endpoint) is an owner decision;
  record what each wallet stores.
* SameSite=Strict cookies and the round trip through a wallet app: may require the user to restart
  from the alert. Record actual behaviour.
