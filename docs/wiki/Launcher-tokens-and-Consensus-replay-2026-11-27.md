# Launcher tokens and a 30-day Consensus replay (2026-11-27)

Read-only queries on the production database (as the website's `dashboard_ro` role), Agora's public token API, and execution prices of tracked wallets. HYPE ≈ $62.87 (Agora, 2026-11-27 01:20 UTC).

## 1. Tokens created by serial launchers, 7 days to 2026-11-27 01:20 UTC

**Set:** every token whose first tracked buy was a create+buy sent to the eleos launch contract `0x9c73d179b2911e89a4edc47cfd78765990bc6761`. That gives **2,596 tokens from 40 wallets**. All 40 are serial launchers (3+ launches each; the busiest, `0x95fa70ef…`, made 510 in the week). Many names are relaunched dozens of times by the same wallet, for example `Rockstar` ×226, `FOMOEGG` ×100, `HOUSE` ×82 and `GANG` ×77.

**Where they are now** (Agora market cap, 2,142 tokens still in Agora's 7-day volume list; the other 454 traded under 5 HYPE in 7 days):

| Market cap now | Launcher tokens (2,142) | Other new tokens of the week (1,247) |
|---|---|---|
| ≥ $100k | **0** | 4 (TAIL, TOKOS, LEASH, pole)¹ |
| ≥ $50k | 1 (HYPEPE, 10 minutes old at the time) | 11 |
| ≥ $10k | 8 | 27 |
| median | **$1,892** (the empty launch curve) | $1,892 |

¹ Two more show $3M on Agora with 40-200 HYPE of volume, most likely a price error, so they are left out.

**Peaks:**

- **241 of the 524 highest-volume launches** (1,346-9,137 HYPE volume) printed a candle high above $100k. The highest was $247k.
- In a stratified random sample of 200 launches (55-minute candles):
  - 59 wicked above $100k. Only 8 had a candle *close* there.
  - **One hour after launch, 0 of 200 were above $25k.** The median was back at $1,892 and the highest was $19.9k.
  - At 6 h, 24 h and now the picture is the same: 0 of 200 above $25k.

**The creators' side** (sample of 200; 195 with both a buy and a sell):

- The creator buys in the creation transaction and makes its first sell a median **1.2 minutes** later (p75 5.5 min).
- It sells at a median **1.64x** its own average buy, and 98% of launches are profitable for it.
- Total creator profit: **+111,846 HYPE** on 2,596 launches. 91% of launches were profitable; the median was +32.6 HYPE per launch.

**Reading:** the "$100k+" is a wick lasting a minute or so. The creator's own buy plus the bots that buy every new eleos launch push it up, and the creator's sell takes it back to the floor. On these curves the market cap moves far more than the HYPE behind it: tokens with 1,300-9,000 HYPE of *total* volume printed $100k-$250k. Nothing was left to follow: an alert fires after the creator's buy, which is near the top.

**Decision:** never alert these wallets or any token they launched. Keep indexing them, because the rule needs their trades to recognise new burner wallets (about 66 since 11-12). PR #138 does this automatically.

## 2. Consensus: 30-day replay (2026-10-28 to 2026-11-27)

**Method:**

- Cards were recomputed with the channel's settings (3 traders, 30 min, 0 HYPE minimum, independent off) on the same public trades the sender reads.
- Each card is judged as it looked **60 s after the trigger**, when it would have been posted.
- **Features** are what was known at that moment:
  - independent operators, with same-block, bundle-wallet and same-size groups counted once;
  - cohort and launcher members, as of the trigger;
  - market cap from the nearest Agora snapshot (±30 min);
  - token age;
  - Agora's 1-hour change from the last snapshot before the trigger;
  - how many earlier cards the token had.
- **Outcome** comes from execution prices of tracked wallets on the token. Highs are therefore a lower bound; for example, PERP measures 4.05x here against 7.9x on Agora.
- **Entry** is the highest buy price paid within 90 s after the trigger (at least the trigger price).
- Agora's candle prices could not be used for ratios: on about a quarter of the tokens they disagree with execution prices by up to ~100x.

**Classes** (within 24 h of the signal):

- **profit:** reached ≥ 2x the entry.
- **trash:** never reached +30%, and the 24-hour close was ≤ 0.5x or the token is dead now (< $5k).
- **flat:** everything else.
- **open:** cards younger than 24 h that are neither profit nor dead.

| | Cards | profit | flat | trash | open / no data |
|---|---|---|---|---|---|
| Replay, channel settings | 472 | 97 (22%) | 145 (33%) | 197 (45%) | 33 |
| Actually posted (deliveries table) | 53 | 11 | 10 | 21 | 11 |

There are no cards on 11-13 to 11-16 because of the tracker stall fixed in PR #137. 11-02 to 11-12 was dominated by launch cohort 1, so most of the 30 days is a different regime from the last 10 days.

### What trash looks like at the moment of the signal (439 evaluable cards; base: 22% profit, 45% trash)

| Feature at the signal | Cards | profit | trash |
|---|---|---|---|
| Market cap < $5k | 70 | 3% | **84%** |
| Market cap $5k-10k | 67 | 13% | 67% |
| Market cap ≥ $10k | 246 | 31% | 32% |
| 0-1 independent operator | 119 | 13% | 55% |
| 4+ independent operators | 37 | 43% | 30% |
| Token launched by a serial launcher | 21 | 14% | **71%** |
| Token under 3 minutes old | 153 | 23% | 59% |
| 2+ cohort wallets among the traders | 126 | 17% | 52% |

Among cards that pass the PR #138 gate (212):

| Feature | Cards | profit | trash |
|---|---|---|---|
| 3rd or later card on the same token | 22 | 5% | 41% |
| Token price falling over the last hour (Agora 1h change < 0) | 32 | 9% | 31% |
| Members sold half or more within 1 h (not usable at entry) | 41 | 39% | 27% |

The last row is a negative result: consensus members taking profit early is *not* a dump warning, so it should not drive an exit alert.

### Gates compared

The mean multiple uses a simple plan: sell at 2x if it is reached within 24 h, otherwise sell at the 24-hour close.

| Gate | Kept | profit | trash | Winners kept | Mean multiple | Since 11-17: kept / profit / trash |
|---|---|---|---|---|---|---|
| None (channel settings) | 439 | 22% | 45% | 100% | 0.98 | 32 / 10 / 16 |
| **PR #138** (no launcher token, ≤1 cohort wallet, 2+ independent after launchers, MC ≥ $10k, or 3+ independent when MC is unknown) | 212 | 30% | 33% | 66% | 1.02 | 11 / 7 / 1 |
| #138 + skip 3rd+ card on a token + skip falling 1h price | 169 | 36% | 33% | 62% | 1.10 | 9 / 6 / 1 |

The second refinement was found on the same data it is scored on, so it ships in **log-only mode** with PR #138 (`CONSENSUS_EXTRA_RULES=log`). The sender still posts those cards and logs `consensus: shadow rule would skip …`. After 1-2 weeks, compare those cards' outcomes with the rest, then switch to `enforce` or drop the rules.

Since 11-17, PR #138 would have:

- **kept** PERP ×2, LEASH, LQDS, TOKOS ×2 and ASYNC (profit), SMOOR ×2, LiquidV and INTERESTING, plus 8 cards still open on 11-27;
- **dropped** all 15 trash cards (ZPRC ×6, AURA, MUSHROOM, LOOP, e/acc on 11-26, MONTRADAR, PCOMP, OFFRECORD, EPYH, UMBRA);
- **lost** three winners: e/acc (11-19, one coordinated trio), EACCVIDIA (a launcher among the buyers) and MNTR6900 (a $1.9k-MC launch snipe that went 2.2x).

### Limits

No combination of signal-time features keeps only winners. The ceiling in this data is about 35-40% winners, with a third of kept cards still dumping. What the gate removes is the *structural* trash: launch farms, one operator posing as several, and sub-$10k caps. What remains is ordinary market risk, which needs an exit plan rather than a better entry filter. The recent sample (32 evaluable cards since 11-17) is small.
