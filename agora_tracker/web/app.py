"""The ASGI application: routes, rendering, error handling, security headers, rate limit."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import jinja2
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..config import Config
from ..timerange import RangeError
from . import format as fmt
from .jsonutil import to_jsonable
from .params import ADDR_RE, PRESET_ORDER, list_params, parse_range
from .service import EARLY_SORTS, SM_VIEWS, TOKEN_SORTS, Busy, Dashboard, NotFound, sparkline

log = logging.getLogger("agora_tracker.web")
UTC = timezone.utc
HERE = Path(__file__).parent
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
NAV = (("overview", "/", "Overview"), ("tokens", "/tokens", "Projects"),
       ("smart", "/smart-money", "Smart money"), ("early", "/early", "Early plays"),
       ("health", "/scanner", "Scanner health"))
RANGE_LABELS = {"15m": "15 min", "1h": "1 hour", "6h": "6 hours", "24h": "24 hours",
                "7d": "7 days", "30d": "30 days"}


# --- middleware --------------------------------------------------------------------------------
def client_ip(request: Request) -> str:
    host = request.client.host if request.client else "?"
    xff = request.headers.get("x-forwarded-for")
    if xff and host in ("127.0.0.1", "::1"):          # set by the local Caddy only
        return xff.split(",")[-1].strip()[:64]
    return host


class RateLimiter:
    """Token bucket per client IP. Loopback callers without X-Forwarded-For (health checks,
    operators on the VPS) are exempt."""

    def __init__(self, rate: float, burst: int):
        self.rate, self.burst = rate, burst
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, ip: str, now: float | None = None) -> bool:
        if self.rate <= 0:
            return True
        now = time.monotonic() if now is None else now
        tokens, last = self.buckets.get(ip, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        ok = tokens >= 1
        self.buckets[ip] = (tokens - 1 if ok else tokens, now)
        if len(self.buckets) > 5000:
            cutoff = now - 300
            self.buckets = {k: v for k, v in self.buckets.items() if v[1] > cutoff}
        return ok


class Guard(BaseHTTPMiddleware):
    def __init__(self, app: Any, limiter: RateLimiter):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request,
                       call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        loopback = (request.client is not None and request.client.host in ("127.0.0.1", "::1")
                    and "x-forwarded-for" not in request.headers)
        if not loopback and path != "/healthz" and not path.startswith("/static/") \
                and not self.limiter.allow(client_ip(request)):
            return PlainTextResponse("Too many requests, slow down.", 429,
                                     headers={"Retry-After": "5"})
        resp = await call_next(request)
        h = resp.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        if request.headers.get("x-forwarded-proto") == "https":
            h["Strict-Transport-Security"] = "max-age=31536000"
        if path.startswith("/static/"):
            h["Cache-Control"] = ("public, max-age=31536000, immutable" if "v" in request.query_params
                                  else "public, max-age=3600")
        elif "cache-control" not in h:
            h["Cache-Control"] = "no-cache"
        return resp


# --- application ---------------------------------------------------------------------------------
def create_app(cfg: Config, dash: Dashboard, engine: Engine | None = None) -> Starlette:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(HERE / "templates"),
        autoescape=jinja2.select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    links = fmt.Links(cfg.links)
    fmt.register(env, links)
    asset_v = hashlib.sha1(b"".join(p.read_bytes() for p in sorted((HERE / "static").glob("*"))
                                    if p.is_file())).hexdigest()[:10]
    w = cfg.web
    started = time.time()

    def now_utc() -> datetime:
        return datetime.now(UTC)

    # -- rendering ----------------------------------------------------------------------------
    def make_qs(request: Request) -> Callable[..., str]:
        base = dict(request.query_params)

        def qs(**over: Any) -> str:
            d = {**base, **{k: v for k, v in over.items() if v is not None}}
            for k, v in over.items():
                if v is None:
                    d.pop(k, None)
            return "?" + urlencode(d) if d else "?"
        return qs

    def page(request: Request, name: str, nav: str, data: dict[str, Any], tr: Any = None,
             status: int = 200, title: str = "") -> HTMLResponse:
        ctx = {"d": data, "nav": nav, "navs": NAV, "cfg": cfg, "w": w, "tr": tr,
               "presets": [(p, RANGE_LABELS[p]) for p in PRESET_ORDER], "qs": make_qs(request),
               "path": request.url.path, "asset_v": asset_v, "title": title, "now": now_utc(),
               "query": dict(request.query_params)}
        body = env.get_template(name).render(**ctx)
        return HTMLResponse(body, status_code=status)

    def error_page(request: Request, status: int, headline: str, detail: str,
                   retry: bool = False) -> HTMLResponse:
        r = page(request, "error.html", "", {"status": status, "headline": headline,
                                             "detail": detail, "retry": retry}, title=headline,
                 status=status)
        if retry:
            r.headers["Retry-After"] = "10"
        return r

    def wants_json(request: Request) -> bool:
        return request.url.path.startswith("/api/")

    def fail(request: Request, status: int, headline: str, detail: str,
             retry: bool = False) -> Response:
        if wants_json(request):
            return JSONResponse({"error": headline, "detail": detail}, status_code=status)
        return error_page(request, status, headline, detail, retry)

    async def run(request: Request, fn: Callable[[], Any]) -> Any:
        return await run_in_threadpool(fn)

    def guarded(handler: Callable[[Request], Awaitable[Response]]
                ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            rid = uuid.uuid4().hex[:8]
            try:
                return await handler(request)
            except RangeError as e:
                return fail(request, 400, "Invalid time range", str(e))
            except NotFound as e:
                return fail(request, 404, "Not found", str(e))
            except Busy:
                return fail(request, 503, "Busy", "The dashboard is handling many requests. "
                            "Try again in a few seconds.", retry=True)
            except (OperationalError, DBAPIError) as e:
                log.error("[%s] database error on %s: %s", rid, request.url.path,
                          type(e.orig).__name__ if getattr(e, "orig", None) else type(e).__name__)
                return fail(request, 503, "Data temporarily unavailable",
                            f"The database did not answer in time (ref {rid}). "
                            "The scanner keeps running; try again shortly.", retry=True)
            except Exception:
                log.exception("[%s] unhandled error on %s", rid, request.url.path)
                return fail(request, 500, "Something went wrong", f"Reference {rid}.")
        return wrapper

    def get_range(request: Request) -> Any:
        return parse_range(request.query_params, now_utc(), w.max_custom_days)

    def page_num(request: Request) -> int:
        try:
            return min(max(int(request.query_params.get("page", "1")), 1), 10_000)
        except ValueError:
            return 1

    def addr_or_404(raw: str) -> str:
        if not ADDR_RE.match(raw):
            raise NotFound("that is not a valid address")
        return raw.lower()

    # -- pages ----------------------------------------------------------------------------------
    @guarded
    async def overview(request: Request) -> Response:
        tr = get_range(request)
        d = await run(request, lambda: dash.overview(tr, now_utc()))
        return page(request, "overview.html", "overview", d, tr, title="Overview")

    @guarded
    async def tokens(request: Request) -> Response:
        tr = get_range(request)
        lp = list_params(request.query_params, TOKEN_SORTS, "last")
        d = await run(request, lambda: dash.tokens(tr, lp, now_utc()))
        return page(request, "tokens.html", "tokens", d, tr, title="Projects")

    @guarded
    async def token(request: Request) -> Response:
        addr = addr_or_404(request.path_params["addr"])
        tr = get_range(request)
        d = await run(request, lambda: dash.token(addr, tr, page_num(request), now_utc()))
        d["spark"] = sparkline(d["series"])
        d["mcap_spark"] = sparkline(d["series"], "mcap_hype")
        return page(request, "token.html", "tokens", d, tr,
                    title=(d["meta_token"].get("symbol") or addr[:10]))

    @guarded
    async def smart(request: Request) -> Response:
        tr = get_range(request)
        view = request.query_params.get("view", "net")
        d = await run(request, lambda: dash.smart_money(tr, view, page_num(request), now_utc()))
        return page(request, "smart.html", "smart", d, tr, title="Smart money")

    @guarded
    async def wallet(request: Request) -> Response:
        addr = addr_or_404(request.path_params["addr"])
        tr = get_range(request)
        d = await run(request, lambda: dash.wallet(addr, tr, page_num(request), now_utc()))
        return page(request, "wallet.html", "smart", d, tr, title=fmt.short_addr(addr))

    @guarded
    async def early(request: Request) -> Response:
        tr = get_range(request)
        sort = request.query_params.get("sort", "score")
        d = await run(request, lambda: dash.early(tr, sort, now_utc()))
        return page(request, "early.html", "early", d, tr, title="Early plays")

    @guarded
    async def scanner(request: Request) -> Response:
        d = await run(request, lambda: dash.health(now_utc()))
        return page(request, "health.html", "health", d, None, title="Scanner health")

    @guarded
    async def about(request: Request) -> Response:
        return page(request, "about.html", "", {}, None, title="Method and limits")

    @guarded
    async def search(request: Request) -> Response:
        q = (request.query_params.get("q") or "").strip()[:80]
        if not q:
            return RedirectResponse("/tokens", 303)
        res = await run(request, lambda: dash.search(q))
        if res["kind"] == "token":
            return RedirectResponse(f"/tokens/{res['address']}", 303)
        if res["kind"] == "wallet" and not res.get("unknown"):
            return RedirectResponse(f"/wallets/{res['address']}", 303)
        if res["kind"] == "tokens":
            return page(request, "search.html", "tokens", {"q": q, **res}, None, title="Search")
        return page(request, "search.html", "tokens", {"q": q, "kind": "none"}, None,
                    title="Search", status=404)

    # -- JSON API ---------------------------------------------------------------------------------
    def api(build: Callable[[Request], Any]) -> Callable[[Request], Awaitable[Response]]:
        @guarded
        async def handler(request: Request) -> Response:
            data = await run(request, lambda: build(request))
            return JSONResponse(to_jsonable(data), headers={"Cache-Control": "public, max-age=15"})
        return handler

    def api_tokens(request: Request) -> Any:
        tr = get_range(request)
        return dash.tokens(tr, list_params(request.query_params, TOKEN_SORTS, "last"), now_utc())

    def api_token(request: Request) -> Any:
        d = dash.token(addr_or_404(request.path_params["addr"]), get_range(request),
                       page_num(request), now_utc())
        d["spark"] = sparkline(d["series"])
        return d

    def api_wallet(request: Request) -> Any:
        return dash.wallet(addr_or_404(request.path_params["addr"]), get_range(request),
                           page_num(request), now_utc())

    def api_meta(request: Request) -> Any:
        return {"service": "elysium-dashboard", "chain_id": cfg.chain.chain_id,
                "timezone": "UTC", "interval": "[start, end)", "page_size": w.page_size,
                "population": "wallets on Agora's public top-PnL leaderboard (not chain-wide)",
                "uptime_s": int(time.time() - started),
                "ranges": PRESET_ORDER + ["custom"], "smart_money_views": list(SM_VIEWS),
                "early_sorts": list(EARLY_SORTS), "token_sorts": list(TOKEN_SORTS)}

    # -- operational ---------------------------------------------------------------------------------
    async def healthz(request: Request) -> Response:
        ok, detail = True, "ok"
        if engine is not None:
            try:
                def ping() -> None:
                    with engine.connect() as c:
                        c.execute(sa.text("SELECT 1"))
                await run_in_threadpool(ping)
            except Exception as e:  # noqa: BLE001 - health must answer, not raise
                ok, detail = False, type(e).__name__
        return JSONResponse({"status": "ok" if ok else "unhealthy", "database": detail,
                             "uptime_s": int(time.time() - started)},
                            status_code=200 if ok else 503, headers={"Cache-Control": "no-store"})

    async def robots(request: Request) -> Response:
        return PlainTextResponse("User-agent: *\nAllow: /\nDisallow: /api/\n")

    async def not_found(request: Request, exc: Exception) -> Response:
        return fail(request, 404, "Page not found", "That page does not exist.")

    routes = [
        Route("/", overview), Route("/tokens", tokens), Route("/tokens/{addr}", token),
        Route("/smart-money", smart), Route("/wallets/{addr}", wallet), Route("/early", early),
        Route("/scanner", scanner), Route("/about", about), Route("/search", search),
        Route("/healthz", healthz), Route("/robots.txt", robots),
        Route("/api/v1/meta", api(api_meta)),
        Route("/api/v1/overview", api(lambda r: dash.overview(get_range(r), now_utc()))),
        Route("/api/v1/tokens", api(api_tokens)),
        Route("/api/v1/tokens/{addr}", api(api_token)),
        Route("/api/v1/smart-money", api(lambda r: dash.smart_money(
            get_range(r), r.query_params.get("view", "net"), page_num(r), now_utc()))),
        Route("/api/v1/wallets/{addr}", api(api_wallet)),
        Route("/api/v1/early", api(lambda r: dash.early(
            get_range(r), r.query_params.get("sort", "score"), now_utc()))),
        Route("/api/v1/health", api(lambda r: dash.health(now_utc()))),
        Route("/api/v1/search", api(lambda r: dash.search(r.query_params.get("q", "")[:80]))),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    return Starlette(routes=routes, exception_handlers={404: not_found},
                     middleware=[Middleware(Guard, limiter=RateLimiter(w.rate_limit_per_ip,
                                                                       w.rate_burst))])
