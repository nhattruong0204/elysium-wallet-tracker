"""Exact request bytes, binding and on-chain matching (issue #92). Pure: no RPC, no crypto lib."""

import pytest
from walletkit import (
    E18,
    NOW,
    OTHER,
    SEL_APPROVE,
    SEL_BUY,
    SEL_SELL,
    TOKEN,
    WALLET,
    a,
    approve_data,
    make_plan,
    swap_data,
    tamper,
    tx_of,
    w,
)

from agora_tracker.trading.v2 import ROUTER, WRAPPED
from agora_tracker.trading.wallet import requests as R


def approval(kind="approve", amount=E18 // 2, plan=None, spender=ROUTER, **kw):
    plan = plan or make_plan("sell", status="approval_required")
    base = dict(plan=plan, order_id="o" * 8, kind=kind, spender=spender, amount=amount,
                gas_limit=60_000, gas_price=10**9, now=NOW, ttl=120, block_number=1000,
                block_hash="0x" + "ab" * 32)
    base.update(kw)
    return R.approval_request(**base)


def test_selectors_are_the_published_ones():
    assert (R.SEL_APPROVE, R.SEL_BUY, R.SEL_SELL) == ("0x" + SEL_APPROVE, "0x" + SEL_BUY, "0x" + SEL_SELL)
    try:
        from eth_hash.auto import keccak
        real = {n: "0x" + keccak(s.encode())[:4].hex() for n, s in {
            "approve": "approve(address,uint256)",
            "buy": "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)",
            "sell": "swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)",
        }.items()}
    except (ImportError, NotImplementedError):
        pytest.skip("keccak unavailable here; CI re-derives the selectors")
    assert real == {"approve": R.SEL_APPROVE, "buy": R.SEL_BUY, "sell": R.SEL_SELL}


# ------------------------------------------------------------------ approve calldata

def test_approve_encoding_is_exact_and_round_trips():
    data = R.encode_approve(ROUTER, 123456789)
    assert data == approve_data(ROUTER, 123456789)
    assert R.decode_approve(data) == (ROUTER, 123456789)
    assert R.decode_approve(R.encode_approve(ROUTER, 0)) == (ROUTER, 0)


@pytest.mark.parametrize("amount", [2**256 - 1, 2**255, 2**128, -1, 1.5, True, "1"])
def test_unlimited_and_nonint_amounts_are_unencodable(amount):
    with pytest.raises(R.WalletRequestError):
        R.encode_approve(ROUTER, amount)


@pytest.mark.parametrize("spender", ["0x" + "00" * 20, ROUTER.upper().replace("0X", "0x"), "0x12", None])
def test_bad_spenders_are_unencodable(spender):
    with pytest.raises(R.WalletRequestError):
        R.encode_approve(spender, 1)


@pytest.mark.parametrize("mutate", [
    lambda d: d + "00",                                          # trailing byte
    lambda d: d[:-2],                                            # truncated
    lambda d: "0x095ea7b4" + d[10:],                             # other selector
    lambda d: d[:10] + "1" + d[11:],                             # dirty high bits of the spender word
    lambda d: d.upper().replace("0X", "0x"),                     # non-canonical case
    lambda d: "0x",
])
def test_decode_approve_is_strict(mutate):
    with pytest.raises(R.WalletRequestError):
        R.decode_approve(mutate(R.encode_approve(ROUTER, 5)))


# ------------------------------------------------------------------ swap calldata

PATH_BUY, PATH_SELL = (WRAPPED, TOKEN), (TOKEN, WRAPPED)


def test_decode_swap_buy_and_sell():
    b = swap_data("buy", amount_in=0, min_out=7, path=PATH_BUY, to=WALLET, deadline=99)
    assert R.decode_swap(b, "buy") == {"amount_in": None, "min_out": 7, "to": WALLET,
                                       "deadline": 99, "path": PATH_BUY}
    s = swap_data("sell", amount_in=11, min_out=7, path=PATH_SELL, to=WALLET, deadline=99)
    assert R.decode_swap(s, "sell")["amount_in"] == 11


@pytest.mark.parametrize("mutate", [
    lambda d: d + w(0),                                          # extra word
    lambda d: d[:-64],                                           # missing word
    lambda d: d.replace(w(0x80), w(0xA0), 1),                    # non canonical array offset
    lambda d: d.replace(w(2), w(3), 1),                          # path length 3
    lambda d: d[:10] + w(1 << 200) + d[74:],                     # dirty word where min_out is (still uint) -> allowed?
])
def test_decode_swap_rejects_noncanonical_layouts(mutate):
    good = swap_data("buy", amount_in=0, min_out=7, path=PATH_BUY, to=WALLET, deadline=99)
    bad = mutate(good)
    if bad == good or bad[:10] == good[:10] and len(bad) == len(good) and bad.count("1") and False:
        pytest.skip("mutation was a no-op")
    try:
        R.decode_swap(bad, "buy")
    except R.WalletRequestError:
        return
    # the only mutation that may decode is a different (still canonical) min_out value
    assert R.decode_swap(bad, "buy")["min_out"] != 7


def test_decode_swap_rejects_dirty_addresses_and_wrong_selector():
    good = swap_data("sell", amount_in=1, min_out=1, path=PATH_SELL, to=WALLET, deadline=9)
    dirty = good.replace(a(WALLET), "1" + a(WALLET)[1:], 1)
    for bad in (dirty, "0x" + SEL_BUY + good[10:]):
        with pytest.raises(R.WalletRequestError):
            R.decode_swap(bad, "sell")


# ------------------------------------------------------------------ approval requests

def test_approval_request_is_finite_exact_and_self_consistent():
    r = approval()
    assert (r.kind, r.to, r.sender, r.value_wei) == ("approve", TOKEN, WALLET, 0)
    assert r.spender == ROUTER and r.amount_wei == E18 // 2 and r.data == approve_data(ROUTER, E18 // 2)
    assert r.max_gas_fee_wei == 60_000 * 10**9
    pub = r.public()
    assert pub["eip1193"] == {"method": "eth_sendTransaction", "params": [r.tx_object()]}
    assert pub["eip1193"]["params"][0]["to"] == TOKEN and pub["chain_hex"] == "0x1863d"
    assert pub["digest"] == r.digest and "user_id" not in pub and "session_binding" not in pub
    assert pub["amount_wei"] == str(E18 // 2) and pub["path"] == []


def test_reset_request_is_zero_amount_only():
    r = approval("reset", amount=0)
    assert R.decode_approve(r.data) == (ROUTER, 0) and r.kind == "reset"
    with pytest.raises(R.WalletRequestError):
        approval("reset", amount=1)
    with pytest.raises(R.WalletRequestError):
        approval("approve", amount=0)


def test_approval_refuses_buy_unlimited_and_unbounded_gas():
    with pytest.raises(R.WalletRequestError):
        approval(plan=make_plan("buy"))
    with pytest.raises(R.WalletRequestError):
        approval(amount=2**256 - 1)
    with pytest.raises(R.WalletRequestError):
        approval(gas_limit=R.MAX_APPROVAL_GAS + 1)


def test_binding_digest_changes_with_every_bound_field():
    base = approval()
    from dataclasses import replace
    seen = {base.digest}
    for field, value in (("sender", OTHER), ("to", OTHER), ("data", base.data[:-1] + "1"),
                         ("chain_id", 1), ("gas_limit", 1), ("gas_price_wei", 2),
                         ("plan_digest", "0" * 64), ("order_id", "x"), ("expires_at", 1.0),
                         ("block_hash", "0x" + "00" * 32)):
        seen.add(replace(base, **{field: value}).digest)
    assert len(seen) == 11


def test_digest_is_deterministic_for_equal_content():
    from dataclasses import replace
    r = approval()
    assert replace(r).digest == r.digest and approval().digest != r.digest   # id differs per issue


def test_self_check_catches_field_drift():
    from dataclasses import replace
    r = approval()
    for bad in (replace(r, spender=OTHER), replace(r, to=OTHER), replace(r, amount_wei=1),
                replace(r, value_wei=1), replace(r, chain_id=1), replace(r, kind="swap")):
        with pytest.raises(R.WalletRequestError):
            R.check_self_consistent(bad)


# ------------------------------------------------------------------ swap requests

@pytest.mark.parametrize("side", ["buy", "sell"])
def test_swap_request_is_the_reviewed_plan_verbatim(side):
    plan = make_plan(side)
    r = R.swap_request(plan=plan, order_id="o" * 8, now=NOW, ttl=120)
    assert (r.kind, r.sender, r.to, r.data) == ("swap", WALLET, ROUTER, plan.transaction.calldata)
    assert r.value_wei == (plan.amount_in_wei if side == "buy" else 0)
    assert (r.min_out_wei, r.deadline, r.recipient, r.path) == (plan.min_out_wei, plan.deadline, WALLET, plan.path)
    assert r.expires_at <= min(NOW + 120, plan.expires_at)


def test_conditional_sell_can_never_become_a_swap_request():
    with pytest.raises(R.WalletRequestError, match="successfully simulated"):
        R.swap_request(plan=make_plan("sell", status="approval_required"), order_id="o", now=NOW, ttl=60)


@pytest.mark.parametrize("change", [
    {"sender": OTHER}, {"target": OTHER}, {"gas_limit": None}, {"gas_price_wei": None},
    {"calldata": swap_data("buy", amount_in=0, min_out=1, path=PATH_BUY, to=OTHER, deadline=int(NOW) + 600)},
])
def test_swap_request_refuses_a_tampered_plan(change):
    with pytest.raises(R.WalletRequestError):
        R.swap_request(plan=tamper(make_plan("buy"), **change), order_id="o", now=NOW, ttl=60)


def test_swap_input_must_equal_the_reviewed_amount():
    plan = make_plan("sell")
    bad = swap_data("sell", amount_in=plan.amount_in_wei + 1, min_out=plan.min_out_wei,
                    path=plan.path, to=WALLET, deadline=plan.deadline)
    with pytest.raises(R.WalletRequestError, match="input"):
        R.swap_request(plan=tamper(plan, calldata=bad), order_id="o", now=NOW, ttl=60)


# ------------------------------------------------------------------ matching the real transaction

@pytest.fixture
def req():
    return approval()


def test_identical_transaction_matches(req):
    assert R.tx_mismatches(req, tx_of(req), "0x" + "cd" * 32) == []


@pytest.mark.parametrize("change,code", [
    ({"to": "0x" + "99" * 20}, "to"),                              # altered target
    ({"input": approve_data(OTHER, E18 // 2)}, "input"),           # altered spender
    ({"input": approve_data(ROUTER, E18)}, "input"),               # altered amount
    ({"value": "0x1"}, "value"),
    ({"chainId": "0x1"}, "chain"),
    ({"hash": "0x" + "ee" * 32}, "hash"),
    ({"gas": hex(60_001)}, "gas_limit"),
    ({"gasPrice": hex(10**9 + 1)}, "fee_cap"),
    ({"maxFeePerGas": hex(10**9 + 1)}, "fee_cap"),
    ({"input": "0x"}, "input"),
])
def test_any_difference_is_reported(req, change, code):
    assert code in R.tx_mismatches(req, tx_of(req, **change), "0x" + "cd" * 32)


def test_other_sender_and_garbage_are_reported(req):
    assert "from" in R.tx_mismatches(req, tx_of(req, sender=OTHER), "0x" + "cd" * 32)
    assert R.tx_mismatches(req, None, "0x" + "cd" * 32) == ["no_transaction"]
    assert "malformed" in R.tx_mismatches(req, tx_of(req, value="banana"), "0x" + "cd" * 32)
    assert "malformed" in R.tx_mismatches(req, tx_of(req, chainId=5), "0x" + "cd" * 32)


def test_swap_transaction_matching():
    r = R.swap_request(plan=make_plan("buy"), order_id="o", now=NOW, ttl=60)
    assert R.tx_mismatches(r, tx_of(r), "0x" + "cd" * 32) == []
    assert "to" in R.tx_mismatches(r, tx_of(r, to="0x" + "77" * 20), "0x" + "cd" * 32)
