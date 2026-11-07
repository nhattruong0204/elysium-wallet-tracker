# Trading security — staged design, review required

Live execution is absent, not merely hidden behind a boolean. There is no signer,
private-key input, signed-byte endpoint or RPC write transport. `PaperFlow.broadcast`
unconditionally refuses. `trading.paper_enabled` defaults false and accepts booleans only;
there is no live_enabled setting. Production has no QuoteProvider or approved route.
The paper module has no public web API and no dashboard/public_api additions.

## Stage B controls implemented

- Existing Telegram from.id/admin allowlist checked before paper dispatch; a group never
  edits an order panel or executes an action for other members. Initial selection goes to
  the caller's own DM. Every subsequent action requires matching private user/chat IDs.
- Token CA, chain 99901 and original chat/message bound to opaque server-side sessions.
  Callback text is a selector, never authorization. Separate users cannot access a session.
- Three-minute expiry, bounded 256-session storage, duplicate alert selection reuse,
  serialized operations, cancellation, pause, fresh review and one-time PAPER confirmation.
  Restart invalidates all sessions; no stale callback can recover a previous order.
- Exact integer wei parsing; 1 HYPE paper cap, 1% max slippage/impact, 30-second quote
  expiry, matching input/route, gas/balance checks and exact minOut. Sell percentages refer
  to a future verified wallet token balance, never an invented bot position.
- Empty production quote/route configuration fails closed. Tests inject clearly named
  fixture providers only. No unavailable quote, balance or fee is converted to zero.

## Mandatory unresolved gates before Stage C / any real funds

Peer review has NOT occurred. This code is not an audited live trading implementation.
Verify official current docs, route ABI/code/factory/pool/hook/ownership/fees and submission
semantics; implement verified real quotes and simulations for supported routes only.
Do not infer a swap ABI from attribution events or bytecode presence. Token taxes, sell
restrictions, multi-hop, Permit2 and approval lifecycle remain unsupported.

Design noncustodial wallet ownership proof, Telegram Mini App init-data signature/freshness,
CSRF, one-time reviewed intents, typed destination/recipient/value/deadline/minOut checks,
finite approvals and a strict destination/code allowlist. No generic calldata endpoint.
Use a separate execution process/private DB role and authenticated ingress; no dashboard
trading authority. Review nonce reservation, concurrent orders, ambiguous timeouts,
same-byte rebroadcast, explicit replacement policy, receipt/reorg reconciliation and audit
retention before creating a broadcast path. A hash acknowledgment is not a fill.

Bot-token compromise must not authorize wallet spending. Never send seeds/private keys
through Telegram or store them in .env, Postgres, source, CI or logs. Optional hot-wallet
mode is a separate design/review with isolated KMS signing, revocation and strict limits.
No such mode exists here. Obtain explicit owner approval for a bounded live BUY and SELL
and for enabling real execution, in addition to independent review. No implicit consent.
