# Transfer-only group on contract 0x1b341fdd

Found by the transfer-only scan of 2026-11-12. Blocked the same day.

## Summary
- **Members:** 7 wallets, all on Agora's Top 100 (ranks 28-80).
- **Handling:** blocked (`TRACKING_BLOCKED_WALLETS`).
- **Why a group:** each wallet sent 28-37 transactions in 30 days, all to the same contract `0x1b341fdd55d56f8e08409a2c25ddfad93eb03c1a`; none has a decoded buy or sell; their last activity falls in the same two minutes (12:48 and 12:55 UTC on 2026-11-11); pairs share 3-4 of their 7-9 tokens within 10 seconds of each other.
- **Agora PnL:** 379-664 HYPE each. Unverified on our side: we have no measured PnL for them.

## Wallets
| Wallet | Rank | Best | Agora PnL (HYPE) | Legs (30 d) | Transactions to the contract | Last activity (UTC) |
|---|---|---|---|---|---|---|
| `0xe3c9ecc9b67e90646de40813a608e895c4d0cddb` | 28 | 16 | 664 | 35 | 32 | 2026-11-11 12:55 |
| `0xf25d0310cc65c3638a9582cf7dcb0ce41645bd30` | 46 | 28 | 490 | 42 | 36 | 2026-11-11 12:55 |
| `0xc574b9ae9ccb685aae0f018f56fa45eac098f1a5` | 49 | 31 | 484 | 46 | 37 | 2026-11-11 12:48 |
| `0x7a1cca8743591a636bc40e2765874e9a5336f825` | 61 | 38 | 432 | 39 | 32 | 2026-11-11 12:55 |
| `0xb96287c353ed3cac8922b44d7e84e1b9d8748d18` | 62 | 39 | 429 | 39 | 32 | 2026-11-11 12:48 |
| `0xfdde597156af67e7ab5c76edeef91cb292d2e017` | 72 | 49 | 403 | 37 | 31 | 2026-11-11 12:48 |
| `0x8517beced25bc8de4c4ede36c437ea3d71e46e42` | 80 | 47 | 379 | 34 | 28 | 2026-11-11 12:55 |

## Limits
- The contract was not identified. It could be a private router (as with cohort 1) or a venue the tracker does not decode; either way there is nothing to follow.
- The launch-cohort detector did not flag them: they share too few launch bursts. The grouping rests on the shared contract and timing.
- Funding sources were not traced.


---
_Labels are rule-based inferences from recorded on-chain activity (labels-v1, 30-day lookback), not proof of common control or misconduct. Snapshot 2026-11-12, chain 99901. Source of this page: `docs/wiki/` in the repository; edit it there._
