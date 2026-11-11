"""Pure state-machine and policy rules (no database)."""

import pytest

from agora_tracker.trading.ledger import (
    ACTIVE,
    EDGES,
    TERMINAL,
    OrderState,
    PolicyError,
    RiskPolicy,
)
from agora_tracker.trading.ledger.policy import CEILINGS, HYPE

S = OrderState
TOK = "0x" + "11" * 20


def test_terminal_states_have_no_outgoing_edges():
    assert not [e for e in EDGES if e[0] in TERMINAL]


def test_every_state_is_reachable_from_created():
    seen, todo = {S.CREATED}, [S.CREATED]
    while todo:
        cur = todo.pop()
        for (a, b) in EDGES:
            if a == cur and b not in seen:
                seen.add(b)
                todo.append(b)
    assert seen == set(S)


def test_users_cannot_report_chain_outcomes():
    """Only the system may move an order to a result: no user-fabricated fills."""
    for (a, b), actors in EDGES.items():
        if b in (S.SUBMITTED, S.INCLUDED, S.REVERTED, S.FINALIZED, S.ORPHANED, S.AMBIGUOUS):
            assert actors == {"system"}, (a, b)
        assert actors <= {"user", "owner", "system"}


def test_no_edge_to_self_or_back_to_created():
    assert all(a != b and b != S.CREATED for a, b in EDGES)


def test_active_excludes_only_settled_states():
    assert set(S) - ACTIVE == {S.INCLUDED, S.REVERTED, S.FINALIZED, S.CANCELLED}


def test_policy_defaults_fail_closed_and_digest_is_stable():
    p = RiskPolicy()
    assert p.allowed_tokens == ()
    assert p.digest() == RiskPolicy.from_params(p.params()).digest()
    assert RiskPolicy.with_allowed(TOK).digest() != p.digest()


@pytest.mark.parametrize("kw", [
    {"per_trade_cap_wei": HYPE + 1},
    {"per_trade_cap_wei": 0},
    {"max_slippage_bps": CEILINGS["max_slippage_bps"] + 1},
    {"daily_notional_cap_wei": HYPE // 2},               # below per-trade cap
    {"global_daily_notional_cap_wei": HYPE},             # below per-user daily cap
    {"max_active_orders_per_user": True},                # bool is not an int
    {"order_ttl_s": 1.5},
    {"allowed_tokens": (TOK.upper(),)},
    {"allowed_tokens": ("0x1234",)},
    {"allowed_tokens": (TOK, TOK)},
])
def test_policy_rejects_unsafe_values(kw):
    with pytest.raises(PolicyError):
        RiskPolicy(**kw)


def test_policy_rejects_unknown_fields():
    with pytest.raises(PolicyError):
        RiskPolicy.from_params({"nope": 1})
