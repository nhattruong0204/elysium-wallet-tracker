# C1 authentication — issue #89

This layer grants **private PAPER preview access only**. Ownership proof is never
transaction authorization. No transaction signer, approval request, signed-transaction
input or broadcaster exists. DenyAuth remains the default. No production startup,
deployment or restart is added. Independent review has **not** been completed.

This branch depends on PR #100 at 8a46e53; review against that branch and retarget
after #100 merges. #90 and all execution work remain out of scope.

## Integration and protocol

Use the existing create_app(flow, auth) factory. Construct AuthService with the same
IntelBot PaperFlow, a dedicated private AuthStore file, the bot token and matching
numeric bot ID, a fixed HTTPS origin, and an existing ReadRpc. A different flow is
refused. No new getUpdates consumer exists. Never mount authentication storage on
the public research dashboard.

| POST endpoint | Required proof | Result |
| --- | --- | --- |
| /api/auth/bootstrap | Fresh initData, owned opaque intent, configured HTTPS Origin/Host | One Telegram-only session and CSRF secret |
| /api/auth/challenge | Secure cookie, CSRF, owned intent, matching watch wallet, chain 99901, empty account code | Exact SIWE message and random nonce |
| /api/auth/verify | Same session, CSRF, nonce, canonical signature over that exact message | Consume nonce, recheck EOA, rotate cookie/CSRF, grant preview scope |
| /api/auth/revoke | Same cookie, CSRF, bound intent | Revoke ownership and challenges |
| /api/auth/logout | Same cookie, CSRF, bound intent | Delete session/challenges, expire cookie |

No endpoint accepts an arbitrary message/domain/URI or issues a signature. Extra
fields and duplicate JSON keys refuse. Existing session/quote/cancel interfaces
remain, with intent-bound principals and post-I/O auth rechecking for private quotes.
Changed or malformed claimed accounts revoke proof. No PAPER order is placed.

The browser exchanges initData once and keeps CSRF only in module memory. It shows
the complete SIWE text and requires an explicit ownership-sign click. The only new
wallet method is personal_sign for locally checked, server-issued SIWE bytes.
Transaction signing stays disabled. Auth HTTP calls serialize cookie/CSRF rotation
before queued account/network revocation. Late proof responses are revoked.

Account/chain changes invalidate locally and notify the server. Background activity
also invalidates proof/review, except while an explicit ownership prompt is pending:
that prompt rechecks account/chain/expiry on return. No spending is authorized.
Network failure cannot guarantee immediate remote revocation; the client refuses
further use and server expiry remains the bound.

## Cryptography and state policy

Telegram verification follows the [official bot-secret algorithm](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
HMAC-SHA256 with WebAppData as the key derives the bot key; the sorted decoded
field/value string excludes hash only. The optional Telegram signature field stays
in this HMAC string. Third-party Ed25519 verification is a different protocol and
is not implemented. Hash comparisons are constant-time after shape validation.

Strict percent/UTF-8 decoding rejects duplicate keys, encoded aliases, malformed
encodings and delimiter injection. Duplicate JSON user keys, noninteger/bot user
IDs, missing query IDs, wrong bot secrets and stale/future auth_date refuse. Only
private inline-button launches are supported; group/attachment/join contexts,
receiver/chat instances, mismatched start parameters and non-sender chat types
refuse. initDataUnsafe/client identities are never trusted. Bot identity is bound
to the configured token; source alert/token/chain come from the owned PAPER intent.

auth_date must be at most 119 seconds old and never future-dated. A bot/hash launch
fingerprint is single-use across workers/restarts until the launch expires.
Sessions are also bound to a bot-secret-derived realm; rotating the bot secret
invalidates old cookies. Sessions last at most 120 seconds, further bounded by launch and intent expiry.
No refresh token exists. Reload/lost CSRF, logout or exchange-response loss requires
a **fresh Telegram launch**; old initData cannot recover a session.

[ERC-4361](https://eips.ethereum.org/EIPS/eip-4361) messages bind a checksummed EOA,
fixed domain/URI, chain 99901, cryptographic 256-bit nonce, issued/not-before/expiry,
and opaque request ID binding session, Telegram user and intent. Nonces last at most
60 seconds and cannot outlive sessions. Verification hashes exact server-issued
bytes using [ERC-191](https://eips.ethereum.org/EIPS/eip-191), then recovers with
eth-keys. Only 65-byte low-s signatures with v=27/28 are supported.

ReadRpc verifies chain 99901, fresh head and pinned account code before challenge
and proof acceptance. Any code, including EIP-7702 delegation, refuses.
[ERC-1271](https://eips.ethereum.org/EIPS/eip-1271), counterfactual wallets and compact
signatures remain unsupported. EOA classification is a snapshot, not a future
spending check. RPC providers see queried public addresses; Telegram identities,
cookies, CSRF and signatures are never sent to RPC.

AuthStore reuses the existing PAPER file guard: owner-only directory 0700, regular
file 0600, inode/ownership/symlink/hardlink/permission checks. PAPER keeps a separate
journal and its existing behavior. No PostgreSQL/public_api/dashboard_ro tables or
grants are added. Cookie and CSRF secrets have 256 bits of randomness; only SHA-256
digests persist. Raw initData, names, bot tokens, signature bytes and wallet private
keys do not persist. Wallet/binding metadata stays private; no group message is sent.

BEGIN IMMEDIATE serializes launches and nonce consumption. Verification consumes
the nonce **before** recovery/RPC, and failures never restore it. Completion checks
consumption, generation and expiry after RPC. Logout, superseding challenges and
account revocation defeat in-flight elevation. Success rotates both cookie/CSRF;
old credentials cease to authenticate.

Protected requests require exact HTTPS Origin/Host, same-origin fetch metadata
when present, a unique __Host-elysium_auth HttpOnly/Secure/SameSite=Strict/Path=/
cookie, and matching unique CSRF header. The adapter does not interpret forwarded
headers or grant CORS credentials. A reviewed listener/proxy must supply accurate
ASGI scheme/Host and restrict trusted proxies.

Persistent fixed one-minute rate buckets: bootstrap 10/IP and 6/user; challenge
6/user+intent; verify 10/user+intent; preview checks 60/user+intent. Quotes do two
checks. IP keys are bot-keyed digests. Sessions cap at 256 and rate keys at 2048;
storage/capacity errors refuse. This is not distributed abuse protection.

## Evidence and independent review gates

Tests cover official eth-account ERC-191 vectors, independent HMAC fixtures, byte
mutation properties, malformed/duplicate fields, cross-bot/user/session/origin/chain,
time bounds, high-s/wrong signatures, contract/delegated wallets, rotation, storage
failures, launch/nonce races and concurrent full verification. HTTPS tests use real
crypto and a self-signed fixture TLS certificate. Chromium desktop/mobile and WebKit
iPhone emulation use mocked Telegram/wallet/RPC and public unfunded test identities.
This is not physical iPhone or real-wallet compatibility evidence.

An independent reviewer must assess the **exact final commit** and:
- Telegram HMAC canonicalization, launch-mode restrictions and replay policy.
- SIWE grammar/domain/URI/time/binding, EOA checks and signature recovery.
- Atomic consumption/elevation, rotation, revocation races and persistent limits.
- Cookie/CSRF/origin/proxy assumptions and private/public storage isolation.
- Separation of ownership proof from every future spending decision.

Unresolved assumptions and release gates:
- Telegram/bot-token compromise can impersonate Telegram identity; EOA proof is
  separately required. Stolen unused initData is a short-lived bearer credential;
  replay prevention cannot stop an attacker winning its first exchange.
- Phishing, a compromised origin/official SDK/server, malicious wallet providers
  and device compromise remain threats. CSP does not make these parties trustworthy.
- RPC, host OS, same-UID processes, local SQLite filesystem and wall clock are
  trusted. Backups must stay private. After restoring old replay state, wait out the
  full auth lifetime before accepting logins. Workers must share one local store;
  independent DB copies or NFS are unsupported.
- SameSite=Strict fails closed if Telegram Web/third-party-cookie policy blocks it.
  No cookie-policy downgrade or external-browser auth transfer is implemented.
  Physical Telegram/native-wallet/device testing remains open.
- Internet exposure needs reviewed upstream network abuse controls.
- **No independent approval exists.** Do not use this proof as a spending prerequisite,
  deploy or enable trading before independent review and separate owner approval.
  Keep #89 open until its independent-review acceptance is met.
