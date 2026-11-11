"""Exact V2 engine/ABI/state fixtures, not a signer or live-chain demonstration."""

from dataclasses import FrozenInstanceError, replace

import pytest
from eth_abi import decode
from test_verified_paper import E18, PAIR, TOKEN, WALLET, RpcFixture

from agora_tracker.trading.chain import ReadRpc, SimulationReverted
from agora_tracker.trading.paper import PaperFlow, TradeRefused
from agora_tracker.trading.plans import PlanBook, PlanOwner
from agora_tracker.trading.v2 import (
    ALLOWANCE,
    BALANCE,
    CONTRACTS,
    DIRECT_ROUTE,
    FACTORY,
    ROUTER,
    WRAPPED,
    DirectV2Provider,
    amount_out,
)


class PlanRpc(RpcFixture):
    def __init__(self, wallet=WALLET):
        self.wallet = wallet
        super().__init__(allowance=10**30)
        self.clock = [100.0]
        self.now = lambda: self.clock[0]
        self.head = 10
        self.headers = {10: {"number": "0xa", "timestamp": "0x64", "hash": "0x" + "12" * 32}}
        self.chain = 99901
        self.token_balance = 100 * E18
        self.gas = 100000
        self.gas_price = 10**9
        self.wallet_code = "0x"
        self.token_hash = "0x" + "34" * 32
        self.wrapped_hash = "0x" + "56" * 32
        self.delivered_bps = 10000
        self.reserves = (100000 * E18, 1000 * E18)
        self.on_call = None

    def advance(self):
        self.head += 1
        self.clock[0] += 1
        self.headers[self.head] = {
            "number": hex(self.head),
            "timestamp": hex(int(self.clock[0])),
            "hash": "0x" + f"{self.head:064x}",
        }

    def snapshot(self):
        return ReadRpc.snapshot(self)

    def check_block(self, block, expected):
        return ReadRpc.check_block(self, block, expected)

    def code_hash(self, target, block):
        if target in (TOKEN, WRAPPED):
            self.requests.append(("code_hash", [target, block]))
            return self.token_hash if target == TOKEN else self.wrapped_hash
        return super().code_hash(target, block)

    def view(self, target, data, block, types):
        from agora_tracker.trading.chain import call_data

        if target == PAIR and data == call_data(CONTRACTS["pair"]["abi"], "factory", []):
            return (FACTORY,)
        if target == TOKEN and data == BALANCE + self.wallet[2:].zfill(64):
            return (self.token_balance,)
        if target == TOKEN and data == ALLOWANCE + self.wallet[2:].zfill(64) + ROUTER[2:].zfill(64):
            return (self.allowance,)
        return super().view(target, data, block, types)

    def call(self, method, params):
        self.requests.append((method, params))
        if self.on_call:
            self.on_call(method, params)
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "eth_getBlockByNumber":
            return dict(self.headers[self.head if params[0] == "latest" else int(params[0], 16)])
        if method == "eth_getCode":
            assert params[0] == self.wallet
            return self.wallet_code
        if method == "eth_getBalance":
            return hex(self.native)
        if method == "eth_gasPrice":
            return hex(self.gas_price)
        if method == "eth_estimateGas":
            return hex(self.gas)
        assert method == "eth_call"
        tx = params[0]
        if self.transport_error:
            raise TradeRefused("Fixture transport error")
        if self.malformed:
            return "0xdead"
        if tx["value"] == "0x0":
            n, minimum, path, wallet, deadline = decode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                bytes.fromhex(tx["data"][10:]),
            )
            if self.allowance < n:
                raise SimulationReverted("Fixture missing allowance")
        else:
            minimum, path, wallet, deadline = decode(
                ["uint256", "address[]", "address", "uint256"], bytes.fromhex(tx["data"][10:])
            )
            n = int(tx["value"], 16)
        rin, rout = self.reserves if path[0] == TOKEN else self.reserves[::-1]
        delivered = amount_out(n, rin, rout) * self.delivered_bps // 10000
        if self.revert or delivered < minimum:
            raise SimulationReverted("Fixture restricted/taxed transfer")
        assert wallet == self.wallet and deadline > self.clock[0]
        return "0x"


def prepared(side="buy"):
    rpc = PlanRpc()
    provider = DirectV2Provider(rpc, now=lambda: rpc.clock[0], optimize=False)
    flow = PaperFlow(
        {1, 2}, provider=provider, routes=frozenset({DIRECT_ROUTE}), now=lambda: rpc.clock[0]
    )
    flow.set_wallet(1, 1, WALLET)
    i = flow.create(1, 1, TOKEN, side, -100123, 55)
    owner = PlanOwner(1, WALLET, "a" * 64, 220)
    return PlanBook(flow), owner, i, rpc


@pytest.mark.parametrize(
    "side,allowance,status",
    [("buy", 10**30, "success"), ("sell", 10**30, "success"), ("sell", 0, "approval_required")],
)
def test_exact_immutable_payload_and_full_revalidation(side, allowance, status):
    book, owner, i, rpc = prepared(side)
    rpc.allowance = allowance
    p, q = book.build(owner, i.id, "0.1" if side == "buy" else "50", 50)
    assert p.chain_id == 99901 and p.wallet == p.recipient == WALLET
    assert p.transaction.target == ROUTER and p.snapshot.route.factory == FACTORY
    assert p.spender == (ROUTER if side == "sell" else None)
    assert p.simulation_status == status
    assert p.transaction.calldata == q.transaction["data"]
    assert p.transaction.calldata[:10] == ("0xb6f9de95" if side == "buy" else "0x791ac947")
    assert p.finite_approval_wei == (p.amount_in_wei if status == "approval_required" else 0)
    calls = [params[0] for method, params in rpc.requests if method == "eth_call"]
    assert calls[-1] == p.transaction.rpc_payload()
    if status == "approval_required":
        assert p.max_gas_fee_wei is None and p.transaction.gas_limit is None
    else:
        assert p.transaction.gas_limit == 120000
        assert calls[-1]["gas"] == "0x1d4c0" and calls[-1]["gasPrice"] == hex(10**9)
    wire = p.public()
    assert type(wire["amount_in_wei"]) is str
    assert "session_binding" not in wire and wire["delivered_output"] == "unknown"
    assert wire["signing_enabled"] is False
    with pytest.raises(FrozenInstanceError):
        p.transaction.calldata = "0xdead"
    with pytest.raises(FrozenInstanceError):
        p.snapshot.route.pair = ROUTER
    wire["transaction_payload"]["data"] = "0xdead"
    assert p.transaction.calldata == q.transaction["data"]
    rpc.advance()
    same, evidence = book.revalidate(owner, i.id, p.id)
    assert same is p and evidence.block_number == 11 and evidence.simulation_status == status
    assert same.public()["digest"] == p.public()["digest"]
    assert not book.flow.orders(1, 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("native", 9 * E18),
        ("token_balance", 101 * E18),
        ("allowance", 1),
        ("decimals", 17),
        ("reserves", (99999 * E18, 1000 * E18)),
        ("token_hash", "0x" + "78" * 32),
        ("wrapped_hash", "0x" + "90" * 32),
        ("bad_hash", ROUTER),
        ("bad_hash", FACTORY),
        ("bad_hash", PAIR),
        ("wallet_code", "0x6000"),
        ("chain", 1),
        ("gas", 120001),
        ("gas_price", 10**9 + 1),
        ("malformed", True),
        ("revert", True),
    ],
)
def test_mutable_state_route_code_and_gas_changes_revoke(field, value):
    book, owner, i, rpc = prepared("sell")
    p, _ = book.build(owner, i.id, "25", 50)
    rpc.advance()
    setattr(rpc, field, value)
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)
    # Returning the state to its old value cannot resurrect a revoked review.
    assert book._slots[(1, i.id)][2] is None


@pytest.mark.parametrize(
    "target", ["calldata", "target", "sender", "value_wei", "min_out_wei", "chain_id", "recipient"]
)
def test_nested_memory_mutation_detected(target):
    book, owner, i, rpc = prepared()
    p, _ = book.build(owner, i.id, "0.1", 50)
    node = p.transaction if target in ("calldata", "target", "sender", "value_wei") else p
    replacement = {
        "calldata": "0xdead",
        "target": FACTORY,
        "sender": TOKEN,
        "value_wei": 1,
        "min_out_wei": 1,
        "chain_id": 1,
        "recipient": TOKEN,
    }[target]
    object.__setattr__(node, target, replacement)  # deliberate bypass of dataclass freeze
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)


def test_requote_order_changes_session_changes_expiry_and_reorg():
    book, owner, i, rpc = prepared()
    p, _ = book.build(owner, i.id, "0.1", 50)
    newer, _ = book.build(owner, i.id, "0.1", 50)
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)
    with pytest.raises(TradeRefused):
        book.revalidate(replace(owner, session_binding="b" * 64), i.id, newer.id)
    p, _ = book.build(owner, i.id, "0.1", 50)
    book.flow.select(1, 1, i.id, "0.5")
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)
    p, _ = book.build(owner, i.id, "0.1", 50)
    rpc.headers[10]["hash"] = "0x" + "99" * 32
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)
    book, owner, i, rpc = prepared()
    p, _ = book.build(owner, i.id, "0.1", 50)
    rpc.clock[0] = 130
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)


@pytest.mark.parametrize(
    "value", ["-1", "NaN", "1e9", "0", "1.000000000000000001", "0.0000000000000000001"]
)
def test_invalid_exact_amounts_refuse_and_revoke(value):
    book, owner, i, rpc = prepared()
    p, _ = book.build(owner, i.id, "0.1", 50)
    with pytest.raises(TradeRefused):
        book.build(owner, i.id, value, 50)
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)


def test_taxed_tokens_minimum_is_enforced_but_delivered_output_unknown():
    book, owner, i, rpc = prepared()
    rpc.delivered_bps = 9900
    with pytest.raises(SimulationReverted):
        book.build(owner, i.id, "0.1", 50)
    rpc.delivered_bps = 9990
    p, _ = book.build(owner, i.id, "0.1", 50)
    assert p.public()["delivered_output"] == "unknown"


def test_reorg_head_advance_or_parameters_change_during_simulation_refuse():
    for change in ("head", "amount"):
        book, owner, i, rpc = prepared()
        p, _ = book.build(owner, i.id, "0.1", 50)

        def race(method, params, rpc=rpc, change=change, book=book, i=i):
            if method == "eth_call":
                rpc.on_call = None
                if change == "head":
                    rpc.advance()
                else:
                    book.flow.select(1, 1, i.id, "0.5")

        rpc.on_call = race
        with pytest.raises(TradeRefused):
            book.revalidate(owner, i.id, p.id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("decimals", 37),
        ("decimals", -1),
        ("token_balance", None),
        ("allowance", -1),
        ("reserves", (0, 1000 * E18)),
        ("token_hash", "0x"),
    ],
)
def test_incomplete_or_nonstandard_token_state_never_yields_plan(field, value):
    book, owner, i, rpc = prepared("sell")
    setattr(rpc, field, value)
    with pytest.raises((TradeRefused, TypeError, OverflowError)):
        book.build(owner, i.id, "25", 50)


def test_wrong_abi_cannot_override_frozen_plan_encoder(monkeypatch):
    from copy import deepcopy

    import agora_tracker.trading.v2 as v2

    modified = deepcopy(v2.CONTRACTS)
    swap = next(
        f
        for f in modified["router"]["abi"]
        if f.get("name") == "swapExactETHForTokensSupportingFeeOnTransferTokens"
    )
    swap["inputs"][0]["type"] = "uint128"
    monkeypatch.setattr(v2, "CONTRACTS", modified)
    book, owner, i, rpc = prepared()
    with pytest.raises(TradeRefused, match="frozen ABI"):
        book.build(owner, i.id, "0.1", 50)


def test_unauthorized_owner_does_not_read_or_revoke_another_users_plan():
    book, owner, i, rpc = prepared()
    p, _ = book.build(owner, i.id, "0.1", 50)
    start = len(rpc.requests)
    with pytest.raises(TradeRefused):
        book.revalidate(replace(owner, user_id=2), i.id, p.id)
    assert len(rpc.requests) == start
    assert book.revalidate(owner, i.id, p.id)[0] is p


def test_stale_head_and_legacy_requote_invalidate_without_extending_expiry():
    book, owner, i, rpc = prepared()
    p, _ = book.build(owner, i.id, "0.1", 50)
    expires = p.expires_at
    rpc.clock[0] = 131
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)
    assert p.expires_at == expires
    book, owner, i, rpc = prepared()
    p, _ = book.build(owner, i.id, "0.1", 50)
    book.flow.review(1, 1, i.id)  # same parameters still produce a new PAPER review token
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)


def test_revoke_during_build_cannot_publish_a_late_plan():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    book, owner, i, rpc = prepared()
    entered, release = threading.Event(), threading.Event()

    def wait(method, params):
        if method == "eth_call":
            rpc.on_call = None
            entered.set()
            assert release.wait(5)

    rpc.on_call = wait
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(book.build, owner, i.id, "0.1", 50)
        assert entered.wait(3)
        book.revoke(1, i.id)
        release.set()
        with pytest.raises(TradeRefused):
            future.result()
    assert book._slots[(1, i.id)][2] is None


def test_plan_expiry_is_bounded_by_auth_session():
    book, owner, i, rpc = prepared()
    owner = replace(owner, expires_at=105)
    p, q = book.build(owner, i.id, "0.1", 50)
    assert p.expires_at == 105 < q.expires_at
    rpc.clock[0] = 106
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)
    rpc.clock[0] = 100
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)


@pytest.mark.parametrize("height,timestamp", [(11, 50), (11, 111), (9, 100)])
def test_stale_future_or_regressed_head_refuses_while_quote_is_not_expired(height, timestamp):
    book, owner, i, rpc = prepared()
    p, _ = book.build(owner, i.id, "0.1", 50)
    rpc.clock[0] = 110
    rpc.head = height
    rpc.headers[height] = {
        "number": hex(height),
        "timestamp": hex(timestamp),
        "hash": "0x" + "98" * 32,
    }
    assert rpc.clock[0] < p.expires_at
    with pytest.raises(TradeRefused):
        book.revalidate(owner, i.id, p.id)
    assert book._slots[(1, i.id)][2] is None
