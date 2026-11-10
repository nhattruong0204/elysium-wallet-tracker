# Trading security — independent review required

Live execution is absent. There is no private-key input, transaction signer, approval sender,
signed-byte endpoint or transaction broadcaster. `PaperFlow.broadcast` always refuses.
`trading.paper_enabled` defaults false and has no corresponding live-enable setting.
PAPER data is never exposed through the dashboard or public PostgreSQL views.

## Optional C1 authentication — independent review still pending

Issue #89 adds private HTTPS Telegram/SIWE ownership proof for read-only preview
access. See [TRADING_AUTH.md](TRADING_AUTH.md) for cryptography, replay, storage,
cookie/CSRF, revocation and trust assumptions. The factory still defaults to deny;
no production startup or spending authorization was added. Ownership proof is
separate from transaction signing. Independent approval is still required.

## Exact C1 unsigned plans — review is not execution

Issue #90 adds immutable, ownership-session-bound V2 plans and fresh pinned-state/
exact-payload simulation. See [TRADING_UNSIGNED_PLANS.md](TRADING_UNSIGNED_PLANS.md)
for API restrictions, runtime evidence, gas/allowance/expiry bounds, race protection
and remaining trust assumptions. No transaction signing or approval/broadcast API
exists. Conditional SELL is not a successful simulation or fill. Independent
security review and separate owner authorization remain required before spending.

## Private order ledger, budgets and durable pause — review is not execution

Issue #91 adds a private PostgreSQL ledger (schema `trading_private`, role `trading_rw`) with a
database-enforced state machine, immutable reviewed-intent columns, a hash-chained append-only
audit log, row-level owner isolation, per-trade/daily budgets and a **durable** emergency pause.
Both its switches (`trading.ledger_enabled`, `controls.execution_enabled`) default OFF. It stores
references (nonces, hashes) only: no key, signature, raw transaction or credential, and no fill or
PnL field. See [TRADING_ORDER_LEDGER.md](TRADING_ORDER_LEDGER.md) for authorization boundaries,
recovery, backup/restore/rollback and the assumptions a reviewer must confirm. No signing,
approval, broadcast, funds, deployment or public exposure were added.

## Implemented PAPER boundaries

- Existing IntelBot allowlist and a matching private user/chat govern wallet associations,
  amount selection, balances, review, confirmation and history. Group alerts open only the
  caller's DM. A watched public address proves no ownership or spending authority.
- Token/chain/source message/wallet are bound to opaque, bounded, expiring sessions. Changing
  a wallet cancels that caller's existing sessions. No other user's state is disclosed.
- Each review has a new random confirmation token and a digest of the entire quote, including
  nested unsigned payload. Refresh, amount changes, expiry, cancellation and completion revoke
  prior confirmation. Duplicate confirmations produce at most one private hypothetical record.
- Quotes release the session lock during I/O and recheck selection, wallet, generation, expiry
  and pause afterwards. Cancel/history remain available during pause. Network operations have
  a busy guard, bounded total budget, five-second per-call timeouts and no retries.
- RPC dispatch accepts only seven explicit read methods. Signing, approvals and all transaction
  submission methods refuse before network I/O. Scanner RPC sessions/transactions are unused.
- Canonical V2 ABI fragments come from pinned published Uniswap artifacts, not invented selectors.
  Router immutables are filled only with the documented factory/wrapped HYPE addresses. Runtime
  keccak hashes, factory/token identities and pinned-block reads are checked on every quote.
- Exact integer input/percentage math, decimals <=36, matching router/reserve quotes, minOut,
  liquidity, impact, age, gas and balance controls are enforced. Confirmation reconstructs and
  compares the exact target/recipient/value/path/minimum/deadline payload. No ticker routing.
- The proposed fee-on-transfer-supporting payload enforces minimum output in read-only simulation.
  It returns no delivered amount. Expected output assumes normal transfers; token taxes, malicious
  balance reporting, future restrictions, RPC honesty and future execution remain uncertain.
- Missing SELL allowance is an explicitly conditional PAPER record with no successful simulation
  or gas estimate. No approvals are sent, and transport errors are never relabelled as approvals.
- The public Agora v1 builder is only a validated-envelope API comparison. Its outer contract ABI
  and source/build identity remain unresolved; opaque calldata is never treated as verified.
  Fees/output are API-reported, not independent on-chain fee verification.
- The private journal requires an owned 0700 directory and 0600 regular file, rejects direct
  symlinks and stores unique intent IDs plus user-filtered records. Initialization failure pauses
  PAPER; write failure refuses confirmation. Watch associations persist; sessions/tokens do not.
  No keys, signatures, live receipts or fake fills are stored. This is single-process PAPER
  persistence, not a reviewed execution-service audit database.

## Independent reviewer checklist

All items below remain **unapproved by an independent reviewer**. Author tests and this
checklist are preparation only; neither constitutes independent approval.

- [ ] Review exact #84 head, dependency changes, canonical artifact provenance, immutable filling,
  code pins, ABI encoding and the unsupported-venue refusal boundary. Reproduce verifier output.
- [ ] Exercise forged/private/group callbacks, cross-user wallets/history, wallet changes during
  quotes, refresh races, nested payload mutation, expiry, pause/cancel and duplicate confirmations.
- [ ] Review conditional SELL labels: missing allowance must never imply simulation success,
  estimated gas, actual fill, sellability or permission to submit. Verify zero broadcast traffic.
- [ ] Reconcile fee units, price impact, input/output decimals, integer rounding and minimums.
  Review thin pools, zero output, token taxes/restrictions and dishonest RPC/token responses.
- [ ] Review journal ownership, symlink/rename threat assumptions, corruption/full disk, unique
  constraints, crash-after-commit behavior, privacy, persistent mount, backups and retention.
- [ ] Review startup/dependency isolation: PAPER failure must not change scanner/outbox reliability
  or create another production getUpdates consumer. Approve any deployment/restart separately.

## Exact gates before Stage C

- [ ] Establish canonical AgoraRouter source/ABI/build identity before accepting aggregator payloads;
  verify every chosen venue/hook/factory/pool, token behavior, applicable fees and actual net output.
  No private API/session scraping or ABI inference from a selector is sufficient.
- [ ] Use noncustodial signing first: wallet-controlled client signs only a reviewed exact intent.
  Telegram bot tokens and public watch addresses cannot authorize spending. Implement wallet
  ownership proof, Mini App init-data signature/freshness, CSRF, domain/chain binding and replay limits.
- [ ] Threat-model compromised bot token/UI/provider/RPC, malicious calldata/recipient/approval,
  token transfer restrictions, MEV, stale state, reorgs, key compromise and infrastructure compromise.
  Independent reviewer signs off on the chosen signer architecture and trust assumptions.
- [ ] Isolate execution from scanner and public web service. Use authenticated private ingress,
  typed allowlisted transaction plans, least-privilege private storage and secure operational logs.
  No generic destination/calldata endpoint. No wallet secrets in chat, .env, DB, source or CI logs.
- [ ] Implement finite allowance/zero-reset lifecycle, serialize wallet/chain nonce reservations,
  one conflicting order at a time, idempotent submission and ambiguous-timeout reconciliation.
  Re-broadcast only identical signed bytes; review replacement limits and revocation procedures.
- [ ] Validate on-chain minOut/deadline/recipient/value and fresh balance/allowance/gas/simulation
  immediately before signing. Receipt matching, actual output/gas/fees, finality/reorg handling,
  reconciled balances and truthful order statuses are required. RPC acknowledgment is not a fill.
- [ ] Rehearse private-state backup/migration/rollback and emergency pause. Review resource/RPC
  budgets and health metrics with both bots. Obtain explicit owner approval for a capped real BUY
  and SELL test, then separate approval for enabling live execution after reviewing results.

Stage D hot-wallet mode would need a separate opt-in design, isolated KMS/secret-manager
signer, strict per-trade/daily budgets, revocation, key rotation and independent review.
No such mode is implemented or authorized. PAPER tests establish no real-money readiness.

## Follow-up author hardening review

See `PAPER_REVIEW.md` for reproduced findings, remediation, adversarial evidence and
unresolved trust/resource assumptions. Returned quote payloads are isolated copies;
invalid amount attempts revoke previous reviews; outer Telegram identity/context checks
are strict; journal identity/permissions and record bindings are checked at each access;
JSON responses are size-bounded; a final pinned-block hash check detects mid-quote reorgs.
Immutable metadata caching is bounded and gated by live code identity on every quote;
mutable financial state and token decimals are never cached. The pilot is a prepared
UI-only overlay/runbook (`PAPER_PILOT.md`), not a deployed service or extra consumer.
Independent review/approval is still absent. Stage C is tracked separately in #85 and
`STAGE_C_PLAN.md`; the Agora outer router and all real-money paths remain disabled.
