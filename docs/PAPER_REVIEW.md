# PAPER author review and hardening — parent #82

Scope: complete PR #84 diff at fa882f2, including alert rendering/config/UI integration,
read transports, canonical ABI/runtime manifest, quote validation, callbacks, persistence,
benchmarks, dependencies and security/release documentation. GitHub reports #84 already
merged externally with no submitted reviews. This report is **author review preparation,
not independent security approval**. The follow-up PR stays unmerged pending approval.

## Findings and remediation

Nine targeted adversarial tests initially failed against the merged behavior. Those
failures were grouped into the first six findings below; regression tests now pass.
No finding enabled a signer, token approval or broadcast path: none exists in PAPER.

| ID | PAPER impact | Finding | Remediation / evidence |
| --- | --- | --- | --- |
| P1 | Moderate interaction integrity | Invalid amount/wallet change attempts could leave the old review confirmable | Revoke review/token/digest before validating or persisting any attempted amount/wallet selection; test old confirmation after refusal |
| P2 | Low integrity/availability | Frozen Quote returned a shared mutable transaction dict | Deep-copy provider/review/panel boundaries; retain stored digest and exact-plan check against internal mutation |
| P3 | Low defensive auth | Boolean identity compared equal to allowlisted integer at outer UI, allowing malformed pause input | Require positive integer Telegram user ID and integer chat ID before dispatch; core binding remains enforced |
| P4 | Low context integrity | Initial callback accepted channel/unknown chat types and impossible positive group IDs | Accept only private or negative group/supergroup context; private user/chat must match |
| P5 | Moderate privacy/config drift | Journal permissions and direct symlinks were checked only at startup | Recheck owned private directory, regular 0600 single-link file and device/inode at every connection, use mode=rw to avoid accidental recreation, disable trusted_schema |
| P6 | Low availability/data integrity | Malformed persistent JSON raised unhandled errors | Redacted fail-closed errors and SQL-user/intent/quote binding validation before showing history |
| P7 | Moderate quote integrity | Number-pinned reads lacked a final canonical block-hash comparison | Validate full hash syntax and re-read the same height/hash/timestamp after quote/simulation reads; refuse a reorg even with a warm cache |
| P8 | Moderate upstream availability | Public JSON responses were not size-bounded | Stream at most 1 MiB, check download budget at chunk boundaries, close responses and redact malformed/oversize errors; no retries/redirects |
| P9 | Label accuracy | Missing allowance plus a revert did not prove allowance was the only cause | Explicitly state that approval may not resolve a revert; conditional record remains unsimulated, without gas or any approval sent |

Additional adversarial coverage exercises wrong-chain/stale RPCs, changed code and
unsupported pools, mutable decimals/balances/allowances across warm cache hits, replay,
Unicode confirmation input, malformed integer/ABI/hash/timestamps, payload mutation,
private user separation, quote expiry, pause/cancel races, persistence errors and malicious
sell reverts despite sufficient allowance. Existing default-OFF/no-broadcast tests remain.
The full suite covers normal scanner, notification, PostgreSQL, backup/restore and browser
behavior; no production process is involved in those checks.

## Performance changes and safety argument

The old ~215 ms direct quote was one RPC, whereas Review performed 18–19 serial reads
plus an Agora comparison, validation and rendering. Instrumentation now records static
operation names, method, duration and success/refusal, local quote calculation and panel
rendering. It records no request payloads, URLs/credentials, wallet addresses or balances.

A process-local LRU cache holds at most 64 token/pair relationships and the router's
verified factory/wrapped-HYPE immutables. The cache is justified only by the exact pinned
non-proxy canonical router/factory/pair code: those interfaces cannot change these values.
Chain and all three code hashes are rechecked at the current block on **every quote**.
Wrong/changed code refuses and invalidates relevant cached metadata. Pair creation/reorg
cannot bypass the live code and final block-hash checks. Restart starts a cold cache.

Decimals, balances, allowances, reserves, heads, simulations, gas and quotes are never
cached. Their reads remain at the same pinned block, with the final hash recheck. The
redundant getAmountsOut call is replaced by the exact canonical 997/1000 integer formula
on freshly read reserves; the unoptimized benchmark retains that RPC cross-check.
Cold verification remains available and fully performed on the first pair review.
See `docs/benchmarks/paper-profile-2026-11-07.json` and `RPC_BENCHMARK.md` for complete
before/after data, cold starts, one stale-head refusal and sampling limitations.

## Unresolved risks / mandatory independent disposition

- Public RPCs may lie coherently about code/state/hash; one provider's repeated hash is
  not a consensus or finality proof. Freshness/refusal and runtime pins reduce mistakes,
  not that trust assumption. Current Elysium submission/finality semantics remain a Stage C gate.
- Malicious token balance reporting, taxes, changing transfer restrictions and future
  sellability cannot be established by a void-return simulation. Conditional SELLs are
  hypothetical only. No unknown token or V2 absence can fall back to an unverified route.
- Agora outer-router implementation/ABI/build identity remains unresolved. API-reported
  prices/fees are comparisons only; opaque builder calldata stays disabled.
- Same-UID/host compromise can tamper with a local private journal and application code.
  Path/permission/inode checks are defense in depth, not a secure enclave or complete
  filesystem race proof. Protect ancestor directories; replacement/restore needs a new
  journal instance. Stage C requires independently designed private audit/order authority.
- HTTP memory is bounded and time budgets checked per chunk; requests' connect/read
  timeouts are not a hard process-wide wall-clock interrupt against every slow upstream.
  One busy provider refuses concurrent work; pilot resource/RPC budgets still need observation.
- PAPER associations prove no wallet ownership. App restart invalidates callbacks and
  keeps private records; it does not supply live nonce, transaction or receipt recovery.
  Pause is a PAPER control, not yet the durable Stage C emergency submission gate.
- A UI-only pilot cannot add buttons to tracker-rendered alerts without a separately
  authorized tracker config/restart. No new getUpdates consumer may be introduced.

Independent reviewer must review the **exact follow-up head**, reproduce adversarial
checks and cold/warm cache behavior, assess these residual risks and approve the bounded
PAPER pilot separately. No independent approval is present at author handoff. Do not
self-approve/merge or deploy based only on CI. Stage C design is separate issue #85 and
`STAGE_C_PLAN.md`; no real-money implementation is included here.
