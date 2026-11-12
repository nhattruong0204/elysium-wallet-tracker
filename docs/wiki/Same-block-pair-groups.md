# Same-block pair groups

The wallets labelled `bundler` that are **not** in a launch cohort (found 2026-11-12 by the wallet-label rules, PR #131). Live: https://onchainflow.app/leaderboard?window=all&label=bundler

## Rule
A wallet is a `bundler` when it bought the same token in the same block as **one specific other tracked wallet** at least 5 times on at least 3 different tokens in 30 days. Sharing a busy launch block with whoever else sniped it does not count. Pairs are reported as pairs: A-B and B-C do not make A-C related.

## Summary
- 18 wallets in 7 small groups (two triangles, one group of four, four pairs). Only 2 are on Agora's Top 100 today (ranks 50 and 91).
- Weaker evidence than a launch cohort: confidence is medium at best, and capped at low where our history of the wallet has gaps.
- Current handling: followed and alerted like any wallet; labelled on Wallet intelligence. They are **not** collapsed in signal scoring by this label (Consensus uses its own, looser same-block rule).

### Group 1 (3 wallets)
Three snipers, 37-43 shared same-block buys on 30-36 tokens. Two carry `high_churn` (2-3 minute holds, sells about equal to buys).

| Wallet | Rank | Style | Traits | Tier | Agora PnL | Measured PnL 30d | Trade legs | Median hold (min) | Same-block buys / tokens | Partners |
|---|---|---|---|---|---|---|---|---|---|---|
| `0x63e30ef22e1a3f6264ca9612f8efe0a30b9b3cf7` | – | sniper | high_churn, diversified | consistent winner | 140 | 11.6 | 171 | 2.1 | 43 / 36 | `0xb0095c3e`, `0xfae9e5a5` |
| `0xb0095c3e6ac1a4f936df3914e8cc46783b9b5287` | – | sniper | high_churn, diversified | mixed | 140 | 11.9 | 172 | 3.1 | 43 / 36 | `0x63e30ef2`, `0xfae9e5a5` |
| `0xfae9e5a545bea1f90e715165b002150d04a28c41` | – | sniper | diversified | mixed | 392 | -16.9 | 303 | 6.3 | 37 / 30 | `0x63e30ef2`, `0xb0095c3e` |

### Group 2 (3 wallets)
Three snipers, 41-50 same-block buys on 32-41 tokens, all `high_churn` with 1.5-1.7 minute median holds. All three are measured `consistent_winner`.

| Wallet | Rank | Style | Traits | Tier | Agora PnL | Measured PnL 30d | Trade legs | Median hold (min) | Same-block buys / tokens | Partners |
|---|---|---|---|---|---|---|---|---|---|---|
| `0x78685a43d714b9476af20fae683b219e998f9a27` | – | sniper | high_churn, diversified | consistent winner | 131 | 12.3 | 182 | 1.5 | 50 / 41 | `0xaa78d80e`, `0xbee5bacc` |
| `0xaa78d80e33aee56b8c012eac3bb3f6e8acf1028d` | – | sniper | high_frequency, high_churn, diversified | consistent winner | 463 | 10.9 | 186 | 1.7 | 41 / 32 | `0x78685a43`, `0xbee5bacc` |
| `0xbee5baccc945678e67949e0e45536b490febd63b` | – | sniper | high_churn, diversified | consistent winner | 427 | 5.1 | 177 | 1.7 | 50 / 41 | `0x78685a43`, `0xaa78d80e` |

### Group 3 (4 wallets)
Four snipers, 7-13 same-block buys on 6-12 tokens. Linked as pairs, not a full clique (see partners).

| Wallet | Rank | Style | Traits | Tier | Agora PnL | Measured PnL 30d | Trade legs | Median hold (min) | Same-block buys / tokens | Partners |
|---|---|---|---|---|---|---|---|---|---|---|
| `0x4cda040ebffac403946bb05414ceb60381aff82e` | – | sniper | – | mixed | 322 | -0.1 | 98 | 12.9 | 13 / 12 | `0xacdf687a`, `0xbb30f82e`, `0xe11c32ed` |
| `0xacdf687aa7c8e52f2cbecbc95b7e256829265b04` | 91 | sniper | concentrated, linked | consistent winner | 347 | 21.4 | 82 | 7.2 | 13 / 12 | `0x4cda040e`, `0xbb30f82e`, `0xe11c32ed` |
| `0xbb30f82ed01c2461cde308379ef78db0a4e4a543` | – | sniper | – | consistent winner | 278 | 20.2 | 74 | 8.2 | 13 / 11 | `0x4cda040e`, `0xacdf687a` |
| `0xe11c32eda8fa9be59f3ed28088e06058a75b3583` | – | sniper | concentrated | mixed | 144 | 7.6 | 39 | 4.7 | 7 / 6 | `0x4cda040e`, `0xacdf687a` |

### Group 4 (2 wallets)
Two swing wallets with the strongest pair signal: 87 same-block buys on 52 tokens, median holds of 18-19 hours, both measured positive (+18 to +20 HYPE). Addresses share the `0xfd` vanity prefix.

| Wallet | Rank | Style | Traits | Tier | Agora PnL | Measured PnL 30d | Trade legs | Median hold (min) | Same-block buys / tokens | Partners |
|---|---|---|---|---|---|---|---|---|---|---|
| `0xfd7e55a555555c2f25053a38ec744de1afea4fa4` | – | swing | high_frequency, early_buyer, diversified | mixed | 332 | 19.8 | 467 | 1,156.3 | 87 / 52 | `0xfdaf199a` |
| `0xfdaf199ae935753c70824f6c73d4877ddceea57b` | – | swing | high_frequency, early_buyer, diversified | mixed | 171 | 17.6 | 321 | 1,067.0 | 87 / 52 | `0xfd7e55a5` |

### Group 5 (2 wallets)
Two swing wallets, 48 same-block buys on 25 tokens; both measured negative.

| Wallet | Rank | Style | Traits | Tier | Agora PnL | Measured PnL 30d | Trade legs | Median hold (min) | Same-block buys / tokens | Partners |
|---|---|---|---|---|---|---|---|---|---|---|
| `0x7b3d8e939ee08b52d06ab5e6f85791a6007e8d61` | – | swing | high_frequency, accumulator, diversified, linked | mixed | 653 | -14.0 | 346 | 183.1 | 48 / 25 | `0xe3c2c4a2` |
| `0xe3c2c4a2f0883d16df3029b0c090c47652468a58` | – | swing | early_buyer, diversified, linked | mixed | 318 | -6.0 | 134 | 34.5 | 48 / 25 | `0x7b3d8e93` |

### Group 6 (2 wallets)
Two swing wallets, 11 same-block buys on 10 tokens, identical 190-minute median hold.

| Wallet | Rank | Style | Traits | Tier | Agora PnL | Measured PnL 30d | Trade legs | Median hold (min) | Same-block buys / tokens | Partners |
|---|---|---|---|---|---|---|---|---|---|---|
| `0x47ab69ea0e8a90a5c2973b152f3bacf787cbc1f6` | – | swing | early_buyer, linked | mixed | 110 | -2.2 | 39 | 190.4 | 11 / 10 | `0xa2a85ea4` |
| `0xa2a85ea4db18e55129eb0079ed260828137eb98b` | – | swing | early_buyer, diversified, linked | persistent loser | 232 | -0.4 | 104 | 190.4 | 11 / 10 | `0x47ab69ea` |

### Group 7 (2 wallets)
Two snipers, 6 same-block buys on 4 tokens: the weakest pair, just over the threshold.

| Wallet | Rank | Style | Traits | Tier | Agora PnL | Measured PnL 30d | Trade legs | Median hold (min) | Same-block buys / tokens | Partners |
|---|---|---|---|---|---|---|---|---|---|---|
| `0x6991fb794b3655b7c0a3bb86d60ea58c4ae8a1f6` | 50 | sniper | concentrated | mixed | 466 | -3.6 | 36 | 3.1 | 6 / 4 | `0x840c17ae` |
| `0x840c17ae8a1bf0f80cb4ce5650e952362fe7c418` | – | sniper | – | mixed | 382 | -0.2 | 44 | 12.7 | 6 / 4 | `0x6991fb79` |

## Open questions
- Groups 1 and 2 look like one operator or one bot each (near-identical holds, sizes and trade counts). Worth deciding whether pair groups should count as one operator in cluster/consensus scoring, as cohorts do.
- Not shown above: four unflagged wallets with an identical profile (16 measured closes, `concentrated`, +7 to +10 HYPE): `0x08d92075…`, `0x2edb11b2…`, `0x17f76228…`, `0x6cc23368…`. No rule links them yet; noted for review.
- Funding sources were not traced for any group.

---
_Labels are rule-based inferences from recorded on-chain activity (labels-v1, 30-day lookback), not proof of common control or misconduct. Snapshot 2026-11-12, chain 99901. Source of this page: `docs/wiki/` in the repository; edit it there._
