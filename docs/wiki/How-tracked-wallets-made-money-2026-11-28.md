# How tracked wallets made money on Elysium (2026-11-02 to 11-28)

Read-only analysis of the production database plus Montra's and Agora's public APIs, 2026-11-28 10:00 UTC.

## Data and limits

- **Scope:** Elysium mainnet (chain 99901), 2026-11-02 to 11-28. Testnet history is not in the database.
- **Data:** 35,846 high-confidence buys and sells by 428 tracked wallets (Agora's top 100 plus discovered wallets) on 6,211 tokens.
- **Measured PnL:** realized PnL from `realized_pnl` with a known cost basis. That covers 66% of sells: 7,247 closed positions held by 281 wallets.
- **Units:** in HYPE. Agora's leaderboard is in HYPE too (`pnlHype`), and agrees with ours within about 15% for the top wallets. The average HYPE price over the period was about $70.
- **Survivorship bias:** tracked wallets are the winners' side. Most counterparties (launch bots, retail) are not in the data, so this shows how money was **made**, not the whole market's result.

**Ecosystem scale (Montra `/api/stats`, 11-28):**

| | All time | Last 24 h |
|---|---|---|
| Tokens launched | 117,529 | 7,952 |
| Volume | $146M | $19.3M |
| Fees paid | $2.37M | $373k |

- Montra has 31,416 holders.
- Fees average about 1.6% of volume per swap. On HYPE pools on 11-28 it was 2.3%, about 18% of which went to the protocol.

## Where the measured profit came from

The table sorts positions by the wallet's entry time relative to the token's launch.

| How | Positions | Wallets | PnL (HYPE) | Win | Median | ROI |
|---|---|---|---|---|---|---|
| Launch your own token: buy in the creation tx, sell within ~1 min | 4,140 | 78 | **+174,137** | 96% | +28.0 | 73% |
| Snipe someone's launch (< 60 s) | 592 | 79 | +9,784 | 58% | +0.24 | 45% |
| Early (1-10 min) | 823 | 117 | +1,235 | 31% | -0.05 | 23% |
| Momentum (10 min - 6 h) | 773 | 129 | -53 | 31% | -0.25 | -1% |
| Late (> 6 h) | 778 | 162 | -1,858 | 27% | -0.40 | -17% |
| No launch time known | 141 | 46 | +429 | 48% | -0.02 | 30% |
| **All** | 7,247 | 281 | **+183,674** | 70% | | |

**Launching:**
- The 67 serial-launcher wallets made **+174,523 HYPE, 95% of all measured profit**. That is roughly $11-12M at the period's HYPE price.
- They hold Agora's ranks 1 and 3-11.
- By launchpad, eleos carries +183,802 of the total.

**Sniping:**
- +9.7k HYPE without launcher wallets, and two wallets made about 92% of it: `0x0641315f` (+6.1k, Agora #2) and `0x85a5fd59` (+2.9k).
- The pattern: 50-100 HYPE in the launch second, sold 7-15 min later at 2.4-4.2x, on eleos launches.

**Everyone else:**
- Every entry 1 min or more after launch, about 2,370 positions in all, is **net about -0.7k HYPE**, with 27-31% win rates. These are Agora's top-100 wallets.
- By hold time, 2-15 min was the best bucket (+8.1k, mostly snipers). Holds longer than 2 h are net negative.
- Other launchpads:

| Launchpad | PnL (HYPE) | Win |
|---|---|---|
| montra | +768 | 31% |
| ascend | -684 | 31% |
| volta | -564 | 28% |
| others | about 0 | |

**Concentration:**
- 154 wallets are up and 127 are down.
- The top 10 wallets hold 64% of all gains, and the top 50 hold 97%.

**The rare real winners:** early or patient entries in tokens that became projects.

| Token | Entry | Held | Multiple | PnL (HYPE) |
|---|---|---|---|---|
| LiquidV | 11 h after launch | 16 days | 15.4x | +301 |
| POCKET | 2 min after launch | 4 h | 14.8x | +207 |
| MONTRA | 2 min after launch | 17 days | 3.2x | +140 |

- MONTRA was profitable for 28 of the 43 tracked positions in it (+609 in total).
- TOKOS was profitable for 8 of 9.

**Not in the PnL above** (received as transfers, not measured):
- Creator fees: about 82% of swap fees on HYPE pools go to creators and holders, not the protocol.
- Holder rewards: for example, the BLOB contract made 1,722 payouts to 17 tracked wallets.
- Montra's buyback-and-burn, and builder-programme payouts.
- Airdrops, which are mostly spam: for example, one sender gave 200k units of one token to each of 530 wallets.

## Copy-trading test (walk-forward)

**Rules:**
- Each day, rank wallets by measured PnL over the trailing 14 days. Own launches, snipes and serial launchers are excluded. A wallet needs at least 5 closes on at least 2 tokens and must be up.
- Copy each new position of a top-10 or top-30 wallet.
- **Entry:** the first tracked buy price at least 15 s or 60 s after the leader's buy.
- **Exit:** the first tracked sell price after the leader's first sell.
- Fees: 1% per side in the table. At Montra's measured 2.3% per side, each figure is about 3 points lower.

| Followed | Delay | Trades | Win | Mean | Median | Leader's own mean |
|---|---|---|---|---|---|---|
| top 10 | 15 s | 35 | 54% | +25.5% | +9.5% | +53.5% |
| top 10 | 60 s | 33 | 52% | +26.2% | +0.1% | +54.9% |
| top 30 | 15 s | 47 | 55% | +32.3% | +13.3% | +72.3% |
| top 30 | 60 s | 43 | 53% | +34.6% | +4.3% | +74.6% |

**Not conclusive:**
- 16-27 copies had no later tracked trade to price the exit, so they are excluded. If those were near-total losses, the top-10/15 s result turns negative: 56 trades at about -22% mean.
- Results are skewed: a few winners carry the mean.
- The ranking only starts after 14 days of history, which leaves about 12 days of signals.

The test needs forward paper trading with live pool prices (Montra's API) before any money goes in.

Consensus (from the 11-27 replay):
- The #138 gate keeps 30-36% winners, with a mean multiple of 1.02-1.10 before fees.
- After 3-5% round-trip fees, that is roughly break-even.

## Reading

Almost all of the measured profit was extraction at launch: create and sell, or snipe in the launch seconds. Nobody can copy either one. The second-best pile is fees, which go to whoever launches tokens and to holders. Ordinary trading after the first minute, even by the leaderboard's top 100, is net negative here, so a follower's edge has to come from *which* wallets and tokens to follow, quickly and with an exit plan.
