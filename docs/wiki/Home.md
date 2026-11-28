# Elysium wallet tracker: research records

Findings about tracked wallets, kept out of the issue tracker so they stay readable. Live labels: https://onchainflow.app/leaderboard

## Coordinated groups
| Page | Wallets | Handling |
|---|---|---|
| [Launch cohort 1: router 0x3e328152](Launch-cohort-1-router-0x3e328152) | 16 + 3 | blocked (transfer-only) |
| [Launch cohort 2: same-block launch snipers](Launch-cohort-2-same-block-snipers) | 7 | followed as usual |
| [Transfer-only group on contract 0x1b341fdd](Transfer-only-group-0x1b341fdd) | 7 | blocked (transfer-only) |
| [Same-block pair groups](Same-block-pair-groups) | 18 in 7 groups | followed, labelled |
| [Launch distributor 0xc57bf04f](Launch-distributor-0xc57bf04f) | 1 | blocked |
| [Transfer-only group 2 on router 0x3e328152](Transfer-only-group-2-router-0x3e328152) | 13 | blocked |
| Serial token launchers (rule, see the weekly review) | 41 this week | never alerted (automatic) |

## Scans
- [Weekly review 2026-11-19 to 11-26: smart money, launchers, inactive wallets, Consensus](Weekly-review-2026-11-26)
- [Launcher tokens and a 30-day Consensus replay (2026-11-27)](Launcher-tokens-and-Consensus-replay-2026-11-27)
- [Elysium market structure and strategy research (2026-11-28)](Market-structure-and-strategy-research-2026-11-28)
- [Transfer-only scan (2026-11-12)](Transfer-only-scan)

## Policy (owner, 2026-11-12)
- A group with **no decoded buy or sell** (token transfers only) is blocked: there is nothing to follow.
- A group that **really buys and sells** stays followed; the launch-cohort detector keeps its wallets from being counted as independent signals.

Rules and operations: `docs/OPERATIONS.md` in the repository. These pages are published from `docs/wiki/`; edit them there.
