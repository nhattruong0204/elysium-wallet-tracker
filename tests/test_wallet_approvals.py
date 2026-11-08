"""Finite approval planning with a scripted chain (issue #92)."""

from dataclasses import replace

import pytest
from walletkit import E18, NOW, TOKEN, WALLET, FakeChain, make_plan

from agora_tracker.trading.v2 import ROUTER
from agora_tracker.trading.wallet import requests as R
from agora_tracker.trading.wallet.approvals import ApprovalError, ApprovalPlanner

AMT = E18 // 2


def planner(chain, **kw):
    return ApprovalPlanner(chain, clock=lambda: NOW, **kw)


def sell(**kw):
    return make_plan("sell", status="approval_required", amount_in=AMT, **kw)


def test_zero_allowance_gets_one_exact_finite_approve():
    s = planner(FakeChain()).next_step(sell(), "ord")
    assert s.action == "approve" and s.allowance_wei == 0 and s.required_wei == AMT
    r = s.request
    assert (r.kind, r.to, r.spender, r.amount_wei, r.value_wei) == ("approve", TOKEN, ROUTER, AMT, 0)
    assert R.decode_approve(r.data) == (ROUTER, AMT) and r.sender == WALLET
    assert r.gas_limit == 46_000 * 12 // 10 and r.gas_price_wei == 10**9 * 125 // 100
    assert r.max_gas_fee_wei == r.gas_limit * r.gas_price_wei
    assert r.block_number == 1000


def test_sufficient_allowance_needs_no_prompt_and_never_a_swap():
    for have in (AMT, AMT + 1):
        s = planner(FakeChain(allowance=have)).next_step(sell(), "ord")
        assert (s.action, s.request) == ("satisfied", None)


def test_preexisting_unlimited_allowance_is_flagged_not_created():
    s = planner(FakeChain(allowance=2**256 - 1)).next_step(sell(), "ord")
    assert s.action == "satisfied" and "existing_unlimited_allowance" in s.notes and s.request is None


def test_partial_allowance_direct_approve_when_token_allows_it():
    s = planner(FakeChain(allowance=AMT // 2)).next_step(sell(), "ord")
    assert s.action == "approve" and s.request.kind == "approve"


def test_usdt_style_token_gets_zero_reset_first():
    s = planner(FakeChain(allowance=AMT // 2, revert_nonzero_to_nonzero=True)).next_step(sell(), "ord")
    assert s.action == "reset_then_approve" and "zero_reset_required" in s.notes
    assert s.request.kind == "reset" and s.request.amount_wei == 0
    assert R.decode_approve(s.request.data) == (ROUTER, 0)


def test_token_that_reverts_everything_is_refused():
    for have in (0, AMT // 2):
        with pytest.raises(ApprovalError) as e:
            planner(FakeChain(allowance=have, revert_all=True)).next_step(sell(), "ord")
        assert e.value.code == "approve_reverts"


def test_void_return_tokens_are_accepted():
    assert planner(FakeChain(void_return=True)).next_step(sell(), "ord").action == "approve"


@pytest.mark.parametrize("ret", [0, 2, 2**255])
def test_nonstandard_return_values_are_refused(ret):
    with pytest.raises(ApprovalError) as e:
        planner(FakeChain(return_value=ret)).next_step(sell(), "ord")
    assert e.value.code == "nonstandard_return"


@pytest.mark.parametrize("make", [
    lambda: make_plan("buy"),                                           # not a sell
    lambda: make_plan("sell"),                                          # already executable
    lambda: replace(sell(), spender="0x" + "55" * 20),                  # spender not the router
    lambda: replace(sell(), finite_approval_wei=AMT + 1),               # approval above input
    lambda: replace(sell(), finite_approval_wei=2**256 - 1,
                    amount_in_wei=2**256 - 1),                          # unlimited
    lambda: replace(sell(), finite_approval_wei=0),
])
def test_bad_plans_never_reach_the_chain(make):
    chain = FakeChain()
    with pytest.raises(ApprovalError):
        planner(chain).next_step(make(), "ord")
    assert chain.calls == []


def test_expired_plan_is_refused():
    with pytest.raises(ApprovalError) as e:
        ApprovalPlanner(FakeChain(), clock=lambda: NOW + 301).next_step(sell(), "ord")
    assert e.value.code in ("expired", "stale_head")


@pytest.mark.parametrize("age", [31, 600, -30])
def test_stale_or_future_head_is_refused(age):
    with pytest.raises(ApprovalError) as e:
        planner(FakeChain(head_age=age)).next_step(sell(), "ord")
    assert e.value.code == "stale_head"


@pytest.mark.parametrize("which", ["router", "token"])
def test_changed_runtime_code_is_refused(which):
    chain = FakeChain()
    chain.hashes[ROUTER if which == "router" else TOKEN] = "0x" + "ff" * 32
    with pytest.raises(ApprovalError) as e:
        planner(chain).next_step(sell(), "ord")
    assert e.value.code == "code_changed"


def test_excessive_gas_is_refused_not_clamped_silently():
    with pytest.raises(ApprovalError) as e:
        planner(FakeChain(gas=R.MAX_APPROVAL_GAS + 1)).next_step(sell(), "ord")
    assert e.value.code == "gas_too_high"


def test_gas_limit_is_clamped_to_the_hard_cap():
    s = planner(FakeChain(gas=R.MAX_APPROVAL_GAS - 10)).next_step(sell(), "ord")
    assert s.request.gas_limit == R.MAX_APPROVAL_GAS


def test_request_expiry_never_outlives_the_plan():
    s = planner(FakeChain(), ttl=10_000).next_step(sell(expires_in=45), "ord")
    assert s.request.expires_at == NOW + 45


# ---------------------------------------------------------------- post-receipt allowance check

def test_confirm_requires_the_exact_requested_allowance():
    p = sell()
    assert planner(FakeChain(allowance=AMT)).confirm_allowance(p, "approve") == AMT
    assert planner(FakeChain(allowance=0)).confirm_allowance(p, "reset") == 0
    for have, kind in ((AMT + 1, "approve"), (AMT - 1, "approve"), (2**256 - 1, "approve"),
                       (AMT, "reset"), (0, "approve")):
        with pytest.raises(ApprovalError) as e:
            planner(FakeChain(allowance=have)).confirm_allowance(p, kind)
        assert e.value.code == "cap_mismatch"
