"""Isolated unsigned-preview UI; never mount on the public research dashboard.

The default authenticator denies all API access. The optional #89 adapter verifies ownership;
#90 supplies any Stage C revalidation contract. Tests inject explicitly labelled mocks.
This adapter only reuses existing PAPER state/provider; there is no Send/Confirm API.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ...cards import Links
from ...config import LinksConfig
from ..paper import CHAIN, PaperFlow, RouteUnavailable, TradeRefused
from ..plans import PlanBook, PlanOwner
from ..v2 import DirectV2Provider

HERE = Path(__file__).parent
CSP = (
    "default-src 'none'; script-src 'self' https://telegram.org/js/telegram-web-app.js; "
    "style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self' https://web.telegram.org; object-src 'none'"
)


@dataclass(frozen=True)
class Principal:
    user_id: int
    verified_wallet: str | None = None
    fixture: bool = False
    intent_id: str | None = None
    session_binding: str | None = None
    session_expires_at: float | None = None


class Authenticator(Protocol):
    async def authenticate(
        self, request: Request, init_data: str, claimed_wallet: str | None
    ) -> Principal | None: ...


class DenyAuth:
    async def authenticate(self, request, init_data, claimed_wallet):
        return None


class Guard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Content-Security-Policy": CSP,
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        return response


def create_app(flow: PaperFlow | None = None, auth: Authenticator | None = None) -> Starlette:
    """Dependency injection only: no mock-auth flag, keys, scanner or DB connection."""
    authenticator = auth or DenyAuth()
    secure_auth = getattr(authenticator, "ownership_proof", False) is True
    if secure_auth and getattr(authenticator, "flow", None) is not flow:
        raise ValueError("Authentication must use the Mini App's existing PAPER flow")
    auth_routes = getattr(authenticator, "routes", lambda: ())() if secure_auth else ()
    plans = (
        PlanBook(flow)
        if secure_auth and flow and isinstance(flow.provider, DirectV2Provider)
        else None
    )
    links = Links(LinksConfig())

    async def index(request):
        mode = (
            "telegram-siwe-v1"
            if getattr(authenticator, "ownership_proof", False) is True
            else "disabled"
        )
        return HTMLResponse(
            (HERE / "index.html")
            .read_text()
            .replace(
                '<main id="app"',
                f'<main data-auth-mode="{mode}" data-plan-mode="{"v2-exact" if plans else "disabled"}" id="app"',
            )
        )

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TradeRefused("Duplicate preview field")
            result[key] = value
        return result

    async def api(request: Request):
        try:
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                raise TradeRefused("Cross-origin preview request refused")
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 8192:
                    raise TradeRefused("Preview request is too large")
                body.extend(chunk)
            data = json.loads(body, object_pairs_hook=unique_fields)
            if not isinstance(data, dict):
                raise TradeRefused("Invalid preview request")
            sid = data.get("intent")
            if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16}", sid):
                raise TradeRefused("Open a fresh Mini App link from your private alert panel")
            init_data = data.get("init_data", "")
            claimed = data.get("wallet")
            if not isinstance(init_data, str) or (
                claimed is not None and not isinstance(claimed, str)
            ):
                raise TradeRefused("Invalid authentication request")
            request.state.miniapp_intent = sid
            principal = await authenticator.authenticate(request, init_data, claimed)
            if (
                principal is None
                or type(principal.user_id) is not int
                or principal.user_id <= 0
                or flow is None
                or (not principal.fixture and principal.intent_id != sid)
            ):
                return JSONResponse(
                    {
                        "error": "not_authenticated",
                        "message": "Authentication required or expired. Open a fresh private Telegram alert. Transaction signing is unavailable.",
                    },
                    401,
                )
            uid = principal.user_id
            action = request.url.path.rsplit("/", 1)[1]
            i = flow.get(uid, uid, sid, allow_paused=action in ("cancel", "revoke-plan"))
            context = {
                "intent": i.id,
                "token": i.token,
                "chain_id": CHAIN,
                "side": i.side,
                "source_chat": i.source_chat,
                "source_message": i.source_message,
                "expires_at": i.expires_at,
                "server_time": flow.now(),
                "external_url": links.agora_token(i.token),
                "signing_enabled": False,
                "auth": {
                    "telegram_authenticated": True,
                    "wallet_verified": bool(
                        principal.verified_wallet and principal.verified_wallet == i.wallet
                    ),
                    "wallet": principal.verified_wallet,
                    "fixture": principal.fixture,
                },
            }
            if plans and action in ("quote", "plan"):
                plans.revoke(uid, sid)
            if action == "session":
                return JSONResponse(context)
            if action == "cancel":
                if plans:
                    plans.revoke(uid, sid)
                flow.cancel(uid, uid, sid)
                return JSONResponse({"cancelled": True, "signing_enabled": False})
            if action == "revoke-plan":
                if not plans or set(data) != {"intent", "init_data", "wallet", "plan_id"}:
                    raise TradeRefused("Invalid plan revocation")
                plan_id = data.get("plan_id")
                if not isinstance(plan_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", plan_id):
                    raise TradeRefused("Invalid plan identifier")
                plans.revoke(uid, sid, plan_id)
                return JSONResponse({"revoked": True, "signing_enabled": False})
            if (
                not principal.verified_wallet
                or principal.verified_wallet != i.wallet
                or claimed != i.wallet
            ):
                return JSONResponse(
                    {
                        "error": "wallet_unverified",
                        "message": "Wallet ownership is not verified for this alert session. Reopen after authentication.",
                    },
                    403,
                )
            exact_plan = None
            if action in ("plan", "revalidate"):
                if (
                    plans is None
                    or not principal.session_binding
                    or principal.session_expires_at is None
                ):
                    raise TradeRefused("Session-bound V2 planner unavailable")
                expected = {"intent", "init_data", "wallet"} | (
                    {"amount", "slippage_bps"} if action == "plan" else {"plan_id"}
                )
                if set(data) != expected:
                    plans.revoke(uid, sid)
                    raise TradeRefused("Only typed plan parameters are accepted")
                owner = PlanOwner(
                    uid,
                    principal.verified_wallet,
                    principal.session_binding,
                    principal.session_expires_at,
                )
                if action == "revalidate":
                    plan_id = data.get("plan_id")
                    if not isinstance(plan_id, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{24}", plan_id
                    ):
                        raise TradeRefused("Invalid plan identifier")
                    plan, evidence = await run_in_threadpool(plans.revalidate, owner, sid, plan_id)
                    fresh = await authenticator.authenticate(request, "", claimed)
                    if fresh != principal:
                        plans.revoke(uid, sid, plan_id)
                        return JSONResponse(
                            {
                                "error": "not_authenticated",
                                "message": "Ownership session changed",
                                "signing_enabled": False,
                            },
                            401,
                        )
                    return JSONResponse(
                        {
                            "plan": plan.public(),
                            "validation": asdict(evidence),
                            "signing_enabled": False,
                        }
                    )
            value, slippage = data.get("amount"), data.get("slippage_bps")
            if not isinstance(value, str) or type(slippage) is not int or not 10 <= slippage <= 100:
                raise TradeRefused("Invalid amount or slippage")

            def preview():
                flow.select(uid, uid, sid, value)
                return flow.review(uid, uid, sid, slippage)

            if action == "plan":
                if plans is None:
                    raise TradeRefused("Exact planner unavailable")
                exact_plan, q = await run_in_threadpool(plans.build, owner, sid, value, slippage)
            else:
                if plans:
                    plans.revoke(uid, sid)
                q = await run_in_threadpool(preview)
            # Logout/account revocation during network I/O must not release a private
            # quote under a principal checked before those operations.
            if secure_auth:
                fresh = await authenticator.authenticate(request, "", claimed)
                if fresh != principal:
                    if plans and exact_plan:
                        plans.revoke(uid, sid, exact_plan.id)
                    return JSONResponse(
                        {
                            "error": "not_authenticated",
                            "message": "Authentication changed; reconnect",
                            "signing_enabled": False,
                        },
                        401,
                    )
            context["server_time"] = flow.now()
            result = asdict(q)
            if exact_plan:
                result["expires_at"] = min(q.expires_at, exact_plan.expires_at)
            # Every financial integer crosses JSON as a decimal string, never a JS Number.
            for key in (
                "amount_in_wei",
                "amount_out_wei",
                "min_out_wei",
                "balance_wei",
                "native_balance_wei",
                "gas_wei",
                "fee_wei",
                "approval_required_wei",
                "gas_price_wei",
                "agora_amount_out_wei",
            ):
                if result[key] is not None:
                    result[key] = str(result[key])
            response = {"context": context, "quote": result, "signing_enabled": False}
            if exact_plan:
                response["plan"] = exact_plan.public()
            return JSONResponse(response)
        except RouteUnavailable:
            return JSONResponse(
                {
                    "error": "unsupported_route",
                    "message": "No supported verified V2 route. Open the verified external DEX instead.",
                    "signing_enabled": False,
                },
                404,
            )
        except (TradeRefused, ValueError, TypeError, KeyError):
            return JSONResponse(
                {
                    "error": "preview_refused",
                    "message": "Preview refused. Check intent expiry, wallet, amount, liquidity and supported V2 route. Use the verified DEX link when unsupported.",
                    "signing_enabled": False,
                },
                409,
            )

    app = Starlette(
        routes=[
            Route("/", index),
            *[
                Route("/api/" + action, api, methods=["POST"])
                for action in ("session", "quote", "cancel", "plan", "revalidate", "revoke-plan")
            ],
            *auth_routes,
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ]
    )
    app.add_middleware(Guard)
    return app
