# ruff: noqa: F811
"""Ledger-level wallet request gate and migration 0012 (issue #92)."""

from __future__ import annotations

import pytest
from conftest import TEST_URL  # noqa: F401
from test_trading_ledger import (  # noqa: F401
    NOW0,
    OWNER,
    U1,
    W1,
    Actor,
    S,
    clock,
    create,
    ledger,
    live,
    owner_engine,
    permissive,
    raw,
    refused,
    rw_engine,
    step,
    to_awaiting,
)

from agora_tracker.migrate import downgrade, upgrade

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")
D1 = "ab" * 32


def awaiting(lg, n="a", **kw):
    res, _ = create(lg, n, **kw)
    to_awaiting(lg, res.order.id, n)
    return res.order.id


def test_gate_requires_flag_and_records_one_audit_event_per_key(ledger):
    with refused("execution_disabled"):                       # the flag also guards the state itself
        awaiting(ledger, "z")
    ledger.set_execution_enabled(Actor.owner(OWNER), True, "test")
    oid = awaiting(ledger)
    ledger.set_execution_enabled(Actor.owner(OWNER), False, "kill switch")
    with refused("execution_disabled"):                       # turned off after the order exists
        ledger.authorize_wallet_request(U1, oid, kind="swap", request_key="rk-0000001", digest=D1)
    ledger.set_execution_enabled(Actor.owner(OWNER), True, "back on")
    assert ledger.authorize_wallet_request(U1, oid, kind="swap", request_key="rk-0000001", digest=D1)
    assert ledger.authorize_wallet_request(U1, oid, kind="swap", request_key="rk-0000001", digest=D1) is False


def test_gate_matches_prompt_kind_to_state(live):
    oid = awaiting(live)
    with refused("invalid_transition"):
        live.authorize_wallet_request(U1, oid, kind="approval", request_key="rk-0000002", digest=D1)
    res, _ = create(live, "b")
    with refused("invalid_transition"):                       # still `created`: nothing may prompt
        live.authorize_wallet_request(U1, res.order.id, kind="swap", request_key="rk-0000003", digest=D1)


@pytest.mark.parametrize("kw", [dict(kind="transfer"), dict(digest="xyz"), dict(digest=D1.upper()),
                                dict(request_key="short")])
def test_gate_rejects_malformed_inputs(live, kw):
    oid = awaiting(live)
    args = dict(kind="swap", request_key="rk-0000004", digest=D1) | kw
    with refused("invalid_request"):
        live.authorize_wallet_request(U1, oid, **args)


def test_gate_reapplies_current_policy_at_prompt_time(live):
    oid = awaiting(live)
    live.set_policy(Actor.owner(OWNER), permissive(per_trade_cap_wei=1))      # tightened after review
    with refused("per_trade_cap"):
        live.authorize_wallet_request(U1, oid, kind="swap", request_key="rk-0000005", digest=D1)


def test_gate_refuses_after_deadline(live, clock):
    oid = awaiting(live)
    clock.t = NOW0 + 100_000
    with refused("expired"):
        live.authorize_wallet_request(U1, oid, kind="swap", request_key="rk-0000006", digest=D1)


def test_only_one_swap_prompt_per_order_even_with_new_keys(live):
    oid = awaiting(live)
    live.authorize_wallet_request(U1, oid, kind="swap", request_key="rk-0000007", digest=D1)
    with refused("already_requested"):
        live.authorize_wallet_request(U1, oid, kind="swap", request_key="rk-0000008", digest=D1)


def test_other_user_cannot_use_the_gate_on_my_order(live):
    oid = awaiting(live)
    with refused("not_found"):
        live.authorize_wallet_request(1002, oid, kind="swap", request_key="rk-0000009", digest=D1)


def test_wallet_result_is_audit_only_and_once_per_key(live):
    oid = awaiting(live)
    assert live.record_wallet_result(U1, oid, request_key="rk-0000010", outcome="rejected")
    assert live.record_wallet_result(U1, oid, request_key="rk-0000010", outcome="rejected") is False
    assert live.get_order(U1, oid).state == "awaiting_wallet"        # no state change by itself
    with refused("invalid_request"):
        live.record_wallet_result(U1, oid, request_key="rk-0000011", outcome="filled")
    with refused("invalid_request"):
        live.record_wallet_result(U1, oid, request_key="rk-0000012", outcome="sent", tx_hash="0x12")


def test_approval_attempts_only_while_awaiting_approval(live):
    res, _ = create(live, "c")
    oid = res.order.id
    step(live, oid, S.QUOTE_REVIEW, "c1")
    with refused("invalid_transition"):
        live.record_attempt(oid, kind="approval", tx_hash="0x" + "11" * 32)
    step(live, oid, S.REQUIRES_APPROVAL, "c2")
    assert live.record_attempt(oid, kind="approval", tx_hash="0x" + "11" * 32)["recorded"]
    with refused("invalid_transition"):                              # a swap hash is not allowed here
        live.record_attempt(oid, kind="swap", tx_hash="0x" + "22" * 32)


def test_migration_0012_widens_kinds_and_round_trips(pg_engine):
    downgrade(pg_engine, "0011")
    idx = raw(pg_engine, "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'ux_events_wallet_%'")
    assert idx[0][0] == 0
    upgrade(pg_engine)
    assert raw(pg_engine, "SELECT count(*) FROM pg_indexes WHERE indexname IN "
                          "('ux_events_wallet_request', 'ux_events_wallet_result')")[0][0] == 2
    kinds = raw(pg_engine, "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                           "WHERE conname = 'order_events_kind_check'")[0][0]
    assert "WALLET_REQUEST" in kinds and "WALLET_RESULT" in kinds
