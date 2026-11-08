"""HTTP surface for the wallet-managed flow (issue #92). Mounted ONLY when
`trading.wallet_execution_enabled` is on AND the ledger, the secure authenticator and the exact
planner exist. It reuses the Mini App's authentication envelope (Telegram + verified wallet
ownership + alert intent) and adds nothing that can sign: every response tells the browser what to
ask ITS OWN wallet, and the browser reports back what the wallet answered."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..ledger.core import Actor, LedgerRefused, OrderLedger
from ..ledger.states import OrderState
from ..paper import TradeRefused
from ..plans import PlanBook, PlanOwner
from .approvals import ApprovalError
from .flow import FlowRefused, WalletFlow

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
ID24 = re.compile(r"^[A-Za-z0-9_-]{24}$")
ORDER_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REQ_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_BODY = 4096

# stable, non-leaky mapping of refusals to HTTP; messages are fixed text, never exception text
STATUS = {"not_found": 404, "busy": 503, "paused": 409, "execution_disabled": 409,
          "expired": 409, "invalid_state": 409, "invalid_transition": 409, "plan_mismatch": 409,
          "already_requested": 409, "conflict": 409, "duplicate": 409, "invalid_request": 400}


@dataclass(frozen=True)
class WalletRuntime:
    ledger: OrderLedger
    flow: WalletFlow
    plans: PlanBook


def _err(code: str, status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, "server_signs": False}, status)


def _unique(pairs):
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise TradeRefused("duplicate field")
        out[k] = v
    return out


async def _body(request: Request) -> dict:
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise TradeRefused("cross-origin request refused")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            raise TradeRefused("request too large")
        raw.extend(chunk)
    data = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(data, dict):
        raise TradeRefused("invalid request")
    return data


def wallet_routes(rt: WalletRuntime, authenticator, paper_flow) -> list[Route]:
    ledger, flow, plans = rt.ledger, rt.flow, rt.plans

    async def authed(request: Request, allowed: set[str]):
        data = await _body(request)
        if set(data) != {"intent", "init_data", "wallet"} | allowed:
            raise TradeRefused("unexpected fields")
        sid, claimed = data["intent"], data["wallet"]
        if (not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16}", sid)
                or not isinstance(data["init_data"], str) or not isinstance(claimed, str)):
            raise TradeRefused("invalid authentication fields")
        request.state.miniapp_intent = sid
        p = await authenticator.authenticate(request, data["init_data"], claimed)
        if (p is None or type(p.user_id) is not int or p.user_id <= 0
                or (not p.fixture and p.intent_id != sid) or not p.session_binding
                or p.session_expires_at is None):
            return None
        intent = paper_flow.get(p.user_id, p.user_id, sid, allow_paused=True)
        if not p.verified_wallet or p.verified_wallet != intent.wallet or claimed != intent.wallet:
            return None
        owner = PlanOwner(p.user_id, p.verified_wallet, p.session_binding, p.session_expires_at)
        return p, sid, owner, data

    async def recheck(request, principal, claimed):
        fresh = await authenticator.authenticate(request, "", claimed)
        return fresh == principal

    def handler(name: str, fields: set[str], work):
        async def run(request: Request):
            try:
                got = await authed(request, fields)
                if got is None:
                    return _err("not_authenticated", 401, "Authentication or wallet verification required.")
                p, sid, owner, data = got
                result = await run_in_threadpool(work, p, sid, owner, data)
                if not await recheck(request, p, data["wallet"]):
                    return _err("not_authenticated", 401, "Ownership session changed; reconnect.")
                return JSONResponse({**result, "server_signs": False})
            except (FlowRefused, LedgerRefused, ApprovalError) as e:
                code = getattr(e, "code", "refused")
                log.info("wallet api %s refused: %s", name, code)
                return _err(code, STATUS.get(code, 409), "Request refused: " + code.replace("_", " "))
            except (TradeRefused, ValueError, TypeError, KeyError):
                return _err("refused", 409, "Request refused.")
        return run

    def need(data: dict, key: str, pattern: re.Pattern) -> str:
        v = data.get(key)
        if not isinstance(v, str) or not pattern.fullmatch(v):
            raise FlowRefused("invalid_request", f"invalid {key}")
        return v

    def get_plan(owner: PlanOwner, sid: str, plan_id: str):
        plan, _evidence = plans.revalidate(owner, sid, plan_id)     # fresh, or it raises + revokes
        return plan

    def create_order(p, sid, owner, data):
        """Admit the reviewed plan exactly once (idempotent per plan) and move it to the point
        where the next wallet prompt is allowed. The click that calls this IS the user's explicit
        'review and confirm'; no wallet prompt happens here."""
        plan = get_plan(owner, sid, need(data, "plan_id", ID24))
        res = ledger.create_order(user_id=p.user_id, wallet=owner.wallet, plan=plan,
                                  idempotency_key=f"plan:{plan.id}",
                                  session_expires_at=owner.expires_at)
        oid, user = res.order.id, Actor.user(p.user_id)
        target = (OrderState.REQUIRES_APPROVAL if plan.simulation_status == "approval_required"
                  else OrderState.AWAITING_WALLET)
        view = res.order
        if view.state == OrderState.CREATED.value:
            ledger.transition(oid, OrderState.QUOTE_REVIEW, actor=user, request_key=f"q:{plan.id}")
        if view.state in (OrderState.CREATED.value, OrderState.QUOTE_REVIEW.value):
            ledger.transition(oid, target, actor=user, request_key=f"t:{plan.id}")
        view = ledger.get_order(p.user_id, oid)
        return {"order": view.public(), "next": "approval" if target == OrderState.REQUIRES_APPROVAL
                else "swap"}

    def prompt(p, sid, owner, data):
        oid = need(data, "order_id", ORDER_ID)
        plan = get_plan(owner, sid, need(data, "plan_id", ID24))
        state = ledger.get_order(p.user_id, oid).state
        if state == OrderState.REQUIRES_APPROVAL.value:
            return flow.approval_prompt(user_id=p.user_id, session=owner.session_binding,
                                        order_id=oid, plan=plan)
        return flow.swap_prompt(user_id=p.user_id, session=owner.session_binding, order_id=oid,
                                plan=plan, revalidate=lambda _p: None)   # revalidated just above

    def report(p, sid, owner, data):
        outcome = data.get("outcome")
        tx_hash, reason = data.get("tx_hash"), data.get("reason")
        if outcome not in ("hash", "rejected", "error") or not (
                tx_hash is None or isinstance(tx_hash, str)) or not (
                reason is None or isinstance(reason, str)):
            raise FlowRefused("invalid_request", "invalid report")
        return flow.report(user_id=p.user_id, session=owner.session_binding,
                           request_id=need(data, "request_id", REQ_ID), outcome=outcome,
                           tx_hash=tx_hash, reason=reason)

    def status(p, sid, owner, data):
        oid = need(data, "order_id", ORDER_ID)
        v = ledger.get_order(p.user_id, oid)
        return {"order": v.public(), "settled": v.state == "finalized"}

    def cancel(p, sid, owner, data):
        oid = need(data, "order_id", ORDER_ID)
        r = ledger.transition(oid, OrderState.CANCELLED, actor=Actor.user(p.user_id),
                              request_key=f"cancel:{oid}", reason="user_cancelled")
        return {"order": r.order.public()}

    async def page(request):
        return HTMLResponse((HERE / "wallet.html").read_text())

    routes: list = [Route("/wallet", page),
                    Mount("/wallet/static", StaticFiles(directory=HERE / "static"), name="wallet-static")]
    for name, fields, work in (
        ("order", {"plan_id"}, create_order),
        ("prompt", {"order_id", "plan_id"}, prompt),
        ("report", {"request_id", "outcome", "tx_hash", "reason"}, report),
        ("status", {"order_id"}, status),
        ("cancel", {"order_id"}, cancel),
    ):
        routes.append(Route(f"/wallet/api/{name}", handler(name, fields, work), methods=["POST"]))
    return routes
