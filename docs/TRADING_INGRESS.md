# Private Mini App / wallet ingress (#95 F-16)

Status: **OFF by default. Not deployed. Wallet execution stays OFF.** Nothing in this change signs,
broadcasts, spends, approves or calls Telegram.

Before this change no process mounted `miniapp.create_app(...)`, so the authenticated Mini App and
the wallet routes (`/wallet/api/*`) could not be reached at all. This adds the one entrypoint and
only the boundary around it; every security decision that already existed (Telegram identity,
wallet ownership, CSRF, Origin, expiry, per-user checks) is still made by the existing
`AuthService` / `miniapp.create_app` / `wallet_routes` and is **reused, not re-implemented**.

## 1. Architecture

```
 Telegram WebView ──HTTPS──▶ reverse proxy (operator-owned: TLS, DNS, certificate)
                                │  injects  Host, X-Forwarded-Proto: https,
                                │           X-Forwarded-For, X-Elysium-Ingress: <secret>
                                ▼  loopback only (127.0.0.1 / ::1)
        ┌────────────────────── the `--ui` process (unchanged owner of getUpdates) ──────────────┐
        │  ingress thread:  uvicorn ▶ IngressGuard ▶ miniapp.create_app(flow, AuthService, wallet?)│
        │                                              │                │            │             │
        │  bot.run(stop)  ── the ONE getUpdates ── bot.paper_trades ◀──┘   AuthStore  OrderLedger   │
        │  WorkerSupervisor (F-01) ─ ExecutionRpc (shared, read-only) ──────────────────────────────│
        └──────────────────────────────────────────────────────────────────────────────────────────┘
        public read-only dashboard (`--web`, separate service, separate role, separate vhost) — untouched
```

**Why in the `--ui` process and not a second service.** PAPER intents live in `bot.paper_trades`
(memory), the plan book is memory, and the Mini App session must match the intent that the *same*
process created from the Telegram button. A second process cannot see any of that; sharing it would
need a new store and a second source of truth. One process also keeps one `getUpdates` consumer:
the ingress thread never imports or calls the Telegram API (asserted by a test).

**Why not the public dashboard.** `--web` serves unauthenticated, read-only public data under a
least-privilege database role. The Mini App is authenticated, private and writes to a ledger. They
stay separate services, ports, roles and (operator) vhosts; nothing here touches `agora_tracker/web`.

## 2. Boundaries and who enforces them

| Boundary | Enforced by | Test |
|---|---|---|
| Listener exists at all | `trading.ingress_enabled` (default false) → `build_ingress` returns `None`, nothing bound | `test_disabled_by_default_builds_nothing`, `test_run_ui_starts_no_listener_by_default` |
| Not publicly reachable | config rejects any non-loopback `ingress_bind_host`; socket bound in the main thread before the thread starts | `test_default_config_has_no_listener_and_validation_is_strict` |
| Only via the configured proxy | `IngressGuard`: exactly one `X-Elysium-Ingress` equal to the secret (constant-time), exactly one `Host` equal to the canonical origin host, scheme `https` (from the proxy's `X-Forwarded-Proto`, trusted from loopback only). Anything else: empty 404 | `test_anything_not_forwarded_by_the_configured_proxy_…`, duplicate-header tests |
| HTTPS origin canonical | `ingress_origin` must be a lower-case `https` origin without path; `miniapp_url` (the Telegram launcher) must be its root, so launcher and listener cannot drift | `test_the_telegram_launcher_must_point_at_the_listener_origin` |
| Telegram identity | `AuthService` + `TelegramVerifier` (HMAC `WebAppData`, bot-id bound, replay-protected) — unchanged | `test_mock_telegram_session_…`, `test_unauthenticated_and_forged_launch_data_are_refused` |
| Wallet ownership | `AuthService` SIWE (EOA only, fresh head, chain 99901, nonce burn) — unchanged | `…wallet_ownership_and_unsigned_review`, `test_chain_dependency_failure_…` |
| CSRF / Origin / cookie | `AuthService` (`__Host-` cookie, `X-CSRF-Token`, exact `Origin`) — unchanged | `test_csrf_origin_cookie_and_the_proxy_secret_are_each_required` |
| Session expiry | `AuthService` (≤ 120 s, ≤ intent expiry) on the stored clock — unchanged | `test_session_expiry_is_enforced_by_the_stored_clock` |
| Per-user authorization | `AuthService` + `PaperFlow.get(uid, …)` + `wallet_routes` owner-bound ledger reads — unchanged | `test_per_user_authorization_…`, `test_wallet_routes_with_the_ledger_still_demand_a_proven_owner` |
| Wallet routes | mounted **only** if `wallet_execution_enabled` is also true (needs ledger + execution RPC); still behind all of the above and the durable owner-only execution flag in the ledger | `test_wallet_surface_is_absent_…`, `test_wallet_routes_are_mounted_only_with_the_second_opt_in_…` |
| No signing / broadcasting | no such route exists; execution RPC is the read-only allowlist; responses carry `signing_enabled:false` / `server_signs:false` | e2e tests assert both |

## 3. Configuration (all under `trading:`)

| Key | Default | Meaning |
|---|---|---|
| `ingress_enabled` | `false` | Master switch. Needs `paper_enabled`, `paper_order_path`, `ingress_origin`, `ingress_auth_path`, `miniapp_url` |
| `ingress_origin` | – | Canonical public origin, e.g. `https://app.example.com` |
| `ingress_bind_host` / `ingress_port` | `127.0.0.1` / `8765` | Loopback only (validated) |
| `ingress_auth_path` | – | Private SQLite for sessions (dir `0700`, file `0600`, not a symlink — `PrivateSqlite`) |
| `ingress_secret_env` | `TRADING_INGRESS_SECRET` | Env var holding the shared proxy secret (≥ 32 printable chars, no whitespace). Never in config or logs |
| `ingress_max_concurrency` | 32 | Open connections (idle keep-alive included); above it uvicorn answers 503 |
| `ingress_threads` | 8 | Cap on blocking work (RPC, sqlite, ledger) in the listener's own pool |
| `ingress_max_body_bytes` | 16384 | `Content-Length` above it: 413 before the app runs |
| `ingress_request_timeout_s` | 30 | Whole-request budget: 504 |
| `ingress_keepalive_s` | 5 | Idle keep-alive |

`bot_id` is derived from the token prefix and checked by `TelegramVerifier`; there is no separate
setting to get wrong. Generate the secret with `openssl rand -hex 32`.

## 4. What the reverse proxy MUST do (operator-owned, not in this repository)

* Terminate TLS for **exactly** the host in `ingress_origin`; redirect/deny every other host.
* Forward to `http://127.0.0.1:<ingress_port>` and set **overwriting** headers:
  `Host` (must equal the origin's host[:port]; nginx `$host` drops the port, use `$http_host` for a non-443 origin),
  `X-Forwarded-Proto: https`, `X-Forwarded-For: $remote_addr`, `X-Elysium-Ingress: <secret>`.
  The first `AuthService` rate limit is per client IP taken from that header.
* Keep the secret in a root-owned include file (mode 0640), not in the repository or the vhost in git.
* Cap `client_max_body_size` (16k), `proxy_read_timeout` ≥ `ingress_request_timeout_s` + 5 s, and add
  its own `limit_req`. Do not proxy any other path prefix to this port and do not serve the public
  dashboard from this vhost.

```nginx
server {
  listen 443 ssl http2;
  server_name app.example.com;                 # == ingress_origin host
  # ssl_certificate / ssl_certificate_key ...
  client_max_body_size 16k;
  location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
    include /etc/elysium/ingress-secret.conf;  # proxy_set_header X-Elysium-Ingress "…";
    proxy_read_timeout 35s;
  }
}
```

### 4a. Template, rehearsal and verification (F-17)

* `deploy/nginx/elysium-trading.conf.template` + `deploy/nginx/ingress-proxy.conf.template`:
  single-host TLS vhost, catch-all that rejects unknown SNI and closes foreign Hosts (444), HTTP→HTTPS
  redirect, HSTS, `server_tokens off`, no access log, 16 KiB bodies, header/body timeouts,
  `limit_req` (10 r/s, burst 20) + `limit_conn` (20), only the application's route prefixes
  proxied, `GET/HEAD/POST` only, and every forwarded header **set** (Host, `X-Forwarded-Proto
  https`, `X-Forwarded-For $remote_addr`, `X-Forwarded-Host`/`X-Real-IP`/`Forwarded` cleared, the
  secret from a root-owned include).
* `scripts/verify_ingress_proxy.py`: remote checks (TLS, HSTS, version leak, HTTP redirect, foreign
  Host, spoofed `X-Forwarded-*`, client-supplied secret header, 413 body limit, unknown paths,
  methods, optional bounded rate-limit burst) and `--local` checks on the VPS (`nginx -T` contains
  the overwriting directives and no `$proxy_add_x_forwarded_for` / `$http_x_forwarded_proto` /
  `$http_host`, secret only via include, ingress port on loopback only, secret include and env file
  root-owned and ≤ 0640). No secret value is printed (`nginx -T` output is redacted first).
* CI job `ingress-proxy` renders the template on a **disposable** runner with a throwaway
  certificate and secret, runs `nginx -t`, starts the REAL `IngressGuard`/`IngressServer` behind it
  (`scripts/ci_ingress_upstream.py`) and runs the verifier with `--burst 60 --local`.

**F-17 stays OPEN** until an operator runs the verifier (remote **and** `--local`) against the real
VPS proxy and a human reviews `nginx -T` (redacted) against this section. Rotation of the secret
still needs a service restart (documented in the template header). No production proxy change is
made by this repository.

## 5. Lifecycle

Startup order in `run_ui` (all failures are `SystemExit` **before** `getUpdates` starts):
config validation → ledger → execution RPC + worker (only if wallet execution is on) →
`build_ingress` (checks every dependency, then binds the loopback socket) → collector/scoring →
`worker.start()` → `ingress.start()` (waits for the listener, else exit) → `bot.run(stop)`.

Shutdown (`SIGTERM` → `stop`): `bot.run` returns → `ingress.stop()` (stop accepting, drain ≤ 5 s,
close the socket) → `worker.stop()` → `bot.shutdown()` → engine dispose. A restart drops
in-memory PAPER intents/plans by design: old links answer 401 and the user opens a fresh alert.

Fail-closed matrix (each is a test): no/short/whitespace secret · malformed or absent bot token ·
PAPER flow without journal or verified V2 provider · invalid auth-store directory or mode · port in
use · wallet routes requested without ledger or execution RPC · listener failing to start. Request
time: dependency (RPC/chain) failures become 4xx refusals — never a 5xx and never an
unauthenticated success.

Resource limits for the shared VPS: one process, one event loop, `ingress_threads` blocking
threads, `ingress_max_concurrency` connections, 16 KiB body, 30 s per request, no access log, no
WebSocket, h11 header size 16 KiB. The execution RPC is shared with the worker, so its existing
concurrency cap (4), breaker and timeouts bound the combined load.

## 6. Private health

`/wallet_health` (owner only, private chat, same neutral "Not available." otherwise) now reports the
worker **and** the listener: state, bind address, requests/in-flight/peak, 2xx/4xx/5xx, timeouts, and
refusals at the boundary by reason (`secret`, `host`, `scheme`, `method`, `body_size`, `length`).
No public health route exists and no secret, cookie or user data is in the report. Push alerts,
bounded listener restarts and the degraded mode are in `docs/TRADING_LIVENESS.md` (F-18).

## 7. Rollback

Set `ingress_enabled: false` (or remove it) and restart `--ui`: no listener is created. Also remove
the proxy vhost. Wallet routes disappear independently when `wallet_execution_enabled` is false.

## 8. Unresolved / not done

* The proxy is trusted: anyone who holds the secret *and* can reach loopback can bypass TLS/rate
  limits at the edge (they still need Telegram identity, SIWE ownership and CSRF). Rotating the
  secret needs a restart (F-17).
* If the listener thread dies the bot keeps running and the Mini App is silently unavailable until
  someone reads `/wallet_health` (F-18). No in-process auto-restart or alert push.
* The proxy/TLS/DNS configuration itself is not in this repository and was not reviewed or deployed.
* Chunked request bodies are bounded by the existing handlers (8 KiB / 12 KiB), not by the guard.
* Sessions persist in SQLite across restarts, but the intents they point to do not; the stale
  session is refused, not repaired.
* Not exercised: a real browser through a real proxy, a real Telegram client, the real RPC.
