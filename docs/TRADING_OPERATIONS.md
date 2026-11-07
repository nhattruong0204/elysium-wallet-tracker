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
because no verified adapter is installed. No confirmation button or fake fill is presented.
Custom HYPE parsing is tested in the paper core; custom chat entry is not implemented.
The core's hypothetical order fixture is test-only and not a production execution record.

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
