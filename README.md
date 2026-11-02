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

## Deploy on a VPS (Docker Compose)

Needs Docker with the compose plugin. The VPS must reach `www.agoradex.app`, your Elysium
RPC and `api.telegram.org`.

```bash
# 1. get the code (private repo: use a deploy key or a fine-grained PAT with read access)
git clone https://github.com/nhattruong0204/elysium-wallet-tracker.git
cd elysium-wallet-tracker

# 2. config + secrets (both are git-ignored)
mkdir -p config
cp config.example.yaml config/config.yaml
cp watchlist.example.txt config/watchlist.txt
cp .env.example .env && nano .env            # TG_BOT_TOKEN, TG_CHAT_ID

# 3. find Agora's leaderboard endpoint; paste the printed block into config/config.yaml
docker compose build
docker compose run --rm tracker --discover

# 4. dry run: catch up once and print alerts to the terminal instead of Telegram
docker compose run --rm tracker --once --dry-run

# 5. run for real
docker compose up -d
docker compose logs -f
```

On start the bot sends a "Tracker started" message to Telegram, so you know the token, chat
and RPC all work. The container healthcheck marks it `unhealthy` if the main loop stops for
10 minutes (`docker ps` shows it).

Day to day:

```bash
docker compose run --rm tracker --status           # cursor, wallets, events, pending alerts
git pull && docker compose up -d --build           # update to the latest code
docker compose run --rm tracker --reset-cursor 190000   # replay from a block (safe, deduped)
```

State (cursor, events, positions, alert outbox) lives in the `tracker-data` volume and
survives rebuilds. `docker compose down -v` deletes it.

### If `--discover` finds nothing

The endpoint may only be called from server-side code, or behind bot protection. Then use
your browser once: agoradex.app/traders → DevTools (F12) → Network → Fetch/XHR → reload →
PnL tab → right-click the request that lists wallets → Copy as cURL. Save it as
`config/curl.txt` and run `docker compose run --rm tracker --import-curl /config/curl.txt`.
Without a leaderboard the bot still follows every wallet in `config/watchlist.txt`.

### Without Docker

```bash
pip install -r requirements.txt
python -m agora_tracker --config config/config.yaml --once --dry-run
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
