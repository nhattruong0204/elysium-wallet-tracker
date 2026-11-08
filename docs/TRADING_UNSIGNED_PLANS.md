# Exact unsigned V2 plans — issue #90

This is a read-only review milestone, not transaction execution. It reuses the
merged DirectV2Provider, PAPER amount validation, ABI encoding, quote calculations
and minimum-enforcing simulation. No transaction signature, approval payload,
broadcast, live-enable switch, production startup or restart was added.

The branch depends on PR #101 at 3d15431, which depends on PR #100 at 8a46e53.
Review only this branch's diff against #101; merge dependencies in order before
retargeting to main. Independent authentication/security approval is still pending.

## API and ownership boundary

The existing Mini App factory enables exact-plan mode only with the #101 secure
auth adapter and an existing DirectV2Provider. Default authentication still denies.
Legacy PAPER/Telegram controls and quote APIs retain their behavior; none is a
transaction authorization. There is no new getUpdates consumer or public DB view.

All requests require the #101 private cookie/CSRF/origin policy, matching intent
owner and ownership-proven watch wallet. Principal gains an opaque, server-derived
session-generation binding and auth expiry; these reveal no cookie/CSRF secret.

| POST endpoint | Accepted extra fields | Result |
| --- | --- | --- |
| /api/plan | amount string, slippage_bps integer | New immutable plan and compatible quote DTO |
| /api/revalidate | plan_id only | Same exact plan plus fresh validation evidence |
| /api/revoke-plan | plan_id only | Revoke that review; cannot revoke a newer ID accidentally |

Common fields are intent, init_data (empty after bootstrap), wallet. Duplicate JSON
fields and extra plan fields refuse. In particular, the API accepts no calldata,
destination, recipient, route, signature or approval transaction from callers.

Plans bind user, intent, exact auth session/generation, wallet, token, chain 99901,
side, path, exact integer input, decimals, pool output, minOut, slippage, impact cap,
verified route hashes/addresses, pinned block/hash/time, observed balances/allowance,
finite required allowance, deadline and gas caps. All nested records are frozen
dataclasses/tuples/strings; returned JSON is a detached copy. Stored digest checking
also detects deliberate mutation bypassing Python's frozen protection. Financial
integers cross JSON as decimal strings; RPC quantities use canonical hex.

Plans live only in bounded private process memory (maximum 256 intent slots) and
are revoked on restart. No order, receipt, hypothetical fill or journal entry is
created. A plan ID is a reference requiring session authentication, not a bearer
authorization or signing permission.

## Verified routes and exact payload

Only the existing canonical HYPE↔ERC-20 single-hop V2 route is supported. The
router/factory/pair runtime hashes and published ABI come from the existing
contracts/v2.json artifact, frozen by SHA-256
b8ed8dcd6f45b05be111bffb190168ebb8501a6c052a6e737c68f853a51e9599.
Changing it requires review. The new layer does not invent or discover an ABI.

Every validation checks exact router/factory/pair runtime and metadata, WETH/HYPE
address, factory pair lookup, ordered token pair, decimals and EOA account code.
Token/wrapped runtime hashes are **observed change detectors**, not token safety
approvals; proxy storage/implementation behavior is not certified by a code hash.
Tokens in existing alerts are not necessarily supported. Unsupported routes refuse
and the private alert context still provides the verified Agora token-page link.
Opaque Agora outer-router calldata remains comparison-only; V3/V4/curves/hooks and
generic targets remain unsupported.

The sender and recipient are the proven wallet. BUY value is exact HYPE input;
SELL value is zero and spender is the frozen V2 router. The only methods are the
existing minimum-enforcing supporting-fee-on-transfer BUY and SELL methods. Exact
bytes are independently regenerated from the frozen ABI and compared to the quote.

Successful simulation plans bind an explicit gas limit (existing 20% unit buffer),
legacy gas-price cap and maximum gas fee. The full reviewed RPC payload, including
those gas fields, is simulated again with pinned eth_call and eth_estimateGas.
It is not sufficient that the preliminary PAPER payload without gas fields worked.
Gas estimates/prices must stay within the frozen caps and native balance must cover
value plus maximum gas reserve. The existing 1 HYPE BUY, 1% impact and 10^16 wei gas
reserve caps remain. Pool fee is 30 bps and included in expected output; token taxes
and exact delivered output are unknown.

Chain ID is bound in the typed plan and checked against RPC; the displayed RPC
transaction object contains from/to/value/data and gas/gasPrice when available.
It contains no nonce, signed bytes or approval. Future execution work must not add
or alter transaction fields under old evidence: it must revalidate the exact
eventual wallet request and retain all chain/intent/session/gas checks.

SELL with insufficient allowance is **conditional only**: the actual swap call
reverted, the cause may include restrictions besides allowance, and no gas estimate
or successful SELL is claimed. Required future allowance is the finite exact input,
not an unlimited approval or an approval transaction. Already-approved SELL and BUY
must pass actual exact-payload simulation. Supporting taxed transfers enforces the
minimum at the snapshot; it does not reveal the actual delivered amount or promise
that future transfers will behave identically.

## Fresh state and revocation

Plan construction reuses PAPER selection/review, then binds and independently
checks the same pinned snapshot. Review calls revalidation on the server, not just
client-side expiry checking. It verifies the original block was not reorganized,
reads a fresh head on chain 99901, rechecks contracts/EOA and mutable state, and
simulates the **unchanged** payload at the new pinned block.

Any balance, allowance, decimals, reserves or observed code change requires a new
plan, even if the rounded output or selected percentage would otherwise be unchanged.
Increased required gas or price beyond the original caps refuses. Old parameters,
wallet/session, PAPER quote digest/review token, changed provider, expired quote/auth
or deadline, stale/future head, original-block reorg and regressed head also refuse.
A failed validation revokes that matching plan; returning state to old values does
not revive it. Revalidation never extends plan expiry or changes minOut/deadline.

Both original/current block identity are checked around the reads and simulation.
The conservative policy refuses if the head advances during validation; retry a
fresh review/quote. Slow RPCs or fast blocks can therefore cause refusals. This has
fixture evidence, not live latency/availability evidence. A future execution RPC
pool and real-chain timing validation remain separate requirements.

Per-intent generations and PAPER review tokens prevent late build/revalidation
responses publishing a superseded plan. Re-quote attempts revoke old plans.
Cancellation, account/network changes, disconnection and activity changes discard
client review; auth revocation/session-generation checks prevent old plans being
reused. Returned browser DTOs are deep-frozen and compared exactly with the fresh
server plan. The displayed quote expiry is capped by auth/plan expiry too.

Fresh evidence is a point-in-time observation, **never** a durable execution permit.
Before every future spending wallet request, call revalidation again and check
current wallet/chain/session and unchanged payload. Blockchain state can change
immediately afterward; independent security review and later execution controls
remain mandatory. There is no spending request in this milestone.

## Verification and remaining gates

Tests cover exact ABI/payload diffs including gas fields, immutable nested data,
wrong ABI despite mutable provider metadata, wrong chain/router/recipient/wallet,
malformed/nonstandard token values, taxed/reverting transfers, session separation,
balance/allowance/code changes, gas caps, expiry, stale head/reorg, re-quote and
in-flight change/revocation races. HTTPS tests use actual HMAC/SIWE and existing V2
logic with explicit fixtures. Browser E2E covers BUY, approved SELL, conditional
SELL and altered responses on Chromium desktop/mobile and WebKit iPhone emulation.
There is no physical iPhone, real Telegram/wallet or live-chain execution evidence.

Remaining gates: dependency reviews/merges, independent auth and plan/security
review, device/wallet compatibility, trusted RPC/clock/filesystem/proxy assumptions
and real-chain timing checks. No self-review is independent approval. Do not deploy,
restart production, enable live trading or implement dependent execution issues as
part of this change. Keep #90 open pending review/acceptance.
