# Trading operations — Issue #82

## Current release state

Stages A/B are proposed for review; no new bot service has been deployed. Stage A's bounded
read-only benchmark ran on the VPS without signing, broadcasting, migrations or restarts.
Both existing bots must remain running. Do not turn on paper controls in production during
this task: applying UI/alert code would require a separately reviewed maintenance window.

`trading: {paper_enabled: false}` is the default. Existing YAML does not need changing.
A local/test opt-in adds PAPER Buy/Sell controls to existing token alert keyboards; normal
notifier fallback remains. `/trade <CA>` is private and PAPER-only; `/pause_trading` blocks
paper actions until restart. `/orders` and `/positions` report that no connected wallet or
durable live records exist, rather than fabricating holdings. Buy presets are 0.1/0.5/1 HYPE;
sell presets are 25/50/100% of verified wallet balance. Quote review refuses in production
because no verified adapter is installed. No confirmation button is presented with the default provider/route configuration.
The WSL continuation below adds custom chat input, a fixture-tested review UI, public
indicative prices and optional private hypothetical journals. No live record is created.

## Run read-only research

Use scripts/trade_rpc_bench.py with the same workload file per endpoint and conservative
provider budgets. The ops `trade-routing-benchmark` step executes only this standalone
script inside the existing image, followed by health inspection; no services are recreated.
Prefer documented approved RPCs. Never pass credentials in report labels. Keep raw URL
paths/query keys, exception text and unsigned/signed payloads out of artifacts and logs.
No actual swap quote or gas test is currently authorized by a verified route interface;
record it as unrun rather than substituting a transfer estimate.

## Rollback and future live release

No migrations are introduced. Keep feature flag false. Do not reset databases, checkpoint,
positions, outbox or trade history, and never touch Revival. Future paper rollout needs bot
health baselines and a controlled UI/tracker restart window; this task does not approve it.
If UI behavior degrades, pause paper flow and use the previously verified image/config in
an authorized window, preserving DB state. Live release requires the security checklist in
TRADING_SECURITY.md, backup/rehearsal, independently verified contracts, spend limits,
owner-reviewed signer design and explicit capped test approval. Stage D hot-wallet signing
requires a separate review. No quote or unsupported route -> use the existing wallet/DEX
links, never manufacture calldata or silently loosen risk limits.

## WSL continuation — local evaluation only

See `LOCAL_WSL_SETUP.md` for the validated isolated environment. Stage A now records
reachable official public API docs, six indicative quotes, current RPC reads and Eleos's
self-reported inbox mode. Source/ABI verification and actual submission remain blocked.
No production restart, deployment, migration, bot message or OPS_REQUEST is needed.

With PAPER enabled **locally**, select a buy preset and choose "Indicative Agora price"
for a real public market observation. It does not connect a wallet, validate a route or
permit confirmation. It shows raw token units because decimals are unverified. Sells
remain unavailable without verified wallet balances/decimals. The verified Review path
still refuses with the default empty route/provider configuration. A separate approved
fixture provider exercises the complete hypothetical Review → Confirm UI in tests only.

Custom input uses `/paper_amount SESSION AMOUNT` in the caller's private chat, after
"Custom amount" shows the opaque session. Buy units are HYPE (at most 1); sell units
are percent of verified wallet token balance (positive, at most 100, two decimal places).
Sessions expire in three minutes. A fresh review needs its own confirmation token.
Every panel retains Cancel, Agora, explorer and, for supergroup alerts, the source link.

For optional local durability, set `trading.paper_order_path` to a private filesystem
path under a directory owned by the UI process with mode 0700. The file is mode 0600.
`/orders` lists only the caller's latest ten hypothetical record IDs. Without that
setting, confirmation explicitly says the record is in memory only. No production
provider currently reaches confirmation, and no live position or receipt is fabricated.
A journal initialization failure pauses PAPER; a write failure preserves the review
and refuses confirmation. Restart discards sessions/tokens but retains stored records.
This introduces no PostgreSQL migration and no scanner or public-dashboard changes.
A future isolated execution service needs its own reviewed durable schema/role and
backup, retention, reconciliation and rollback procedures before live operation.
