"""HTTPS tests use the existing C1 auth adapter, real crypto and V2 RPC fixtures."""

from types import SimpleNamespace

import pytest
import requests
from test_trading_auth import WALLET, secured, signed_init  # noqa: F401
from test_trading_auth_http import auth_live, prove
from test_unsigned_plans import PlanRpc

from agora_tracker.trading.v2 import DIRECT_ROUTE, DirectV2Provider

pytestmark = pytest.mark.filterwarnings("ignore:Unverified HTTPS request")


@pytest.fixture
def plan_live(secured, tmp_path):
    rpc = PlanRpc(wallet=WALLET)
    rpc.clock = secured.clock
    secured.flow.provider = DirectV2Provider(rpc, now=lambda: secured.clock[0], optimize=False)
    secured.flow.routes = frozenset({DIRECT_ROUTE})
    fixture = auth_live.__wrapped__(secured, tmp_path, SimpleNamespace(param=True))
    live = next(fixture)
    live.plan_rpc = rpc
    try:
        yield live
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def build(live, **extra):
    return live.post("plan", init_data="", wallet=WALLET, amount="0.1", slippage_bps=50, **extra)


def validate(live, plan_id, **extra):
    return live.post("revalidate", init_data="", wallet=WALLET, plan_id=plan_id, **extra)


def test_ownership_required_exact_payload_and_changed_state_rejected(plan_live):
    p = plan_live
    assert p.post("auth/bootstrap", init_data=signed_init()).status_code == 200
    assert build(p).status_code == 403
    # A new valid launch rotates the Telegram-only session before ownership proof.
    prove(p)
    response = build(p)
    assert response.status_code == 200, response.text
    plan = response.json()["plan"]
    assert plan["wallet"] == WALLET and plan["recipient"] == WALLET
    assert plan["transaction_payload"]["gas"] == "0x1d4c0"
    assert "session_binding" not in plan
    assert validate(p, plan["id"]).json()["plan"] == plan
    p.plan_rpc.advance()
    p.plan_rpc.native -= 1
    assert validate(p, plan["id"]).status_code == 409
    assert validate(p, plan["id"]).status_code == 409


def test_strict_api_requote_revoke_and_no_transaction_input(plan_live):
    p = plan_live
    prove(p)
    original = build(p).json()["plan"]
    assert build(p, transaction={"to": "0x" + "ff" * 20, "data": "0xdead"}).status_code == 409
    assert validate(p, original["id"]).status_code == 409
    newer = build(p).json()["plan"]
    assert (
        p.post("plan", init_data="", wallet=WALLET, amount="NaN", slippage_bps=50).status_code
        == 409
    )
    assert validate(p, newer["id"]).status_code == 409
    newer = build(p).json()["plan"]
    assert p.post("revoke-plan", init_data="", wallet=WALLET, plan_id=newer["id"]).json()["revoked"]
    assert validate(p, newer["id"]).status_code == 409
    assert not p.flow.orders(1, 1)
    for route in ("sign", "approve", "broadcast", "execute", "submit"):
        assert p.post(route).status_code == 404


def test_plan_bound_to_one_ownership_session(plan_live):
    p = plan_live
    prove(p)
    plan = build(p).json()["plan"]
    # Another independently proved session for the SAME user/wallet cannot reuse it.
    other = requests.Session()
    other.verify = False
    other.headers["Origin"] = p.base
    # Use a separate cookie/CSRF exchange for the second ownership session.
    bootstrap = other.post(
        p.base + "/api/auth/bootstrap",
        json={"intent": p.intent.id, "init_data": signed_init()},
        timeout=5,
    )
    other.headers["X-CSRF-Token"] = bootstrap.json()["csrf"]
    q = other.post(
        p.base + "/api/auth/challenge",
        json={"intent": p.intent.id, "wallet": WALLET, "chain_id": 99901},
        timeout=5,
    ).json()
    from test_trading_auth import sign

    verified = other.post(
        p.base + "/api/auth/verify",
        json={
            "intent": p.intent.id,
            "wallet": WALLET,
            "chain_id": 99901,
            "nonce": q["nonce"],
            "signature": sign(q["message"]),
        },
        timeout=5,
    )
    other.headers["X-CSRF-Token"] = verified.json()["csrf"]
    response = other.post(
        p.base + "/api/revalidate",
        json={"intent": p.intent.id, "wallet": WALLET, "init_data": "", "plan_id": plan["id"]},
        timeout=5,
    )
    assert response.status_code == 409
    other.close()


def test_auth_revocation_during_plan_simulation_discards_response(plan_live):
    p = plan_live
    prove(p)
    from agora_tracker.trading.auth.service import COOKIE, digest

    token = digest(p.client.cookies.get(COOKIE))

    def revoke(method, params):
        if method == "eth_call":
            p.plan_rpc.on_call = None
            p.store.revoke(token, 100)

    p.plan_rpc.on_call = revoke
    assert build(p).status_code == 401


def test_quote_ui_expiry_respects_shorter_auth_session(plan_live):
    from agora_tracker.trading.auth.service import COOKIE, digest

    p = plan_live
    prove(p)
    with p.store._connect() as c:
        c.execute(
            "UPDATE auth_sessions SET expires=105 WHERE token=?",
            (digest(p.client.cookies.get(COOKIE)),),
        )
    response = build(p)
    assert response.status_code == 200, response.text
    assert response.json()["plan"]["expires_at"] == 105
    assert response.json()["quote"]["expires_at"] == 105
