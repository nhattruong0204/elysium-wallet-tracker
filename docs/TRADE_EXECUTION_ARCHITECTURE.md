# Trade execution architecture — Issue #82

Stage A is read-only research. Stage B is an isolated PAPER intent flow. There is no
signer or broadcast implementation. Draft Stage B now has a canonical direct V2
read-only quote/simulation adapter; its scope and limits are in TRADING_OPERATIONS.md. Enabling
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
announced public launch on November 9, 2026. The earlier cloud session could not access these docs; the WSL revalidation below
supersedes that network limitation. Do not infer current submission permissions, fee applicability, code safety,
ABI or fast-lane access from this description. Bytecode presence is insufficient.

## WSL revalidation — November 7, 2026

The cloud network restriction does not apply here: all five starting documentation
pages loaded directly in WSL. The [official developer documentation](https://docs.agoradex.app/developers)
now establishes an authorized public v1 API and links its
[OpenAPI specification](https://api.agoradex.app/api/v1/openapi.json).
`GET /api/v1/quote` supports token, side, decimal amount, slippageBps and optional
recipient; omitting recipient yields an indicative quote without a transaction.
The documented limits are 30 requests/minute/IP without a key and 300 with a key.
A wallet-created API key is optional for this research; no wallet login or secrets used.
The spec currently supplies an example rather than a typed response schema. Validate
responses explicitly and treat added/unknown fields as data, never authority to execute.

Six public buy observations (AGORA and CHAMELEON, 0.1/0.5/1 HYPE) are recorded in
`benchmarks/agora-quotes-wsl-2026-11-07.json`. Provider-ranked venues change with size.
Those `venues` entries are provider reports, not independently queried direct venues.
No balance, gas estimate, simulation or recipient-specific fee is established. All six
returned `feeBps: 0` without a recipient, while the fee page describes a standard 0.5%
fee. Do not use either value as an independently validated applicable fee.

The [contract reference](https://docs.agoradex.app/reference/contracts) confirms the
candidate router address and describes its separate executor, net-output protection,
fee cap and owner permissions. It also identifies Agora V2 factory/router addresses.
These are publisher assertions, not our deployed-source/ABI verification. The page
mentions a contracts repository/audit directory but does not link a source repository.
The explorer's conventional Blockscout/Etherscan source API paths returned HTTP 404.
No canonical ABI/source match or immutable supported route was established; the
execution allowlist stays empty. Do not infer swap selectors from returned calldata.

[Routing docs](https://docs.agoradex.app/trading/routing) describe V2/V3/V4, hooks,
curves and multi-hop. That is Agora's supported coverage, not this bot's coverage.
The decision matrix above remains valid for execution; the Agora row gains indicative
read-only public API capability. Approved quote/simulation providers remain absent.

On November 7 at 14:21 GMT+7, `GET https://rpc.eleos.fun` reported `net=mainnet`,
`mode=inbox`, `dry=false`; sanitized status is in `benchmarks/rpc-status-wsl-2026-11-07.json`.
This is endpoint self-reporting, not proof of public sequencer access, inclusion or
settlement. Agora GET returned HTTP 405; its POST JSON-RPC reads work as measured.
The wallet docs advertise transaction forwarding but do not establish direct sequencer
permissions. Current submission mode must be verified independently for each provider;
neither a launch calendar nor chain ID authorizes a write probe.

Remaining research gates: obtain publisher-linked source/ABI/build identity and review
permissions; verify actual unsigned swap semantics, token taxes/approvals and simulations;
independently compare direct quotes/net output/gas with the same pinned workload. Stage A
routing verification remains incomplete even though public API discovery is resolved.


## Canonical V2 PAPER adapter (November 7 continuation)

`agora_tracker/trading/contracts/v2.json` pins official published Uniswap artifacts,
ABI fragments and deployed runtime keccak hashes. Router immutables match the official
Agora V2 factory and wrapped HYPE addresses; factory and pair runtimes match exactly.
`verify_trade_contracts.py` reproduces those comparisons without signing or writes.
This supports single-hop HYPE ↔ ERC-20 pool scenarios only, checked again at every quote.
The fee-on-transfer-supporting payload enforces the minimum in read-only simulation;
its void return does not establish an exact delivered output. Missing SELL allowance
is a conditional PAPER scenario with unknown gas, not successful simulation or readiness.
The separate public Agora v1 comparison does not verify its opaque outer router ABI.
All live signing, approval, submission and settlement gates remain unresolved.
