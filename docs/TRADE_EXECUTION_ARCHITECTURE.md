# Trade execution architecture — Issue #82

Stage A is read-only research. Stage B is an isolated PAPER intent flow. There is no
production signer, swap adapter or broadcast implementation in this stage. Enabling
paper controls must never enable execution. Existing scanner/outbox delivery and the
single Telegram update consumer are preserved.

## Evidence and route decision matrix

| Candidate | Evidence | Current capability | Release gate |
| --- | --- | --- | --- |
| AgoraRouter 0xaf8dec958df2bd664aa661716a8de5fe0417a295 | Address supplied in #82; bytecode probe only | Research, existing Agora link | Published stable builder/ABI, code identity, ownership/fees and semantics verified |
| Kleos/BOXA/ElysPump | Existing token-page links | Wallet/DEX link fallback | Verified factory/router/pool ABI and representative quote/simulation fixtures |
| Ascend V4 hooks | Existing attribution/indexing code | Research and platform link | Hook/pool/approval/deadline/minOut review; indexing ABI is not a swap ABI |
| Montra/Obol | Owner-verified token URLs | Token-page fallback | Verified swap construction, fees and token behavior |
| Other curves, V2/V3/V4, graduated/multi-hop routes | Not verified for execution | Reject native execution | Explicit adapter fixtures and on-chain contract verification |

A web link does not establish execution support. No invented router selectors or private
browser/session APIs are used. No unavailable quote, fee, balance, allowance or output is
rendered as zero or as a successful trade. Measured routing comparisons remain unrun until
a supported interface and identical representative trade workloads are available.

## Boundaries

Alert -> authenticated callback -> private user-bound expiring intent -> reviewed quote ->
manual noncustodial signing -> isolated broadcast -> receipt reconciliation. Route/quote,
signing, submission acknowledgment, inclusion and settlement are separate measurements.
The current stage ends before signing. No public dashboard endpoint accepts trade data.

The tracker sends alerts; IntelBot alone consumes updates. Optional controls reuse the
existing alert payload and preserve notifier fallback. Group controls cannot spend funds;
private actions require the callback user's allowlisted identity and matching private chat.
Order authorization cannot come from callback_data, a ticker, a group chat ID or bot token.

Future execution uses a separate process, least-privilege private trade role, bounded RPC
pool and serialized signer/chain nonce reservations. It must not run within indexing DB
transactions. Telegram tokens are messaging credentials, never wallet signing keys.

## Authoritative sources requiring current verification

- https://docs.agoradex.app/reference/contracts
- https://docs.agoradex.app/trading and /trading/routing
- https://docs.agoradex.app/start/wallet and /reference/fees

The issue's starting research identifies chain 99901, not unrelated chain 1339, and an
announced public launch on November 9, 2026. Docs access is blocked in the current cloud
network. Do not infer current submission permissions, fee applicability, code safety,
ABI or fast-lane access from this description. Bytecode presence is insufficient.
