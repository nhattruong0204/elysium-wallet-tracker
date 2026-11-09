# Liveness alerts and degraded mode — #95 finding F-18

Status: **mitigated in code, awaiting independent verification.** Alert delivery to a real
Telegram chat has not been exercised (no bot token in the build environment).

## What runs
`agora_tracker/trading/liveness.py` — one daemon thread in the `--ui` process, built only when the
wallet worker **or** the ingress listener runs and `trading.liveness_alerts_enabled` (default
`true`). It reads the existing in-memory snapshots every `liveness_interval_s` (30 s): no extra
database or RPC traffic.

| Key | Severity | Condition (defaults) |
|---|---|---|
| `ingress_down` | CRITICAL | listener thread dead / not serving |
| `ingress_degraded` | CRITICAL | listener died `liveness_ingress_max_restarts` (3) times within 1 h: restarts stop |
| `worker_down` | CRITICAL | worker crash-looped (`failed`) or its thread died |
| `worker_stale` | CRITICAL | no successful cycle for 5 min (after a 2 min startup grace) |
| `worker_failing` | WARNING | ≥ 5 consecutive failed cycles |
| `rpc_outage` | CRITICAL | execution-RPC circuit breaker open |
| `ambiguous_legs` / `orphaned_legs` | WARNING | ≥ 1 such leg (its wallet is blocked) |
| `receipt_lag` | WARNING | oldest leg waiting for evidence > 15 min |
| `queue_growth` | WARNING | evidence + receipt queue > 20 |
| `monitor_error` | WARNING | the evaluation itself failed |

A condition must hold for 2 consecutive ticks, then alerts once; reminders at most every
`liveness_cooldown_s` (1 h); a `RESOLVED` message when it clears; at most 12 messages per hour in
total. Messages go by `sendMessage` (never `getUpdates`) to each trading owner's **private** chat
(positive user ids from `TG_CHAT_ID` / `TRADING_OWNER_IDS`); text is plain and scrubbed of
anything that looks like an address, hash, URL or long id. Delivery errors are counted, logged and
retried at the next tick; they never stop the monitor.

## Restart policy (why this one)
* **Ingress listener: bounded supervised restart, then fail-closed degraded mode.** A restart
  rebinds the same loopback port around the same application object; it does not poll Telegram,
  touch the ledger, clear a pause or create a wallet request, and the wallet only ever signs what
  the user confirms — so it cannot double-submit. After 3 deaths in an hour it stops trying: the
  Mini App stays offline (no new prompts can be created), the bot, scanner and worker keep running,
  and the owner is told. No unlimited restart loop.
* **Worker:** never restarted here. It already has its own crash-loop breaker (`failed` state);
  the monitor only reports it. Unresolved legs and their evidence stay in the ledger.
* **Process:** never terminated by the monitor; systemd's own restart policy (unchanged) applies
  if the whole process dies, and the single `getUpdates` consumer is preserved.

Shutdown order: the monitor stops **first** (so no restart races a shutdown), then the listener,
then the worker.

## Owner view
`/wallet_health` now also shows the active alert keys, degraded flag, and sent / suppressed /
delivery-error / restart counters.

## Tests
`tests/test_trading_liveness.py` (22: every condition, confirmation, dedup, cooldown, resolve,
hourly cap, delivery failure, owner filtering, redaction, bounded restart → degraded, successful
restart is silent, evaluation errors, thread lifecycle, the REAL loopback listener dying and being
rebound on the same port, config validation) and `tests/test_wallet_ingress.py` (`run_ui` builds
the monitor beside the listener, stops it first, one `getUpdates` consumer).

## Not done
Real Telegram delivery; alert routing other than Telegram (e-mail / pager) — if Telegram itself is
down there is no second channel. Process-level death relies on systemd (`Restart=` in the unit).
