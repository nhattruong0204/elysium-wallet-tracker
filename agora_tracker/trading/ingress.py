"""The ONE production entrypoint of the private Mini App and wallet routes (#95 F-16).

Why in-process: PAPER intents live in `bot.paper_trades` (memory), the auth store, plan book and
ledger belong to the `--ui` service, and the single Telegram `getUpdates` consumer is that same
process. A second process cannot share any of that, so the HTTP app runs here, on a thread, and
reuses `miniapp.create_app`, `AuthService`, `AuthStore`, `PlanBook`, `WalletFlow` unchanged.

What this module adds is only the boundary:
  * OFF by default: `build_ingress` returns None and nothing is bound.
  * Loopback bind only (validated in the config). TLS is terminated by an explicitly configured
    reverse proxy; this process never serves cleartext to a network.
  * `IngressGuard` refuses every request that did not come through that proxy: wrong/missing
    shared secret, wrong Host, scheme not https. Telegram identity, wallet ownership, CSRF,
    Origin and expiry stay in `AuthService` (they are NOT re-implemented here).
  * Resource limits for the shared VPS: connection cap, thread cap, body cap, request timeout.
  * Fail closed: any missing dependency raises `IngressSetupError` before a socket is bound.

Nothing here signs or broadcasts, and nothing here calls Telegram.
"""

from __future__ import annotations

import hmac
import logging
import os
import socket
import threading
import time
from collections.abc import Mapping
from typing import Any

from ..config import Config

log = logging.getLogger("agora_tracker.trading.ingress")

SECRET_HEADER = b"x-elysium-ingress"
MIN_SECRET_LEN = 32
METHODS = frozenset({"GET", "HEAD", "POST"})
SECURITY_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"strict-transport-security", b"max-age=15552000"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


class IngressSetupError(RuntimeError):
    """A requirement of the ingress is missing. Never echoes secrets."""


class IngressStats:
    """Counters for the private health report. Cheap, lock-protected, no per-user data."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.requests = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.rejected: dict[str, int] = {}
        self.status = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
        self.timeouts = 0
        self.last_log: dict[str, float] = {}

    def begin(self) -> None:
        with self._lock:
            self.requests += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def end(self, status: int | None, *, timeout: bool = False) -> None:
        with self._lock:
            self.in_flight -= 1
            if timeout:
                self.timeouts += 1
            if status is not None:
                self.status[f"{status // 100}xx"] = self.status.get(f"{status // 100}xx", 0) + 1

    def reject(self, reason: str) -> bool:
        """Count a refusal; True when it should also be logged (at most once a minute each)."""
        with self._lock:
            self.rejected[reason] = self.rejected.get(reason, 0) + 1
            now = time.monotonic()
            if now - self.last_log.get(reason, -1e9) >= 60:
                self.last_log[reason] = now
                return True
            return False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"requests": self.requests, "in_flight": self.in_flight,
                    "max_in_flight": self.max_in_flight, "rejected": dict(self.rejected),
                    "status": dict(self.status), "timeouts": self.timeouts}


class IngressGuard:
    """Pure ASGI wrapper. Anything that is not exactly what the configured proxy forwards gets an
    empty 404 (so a stray local process or a port scan learns nothing)."""

    def __init__(self, app, *, host: str, secret: str, max_body: int, timeout_s: float,
                 threads: int, stats: IngressStats) -> None:
        self.app, self.host = app, host.encode("ascii")
        self.secret = secret.encode("ascii")
        self.max_body, self.timeout_s, self.threads, self.stats = max_body, timeout_s, threads, stats

    # ---------------------------------------------------------------- lifespan
    async def _lifespan(self, receive, send) -> None:
        import anyio.to_thread
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                # The blocking work (RPC, sqlite, ledger) runs in this loop's default thread pool;
                # cap it so a burst cannot starve the tracker that shares the VPS.
                anyio.to_thread.current_default_thread_limiter().total_tokens = self.threads
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ---------------------------------------------------------------- checks
    def _refusal(self, scope) -> tuple[str, int] | None:
        """(reason, status) when this request must not reach the application."""
        if scope.get("scheme") != "https":
            return "scheme", 404
        hosts, secrets, length = [], [], []
        for k, v in scope["headers"]:
            if k == b"host":
                hosts.append(v)
            elif k == SECRET_HEADER:
                secrets.append(v)
            elif k == b"content-length":
                length.append(v)
        # compare even when the secret is absent so timing does not reveal which check failed
        ok = len(secrets) == 1 and hmac.compare_digest(secrets[0], self.secret)
        if not ok:
            return "secret", 404
        if hosts != [self.host]:
            return "host", 404
        if scope["method"] not in METHODS:
            return "method", 405
        if length:
            if len(length) != 1 or not length[0].isdigit() or len(length[0]) > 9:
                return "length", 400
            if int(length[0]) > self.max_body:
                return "body_size", 413
        return None

    @staticmethod
    async def _plain(send, status: int) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [*SECURITY_HEADERS, (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    # ---------------------------------------------------------------- dispatch
    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self._lifespan(receive, send)
            return
        if kind != "http":                     # websockets are not part of this application
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        refused = self._refusal(scope)
        if refused is not None:
            reason, status = refused
            if self.stats.reject(reason):
                log.warning("ingress refused a request: %s", reason)
            await self._plain(send, status)
            return

        import anyio
        started: list[int] = []

        async def guarded_send(message) -> None:
            if message["type"] == "http.response.start":
                started.append(message["status"])
                have = {k.lower() for k, _ in message["headers"]}
                message = dict(message)
                message["headers"] = [*message["headers"],
                                      *[h for h in SECURITY_HEADERS if h[0] not in have]]
            await send(message)

        self.stats.begin()
        timed_out = False
        try:
            with anyio.move_on_after(self.timeout_s) as scopes:
                await self.app(scope, receive, guarded_send)
            if scopes.cancelled_caught:
                timed_out = True
                if not started:
                    await self._plain(send, 504)
                    started.append(504)
        finally:
            self.stats.end(started[0] if started else None, timeout=timed_out)


class IngressServer:
    """Owns the loopback socket and the uvicorn thread. `start()` fails closed."""

    def __init__(self, app, sock: socket.socket, *, origin: str, cfg: Config, stats: IngressStats,
                 wallet_routes: bool) -> None:
        t = cfg.trading
        self.origin, self.stats, self.wallet_routes = origin, stats, wallet_routes
        self._app, self._cfg = app, cfg
        self._sock = sock
        self._address = sock.getsockname()[:2]
        self._error: str | None = None
        self._thread: threading.Thread | None = None
        self.restarts = 0
        self.limits = {"max_concurrency": t.ingress_max_concurrency, "threads": t.ingress_threads,
                       "max_body_bytes": t.ingress_max_body_bytes,
                       "request_timeout_s": t.ingress_request_timeout_s}
        self._server = self._make_server()

    def _make_server(self):
        import uvicorn
        t = self._cfg.trading
        return uvicorn.Server(uvicorn.Config(
            self._app, log_level="warning", lifespan="on", interface="asgi3", http="h11", ws="none",
            # the proxy is on loopback; honour its X-Forwarded-Proto/For from loopback ONLY
            proxy_headers=True, forwarded_allow_ips=t.ingress_bind_host,
            access_log=False, server_header=False, date_header=True,
            timeout_keep_alive=int(t.ingress_keepalive_s),
            limit_concurrency=t.ingress_max_concurrency, backlog=64,
            h11_max_incomplete_event_size=16384, timeout_graceful_shutdown=5, workers=1))

    # ---------------------------------------------------------------- lifecycle
    def _run(self) -> None:
        try:
            self._server.run(sockets=[self._sock])
        except BaseException as exc:                      # noqa: BLE001 - uvicorn raises SystemExit
            self._error = type(exc).__name__
            log.error("ingress listener stopped: %s", self._error)

    def start(self, timeout_s: float = 10.0) -> None:
        self._thread = threading.Thread(target=self._run, name="ingress", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop(2.0)
                raise IngressSetupError("the ingress listener did not start")
            time.sleep(0.02)
        log.info("ingress listening on %s:%s for %s (wallet routes: %s)", *self._address,
                 self.origin, "on" if self.wallet_routes else "off")

    def stop(self, timeout_s: float = 10.0) -> None:
        self._server.should_exit = True
        t = self._thread
        if t is not None:
            t.join(timeout_s)
            if t.is_alive():
                self._server.force_exit = True
                t.join(2.0)
        try:
            self._sock.close()
        except OSError:
            pass

    def restart(self, timeout_s: float = 10.0) -> None:
        """Bounded recovery of a dead listener (F-18), driven by the liveness monitor. Rebinds the
        SAME loopback address with a fresh uvicorn server around the SAME application object (so
        in-memory sessions/plans survive). It never polls Telegram, never touches the ledger or a
        pause, and creates no wallet request. Raises `IngressSetupError` if it cannot come back."""
        if self.healthy():
            return
        self.stop(2.0)
        self._sock = _bind_loopback(*self._address)
        self._server = self._make_server()
        self._error = None
        self.restarts += 1
        log.warning("restarting the ingress listener (restart #%d)", self.restarts)
        self.start(timeout_s)

    # ---------------------------------------------------------------- health
    @property
    def address(self) -> tuple[str, int]:
        return self._address

    def healthy(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive() and self._server.started and not self._server.should_exit)

    def snapshot(self) -> dict[str, Any]:
        t = self._thread
        alive = bool(t and t.is_alive())
        state = "running" if self.healthy() else ("failed" if self._error else "stopped")
        return {"state": state, "thread_alive": alive, "origin": self.origin,
                "bind": f"{self._address[0]}:{self._address[1]}",
                "wallet_routes": self.wallet_routes, "error": self._error,
                "restarts": self.restarts,
                "limits": dict(self.limits), **self.stats.snapshot()}


def render_text(s: Mapping[str, Any], esc=lambda x: x) -> str:
    rej = ", ".join(f"{esc(k)}={v}" for k, v in sorted(s["rejected"].items())) or "none"
    st = s["status"]
    return "\n".join([
        "<b>Mini App ingress</b>",
        f"state: {esc(str(s['state']))} (thread={s['thread_alive']}); bind {esc(s['bind'])} "
        f"for {esc(s['origin'])}",
        f"wallet routes: {'mounted' if s['wallet_routes'] else 'not mounted'}",
        f"requests: {s['requests']} (in flight {s['in_flight']}, peak {s['max_in_flight']}); "
        f"2xx {st['2xx']}, 4xx {st['4xx']}, 5xx {st['5xx']}, timeouts {s['timeouts']}",
        f"refused at the boundary: {rej}",
        f"last error: {esc(str(s['error']))}",
    ])


# ----------------------------------------------------------------------------- wiring

def _bind_loopback(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(64)
        sock.set_inheritable(False)
    except OSError as exc:
        sock.close()
        raise IngressSetupError(f"cannot bind the loopback listener {host}:{port}: "
                                f"{exc.strerror or 'error'}") from None
    return sock


def build_ingress(cfg: Config, *, flow, ledger, exec_rpc, environ: Mapping[str, str] = os.environ,
                  now=time.time) -> IngressServer | None:
    """None when `trading.ingress_enabled` is false (the default): nothing is built or bound.
    When true, every missing requirement raises `IngressSetupError` BEFORE a socket is opened."""
    t = cfg.trading
    if not t.ingress_enabled:
        return None
    from ..cards import Links
    from .auth import AuthService
    from .auth.store import AuthStore
    from .auth.telegram import AuthRefused
    from .chain import ReadRpc
    from .miniapp import create_app
    from .paper import TradeRefused
    from .plans import PlanBook
    from .v2 import DirectV2Provider

    token = cfg.telegram.bot_token or ""
    head, _, _ = token.partition(":")
    if not head.isdigit():
        raise IngressSetupError("the Telegram bot token is missing or malformed")
    if flow is None or getattr(flow, "journal", None) is None \
            or not isinstance(getattr(flow, "provider", None), DirectV2Provider) \
            or not isinstance(flow.provider.rpc, ReadRpc):
        raise IngressSetupError("the verified PAPER flow (journal + V2 provider) is unavailable")
    secret = environ.get(t.ingress_secret_env, "")
    if len(secret) < MIN_SECRET_LEN or not secret.isascii() or not secret.isprintable() \
            or secret != secret.strip() or " " in secret:
        raise IngressSetupError(f"{t.ingress_secret_env} must be set to a random token of at "
                                f"least {MIN_SECRET_LEN} printable characters")
    assert t.ingress_origin and t.ingress_auth_path       # guaranteed by TradingConfig
    try:
        store = AuthStore(t.ingress_auth_path)
        auth = AuthService(flow=flow, store=store, bot_token=token, bot_id=int(head),
                           origin=t.ingress_origin, rpc=flow.provider.rpc, now=now)
        plans = wallet = None
        if t.wallet_execution_enabled:                  # the wallet routes are a second opt-in
            if ledger is None or exec_rpc is None:
                raise IngressSetupError("wallet routes need the order ledger and the execution RPC")
            from .wallet.api import WalletRuntime
            from .wallet.approvals import ApprovalPlanner
            from .wallet.flow import WalletFlow
            plans = PlanBook(flow)
            wallet = WalletRuntime(ledger, WalletFlow(ledger, ApprovalPlanner(exec_rpc),
                                                      nonce_reader=exec_rpc), plans, Links(cfg.links))
        app = create_app(flow, auth, wallet, plans=plans)
    except (TradeRefused, AuthRefused, ValueError, OSError) as exc:
        raise IngressSetupError(f"ingress dependency unavailable: {type(exc).__name__}") from None
    stats = IngressStats()
    guarded = IngressGuard(app, host=t.ingress_origin.split("://", 1)[1], secret=secret,
                           max_body=t.ingress_max_body_bytes, timeout_s=t.ingress_request_timeout_s,
                           threads=t.ingress_threads, stats=stats)
    sock = _bind_loopback(t.ingress_bind_host, t.ingress_port)
    return IngressServer(guarded, sock, origin=t.ingress_origin, cfg=cfg, stats=stats,
                         wallet_routes=wallet is not None)
