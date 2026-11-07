# Stage C implementation plan — design only

Tracking issue: [#85](https://github.com/nhattruong0204/elysium-wallet-tracker/issues/85),
parent #82. This plan implements no signer/submission path and grants no funded-test or
production authorization. PAPER and live release gates are separate.

## Milestone C1 — authenticated unsigned review prototype

Use a separate authenticated trade origin/service, never dashboard_ro or the public
research API. Validate Telegram Mini App initData server-side with the official algorithm,
short auth_date lifetime/future skew, strict parsing (including duplicate-field rejection),
origin/session/CSRF binding and replay protection. Do not trust initDataUnsafe.
Independently establish wallet ownership using a one-time ERC-4361 challenge bound to
bot/user/session/domain/URI/chain 99901, nonce and issued/expiry times. A PAPER watch
address and a Telegram bot token establish no spending authority. Begin with EOA wallets;
reject contract accounts until ERC-1271 verification/revocation is explicitly reviewed.

Test a real wallet-connection UX inside Telegram WebView and external-wallet fallback
using mocks/forks and unsigned plans. Account/chain changes invalidate authentication or
review as appropriate. Define compatibility before choosing broadcast architecture:
not all wallet providers offer raw signTransaction. Authentication signing is distinct
from wallet approval of a transaction; no server custody fallback is permitted.

Deliver auth/ownership threat model, unsigned exact V2 review UI, adversarial/browser
tests and independent reviewer approval. Keep submission impossible at this milestone.

## Milestone C2 — constrained approval and transaction state machine

Construct plans only from typed side/token/amount fields. Validate canonical router,
factory/pair/token policy, chain, fresh pinned state, sender/recipient/value/path/minOut,
deadline, gas and actual fee assumptions before each wallet prompt. Unknown tokens and
V3/V4/hooks/curves/multi-hop refuse; V2 does not cover all alerts. Keep Agora's outer
router disabled until implementation/ABI/build identity is independently verified.

Read allowance. Ask for finite exact allowance, with separate explicit zero-reset and
approval prompts where required. Reconcile approval receipt, then obtain a fresh quote
and user review before the swap. No approval automatically initiates a swap. A successful
simulation does not guarantee future execution, token sellability or an exact output.

Maintain one active conflicting order per wallet/chain, unique idempotency keys per
order/leg and serialized nonce leases. Reconcile pending/latest and external wallet
transactions. Bind/recover every returned transaction's sender, chain, nonce and exact
payload to the reviewed intent; wallet overrides require a fresh review/reconciliation.

## Milestone C3 — isolated submission, receipts and accounting

Choose either client wallet signing/broadcast, or compatible client raw signing followed
by authenticated exact-plan validation and submission. The server never receives a key.
A dedicated execution RPC pool has independent sessions/budgets, chain/head validation,
circuit breakers and metrics; it cannot starve scanner/notifier RPC work. Establish actual
Elysium inbox/sequencer permissions and finality before any real test.

A timeout after wallet/RPC acceptance is ambiguous, not failed. Reconcile hash/nonce
before retry. In raw-signing mode only identical signed bytes may be rebroadcast; no
new signature/nonce replacement without explicit policy and user approval. In wallet-
managed mode the backend cannot invent/resend a transaction or assume exclusive wallet
nonce control. A hash is an acknowledgment, not a fill.

Persist approval → fresh review → awaiting wallet → submitted/ambiguous → included
success/reverted → finalized/orphaned. Verify receipt block/hash/chain and actual token/
native deltas, paid gas and fees. Account provisionally until finality; reverse provisional
entries on reorg and reconcile. Missing cost basis produces unknown PnL, never fake profit.
Use a least-privilege private order/audit role with reviewed backups and retention.

## Milestone C4 — independent release gate

Persist emergency pause, revocation, per-order/daily spend and gas limits, expiry and
active-order limits. Pause stops new wallet prompts/submission while receipt monitoring
continues; it cannot undo a broadcast transaction. Threat-model compromised bot token,
UI bundle/origin, signer client, RPC/provider, malicious token/approval, phishing, replay,
MEV, reorgs, local filesystem/service compromise and ambiguous crash recovery.

Required before real funds: independent signer/security review of the exact head;
auth/approval/nonce/retry/reorg/browser and backup/rollback rehearsals; explicit owner
approval identifying wallet, per-trade/total notional and gas risk caps for a tiny BUY and
SELL; real receipts/balance reconciliation; then separate enablement/deployment approval.
Live execution stays OFF by default. No automated trading, copy trading or hot wallet.

Primary references: [Telegram Mini Apps](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app),
[ERC-4361](https://eips.ethereum.org/EIPS/eip-4361),
[ERC-1271](https://eips.ethereum.org/EIPS/eip-1271),
[EIP-1193](https://eips.ethereum.org/EIPS/eip-1193). Full acceptance criteria are in #85.
