# Weekly review: 2026-11-19 to 2026-11-26

**Window:** 7 days to 2026-11-26 17:42 UTC, chain 99901. Read-only queries on the production database (trades, measured PnL, positions, Agora market snapshots, Consensus deliveries), plus on-chain receipts for a sample of buys. HYPE ≈ $60.15.

## Summary

- **Activity rose about 10x.** Tracked buys went from about 2,500 HYPE a day (11-13) to 20,000-45,000 HYPE a day (11-19 to 11-26); 300-600 tokens a day.
- **Almost all of the "profit" is token launching.** 41 tracked wallets created **2,573 tokens** in the week through the eleos launch contract `0x9c73d179b2911e89a4edc47cfd78765990bc6761`. Each time they bought 50-100 HYPE in the creation transaction itself and sold about a minute later to snipers and bots. They made **+117,900 HYPE of the +121,980 HYPE** measured for all tracked wallets (97%). They also caused **7,884 of 11,509 Telegram trade alerts (68%)** and 59 of 63 burst alerts.
- **About half of Agora's current top 100 are these launchers** (ranks 1, 2, 4-11 and most of 15-51). Agora's leaderboard has stopped being a list of good traders.
- **Real discretionary profit is small and concentrated** in about 15 wallets, most of them *former* top-100 members. They found the week's real winners (PERP, LQDS, LEASH, PANTHEON, TAIL, LiquidV, BLOB) early.
- **Consensus:** every bad card was driven by one coordinated group or sat at a tiny market cap. A rule of "2+ independent wallets, $10k+ market cap" would have kept all 7 winners and dropped all 5 bad cards. It would also have dropped one card whose token rose ~9x by Agora's market cap (e/acc on 11-19, driven by one coordinated trio). SMOOR (-45%) passes the rule either way.

## How the launch evidence was established

- **Transaction target:** every one of the 2,573 launch buys was sent by the wallet itself to `0x9c73d179…`. No buy of an already-trading token went there.
- **Receipts:** 8 of 8 sampled launch buys from different top earners mint the token in the same transaction (Transfer from the zero address). 0 of 9 control buys from discretionary wallets do.
- **Launch time:** the buy lands within 2 seconds of the launch time Agora reports (median 0.0 s; 2,466 of 2,519).
- **Rule check:** "bought in the launch second, as the first tracked trade, on 3+ tokens in 7 days" (stored data only) selects exactly the same 41 wallets as the receipt evidence. No wallet is selected by one method and not the other.
- **Wallet lifetime:** most launcher wallets are used for 1-3 days and then abandoned. 66 such wallets exist since 11-12.

## Who to focus on: likely smart money

Picks are buys of tokens the wallet did **not** launch, scored by market cap at entry versus later (Agora snapshots). This is a 7-day sample in a rising market, so treat it as a watch-list, not proof of skill. Measured realized PnL is small for all of them; most still hold.

| Wallet | Picks ≥1.5x now | Best calls (entry MC → now) | Notes |
|---|---|---|---|
| `0x05f6ff40ed83b8946dfc95e133131fd7bf82d0e1` | 7 / 10 | PANTHEON $17k → x16 (sold +43 HYPE), TAIL x3.9, LEASH x3.6, SUNFALL x2.6 | Best picker of the week; in the DEMI card |
| `0x91db3c4396966e0d768689cf75f31d60e0422101` | 11 / 18 | LQDS $22k → x5.5, BLOB $76k → x3.8, AVALON x2.6, AGORA x1.9 | Most consistent; holds hours; in the LEASH and LQDS cards |
| `0x2dc94f0db9ad9f662dc211c9486c96fff5d49d1f` | 4 / 8 | TAIL $62k → x5 (sold +14 HYPE), LEASH x4, LQDS x3.3 | Holder |
| `0xfd7e55a555555c2f25053a38ec744de1afea4fa4` + `0xfdaf199ae935753c70824f6c73d4877ddceea57b` | 7 / 12 | PERP $22k → x7.9, QUOTED x3.8, IMP x3.6, KLEOS x2 | One operator, two wallets (same-block pair); in 4 cards |
| `0xebd75100ac3e301f41b1c9be71ac4215e5c60188` | 7 / 11 | PERP $22k → x7.9, INTERESTING $37k → x1.8, BLOB x4.5 | Largest sizes (634 HYPE; 292 HYPE in LiquidV); in 5 of 14 cards |
| `0x246730c82b24ebe75c285c84b734e7415a0c8d7e` | 2 / 3 | LiquidV $224k → x8.7 (peak 12x, +19 HYPE realized), LQDS x2.5 | |
| `0xddbcdf710c21219dc5e56a6e1e8576fb4aa99d96` | 5 / 5 | BLOB (elyspump) $62k → +42 HYPE realized | |
| `0x8ea3b8e19221a145e180b704dbd827bf0deca4b0` | 4 / 8 | LiquidV $253k → x7.7 (holding), QUOTED x2.1 | 213 HYPE deployed |
| `0xe17b2c7a32feee41a71663d428475f7b5be6a01e` | 6 / 9 | BLOB x16 peak, INTERESTING $20k → x3.4 | Small sizes |
| `0x775256e60f88c5e0f149db67fbcb738da75143d7` | 10 / 18 | BUTTERFLY, TAIL, SUNFALL, LQDS | Small sizes, quick exits |
| `0xf9ecd9891d670bf37c4ec72e276c563e5c988297` | 3 / 3 | LQDS +5.7 HYPE, INTERESTING, DEMI | |
| `0x6c39d7e6d7689b7a0d81aff7cc8a346fe054ff13` | 4 / 7 | PANTHEON at $12k (x22), INTERESTING at $9k (x7) | Finds tokens very early but sells within minutes: a radar, not a holder |

Also worth watching (one or two large calls): `0xd74ad86139121726b1f5ff922c0d37c8a07bb88f` (116 HYPE LiquidV at $343k, x5.7), `0x2e27296db73efa090c33823b0b637f031d1a1f97`, `0x95ac25fdecbc6a4b6ee398a62058bb048cee70c4`, `0x99038bb60d8d812f8ac47d3bc3ae6ff0dfd3e68b`, `0xaf0fdd39e5d92499b0ed9f68693da99c0ec1e92e`.

**Skilled but not followable:** `0x0641315fda9935624bbb014a38b7a30e6b1e6e46` (Agora rank 3) and `0x88e79da7b98e7eb4b7917a5bfcc14fe399ba813c` (rank 61) are launch scalpers. They buy new launches 2-6 minutes in, sell within a minute, and 666 of 670 (and 122 of 123) of their tokens are now dead. They make +300 to +400 HYPE a week from the launchers' tokens, but nobody can copy a 40-second hold from an alert.

## Ignore: serial token launchers (41 active this week)

Top ten by 7-day measured PnL (HYPE):

| Wallet | Agora rank | Tokens launched | 7d PnL | Median hold |
|---|---|---|---|---|
| `0x95fa70efcb193961d3c0a7e6889616643a0c5576` | 1 | 483 (272 named the same) | +21,917 | 34 s |
| `0x4945280027ffced153401d699f1621a46b1adb46` | 2 | 235 | +15,993 | 24 s |
| `0xe065bbc60b355be9d884489ad63fcb5c663fd52e` | 4 | 261 | +11,434 | 35 s |
| `0x134e50aafd9fe4e8525092ec0ee2b97218298f6a` | 5 | 188 | +7,349 | 84 s |
| `0xa98ca0cdddbb239af17d4480230abd191a89221e` | 6 | 136 | +6,363 | 52 s |
| `0x556c89cc2b3f33002576fed06873d84ca164ce62` | 7 | 104 | +6,345 | 46 s |
| `0x8dbe0a2f8ee46e2d0ac0530058466ae34f010838` | 8 | 163 | +5,980 | 42 s |
| `0x6352d587293b39790f96f5ca945abc7e8881d36b` | 10 | 129 | +4,017 | 64 s |
| `0xc66d9fdbee720693026234c47c63311476b97321` | 11 | 50 | +3,455 | 52 s |
| `0x1035548d24662e5bf5ab6e7d902ee87d2a4cd1ce` | 15 | 151 | +3,158 | 102 s |

They often reuse a trending name (PERP, AGORA, UP, "epacc", FOMOEGG) so their launches catch buyers looking for the real token. No list is kept by hand: the rule below finds new launcher wallets as they appear.

## Inactive

- **Overall:** 779 tracked wallets are active in our records. 170 bought or sold in the last 7 days, 104 last traded 8-14 days ago, 148 15-30 days ago, and 357 have never had a decoded buy or sell.
- **Current top 100:** 27 of the 83 wallets we follow made no decoded buy or sell this week:
  - 13 are a second transfer-only group on router `0x3e328152` (cohort 1's router), silent since 11-20 07:41 UTC. Blocked: see [[Transfer-only-group-2-router-0x3e328152]].
  - 6 are launcher wallets that have gone quiet (burners).
  - Old names (last trade 11-07 to 11-15): `0x9e39d6282e4525526f33092c8aad27005e0ea02d`, `0xa1f9ea41cab1167b91c7e6226f6b7da4dbbba77e`, `0xc88252fcf1690e45195f690f9a6c1b6423affacc`, `0xbc4a11a09a9898568e28533ef537adeb489900a6`, `0x657e8916d9edc72d5d25fdc012f29778bec0a0b3`.
  - `0x4afcfed6306de68a8f8ddaa54b7fd3c12c158fd3` (rank 17): transfer-only through `0xa13f7611…`, a venue we do not decode.
  - Two have nothing recorded at all.

## Consensus channel: what separated good from bad cards

| Card | Independent | MC at signal | Outcome (MC now ÷ at signal) | Traders |
|---|---|---|---|---|
| PERP (11-19, 11-20) | 3 | $22k / $35k | **7.9x / 5.1x** | 0xe53e53, 0xebd751, 0xfd7e55 · 0xb2f316, 0xfd7e55, 0x95ac25 |
| LQDS | 3 | $22k | **5.5x** | 0x6c39d7, 0xebd751, 0x91db3c |
| LEASH | 3 | $67k | **3.9x** | 0x91db3c, 0x23299a, 0xbc4398 |
| LiquidV | 3 | $834k | **2.3x** | 0x99038b, 0xaf0fdd, 0x2e2729 |
| DEMI | 3 | $17k | **2.1x** | 0x645459, 0x05f6ff, 0xf9ecd9 |
| INTERESTING | 3 | $35k | **1.9x** | 0x3db29f, 0x7e272b, 0xebd751 |
| SMOOR (2 cards) | 3 | $135k | 0.55x | 0xe53e53, 0x2e2729, 0xebd751 · 0xc72bf6, 0xfd7e55, 0xa35d41 |
| MUSHROOM | 3 | **$4k** | 0.73x (dead) | 0x2dc94f, 0xfd7e55, 0xebd751 |
| e/acc (11-19) | **1** (3-wallet pair group, 2.00 HYPE each) | $2.2M (card: unavailable) | ~9x by Agora MC; tracked wallets scalped it | 0xbee5ba, 0x78685a, 0xaa78d8 |
| LOOP | **1** (cohort 2, 2.90 HYPE each) | $1.8k (card: unavailable) | dead | 0x8f8952, 0x8bfee4, 0xd8ba9e |
| e/acc (11-26, elyspump) | **1** (cohort 2, 5 x 1.30 HYPE) | $2.7k (card: unavailable) | 0.69x | cohort 2 |
| MONTRADAR | **1** (cohort 2, 3 x 1.33 HYPE) | $4.6k (card: unavailable) | 0.86x | cohort 2 |

**Good cards** had 3 independent wallets, varied sizes, $17k+ market cap, and recurring early callers (0xebd751, 0x91db3c, 0xfd7e55, 0x05f6ff).

**Bad cards** had one operator (cohort 2 or pair group 2), identical buy sizes, and a $2-5k market cap or none. Cohort 2's launch-burst count fell under the detector's threshold this week, so the cohort filter no longer caught it. The bundle rule did collapse it to "1 independent", but the channel's "independent only" setting was off, so the card was posted anyway.

## What changed because of this

See the PR "Launch-farm filter and Consensus quality gate" and `docs/OPERATIONS.md`:

- **Tracker:** serial launchers are detected (receipt proof in real time plus the stored-data rule) and never alerted. Their own launch tokens are muted for everyone, and they never count as independent buyers.
- **Wallet intelligence:** new `launcher` label.
- **Consensus:**
  - 2+ independent wallets are required, after removing launchers.
  - A $10k market-cap floor, and 3+ independent wallets when market data is missing.
  - 3+ wallets buying the same amount within two minutes count as one operator.
  - Tokens created by a launcher are skipped.
- **Website:** the wallet table, labels and launcher queries get a 30 s limit, which fixes the CSV export's 503.

---
_Labels are rule-based inferences from recorded on-chain activity, not proof of common control or misconduct. Source of this page: `docs/wiki/` in the repository; edit it there._
