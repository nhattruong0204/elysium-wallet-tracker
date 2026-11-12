# Launch cohort 1: router 0x3e328152 (transfer-only)

The first coordinated launch cohort, found on 2026-11-12 (the "JEWBOTS" investigation). Live: https://onchainflow.app/cohorts/0x0a90d9d0d0c76c5fe79faa6f6de4105fd3e68c79 · `GET /api/v1/cohorts/0x0a90d9d0d0c76c5fe79faa6f6de4105fd3e68c79`

## Summary
- **Members:** 14 detected by the launch-cohort rule, plus 2 more blocked with the group the same morning (16 total). All were on Agora's Top 100 (ranks 6-57 today).
- **Confidence:** high (21 shared launches in 7 days).
- **Handling:** blocked (`TRACKING_BLOCKED_WALLETS`): not followed, no alerts. Owner decision 2026-11-12: transfer-only wallets give nothing to follow.

## How they behave
- **A new token roughly every 29 minutes.** On each one, 9-16 of these wallets are among the first tracked holders within seconds of each other.
- **They do not trade like the rest of the board.** In 30 days none of them has a single decoded buy or sell: every leg we recorded is a token transfer. They go through router `0x3e328152d399a500a7423a7373ef767a906628d4`, which only this group uses and which the tracker does not decode. Style label: `mover`; 7 of 14 are `high_frequency`.
- **Tokens move between the wallets** (transfer in, transfer out) before being sold into whoever follows.
- **Agora still credits each wallet with 440-1,118 HYPE of PnL**, which is what put them on the leaderboard. We have no measured PnL for them (no decoded closes), so Agora's figure is unverified.
- **Effect on us before the block:** their entries looked like many independent top traders buying the same launch, which triggered cluster and consensus signals on tokens where followers were the exit liquidity.

## Wallets
| Wallet | Rank | Best | Agora PnL (HYPE) | Traits | Detection |
|---|---|---|---|---|---|
| `0x34518ccd5d4ff95bf9796737c251981ac2406380` | 6 | 6 | 1,118 | high_frequency | detected |
| `0x67b730d0f0e6bcd300f1f56830d2e4a098467607` | 7 | 7 | 1,020 | high_frequency | detected |
| `0x0a90d9d0d0c76c5fe79faa6f6de4105fd3e68c79` | 9 | 9 | 911 | high_frequency | detected |
| `0x88eb6d4ffe88dbe1630fa1b90c55a0c1b6aef3f5` | 14 | 11 | 825 | high_frequency | detected |
| `0x661e930767f642022ca44a3d54cab84be0b1dfed` | 15 | 15 | 786 | high_frequency | detected |
| `0xf3c195155de7746aae67593a46c0a946ce2dbf3f` | 17 | 16 | 771 | high_frequency | detected |
| `0x59cbdc879f275c661cb957f1f2f96671cae71a17` | 22 | 21 | 695 | high_frequency | detected |
| `0x782056dd4c5343c0acbefe7c2f8f062c4d451e0c` | 31 | 31 | 592 | – | detected |
| `0xb4cc96786ec93db7f421dd0ee3c83fb578684fb1` | 32 | 32 | 583 | – | detected |
| `0x36a95bd7481b9cb2a42771067f8bfd8ee22cc7ca` | 37 | 37 | 550 | high_frequency | detected |
| `0x6b9e6a679cab2481c6ddb4b43e5ef4853e34c3d8` | 41 | 39 | 526 | – | detected |
| `0xdfc284ba237f0b5fb1ffe13bc7092b262bb5f9bd` | 42 | 38 | 523 | – | detected |
| `0x74fae0ec6ac058c9cdbce006242d8bf37951d858` | 56 | 55 | 443 | – | detected |
| `0x89421958985e15f5f24efed8c260d662e4b5574a` | 57 | 54 | 440 | – | detected |
| `0x0034f8e8e551b12ff6c4c678774bc64ccf13aab8` | – | 63 | 367 | – | blocked with the group, not re-detected |
| `0x77e9349a3964bcacc933a18f8f14e6f8a99f649f` | – | 50 | 404 | – | blocked with the group, not re-detected |

## Shared launches (21)
JEWBOTS, Finera, CYBERHOG, ASR, SPON, BYESEN, ROCOO, BIND, SP, JENLON, CPU, MEMEPANY, X, THIN, BERNIE, EXPERIMENT, PHANTOM, FI, NVIDIA, STEPR, MONEY

Example tokens examined by hand: JEWBOTS `0x0233722e99196c355fd8c0bc76a3b694e6c61bbe`.

## What we changed because of it
- PR #130: launch-cohort detector (alerts from cohort wallets are not sent; tokens they launched into carry a warning; Consensus channel skips cards with 2+ cohort wallets).
- PR #131: cohort / bundler labels, cohort page and CSV export on Wallet intelligence.


## Limits
- Because their trades are undecoded, entry sizes, hold times and realised PnL are unknown.
- Funding sources were not traced (native-coin funding transfers are not indexed).
- The cohort id is the lowest member address and changes if membership changes.

## Three more wallets on the same router (blocked 2026-11-12)
Found by the transfer-only scan. They are not in the detected cohort (too few shared launch bursts), but every one of their recorded transactions goes to the same router and they have no decoded buy or sell.

| Wallet | Rank | Best | Agora PnL (HYPE) | Transactions to the router (30 d) |
|---|---|---|---|---|
| `0x791d2bcbcd52913b13cdcc490438b0f1d9be2ab0` | 71 | 69 | 404 | 34 of 34 |
| `0x198db2966622b3590f392ac5aa6b21d30c627838` | 94 | 92 | 343 | 22 of 22 |
| `0x87eda45e1ba46dd8549e3df18b875baa25551a65` | – | 92 | 336 | 29 of 29 |

---
_Labels are rule-based inferences from recorded on-chain activity (labels-v1, 30-day lookback), not proof of common control or misconduct. Snapshot 2026-11-12, chain 99901. Source of this page: `docs/wiki/` in the repository; edit it there._
