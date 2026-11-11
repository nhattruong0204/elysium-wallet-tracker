# Wallet-managed approvals and Buy/Sell (Stage C2, issue #92)

**Status: implemented, default OFF, never enabled by this change. No real funds, no deployment.**

The server prepares an exact request; the **user's own wallet** signs and broadcasts it. The server
holds no key or seed phrase, runs no signer, exposes no signing endpoint and never broadcasts or
rebroadcasts. `trading.wallet_execution_enabled` is `false` by default and additionally requires
`trading.ledger_enabled`; at runtime every prompt is also gated by the ledger's durable,
owner-only `execution_enabled` flag (also OFF) and by the pause switch.

## Flow

```
reviewed plan (#90, unsigned, immutable)
   │  POST /wallet/api/order   → ledger order (idempotent per plan), created → quote_review →
   │                              requires_approval (conditional SELL) | awaiting_wallet (executable)
   ▼
POST /wallet/api/prompt  ── ledger.authorize_wallet_request (pause · execution flag · policy
   │                          re-check · state · expiry; durable WALLET_REQUEST audit event)
   ▼
browser re-verifies the request, then asks ITS wallet: eth_sendTransaction (EIP-1193)
   ▼
POST /wallet/api/report {hash | rejected | error}  → WALLET_RESULT event; hash ⇒ attempt recorded
```

* **BUY** – the swap request is the reviewed plan's calldata verbatim (`swap_request`): same
  sender, router, value, gas caps, recipient, path, `min_out`, deadline. Hash reported ⇒ order
  `submitted` (not "filled"). Rejection ⇒ `cancelled` (reason `wallet_rejected`).
* **SELL** – a plan whose allowance is short is *conditional*: it can never become a swap request.
  `ApprovalPlanner` decides the single next step from the chain:

  | on-chain allowance (fresh head) | action |
  |---|---|
  | ≥ required | nothing to sign; requote |
  | 0 | `approve(ROUTER, exact amount)` |
  | 0 < a < required | `approve(ROUTER, amount)` if simulation accepts it, else `approve(ROUTER, 0)` first (USDT-style) |

  After the approval is mined the allowance must equal the requested amount **exactly**
  (`confirm_allowance`, else `cap_mismatch` and the flow stops). The swap is then a **new** plan /
  new ledger order from a fresh quote; the approval order is never silently turned into a swap.

## Approval safety invariants (each has a test)

* spender is the frozen V2 router; router and token runtime hashes are re-checked at the head;
* amount is exactly the reviewed input amount; anything ≥ 2^128 is unencodable (no "unlimited");
* a pre-existing unlimited allowance is reported (`existing_unlimited_allowance`), never created;
* `approve` must simulate cleanly and return nothing or exactly `true`; other return data refused;
* bounded gas (`MAX_APPROVAL_GAS` = 200k, estimate +20 %, fee cap = node price +25 %);
* head must be ≤ 30 s old; request expiry ≤ plan expiry (and ≤ swap deadline);
* the browser independently decodes the approval and refuses a wrong target, spender, sender,
  chain, missing gas caps or a non-finite amount before the wallet is ever called.

## What "submitted" means

A hash from the browser is an **acknowledgment**: the order becomes `submitted` and stays
unsettled. Nothing in #92 verifies that the hash matches the request, that it was mined, or what
was received. `tx_mismatches` (exact comparison of from/to/value/input/chain/gas caps) exists and
is tested but is *used by #94 reconciliation*, which also owns receipts, fills and reorgs.

## Exactly-once posture in #92 (full nonce/idempotency design is #93)

* one durable swap prompt per order, ever (`already_requested`) – survives restarts; a lost answer
  is left to chain reconciliation rather than asking the wallet to send again;
* open prompts are replayed (same id) on double click; reports are idempotent per request id and
  conflicting second answers are refused;
* a wallet `error` is "unknown", not "failed": the order stays `awaiting_wallet`.

## Security assumptions and limits (be honest)

* The page's checks defend against a tampered *response*, not a compromised page origin: a hostile
  origin can show anything. Users must still read what their wallet displays.
* Wallet-reported reasons are untrusted: only a fixed code set is stored (`unspecified` otherwise).
* Compatibility: injected EIP-1193 providers only. Telegram's in-app WebView generally has none;
  the page then says so and asks the user to open it in their wallet's browser. Mock-wallet browser
  tests are page-logic evidence, **not** a claim about any real wallet or device.
* Fee caps use legacy `gasPrice`; wallets may still show their own fee UI.
* Not verified here: behaviour of any specific real token or wallet; real Agora RPC behaviour.

## Operator behaviour

* default OFF; nothing in `__main__` launches the wallet routes. They are mounted only when
  `create_app(..., wallet=WalletRuntime(...))` is called with the secure authenticator and the
  same `PlanBook`.
* pausing or turning `execution_enabled` off stops new prompts immediately (checked in the ledger
  transaction); already-issued prompts live ≤ `ttl` (120 s) and are still bounded by the plan.
* migration `0012` widens the audit kinds and adds unique indexes; downgrade drops the indexes only.

## Next dependency

#93 persistent legs + nonce/idempotency (builds on the one-prompt rule), #94 receipt
reconciliation (builds on `tx_mismatches`), #95 security review, then #96 (separately authorized).
