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
       PostgreSQL (evidence, trades, positions, PnL, checkpoint) ─> outbox ─> Telegram
```

- **Leaderboard only picks who to watch.** Trades come straight from the chain, so alerts don't
  wait on Agora's refresh and keep working if Agora changes or goes down.
- **Protocol-agnostic.** Works for every launchpad and Uniswap pool with no per-protocol ABI code.
- **Replay-safe.** Trades and alerts have unique keys; each block range (raw evidence, trades,
  positions, PnL, alerts, checkpoint) commits in one PostgreSQL transaction. Crash, restart or
  `--reset-cursor` never double-counts or double-alerts.
- **Alerts:** each buy/sell (size, price, new position / adding / partial or full exit, realized
  PnL vs that wallet's average cost), wallet-to-wallet token moves, **cluster buys** (N tracked
  wallets into the same token within a window), and leaderboard changes (entered/exited top,
  rank jumps, PnL jumps).

## Deploy on a VPS (Docker Compose)

Needs Docker with the compose plugin. The VPS must reach `www.agoradex.app`, your Elysium
RPC and `api.telegram.org`. The stack is PostgreSQL 16, a one-shot migration step, the
tracker, and a daily verified backup job.

```bash
git clone https://github.com/nhattruong0204/elysium-wallet-tracker.git
cd elysium-wallet-tracker
cp .env.example .env && nano .env      # POSTGRES_PASSWORD (openssl rand -hex 24), TG_BOT_TOKEN, TG_CHAT_ID
mkdir -p config
cp config.example.yaml config/config.yaml
cp watchlist.example.txt config/watchlist.txt

docker compose build
docker compose up -d postgres && docker compose run --rm migrate
docker compose run --rm tracker --discover            # paste the printed block into config/config.yaml
docker compose run --rm tracker --once --dry-run      # catch up once, print alerts
docker compose up -d
```

**Upgrading from the SQLite version?** Follow
[docs/OPERATIONS.md → Upgrading from the SQLite version](docs/OPERATIONS.md#upgrading-from-the-sqlite-version-v01--v02):
a validated, all-or-nothing import that keeps your history, cursor and delivered alerts.

Day to day:

```bash
docker compose run --rm tracker --status           # cursor, watchlist, trades by confidence, outbox
git pull && docker compose up -d --build           # update (migrations run automatically)
./scripts/restore.sh backups/tracker-<UTC>.dump    # restore a backup
```

Docs: [operations runbook](docs/OPERATIONS.md) · [database design & ERD](docs/DATABASE.md) ·
[audit of v0.1](docs/AUDIT.md).

### If `--discover` finds nothing

The endpoint may only be called from server-side code, or behind bot protection. Then use
your browser once: agoradex.app/traders → DevTools (F12) → Network → Fetch/XHR → reload →
PnL tab → right-click the request that lists wallets → Copy as cURL. Save it as
`config/curl.txt` and run `docker compose run --rm tracker --import-curl /config/curl.txt`.
Without a leaderboard the bot still follows every wallet in `config/watchlist.txt`.

### Development

```bash
pip install -r requirements.txt pytest ruff mypy
export TEST_DATABASE_URL=postgresql+psycopg://tracker:tracker@127.0.0.1/tracker_test   # disposable DB
pytest -q && ruff check . && mypy agora_tracker
```

## Limits you should know

- **Pre-launch (until Nov 9) trades land in bursts.** Elysium only advances when a batch posts
  (~every 3 h), so you'll get a wave of alerts per batch, ordered by inbox position. You cannot
  copy at their price; treat alerts as research, not signals. After launch, latency is the
  poll interval plus RPC lag.
- **Every trade carries a confidence level**, shown in alerts and stored per event:

  | Confidence | Meaning | Shown as |
  |---|---|---|
  | `high` | HYPE leg measured: the wallet's balance change across the block, with its nonce proving this was its only tx there; or paid in wrapped HYPE (exact from logs) | plain numbers |
  | `estimated` | HYPE inferred from `tx.value` because the block was ambiguous | `≈` and "estimated (tx_value)" |
  | `unknown` | Only the token movement is known | "OUT (sell?)" / "HYPE leg unknown" |

  A tx sent by someone else (airdrop, escrow or limit-order fill) is always a transfer, never
  a trade. One residual risk remains even at `high`: HYPE *received* from a third party in the
  same block can't be told apart without trace APIs.
- **PnL never mixes measured and estimated numbers.** Realized PnL from measured trades on a
  fully known cost basis is kept separately from PnL that relies on any estimate. Tokens
  received with unknown cost make later sells "PnL unknown" instead of inventing a profit.
- **Missing data stops the range, it doesn't skip it.** If the node can't return a tx,
  receipt or block, the whole block range is retried later and the cursor does not move.
- **RPC endpoints are checked for chain ID** before first use; an endpoint on another chain
  is quarantined, so failover can't mix networks. Provider URLs are redacted in logs.
- **Balance diffs need state for block N-1.** Most Nitro nodes keep recent state; if the RPC
  prunes it, trades fall back to `estimated` / `unknown`.
- **Positions start at tracking time.** Sells of bags bought earlier show "PnL unknown".
- **Telegram delivery is at-least-once.** Failed sends back off (10 s doubling to 1 h) and are
  dead-lettered after `max_attempts`, visible in `--status`. A timeout after Telegram accepted
  a message can cause a rare duplicate; alerts are never silently dropped.
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

## Telegram Intelligence Center

Send `/start` to the bot for an interactive menu: market overview, smart-money flows, trending projects, gainers/losers, top traders, wallet/token search and CSV/XLSX exports over any time range. See [docs/TELEGRAM_UI.md](docs/TELEGRAM_UI.md).
