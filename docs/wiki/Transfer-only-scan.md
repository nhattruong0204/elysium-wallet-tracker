# Transfer-only scan (2026-11-12)

**Question:** which tracked wallets show token transfers but no decoded buy or sell, and do they form groups?

**Method:** leaderboard wallets with 20+ recorded legs and zero BUY/SELL legs in 30 days; for each, the contracts its transactions were sent to; pairs that touched the same token within 10 seconds.

**Result:** 32 wallets.

| Group | Wallets | Decision |
|---|---|---|
| [Launch cohort 1](Launch-cohort-1-router-0x3e328152) (router `0x3e328152…`) | 16 | already blocked |
| Same router, outside the cohort | 3 | blocked |
| [Contract `0x1b341fdd…`](Transfer-only-group-0x1b341fdd) | 7 | blocked |
| Machine wallet `0xfffff389…` (contract `0xf045fae5…`) | 1 | already blocked |
| No group evidence (below) | 5 | left alone |

## Left alone: no group evidence
| Wallet | Rank | Agora PnL (HYPE) | Legs | What we see |
|---|---|---|---|---|
| `0x4afcfed6306de68a8f8ddaa54b7fd3c12c158fd3` | 1 | 5,820 | 37 | 30 self-sent transactions to `0xa13f7611115752deb2b13813bdde16a5073e4b45`. The top Agora trader; probably a venue we do not decode. One other (non-leaderboard) tracked wallet uses the same contract. |
| `0x8d1917ac20e4ed13a076cf63cd66c430b303bb0d` | 24 | 687 | 107 | 50 transactions to `0x7ea47a73f3c51c4b9d4230d2d4ecb3ed272a6227`, 6 to `0xa13f7611…`. |
| `0xe6133120cdc4fcd56a389a0be1ccf13dacc8870d` | 42 | 533 | 998 | 998 transactions to `0xe8488e1a114e65e23725c033c69561e3b6fbb2b8`, none sent by the wallet itself. Already labelled `automated` and not alerted. |
| `0x581ad4dbe58ef8cedac6988b34500d505de1b72a` | – | 579 | 42 | Receives only: transactions sent by others via `0xa834e70303322d86e5dae95ee47e9c6a073d9812`. |
| `0x0c175c6a0065ee05f871a68783d2de432a1e6cbe` | – | 454 | 20 | Receives only, same contract `0xa834e703…`. |

A transfer-only wallet is not necessarily coordinated: it may trade on a venue the tracker does not decode. The first two rows are candidates for decoding that venue rather than blocking.


---
_Labels are rule-based inferences from recorded on-chain activity (labels-v1, 30-day lookback), not proof of common control or misconduct. Snapshot 2026-11-12, chain 99901. Source of this page: `docs/wiki/` in the repository; edit it there._
