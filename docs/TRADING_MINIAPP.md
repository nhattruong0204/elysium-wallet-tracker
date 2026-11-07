# C1 Mini App — issue #88

This is an unsigned preview UI, reusing the merged Stage B PAPER implementation.
There is no transaction-signing, approval, Send, signed-byte or broadcast endpoint.
Issue #89 adds optional ownership authentication; see [TRADING_AUTH.md](TRADING_AUTH.md).
The wallet-method exception is personal_sign over a checked, server-issued SIWE
message, granting private preview access only. The review button reads accounts/chain again and displays the
existing provider's exact unsigned payload. The final button is permanently disabled.

## Scope and integration boundary

- Existing IntelBot consumes Telegram updates. Its existing Buy/Sell alert callbacks
  open the caller's private PAPER panel. Optional `trading.miniapp_url` adds a
  private-chat-only `web_app` button; defaults and original alert buttons are preserved.
- That button carries only an opaque 16-character intent ID in a URL fragment.
  Token, chain 99901, side, original chat/message, wallet and expiry come from
  `PaperFlow`. Client token/chain/source fields cannot override them. The fragment
  is removed from browser history immediately; it grants no authentication.
  Intents expire within 180 seconds; existing provider quotes expire within 30.
- `agora_tracker.trading.miniapp.create_app(flow, auth)` is an isolated Starlette
  factory. It must receive the **same** in-process `PaperFlow` as IntelBot, not a
  second bot/update consumer or a reconstructed flow. Do not mount it on the
  public research dashboard. No production listener/deployment has been added.
- Default `DenyAuth` refuses every API request. There is no environment flag or
  production mock-auth implementation. Tests alone inject `MockAuth` and return
  `Principal(..., fixture=True)`; the page displays an explicit fixture banner.
  Tests do not prove Telegram identity or wallet ownership.
- #89 supplies AuthService for the existing authentication boundary. The default
  still denies and no production listener is wired. Raw initData and a watch
  address alone never prove ownership. See [TRADING_AUTH.md](TRADING_AUTH.md) for
  the exact session/CSRF, replay, EOA proof, private-store and review policies.
  The proven address must equal the intent's existing watch wallet.
- #90 owns subsequent executable-state validation. This UI's identity/route/expiry
  checks and existing PAPER validations do not replace it. The auth adapter does not implement #90. Do not connect a signer to this UI.

## Behavior and supported routes

BUY 0.1/0.5/1 HYPE or custom input; SELL 25/50/100% or custom percentage of the
verified token balance. Slippage selections are 50/100 bps. Existing 1 HYPE buy cap,
1% impact bound, balance, integer calculations and pinned-block provider checks apply.

Only verified canonical single-hop V2 HYPE↔token routes are previewed. Tokens in
alerts are not necessarily supported. Unsupported routes refuse and expose the
server-derived Agora token page. The outer Agora router, V3/V4 and other routes
remain disabled. This is not a new route discovery or execution implementation.

The page distinguishes authentication required, disconnected, ownership unverified,
verified wallet, loading, unavailable route, stale quote, expired intent, unsigned
review, cancelled and signing disabled. It shows input/output/minimum, pool fee,
gas estimate/reserve, allowance condition, impact, route/pool, block, observation
time, deadline and exact from/to/value/calldata. All large integer amounts use JSON
decimal strings; formatting never converts financial amounts to JavaScript Number.
Expected output assumes normal transfers. A successful read-only call does not
guarantee future execution; missing allowance is explicitly conditional, with gas
unavailable. No hypothetical order is written by this preview UI.

Account/chain changes, changed input/slippage, quote/intent expiry, background
activity and cancellation discard the unsigned review. Generation IDs discard
late requests after invalidation. Wallet listeners are detached on cancellation,
expiry and page exit. Restoring a cached page requires reopening a fresh session.

CSP excludes inline/eval scripts, objects and arbitrary network destinations.
Rendering uses DOM text/value properties, not HTML insertion. Responses are
no-store/no-referrer/nosniff; request bodies are bounded and cross-origin requests
are refused when an Origin is present. Authentication must supply the remaining
security policy before deployment. The official Telegram SDK is an explicitly
trusted external script; CSP does not protect against compromise of that SDK or
a malicious wallet provider. No wallet seed/private-key inputs exist.

## Browser evidence and limitations

Automated checks run the actual Starlette adapter and existing DirectV2Provider,
with deterministic read-only RPC fixtures and explicitly mocked Telegram identity
and EIP-1193 wallets. No live wallet, bot, RPC or Telegram message is used.

| Environment | Evidence | Status |
| --- | --- | --- |
| Linux Chromium 156.0.8078.4, 1280×844 | Actual browser E2E, mock injected provider | Tested |
| Linux Chromium, 390×844 | Mobile layout, touch-target sizing and E2E | Tested; not an iOS device |
| Linux Playwright 1.64.1 / WebKit build 2370, iPhone 13 profile | Viewport/touch emulation and E2E | Tested; not iOS Safari or Telegram WebView |
| HTTPS loopback, self-signed test certificate | Secure-context connect/add/switch on all above; certificate errors ignored **only in tests** | Tested; not a public HTTPS deployment |
| No injected provider | Copy same-app opaque link / Telegram `openLink` fallback; fresh authentication required | Mock-tested |
| Physical iPhone + Telegram iOS WebView | Actual app/OS/wallet behavior | **Not tested; acceptance evidence still required** |
| MetaMask, Rabby, Coinbase Wallet or WalletConnect on a real device | Native return links and ownership authentication | **Not tested/supported claims absent** |

An ordinary external browser may also lack a wallet provider. Opening a link does
not install one or establish authentication. This milestone intentionally uses the
standard injected EIP-1193 interface, with an honest fallback when unavailable;
there is no WalletConnect/deep-link compatibility claim or universal iPhone support.
The external session must authenticate again; never put initData/auth tokens in
the copied link. Real external-browser auth integration belongs to #89.

Verification commands (CI installs Chromium and WebKit with their OS dependencies):

```sh
python -m pytest -q tests/test_miniapp.py tests/test_miniapp_browser.py \
  tests/test_paper_trading.py tests/test_verified_paper.py
ruff check .
mypy agora_tracker
python -m pytest -v -rs
```

Coverage includes mocked alert→private launcher→connect/switch→quote→exact unsigned
review, buy/sell presets/custom inputs, slippage invalidation, wrong chain, rejected
switch, unknown-chain addition, account change, unsupported route, stale intent/
quote, HTML injection, background activity, listener cleanup, delayed responses,
default authentication refusal, user separation, large integer encoding and absence
of execution endpoints. The existing alert and PAPER tests also run unchanged.

## Required manual device smoke before closing #88

Use a separately approved nonproduction HTTPS host and a reviewed authentication
integration. Keep production services and all execution flags unchanged.

1. Record exact iPhone model, iOS, Telegram, external browser and wallet app versions.
   Launch from an allowlisted alert into the existing private panel; confirm the
   server-derived token/chain/source and expiry.
2. In Telegram WebView, test provider detection, connection refusal/acceptance and
   chain 99901 switching. If no injected provider exists, verify the explanation,
   copied link and external-wallet browser fallback; authenticate independently.
3. Test BUY and SELL with a public watch wallet and supported V2 token, all presets,
   custom amount/percent, refresh, slippage, fees/gas/minimum/deadline and exact
   unsigned review. Never request a transaction or approval signature.
4. Change account/network, background/resume, expire the intent/quote, deny wallet
   prompts, try an unsupported token and Cancel. Old details must disappear and
   signing must stay disabled. Confirm existing alert delivery/PAPER remains intact.
5. Record unsupported wallets/return paths honestly. Confirm wallet activity has
   no signing/approval/send request. Independent review must precede deployment.

Issue #88 stays open until physical iPhone Telegram/external-wallet evidence and
its authentication integration acceptance are satisfied. No service was deployed
or restarted; no dependent issue was implemented.
