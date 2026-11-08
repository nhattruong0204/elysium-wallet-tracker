# ruff: noqa: F811
"""HTTP surface of the wallet-managed flow (issue #92): auth envelope, isolation, idempotency."""

from __future__ import annotations

import pytest
from conftest import TEST_URL  # noqa: F401
from starlette.applications import Starlette
from test_trading_ledger import (  # noqa: F401
    NOW0,
    U1,
    U2,
    clock,
    ledger,
    live,
    owner_engine,
    rw_engine,
)
from walletkit import E18, WALLET, FakeChain, Served, make_plan

from agora_tracker.config import TradingConfig
from agora_tracker.trading.miniapp.app import Principal, create_app
from agora_tracker.trading.wallet.api import WalletRuntime, wallet_routes
from agora_tracker.trading.wallet.approvals import ApprovalPlanner
from agora_tracker.trading.wallet.flow import WalletFlow

SID = "A" * 16
HASH = "0x" + "cd" * 32


class StubAuth:
    """Header-driven principal; the real one is #101's SIWE/Telegram authenticator."""
    ownership_proof = True

    async def authenticate(self, request, init_data, claimed):
        who = request.headers.get("x-user")
        if not who:
            return None
        return Principal(int(who), verified_wallet=WALLET, intent_id=SID, session_binding="bind-" + who,
                         session_expires_at=NOW0 + 3600)


class StubIntent:
    wallet = WALLET


class StubPaper:
    def get(self, uid, uid2, sid, allow_paused=False):
        return StubIntent()


class StubPlans:
    def __init__(self):
        self.by_id = {}

    def add(self, plan):
        self.by_id[plan.id] = plan

    def revalidate(self, owner, sid, plan_id):
        p = self.by_id.get(plan_id)
        if p is None or p.user_id != owner.user_id:
            from agora_tracker.trading.paper import TradeRefused
            raise TradeRefused("unknown plan")
        return p, None


@pytest.fixture
def api(live):
    plans = StubPlans()
    flow = WalletFlow(live, ApprovalPlanner(FakeChain(), clock=live.clock), clock=live.clock)
    rt = WalletRuntime(live, flow, plans)
    app = Starlette(routes=wallet_routes(rt, StubAuth(), StubPaper()))
    served = Served(app)
    yield served.client, plans, live
    served.close()


def post(client, name, user=U1, **fields):
    body = {"intent": SID, "init_data": "", "wallet": WALLET, **fields}
    headers = {"x-user": str(user)} if user else {}
    return client.post(f"/wallet/api/{name}", json=body, headers=headers)


def buy_plan(plans, lg, **kw):
    p = make_plan("buy", uid=U1, now=lg.clock(), **kw)
    plans.add(p)
    return p


def test_unauthenticated_requests_are_401_and_touch_nothing(api):
    client, plans, lg = api
    p = buy_plan(plans, lg)
    r = post(client, "order", user=None, plan_id=p.id)
    assert r.status_code == 401 and r.json()["server_signs"] is False
    assert lg.list_orders(U1) == []


def test_full_buy_flow_over_http(api):
    client, plans, lg = api
    p = buy_plan(plans, lg)
    o = post(client, "order", plan_id=p.id)
    assert o.status_code == 200, o.text
    order = o.json()["order"]
    assert o.json()["next"] == "swap" and order["state"] == "awaiting_wallet"
    # idempotent: a double click returns the same order
    assert post(client, "order", plan_id=p.id).json()["order"]["id"] == order["id"]
    pr = post(client, "prompt", order_id=order["id"], plan_id=p.id)
    req = pr.json()["request"]
    assert pr.status_code == 200 and req["data"] == p.transaction.calldata
    assert req["eip1193"]["method"] == "eth_sendTransaction"
    assert post(client, "prompt", order_id=order["id"], plan_id=p.id).json()["request"]["id"] == req["id"]
    rep = post(client, "report", request_id=req["id"], outcome="hash", tx_hash=HASH, reason=None)
    assert rep.status_code == 200 and rep.json()["settled"] is False
    st = post(client, "status", order_id=order["id"]).json()
    assert st["order"]["state"] == "submitted" and st["settled"] is False


def test_sell_flow_stops_at_a_finite_approval_not_a_swap(api):
    client, plans, lg = api
    p = make_plan("sell", status="approval_required", amount_in=E18 // 2, out=E18 * 8 // 10,
                  uid=U1, now=lg.clock())
    plans.add(p)
    o = post(client, "order", plan_id=p.id).json()
    assert o["next"] == "approval" and o["order"]["state"] == "requires_approval"
    req = post(client, "prompt", order_id=o["order"]["id"], plan_id=p.id).json()["request"]
    assert req["kind"] == "approve" and req["amount_wei"] == str(E18 // 2)
    assert req["data"].startswith("0x095ea7b3") and req["to"] == p.token


def test_other_users_cannot_see_prompt_report_or_cancel_an_order(api):
    client, plans, lg = api
    p = buy_plan(plans, lg)
    oid = post(client, "order", plan_id=p.id).json()["order"]["id"]
    rid = post(client, "prompt", order_id=oid, plan_id=p.id).json()["request"]["id"]
    assert post(client, "status", user=U2, order_id=oid).status_code == 404
    assert post(client, "cancel", user=U2, order_id=oid).status_code == 404
    r = post(client, "report", user=U2, request_id=rid, outcome="hash", tx_hash=HASH, reason=None)
    assert r.status_code == 404
    assert post(client, "prompt", user=U2, order_id=oid, plan_id=p.id).status_code in (404, 409)


def test_strict_bodies_and_origin(api):
    client, plans, lg = api
    p = buy_plan(plans, lg)
    h = {"x-user": str(U1)}
    base = {"intent": SID, "init_data": "", "wallet": WALLET}
    extra = client.post("/wallet/api/order", json={**base, "plan_id": p.id, "user_id": 2}, headers=h)
    missing = client.post("/wallet/api/order", json=base, headers=h)
    dup = client.post("/wallet/api/order", content=f'{{"intent":"{SID}","intent":"{SID}"}}', headers=h)
    cross = client.post("/wallet/api/order", json={**base, "plan_id": p.id},
                        headers={**h, "origin": "https://evil.example"})
    huge = client.post("/wallet/api/order", content=b"{" + b" " * 10_000 + b"}", headers=h)
    for r in (extra, missing, dup, cross, huge):
        assert r.status_code in (400, 409), r.text
    assert lg.list_orders(U1) == []


def test_responses_never_leak_session_or_user_fields(api):
    client, plans, lg = api
    p = buy_plan(plans, lg)
    text = post(client, "order", plan_id=p.id).text
    oid = post(client, "order", plan_id=p.id).json()["order"]["id"]
    text += post(client, "prompt", order_id=oid, plan_id=p.id).text
    for banned in ("session_binding", "bind-1001", '"user_id"', "init_data"):
        assert banned not in text


def test_refusals_use_fixed_text_not_exception_text(api):
    client, plans, lg = api
    p = buy_plan(plans, lg)
    oid = post(client, "order", plan_id=p.id).json()["order"]["id"]
    lg.pause(__import__("agora_tracker.trading.ledger", fromlist=["Actor"]).Actor.owner(9000), "t")
    r = post(client, "prompt", order_id=oid, plan_id=p.id)
    assert r.status_code == 409 and r.json()["error"] == "paused"
    assert "trading is paused" not in r.text


def test_user_cancel_before_the_wallet_only(api):
    client, plans, lg = api
    p = buy_plan(plans, lg)
    oid = post(client, "order", plan_id=p.id).json()["order"]["id"]
    # awaiting_wallet cannot be cancelled by the user (edge is system/owner-only)
    assert post(client, "cancel", order_id=oid).status_code == 409


def test_flag_off_means_no_wallet_routes_in_the_miniapp():
    served = Served(create_app())
    try:
        assert served.client.get("/wallet").status_code == 404
        assert served.client.post("/wallet/api/order", json={}).status_code == 404
    finally:
        served.close()


def test_wallet_runtime_requires_the_secure_planner():
    with pytest.raises(ValueError):
        create_app(None, None, wallet=object())


def test_config_flag_defaults_off_and_requires_the_ledger():
    assert TradingConfig().wallet_execution_enabled is False
    with pytest.raises(ValueError):
        TradingConfig(wallet_execution_enabled=True)
    assert TradingConfig(wallet_execution_enabled=True, ledger_enabled=True)
    with pytest.raises(ValueError):
        TradingConfig(wallet_execution_enabled="yes")
