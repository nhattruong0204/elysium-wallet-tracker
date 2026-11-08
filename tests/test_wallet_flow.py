# ruff: noqa: F811
"""Ledger-gated wallet prompts against a real PostgreSQL (issue #92).

Nothing here signs or broadcasts: the "wallet" is a test double that answers with a hash, a
rejection or an error. The point is what the server records, refuses and never repeats."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import TEST_URL  # noqa: F401
from test_trading_ledger import (  # noqa: F401  (fixtures + helpers shared with the ledger tests)
    NOW0,
    OWNER,
    U1,
    U2,
    Actor,
    LedgerRefused,
    S,
    clock,
    ledger,
    live,
    owner_engine,
    raw,
    rw_engine,
    step,
)
from walletkit import E18, TOKEN, WALLET, FakeChain, make_plan

from agora_tracker.trading.paper import TradeRefused
from agora_tracker.trading.plans import fingerprint
from agora_tracker.trading.wallet.approvals import ApprovalPlanner
from agora_tracker.trading.wallet.flow import FlowRefused, RequestStore, WalletFlow

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")

SESSION = "sess-one"
HASH = "0x" + "cd" * 32
AMT = E18 // 2


def mkflow(ledger, chain=None, **kw):
    chain = chain or FakeChain()
    return WalletFlow(ledger, ApprovalPlanner(chain, clock=ledger.clock), clock=ledger.clock, **kw)


def admit(lg, plan, key):
    return lg.create_order(user_id=U1, wallet=WALLET, plan=plan, idempotency_key=key).order.id


def sell_order(lg, key="sell-order-1"):
    plan = make_plan("sell", status="approval_required", amount_in=AMT, out=E18 * 8 // 10, uid=U1,
                     now=lg.clock())
    oid = admit(lg, plan, key)
    step(lg, oid, S.QUOTE_REVIEW, f"{key}-q")
    step(lg, oid, S.REQUIRES_APPROVAL, f"{key}-a")
    return oid, plan


def buy_order(lg, key="buy-order-1", **kw):
    plan = make_plan("buy", uid=U1, now=lg.clock(), **kw)
    oid = admit(lg, plan, key)
    step(lg, oid, S.QUOTE_REVIEW, f"{key}-q")
    step(lg, oid, S.AWAITING_WALLET, f"{key}-w")
    return oid, plan


def events(engine, oid, kind):
    return raw(engine, "SELECT detail FROM trading_private.order_events WHERE order_id = :o "
                       "AND kind = :k ORDER BY chain_no", o=oid, k=kind)


def state(lg, oid):
    return lg.get_order(U1, oid).state


def ok(_plan):
    return None


# ------------------------------------------------------------------------ approvals

def test_approval_prompt_is_gated_by_the_execution_flag(ledger, owner_engine):
    oid, plan = sell_order(ledger)
    flow = mkflow(ledger)
    with pytest.raises(FlowRefused) as e:
        flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)
    assert e.value.code == "execution_disabled"
    assert events(owner_engine, oid, "WALLET_REQUEST") == []


def test_approval_prompt_issues_one_exact_finite_request(live, owner_engine):
    oid, plan = sell_order(live)
    flow = mkflow(live)
    out = flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)
    assert out["status"] == "approve"
    req = out["request"]
    assert req["amount_wei"] == str(AMT) and req["to"] == TOKEN and req["kind"] == "approve"
    assert req["eip1193"]["method"] == "eth_sendTransaction"
    (evt,) = events(owner_engine, oid, "WALLET_REQUEST")
    assert evt[0]["leg"] == "approval" and evt[0]["digest"] == req["digest"]
    assert state(live, oid) == "requires_approval"


def test_double_click_returns_the_same_open_prompt(live, owner_engine):
    oid, plan = sell_order(live)
    flow = mkflow(live)
    first = flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)
    again = flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)
    assert again["request"]["id"] == first["request"]["id"] and again["status"] == "pending"
    assert len(events(owner_engine, oid, "WALLET_REQUEST")) == 1


def test_existing_sufficient_allowance_means_no_prompt_at_all(live, owner_engine):
    oid, plan = sell_order(live)
    out = mkflow(live, FakeChain(allowance=AMT)).approval_prompt(
        user_id=U1, session=SESSION, order_id=oid, plan=plan)
    assert out["status"] == "satisfied" and out["next"] == "requote" and "request" not in out
    assert events(owner_engine, oid, "WALLET_REQUEST") == []


def test_pause_blocks_prompts_until_resumed(live):
    oid, plan = sell_order(live)
    flow = mkflow(live)
    live.pause(Actor.owner(OWNER), "incident")
    with pytest.raises(FlowRefused) as e:
        flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)
    assert e.value.code == "paused"
    live.resume(Actor.owner(OWNER), "all clear")
    assert flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)["request"]


def test_approval_hash_is_recorded_as_an_attempt_and_changes_no_order_state(live):
    oid, plan = sell_order(live)
    flow = mkflow(live)
    rid = flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)["request"]["id"]
    out = flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="hash", tx_hash=HASH)
    assert out["status"] == "acknowledged" and out["settled"] is False
    view = live.get_order(U1, oid)
    assert view.state == "requires_approval"
    assert [(x["kind"], x["tx_hash"]) for x in view.attempts] == [("approval", HASH)]


def test_approval_rejection_leaves_the_order_open(live, owner_engine):
    oid, plan = sell_order(live)
    flow = mkflow(live)
    rid = flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)["request"]["id"]
    assert flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="rejected")["status"] == "rejected"
    assert state(live, oid) == "requires_approval"
    assert events(owner_engine, oid, "WALLET_RESULT")[0][0]["outcome"] == "rejected"


# ------------------------------------------------------------------------ swaps

def test_swap_prompt_is_the_reviewed_plan_and_requires_revalidation(live, owner_engine):
    oid, plan = buy_order(live)
    flow = mkflow(live)
    seen = []
    out = flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                           revalidate=lambda p: seen.append(p.id))
    r = out["request"]
    assert seen == [plan.id] and r["data"] == plan.transaction.calldata
    assert r["to"] == plan.transaction.target and r["value_wei"] == str(plan.amount_in_wei)
    assert r["plan_digest"] == fingerprint(plan)
    assert len(events(owner_engine, oid, "WALLET_REQUEST")) == 1


def test_failed_revalidation_issues_nothing(live, owner_engine):
    oid, plan = buy_order(live)
    flow = mkflow(live)

    def moved(_):
        raise TradeRefused("reserves changed")

    with pytest.raises(TradeRefused):
        flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan, revalidate=moved)
    assert events(owner_engine, oid, "WALLET_REQUEST") == []
    assert flow.store.open_for(oid, "swap", U1, SESSION) is None


def test_swap_hash_marks_submitted_never_filled_and_is_idempotent(live):
    oid, plan = buy_order(live)
    flow = mkflow(live)
    rid = flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                           revalidate=ok)["request"]["id"]
    out = flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="hash", tx_hash=HASH)
    assert out["status"] == "acknowledged" and out["settled"] is False
    view = live.get_order(U1, oid)
    assert view.state == "submitted" and view.attempts[0]["tx_hash"] == HASH
    assert flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="hash",
                       tx_hash=HASH)["status"] == "duplicate"
    with pytest.raises(FlowRefused) as e:
        flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="hash",
                    tx_hash="0x" + "ee" * 32)
    assert e.value.code == "conflict"
    assert len(live.get_order(U1, oid).attempts) == 1


def test_swap_rejection_cancels_with_a_reason_and_cannot_be_reprompted(live):
    oid, plan = buy_order(live)
    flow = mkflow(live)
    rid = flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                           revalidate=ok)["request"]["id"]
    flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="rejected", reason="user_rejected")
    view = live.get_order(U1, oid)
    assert view.state == "cancelled" and view.last_reason == "wallet_rejected"
    with pytest.raises(FlowRefused) as e:
        flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan, revalidate=ok)
    assert e.value.code == "invalid_state"


def test_wallet_error_is_unknown_not_failed_and_survives_restart_without_a_second_prompt(
        live, owner_engine):
    oid, plan = buy_order(live)
    flow = mkflow(live)
    rid = flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                           revalidate=ok)["request"]["id"]
    out = flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="error",
                      reason="provider disconnected\nsecret=abc <script>")
    assert out["status"] == "unknown"
    assert state(live, oid) == "awaiting_wallet"
    assert events(owner_engine, oid, "WALLET_RESULT")[0][0]["reason"] == "unspecified"
    restarted = mkflow(live)                       # new process: the in-memory prompt is gone
    with pytest.raises(FlowRefused) as e:
        restarted.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan, revalidate=ok)
    assert e.value.code == "already_requested"


def test_concurrent_prompts_produce_exactly_one_request(live, owner_engine):
    oid, plan = buy_order(live)
    flow = mkflow(live)
    gate = threading.Barrier(6)

    def go(_):
        gate.wait()
        try:
            return flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                                    revalidate=ok)["request"]["id"]
        except FlowRefused as e:
            return e.code

    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(go, range(6)))
    ids = {r for r in results if len(r) > 20}
    assert len(ids) == 1
    assert {r for r in results if len(r) <= 20} <= {"already_requested", "busy"}
    assert len(events(owner_engine, oid, "WALLET_REQUEST")) == 1


# ------------------------------------------------------------------------ binding / isolation

def test_other_users_sessions_and_plans_cannot_use_a_request(live):
    oid, plan = buy_order(live)
    flow = mkflow(live)
    rid = flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                           revalidate=ok)["request"]["id"]
    for uid, sess in ((U2, SESSION), (U1, "other-session")):
        with pytest.raises(FlowRefused) as e:
            flow.report(user_id=uid, session=sess, request_id=rid, outcome="hash", tx_hash=HASH)
        assert e.value.code == "not_found"
    with pytest.raises(FlowRefused) as e:
        flow.swap_prompt(user_id=U2, session=SESSION, order_id=oid, plan=plan, revalidate=ok)
    assert e.value.code == "not_found"
    other = make_plan("buy", uid=U1, now=live.clock(), out=4 * E18)
    with pytest.raises(FlowRefused) as e:
        flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=other, revalidate=ok)
    assert e.value.code == "plan_mismatch"


@pytest.mark.parametrize("bad", ["0x1234", "0xAB" + "cd" * 31, None, "", "0x" + "zz" * 32])
def test_malformed_hashes_are_refused_before_any_write(live, owner_engine, bad):
    oid, plan = buy_order(live)
    flow = mkflow(live)
    rid = flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                           revalidate=ok)["request"]["id"]
    with pytest.raises(FlowRefused) as e:
        flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="hash", tx_hash=bad)
    assert e.value.code == "invalid_request"
    assert events(owner_engine, oid, "WALLET_RESULT") == [] and state(live, oid) == "awaiting_wallet"


def test_expired_order_gets_no_prompt(live, clock):
    oid, plan = buy_order(live)
    clock.t = NOW0 + 10_000
    with pytest.raises((FlowRefused, LedgerRefused)):
        mkflow(live).swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan, revalidate=ok)


def test_audit_chain_stays_valid_and_never_contains_secrets(live, owner_engine):
    oid, plan = buy_order(live)
    flow = mkflow(live)
    rid = flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                           revalidate=ok)["request"]["id"]
    flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="error",
                reason="rpc https://x.example/?key=SECRETKEY123 failed")
    blob = str(raw(owner_engine, "SELECT detail::text FROM trading_private.order_events"))
    assert "SECRETKEY123" not in blob and "https:" not in blob
    assert live.verify_chain().ok


def test_store_is_bounded():
    from walletkit import NOW
    from walletkit import make_plan as mp

    from agora_tracker.trading.wallet.requests import swap_request
    store = RequestStore(lambda: NOW, limit=2)
    for i in range(2):
        store.put(swap_request(plan=mp("buy"), order_id=f"o{i}", now=NOW, ttl=60), 1, "s")
    with pytest.raises(FlowRefused) as e:
        store.put(swap_request(plan=mp("buy"), order_id="o9", now=NOW, ttl=60), 1, "s")
    assert e.value.code == "busy"
