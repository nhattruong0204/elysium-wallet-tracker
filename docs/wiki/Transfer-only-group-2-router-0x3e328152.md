# Transfer-only group 2 on router 0x3e328152

Found in the weekly review of 2026-11-26 while checking inactive top-100 wallets. Blocked on 2026-11-27, under the policy that a group with no decoded buy or sell is blocked.

## Summary
- **Members:** 13 wallets on Agora's top 100 (ranks 60-100, best ranks 21-49). Agora credits each with about $865-1,420 of PnL; we have no measured PnL for them.
- **Behaviour:**
  - Every transaction they sent went to router `0x3e328152d399a500a7423a7373ef767a906628d4`, the same router as [[Launch-cohort-1-router-0x3e328152]].
  - We recorded 0 decoded buys or sells, only token transfers in and out, across 24-119 tokens per wallet.
- **Timing:** 10 of them went silent at the same minute, 2026-11-20 07:41 UTC; the other three at 2026-11-18 11:13.

## Wallets
| Wallet | Transfers in / out | Tokens | Last activity (UTC) |
|---|---|---|---|
| `0xad89a1599659fbb0d426c2313b76aa8b6290c98f` | 170 / 342 | 119 | 11-20 07:41 |
| `0xae88f8821da1e320f80f393b1a84e644e32e9d03` | 165 / 293 | 105 | 11-20 07:41 |
| `0x7f5e1246c48d84c5e10330e4b82011a9b15b0e12` | 153 / 226 | 96 | 11-20 07:41 |
| `0x276da0b40416cd299fbc5aa94354aac23dbe17b7` | 148 / 162 | 99 | 11-20 07:41 |
| `0xe0925725a3eaf65bd744eb970415e7f114ddfabe` | 115 / 106 | 79 | 11-20 07:41 |
| `0x3a014bfd532c6b430970d010ba91f6df16f46a66` | 83 / 88 | 53 | 11-20 07:41 |
| `0x28a6bbc8e030974fa79bbf2518c2b82f8bbb50cb` | 71 / 75 | 41 | 11-20 07:41 |
| `0x4ebfc752c2536105cc5359d76ae6ef976839ebfd` | 102 / 38 | 62 | 11-20 07:41 |
| `0x17ef80f877e04746089faf7dd4ee0c404250ad23` | 48 / 42 | 32 | 11-20 07:41 |
| `0xbde6fdcbc0e882388727583327497e0c4a4573b0` | 1 / 1 | 1 | 11-20 07:41 |
| `0xfe167490561449a8138ed5e7b062a9583ed14a43` | 67 / 19 | 47 | 11-18 11:13 |
| `0xb879684baa65c02a7fa1a39ad068c287c8044d93` | 33 / 14 | 23 | 11-18 11:13 |
| `0x71da0947605ff234083465184ce692ae4cd3278e` | 9 / 5 | 5 | 11-18 11:13 |

## Limits
- The router is not decoded, so sizes, entries and exits are unknown.
- Funding sources were not traced.

---
_Labels are rule-based inferences from recorded on-chain activity, not proof of common control or misconduct. Source of this page: `docs/wiki/` in the repository; edit it there._
