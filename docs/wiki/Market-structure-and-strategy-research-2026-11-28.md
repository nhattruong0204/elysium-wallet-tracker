# Elysium market structure and strategy research (2026-11-28)

Read-only research for two questions from the owner: should the Telegram bots follow more wallets (for example Fomo KOLs), and how can a trader make money in this market? Sources are our production database (dashboard and tracker roles, read-only sessions), Montra's and Agora's public APIs, and the public web pages cited at the end. Prices are from 2026-11-28 (HYPE ≈ $63).

## 1. The market

- **Chain:** Elysium is built by Kinetiq. Mainnet opened 2026-11-09. It settles to HyperEVM and uses HYPE for gas.
  - Half of sequencer revenue funds KNTQ buybacks and burns; the other half goes to builder incentives and Kinetiq's treasury.
  - Direct links to Hyperliquid's order books were announced for about four weeks after launch, which is early December.
  - Fomo is Elysium's "exclusive trading partner" (Bankless).
- **Montra** is the main launchpad:

| Metric | All time | Last 24 h |
|---|---|---|
| Tokens launched | 119,041 | 7,678 |
| Volume | $148.5M | $18.5M |
| Fees paid | $2.42M | $360k |
| Swaps | 1.77M | 251k |

  Montra also shows 31,284 holders.
- **Fees:** every buy and every sell pays the pool's tier.
  - The tiers are 1, 2, 3, 5 and 10%.
  - A 1% pool splits as 0.1% LP, 0.4% protocol and 0.5% to the token's fee recipient.
  - New launches default to 3% (577 of 800 sampled), but volume sits in cheap pools. Of the top 100 tokens by volume, 1% pools carry 45% of the volume and 2% pools 28%.
- **Protocol share:** per a third-party summary (Bitrue) of Montra's docs, 70% of the 0.4% protocol fee buys and burns MONTRA once buybacks are active. That is unverified on-chain.
- **Base rate:** among the 800 newest launches sampled (up to about 1.3 days old), the median market cap is $2,651 (the empty launch price) and the median holder count is 0. Only 0.5-6% reach $10k, depending on age. Almost every launch is never traded.
- **Fomo** is a social trading app.
  - Trades and PnL are public, with leaderboards for 24h/7D/30D/all-time.
  - The fee is 0.5% per trade.
  - Wallets are self-custodial Privy wallets.
  - Users earn a 25% referral share and creator rewards when others trade.
  - The Block's data, as quoted by Datawallet: about 95% of Fomo traders lost money or made under $100, and only 0.06% made more than $10k.

## 2. Our wallet coverage

- The watchlist holds 7,430 leaderboard entries; 100 are current. 163 tracked wallets traded in the last 7 days.
- The trader board ranks 46 wallets, 30 of them in the top 30.
- Top-30 Montra buys of at least 1 HYPE reached 80 on 11-27 (41 tokens) and 68 on 11-28 (26 tokens). On a quiet evening the paper book saw 3 in 3 hours.
- **Agora's trader list** has 800 traders reachable by `offset`. 640 of them are not tracked: 186 have realized ≥ 10 HYPE, and 131 have ≥ 50 HYPE. They are not yet screened for launch farming.
- **Alert speed** over 3 days (5,227 trade alerts):

| | p50 | p90 | p99 |
|---|---|---|---|
| Block to alert created | 6.0 s | 12.4 s | 30.9 s |
| Alert created to sent | 0.1 s | 5.4 s | |

## 3. Event study on our snapshots (8 days, Montra tokens, launcher tokens excluded)

**Method:**
- **Data:** Agora snapshots every 10 minutes from 11-20 to 11-27, covering 538 Montra tokens.
- **Units:** each pool's real fee tier and quote asset come from Montra's API. KNTQ-quoted figures are scaled to HYPE; MONTRA- and xyz-quoted pools are dropped.
- **Prices:** forward paths come from Agora's 7-day candles.
- **Costs:** every result is net of the pool's own fee on both sides plus 0.5% slippage per side.
- **Bracket rule:** sell at +100% or stop at -35% within 72 h, otherwise exit at the 72 h close. If both levels are touched inside one candle, it counts as a stop.

| Setup | Events | 24 h median | 24 h mean | 24 h win | Bracket TP / stop / time | Bracket EV |
|---|---|---|---|---|---|---|
| Early momentum: ≥ $19k at 1-6 h with 100+ buys | 32 | **-63%** | +41% | 28% | 8 / 24 / 0 | **-6.8%** |
| Survivor breakout: first ≥ $50k after 6 h | 33 | -30% | -12% | 27% | 8 / 22 / 3 | +0.1% |
| Dip: ≥ $100k, 2 d+ old, -30% in 24 h | 10 | +42% | +28% | 60% | 8 / 0 / 2 | +72.7% |
| Dip, -20% threshold | 17 | +50% | +46% | 71% | 14 / 1 / 2 | +72.5% |

**Reading the results:**
- **Early momentum is a lottery.** The median token loses two thirds in a day, and a few moonshots carry the mean. With stops, the expected value is negative.
- **Breakouts after 6 h** are break-even after fees.
- **Dip buying only looks good.** The 17 events come from 9 tokens (MONTRA, LiquidV, EACCAT, NUM, CHAMELEON, JELLY, AGI, LEASH and others), and most fall in one market-wide dip and rebound on 11-24 and 11-25. The latest event, LEASH on 11-27 in a 3% pool, hit the stop. That is about two independent episodes: a hypothesis to test forward, not an edge.

## 4. Strategy scorecard

| Strategy | Evidence | Verdict |
|---|---|---|
| Launch tokens and sell into buyers | 95% of measured profit (wiki 11-28) | The house's game. Launch-to-dump harms buyers; not for us. |
| Snipe launches | 2 wallets made 92% of snipe profit; anti-snipe fees reach 99% | An infrastructure race; no. |
| Copy top-30 wallets within seconds | Backtest mean +22% and median +7% net, with 16-27 unpriceable exits | **Testing live** (paper book `life-1`). |
| Early momentum | Above: -6.8% per trade with stops | Avoid. |
| Breakout after 6 h | About 0 | No edge. |
| Dip in established tokens | +72% per trade over about 2 episodes | **Forward-test** as a second paper book. |
| Builder programme (Montra) | 25% of the protocol fee on launches made with our code, for 90 days. All builders share a cap of 6% of weekly protocol fees, about $31k/week at current volume. | Needs a launch feature in the bot. |
| Fomo referral on our Fomo buttons | 25% referral share of Fomo's 0.5% fee | Verify the terms. |
| Elysium builder incentives | Half of sequencer revenue funds builder incentives and treasury | Apply: the bot is ecosystem infrastructure. |

## 5. Sources

- Bankless, "pump.fun, fomo sign on Elysium day one" (2026-10-29): <https://www.bankless.com/read/news/pump-fun-fomo-sign-on-elysium-day-one>
- Bitrue, "What is MONTRA coin on Elysium network" (2026-11-11): <https://www.bitrue.com/blog/what-is-montra-coin-elysium-network>
- fomo, "Leveraging fomo's social features": <https://fomo.family/blog/learn/leveraging-fomos-social-features>
- Datawallet, "Fomo app explained": <https://www.datawallet.com/crypto/fomo-app-explained>
- Wikipedia, "Fomo (platform)": <https://en.wikipedia.org/wiki/Fomo_(platform)>
- Blockworks, "Fomo could help you track KOL moves": <https://blockworks.co/news/fomo-to-track-kol-moves>
- Montra docs and API: <https://montramarkets.xyz/docs>
