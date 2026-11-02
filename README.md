# elysium-wallet-tracker

Follows the top 100 PnL wallets on [agoradex.app/traders](https://www.agoradex.app/traders)
(plus any wallets you add) and alerts on Telegram when they trade on Elysium.

## How it works

```
Agora traders endpoint ──(every 5 min)──> top 100 set ─┐
watchlist.txt ─────────────────────────────────────────┤
                                                        v
Elysium RPC ── eth_getLogs(Transfer, from|to ∈ set) ──> per-tx net flows
          └── balance diff at block N vs N-1 (+gas) ──> HYPE leg
                                                        v
                         BUY / SELL / SWAP / TRANSFER / MULTI + position & PnL
                                                        v
                     SQLite (events, positions, cursor) ─> outbox ─> Telegram
```

- **Leaderboard only picks who to watch.** Trades come straight from the chain, so alerts don't
  wait on Agora's refresh and keep working if Agora changes or goes down.
- **Protocol-agnostic.** Works for every launchpad and Uniswap pool with no per-protocol ABI code.
- **Replay-safe.** Events and alerts have unique keys; each block range commits atomically with
  the cursor. Crash, restart or `--reset-cursor` never double-alerts.
- **Alerts:** each buy/sell (size, price, new position / adding / partial or full exit, realized
  PnL vs that wallet's average cost), wallet-to-wallet token moves, **cluster buys** (N tracked
  wallets into the same token within a window), and leaderboard changes (entered/exited top,
  rank jumps, PnL jumps).

## Setup

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp watchlist.example.txt watchlist.txt
export TG_BOT_TOKEN=123:abc TG_CHAT_ID=-100123456
```

### Point it at Agora's leaderboard endpoint (one time, ~1 minute)

The traders table is filled by a JSON request from the browser:

1. Open agoradex.app/traders → DevTools (F12) → **Network** → filter **Fetch/XHR**.
2. Reload, click the **PnL** tab, find the request whose response lists the wallets.
3. Right-click it → **Copy → Copy as cURL (bash)**, save to `curl.txt`.
4. `python -m agora_tracker --import-curl curl.txt` prints a `leaderboard:` block with
   `items_path` and `fields` detected. Paste it into `config.yaml`.

If the endpoint takes a `limit`/`pageSize` parameter, set it to 100. If it starts returning
401/403, the copied headers expired: re-import.

### Run

```bash
python -m agora_tracker --config config.yaml --dry-run --once   # test: print alerts, exit
python -m agora_tracker --config config.yaml                    # long-running
python -m agora_tracker --config config.yaml --status
python -m agora_tracker --config config.yaml --reset-cursor 190000   # replay from a block
```

systemd:

```ini
[Service]
WorkingDirectory=/opt/agora-tracker
Environment=TG_BOT_TOKEN=... TG_CHAT_ID=...
ExecStart=/usr/bin/python3 -m agora_tracker --config config.yaml
Restart=always
RestartSec=10
```

Tests: `pytest -q` (offline, fake RPC).

## Limits you should know

- **Pre-launch (until Nov 9) trades land in bursts.** Elysium only advances when a batch posts
  (~every 3 h), so you'll get a wave of alerts per batch, ordered by inbox position. You cannot
  copy at their price; treat alerts as research, not signals. After launch, latency is the
  poll interval plus RPC lag.
- **HYPE leg is exact only when the wallet has one tx in that block.** Otherwise it falls back to
  `tx.value` (buys) and is marked approximate; sells paid in native HYPE then show without size.
  Set `chain.wrapped_native` if routers settle in WHYPE.
- **Balance diffs need state for block N-1.** Most Nitro nodes keep recent state; if the RPC
  prunes it, the approximate path is used.
- **Positions start at tracking time.** Sells of bags bought earlier show "PnL unknown".
- **Read-only.** No keys, no signing. Every RPC you use sees your IP and the wallets you query.

## Reading the leaderboard skeptically

- Agora's *Total worth* marks tokens at the **last traded price**. On thin launchpad pools that
  number is often not sellable; a wallet with large worth and small realized PnL may be holding
  paper. Prefer wallets whose PnL is mostly realized (e.g. low worth, high PnL, sells ≈ buys).
- A top-100 list after one week of a pre-launch chain is a small sample: survivorship and luck
  dominate. Weight wallets by consistency across many tokens, not one 50x.
- Watch for wash-traded volume, wallets that are the deployer of what they "trade", and
  multi-wallet clusters run by one entity (they'll trip the cluster alert on their own).
  `TRANSFER_OUT` to fresh wallets is often the tell.
