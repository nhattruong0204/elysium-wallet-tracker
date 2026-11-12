# Launch distributor 0xc57bf04f

Found on 2026-11-12 from two alert floods. Blocked the same day.

## Summary
- **Wallet:** `0xc57bf04f886c7cc754f66fd30fdc39373e6b9947`, Agora rank 82-93 on the day, Agora PnL about 350-380 HYPE.
- **Handling:** blocked (`TRACKING_BLOCKED_WALLETS`). It was labelled organic, scalper/sniper, consistent winner before this was noticed.
- **Pattern:** buys roughly 60% of a new token in its launch second, sells exactly 1% about a minute later, then sends most of the rest to 50 wallets in equal amounts within about 30 seconds.

## The two launches
| | JELLY `0x7347a64423c67cdb7e6186a217c5e35397819663` | Catbalio `0x5068b816f0c09ab03a1028fb18127f9f5b120b14` |
|---|---|---|
| Opening buy (UTC) | 12:23:04, 624.3M tokens for 50 HYPE | 13:50:35, 599.3M tokens for 45 HYPE |
| First sell | 12:23:30, 6.24M (1% of the bag) | 13:51:48, 5.99M (1% of the bag) |
| Split | 50 transfers of 9,068,878.72 in 33 s | 50 transfers of 7,302,582.99 in 30 s |
| Share of the bag sent out | about 73% | about 61% |
| Sold by the wallet itself | 21.8M for 5.56 HYPE | 8.3M for 1.77 HYPE |
| Telegram alerts caused | 54 | 52 |

JELLY launched on the eleos launchpad at 12:23:04 UTC, the same second as the buy; both buys went through contract `0x9c73d179b2911e89a4edc47cfd78765990bc6761` and the sells through `0x787344f1ac74d78772b5d9314551b3b727ee914b`.

## Reading
Most likely the creator of these tokens, or a first-block buyer working with the creator: the split makes the holder list look distributed and lets the supply be sold from wallets that do not look connected. Anyone following the opening buy would be buying a token about 60% controlled by one party.

## Limits
- The creator address was not confirmed.
- The 50 recipients of each split are not tracked wallets and are not recorded per wallet here.

## What we changed because of it
- Transfer bursts are summarised: one "splitting" alert and one final summary instead of one alert per transfer.
- New `distributor` label for wallets that buy a token and send it out 10+ times within the hour.
