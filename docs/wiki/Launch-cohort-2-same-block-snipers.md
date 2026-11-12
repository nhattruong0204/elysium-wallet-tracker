# Launch cohort 2: same-block launch snipers

The second coordinated launch cohort, found on 2026-11-12. Live: https://onchainflow.app/cohorts/0x5732b9e35e5c5bc4889bbafce634545247601c9d · `GET /api/v1/cohorts/0x5732b9e35e5c5bc4889bbafce634545247601c9d`

## Summary
- **Members:** 7 wallets, 2 currently on Agora's Top 100 (ranks 27 and 53), best ranks 3-94.
- **Confidence:** high (34 shared launches in 7 days).
- **Handling:** followed as usual (owner decision 2026-11-12: they really buy and sell). Labelled `cohort` + `bundler`. While the launch-cohort detector sees the pattern, their own alerts are not sent and they count as one operator in signals.

## How they behave
- **Launch snipers that really trade.** Unlike cohort 1, their buys and sells are decoded. Every member is a `sniper`: they buy within 60 s of the first tracked trade on 85-100% of the launches they touch (up to 154 of 182 for the busiest wallet).
- **Same block, same partners.** 5-7 of them land in the same block on most launches; each wallet shares 14-150 same-block buys with the same 4-6 partners.
- **Small, uniform size.** Median buy 1.4-1.8 HYPE for every member.
- **Short holds.** Median 4-12 minutes for the five active wallets (24-44 minutes for the two quieter ones).
- **Profitable on what we can measure.** Five are `consistent_winner` (measured +11 to +71 HYPE in 30 days); two are roughly flat.
- **Shared infrastructure signals.** They call a contract no other tracked wallet calls, and 13 token transfers were recorded between members (3 carry the `linked` trait).
- **Interpretation:** one operator running several wallets, or clients of the same sniping/copy-trading bot. The data cannot tell these apart.

## Wallets
| Wallet | Rank | Best | Agora PnL | Measured PnL 30d | Tier | Trade legs | Early / launches | Median hold (min) | Median buy | Same-block buys |
|---|---|---|---|---|---|---|---|---|---|---|
| `0xd8ba9e5886b7000bace2c9dbdd0be98ac77d4a28` | 27 | 3 | 667 | 71.0 | consistent winner | 440 | 154/182 | 8.7 | 1.8 | 150 |
| `0x8bfee4f379d78955c5a42e9335af361a7e9101e3` | 53 | 11 | 455 | 69.2 | consistent winner | 379 | 141/166 | 8.8 | 1.7 | 150 |
| `0xdd88b3e5c8000c19514b8c30a8adf83223d784ab` | – | 30 | 210 | 11.2 | consistent winner | 198 | 78/91 | 12.2 | 1.6 | 108 |
| `0xb0dc4980d62543f761554427debd832919b9f7a7` | – | 16 | 318 | 13.4 | consistent winner | 172 | 66/73 | 11.3 | 1.6 | 97 |
| `0x8f8952ce273546d8db48586e20aa884adcb95945` | – | 77 | 170 | 16.6 | consistent winner | 159 | 59/68 | 4.2 | 1.5 | 79 |
| `0x5732b9e35e5c5bc4889bbafce634545247601c9d` | – | 79 | 123 | -0.8 | mixed | 20 | 13/13 | 24.5 | 1.4 | 14 |
| `0xbc7d33197d06917f8f8e50f79b5fd55dbb7f49ca` | – | 94 | 112 | -0.7 | mixed | 18 | 12/12 | 44.4 | 1.4 | 14 |

Amounts in HYPE. Measured PnL covers only sells whose cost basis we know and is never combined with Agora's figure.

## Shared launches (34)
POPO, EDOG, ATM, TIDE, RWA, TICKER, CHEST, LIQ, MNTH, CNINE, NDOG, GNTR, CORE, NEBULA, VANITY, ACT, COMET, ACC, ELYSINO, HERMES, MOSS, SAM, FRIDGE, EASHOOD, ALTNERO, DICES, MOSAIC, LINE0, MCAT, ELYSD, CROSS, PP, ORBIT, GUSHY

## Why it matters for signals
Seven "top traders" buying the same launch within a second is one decision, not seven. They now count as one operator in cluster and consensus scoring.



---
_Labels are rule-based inferences from recorded on-chain activity (labels-v1, 30-day lookback), not proof of common control or misconduct. Snapshot 2026-11-12, chain 99901. Source of this page: `docs/wiki/` in the repository; edit it there._
