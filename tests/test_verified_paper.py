from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from eth_abi import decode
from test_paper_trading import FixtureProvider, cq, flow, intent

from agora_tracker.cards import Links
from agora_tracker.config import LinksConfig, TradingConfig
from agora_tracker.tgui import IntelBot
from agora_tracker.trading.agora import AGORA_ROUTER, AgoraV1
from agora_tracker.trading.chain import ReadRpc, SimulationReverted, call_data, units
from agora_tracker.trading.journal import PaperJournal
from agora_tracker.trading.paper import PaperFlow, TradeRefused
from agora_tracker.trading.v2 import (
    CONTRACTS,
    DIRECT_ROUTE,
    FACTORY,
    ROUTER,
    WRAPPED,
    DirectV2Provider,
    amount_out,
)

TOKEN = "0x" + "ab" * 20
WALLET = "0x" + "11" * 20
PAIR = "0x" + "22" * 20
E18 = 10**18


class RpcFixture(ReadRpc):
    def __init__(self, *, allowance=0, decimals=18, native=10 * E18):
        super().__init__("https://fixture.invalid", now=lambda: 100.0)
        self.allowance, self.decimals, self.native = allowance, decimals, native
        self.requests = []
        self.bad_hash = None
        self.bad_pair_tokens = False
        self.malformed = False
        self.revert = False
        self.transport_error = False
        self.wrong_output = False
        self.reserves = (1000 * E18, 1000 * E18)  # TOKEN sorts before WRAPPED

    def snapshot(self):
        self.requests.append(("snapshot", []))
        return "0xa", {"number": "0xa", "timestamp": "0x64", "hash": "0x" + "12" * 32}

    def check_block(self, block, expected):
        self.requests.append(("check_block", [block]))

    def code_hash(self, target, block):
        self.requests.append(("code_hash", [target, block]))
        if target == self.bad_hash:
            return "0x" + "00" * 32
        return CONTRACTS[{ROUTER: "router", FACTORY: "factory", PAIR: "pair"}[target]][
            "runtime_keccak256"
        ]

    def view(self, target, data, block, types):
        self.requests.append(("view", [target, data, block]))
        values = {
            call_data(CONTRACTS["router"]["abi"], "factory", []): (FACTORY,),
            call_data(CONTRACTS["router"]["abi"], "WETH", []): (WRAPPED,),
            call_data(CONTRACTS["factory"]["abi"], "getPair", [WRAPPED, TOKEN]): (PAIR,),
            call_data(CONTRACTS["pair"]["abi"], "token0", []): (TOKEN,),
            call_data(CONTRACTS["pair"]["abi"], "token1", []): (
                WALLET if self.bad_pair_tokens else WRAPPED,
            ),
            call_data(CONTRACTS["pair"]["abi"], "getReserves", []): (*self.reserves, 100),
            "0x313ce567": (self.decimals,),
            "0x70a08231" + WALLET[2:].zfill(64): (100 * 10**self.decimals,),
            "0xdd62ed3e" + WALLET[2:].zfill(64) + ROUTER[2:].zfill(64): (self.allowance,),
        }
        if data in values:
            return values[data]
        n, path = decode(["uint256", "address[]"], bytes.fromhex(data[10:]))
        rin, rout = self.reserves if path[0] == TOKEN else self.reserves[::-1]
        return ((n, amount_out(n, rin, rout) + int(self.wrong_output)),)

    def call(self, method, params):
        self.requests.append((method, params))
        if self.transport_error and method == "eth_call":
            raise TradeRefused("RPC transport unavailable")
        if method == "eth_getBalance":
            return hex(self.native)
        if method == "eth_gasPrice":
            return hex(10**9)
        if method == "eth_estimateGas":
            return hex(100_000)
        assert method == "eth_call"
        tx = params[0]
        if self.malformed:
            return "0xdead"
        if tx["value"] == "0x0":
            n, minimum, path, wallet, deadline = decode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                bytes.fromhex(tx["data"][10:]),
            )
            if self.allowance < n or self.revert:
                raise SimulationReverted("fixture revert")
            rin, rout = self.reserves
        else:
            minimum, path, wallet, deadline = decode(
                ["uint256", "address[]", "address", "uint256"], bytes.fromhex(tx["data"][10:])
            )
            n = int(tx["value"], 16)
            rin, rout = self.reserves[::-1]
            if self.revert:
                raise SimulationReverted("fixture revert")
        assert wallet == WALLET and deadline == 220
        out = amount_out(n, rin, rout)
        assert out >= minimum
        return "0x"


def prepared(side="buy", *, rpc=None, journal=None, value=None):
    rpc = rpc or RpcFixture()
    provider = DirectV2Provider(rpc, now=lambda: 100.0, optimize=False)
    f = PaperFlow(
        {1, 2},
        provider=provider,
        routes=frozenset({DIRECT_ROUTE}),
        now=lambda: 100.0,
        journal=journal,
    )
    f.set_wallet(1, 1, WALLET)
    i = f.create(1, 1, TOKEN, side, -100123, 55)
    f.select(1, 1, i.id, value or ("0.1" if side == "buy" else "1"))
    return f, i, rpc


@pytest.mark.parametrize("side,allowance", [("buy", 0), ("sell", 10**30), ("sell", 0)])
def test_real_provider_quotes_and_records_explicit_simulation_states(side, allowance, tmp_path):
    journal = PaperJournal(str(tmp_path / "private" / "paper.db"))
    f, i, rpc = prepared(side, rpc=RpcFixture(allowance=allowance), journal=journal)
    q = f.review(1, 1, i.id)
    conditional = side == "sell" and allowance == 0
    assert q.wallet == WALLET and q.token_decimals == 18 and q.block_number == 10
    # Published Router02 selectors enforce the recipient minimum for taxed transfers.
    assert q.transaction["data"][:10] == ("0xb6f9de95" if side == "buy" else "0x791ac947")
    assert q.simulated is (not conditional)
    assert q.simulation_status == ("approval_required" if conditional else "success")
    if conditional:
        assert q.gas_wei is None and q.gas_estimate is None
        assert q.approval_required_wei == q.amount_in_wei
        assert not any(method == "eth_estimateGas" for method, _ in rpc.requests)
    else:
        assert q.gas_wei == 120_000 * 10**9
    order = f.confirm(1, 1, i.id, f.confirmation_token(1, 1, i.id))
    assert order.status == (
        "hypothetical_requires_approval" if conditional else "hypothetical_only"
    )
    restored = PaperJournal(str(tmp_path / "private" / "paper.db"))
    assert restored.wallet(1) == WALLET and restored.wallet(2) is None
    assert restored.recent(1)[0]["snapshot"]["quote"]["simulation_status"] == q.simulation_status
    assert restored.recent(2) == []
    assert not any("send" in m.lower() or "approve" in m.lower() for m, _ in rpc.requests)
    with pytest.raises(TradeRefused):
        f.confirm(1, 1, i.id, "replay")


@pytest.mark.parametrize("bad_target", [ROUTER, FACTORY, PAIR])
def test_changed_runtime_refuses_before_simulation(bad_target):
    rpc = RpcFixture()
    rpc.bad_hash = bad_target
    f, i, _ = prepared(rpc=rpc)
    with pytest.raises(TradeRefused, match="runtime"):
        f.review(1, 1, i.id)
    assert not any(m == "eth_call" for m, _ in rpc.requests)


@pytest.mark.parametrize(
    "change", ["bad_pair_tokens", "wrong_output", "malformed", "revert", "transport_error"]
)
def test_quote_or_simulation_failure_never_creates_confirmable_review(change):
    rpc = RpcFixture()
    setattr(rpc, change, True)
    f, i, _ = prepared(rpc=rpc)
    with pytest.raises(TradeRefused):
        f.review(1, 1, i.id)
    assert f.reviewed(1, 1, i.id) is None


def test_transport_failure_is_not_mislabelled_missing_approval():
    rpc = RpcFixture()
    rpc.transport_error = True
    f, i, _ = prepared("sell", rpc=rpc)
    with pytest.raises(TradeRefused, match="transport"):
        f.review(1, 1, i.id)


@pytest.mark.parametrize("decimals", [0, 6, 18, 36])
def test_sell_percent_and_units_use_exact_verified_decimals(decimals):
    rpc = RpcFixture(decimals=decimals)
    if decimals <= 18:
        rpc.reserves = (100000 * 10**decimals, 1000 * E18)
    f, i, _ = prepared("sell", rpc=rpc, value="12.34")
    if decimals == 36:
        with pytest.raises(TradeRefused, match="impact"):
            f.review(1, 1, i.id)
        return
    q = f.review(1, 1, i.id)
    assert q.amount_in_wei == 100 * 10**decimals * 1234 // 10000
    assert units(q.amount_in_wei, decimals) == ("12" if decimals == 0 else "12.34")


def test_missing_wallet_unsupported_decimals_thin_liquidity_and_funds():
    f, i, rpc = prepared()
    f.set_wallet(1, 1, "0x" + "33" * 20)
    with pytest.raises(TradeRefused):
        f.review(1, 1, i.id)
    f, i, _ = prepared(rpc=RpcFixture(decimals=37))
    with pytest.raises(TradeRefused, match="decimals"):
        f.review(1, 1, i.id)
    rpc = RpcFixture()
    rpc.reserves = (E18, E18)
    f, i, _ = prepared(rpc=rpc)
    with pytest.raises(TradeRefused, match="liquidity"):
        f.review(1, 1, i.id)
    f, i, _ = prepared(rpc=RpcFixture(native=1))
    with pytest.raises(TradeRefused, match="balance"):
        f.review(1, 1, i.id)


def test_mutating_review_payload_refuses_confirmation():
    f, i, _ = prepared()
    f.review(1, 1, i.id)
    f._quotes[i.id][0].transaction["to"] = WALLET
    with pytest.raises(TradeRefused, match="changed"):
        f.confirm(1, 1, i.id, f.confirmation_token(1, 1, i.id))


def test_wallet_change_revokes_review_and_isolated_from_other_users():
    f, i, _ = prepared()
    f.review(1, 1, i.id)
    assert f.wallet(2, 2) is None
    f.set_wallet(2, 2, "0x" + "33" * 20)
    assert f.wallet(1, 1) == WALLET
    f.set_wallet(1, 1, "0x" + "44" * 20)
    with pytest.raises(TradeRefused):
        f.confirm(1, 1, i.id, "old")
    with pytest.raises(TradeRefused):
        f.set_wallet(1, 2, WALLET)


@pytest.mark.parametrize("action", ["pause", "cancel", "wallet", "amount"])
def test_pause_or_cancel_during_quote_cannot_publish_review(action):
    started, finish = threading.Event(), threading.Event()

    class Slow(FixtureProvider):
        def quote(self, i, slippage):
            started.set()
            assert finish.wait(2)
            return super().quote(i, slippage)

    f = flow(Slow())
    i = intent(f)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(f.review, 1, 1, i.id)
        assert started.wait(1)
        if action == "pause":
            f.paused = True
        elif action == "cancel":
            f.cancel(1, 1, i.id)
        elif action == "wallet":
            f.set_wallet(1, 1, WALLET)
        else:
            f.select(1, 1, i.id, "0.5")
        finish.set()
        with pytest.raises(TradeRefused):
            future.result()
    assert i.id not in f._quotes


def test_rpc_allowlist_envelopes_wrong_chain_head_and_budget(monkeypatch):
    rpc = ReadRpc("https://fixture.invalid", now=lambda: 100.0)
    for method in [
        "eth_sendRawTransaction",
        "eth_sendTransaction",
        "personal_sign",
        "eth_sign",
        "eth_accounts",
    ]:
        with pytest.raises(TradeRefused, match="read-only"):
            rpc.call(method, [])
    assert rpc._id == 0
    r = Mock(status_code=200)
    r.headers = {}
    r.iter_content.side_effect = lambda **kw: iter([json.dumps(r.json()).encode()])
    r.json.return_value = {"jsonrpc": "2.0", "id": True, "result": "0x1"}
    monkeypatch.setattr(rpc.session, "post", Mock(return_value=r))
    with pytest.raises(TradeRefused, match="Invalid"):
        rpc.call("eth_chainId", [])
    monkeypatch.setattr(rpc, "call", lambda method, params: "0x53b")
    with pytest.raises(TradeRefused, match="chain mismatch"):
        rpc.snapshot()
    monkeypatch.setattr(
        rpc,
        "call",
        lambda method, params: (
            hex(99901) if method == "eth_chainId" else {"timestamp": "0x1", "number": "0xa"}
        ),
    )
    with pytest.raises(TradeRefused, match="stale"):
        rpc.snapshot()


def test_private_telegram_buy_sell_review_and_conditional_confirmation(tmp_path):
    f, i, _ = prepared("sell", journal=PaperJournal(str(tmp_path / "p" / "paper.db")))
    b = IntelBot.__new__(IntelBot)
    b.cfg = SimpleNamespace(trading=TradingConfig(True), chain=SimpleNamespace(chain_id=99901))
    b.allowed, b._refused = {1, 2}, {}
    b.paper_trades = f
    b.L = Links(LinksConfig())
    b._send, b._show, b._answer = Mock(), Mock(), Mock()
    b.on_callback(cq("pr|" + i.id, chat=1, kind="private"))
    text, kb = b._show.call_args.args[2:]
    assert "CONDITIONAL PAPER" in text and "Gas estimate: unavailable" in text
    assert "No approval is requested or sent" in text
    confirm = next(
        btn["callback_data"]
        for row in kb["inline_keyboard"]
        for btn in row
        if btn.get("callback_data", "").startswith("pf|")
    )
    b.on_callback(cq(confirm, uid=2, chat=2, kind="private"))
    assert f.get(1, 1, i.id).state == "review"
    b.on_callback(cq(confirm, chat=1, kind="private"))
    assert "hypothetical_requires_approval" in b._show.call_args.args[2]
    assert f.orders(1, 1)[0]["status"] == "hypothetical_requires_approval"


def api_payload(i):
    return {
        "token": i.token,
        "side": i.side,
        "payToken": None,
        "amountIn": str(i.amount_wei),
        "amountOut": "10000",
        "minAmountOut": "9950",
        "venue": "fixture",
        "feeBps": 50,
        "priceImpact": 0.003,
        "approvals": [],
        "transaction": {
            "to": AGORA_ROUTER,
            "chainId": 99901,
            "value": str(i.amount_wei),
            "data": "0x12345678",
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        {"feeBps": True},
        {"feeBps": 101},
        {"amountIn": "1"},
        {"minAmountOut": "0"},
        {"token": WALLET},
        {"side": "sell"},
        {"priceImpact": "NaN"},
        {
            "transaction": {
                "to": WALLET,
                "chainId": 99901,
                "value": "100000000000000000",
                "data": "0x12345678",
            }
        },
        {
            "approvals": [
                {
                    "kind": "erc20",
                    "token": TOKEN,
                    "spender": AGORA_ROUTER,
                    "amount": str(2**256 - 1),
                }
            ]
        },
    ],
)
def test_agora_untrusted_builder_fields_rejected(monkeypatch, change):
    f, i, _ = prepared()
    i = f.get(1, 1, i.id)
    p = AgoraV1()
    r = Mock(status_code=200)
    r.headers = {}
    r.iter_content.side_effect = lambda **kw: iter([json.dumps(r.json()).encode()])
    r.json.return_value = {**api_payload(i), **change}
    monkeypatch.setattr(p.session, "get", Mock(return_value=r))
    with pytest.raises(TradeRefused):
        p.observation(i, i.amount_wei, 18, 50)


def test_agora_builder_is_only_a_labelled_comparison(monkeypatch):
    f, i, _ = prepared()
    i = f.get(1, 1, i.id)
    p = AgoraV1()
    r = Mock(status_code=200)
    r.headers = {}
    r.iter_content.side_effect = lambda **kw: iter([json.dumps(r.json()).encode()])
    r.json.return_value = api_payload(i)
    monkeypatch.setattr(p.session, "get", Mock(return_value=r))
    q = p.compare(i, i.amount_wei, 18, 50)
    assert q["agora_status"] == "reported_builder_unverified"
    assert not f._quotes and not f._review_tokens
    assert p.session.get.call_args.kwargs["params"]["recipient"] == WALLET
    assert not p.session.get.call_args.kwargs["allow_redirects"]


def test_rpc_budget_and_malformed_abi_returns_are_bounded(monkeypatch):
    rpc = ReadRpc("https://fixture.invalid")
    post = Mock()
    monkeypatch.setattr(rpc.session, "post", post)
    with rpc.budget(0), pytest.raises(TradeRefused, match="budget"):
        rpc.call("eth_chainId", [])
    assert not post.called
    monkeypatch.setattr(rpc, "call", lambda *a: "0xdead")
    with pytest.raises(TradeRefused, match="ABI return"):
        rpc.view(TOKEN, "0x313ce567", "0xa", ["uint8"])


def test_history_displays_private_scenarios_with_units(tmp_path):
    from agora_tracker.trading.telegram import order_history

    f, i, _ = prepared("sell", journal=PaperJournal(str(tmp_path / "p" / "paper.db")))
    f.review(1, 1, i.id)
    f.confirm(1, 1, i.id, f.confirmation_token(1, 1, i.id))
    text = order_history(f, 1, 1)
    assert TOKEN in text and "SELL" in text and "approval_required" in text
    assert "expected" in text and "no actual fills" in text
    assert TOKEN not in order_history(f, 2, 2)


def test_private_wallet_commands_and_persistent_restart(tmp_path):
    f, i, rpc = prepared(journal=PaperJournal(str(tmp_path / "p" / "paper.db")))
    b = IntelBot.__new__(IntelBot)
    b.cfg = SimpleNamespace(trading=TradingConfig(True))
    b.allowed, b._refused = {1, 2}, {}
    b.paper_trades = f
    b._send = Mock()
    msg = {
        "from": {"id": 2},
        "chat": {"id": 2, "type": "private"},
        "text": "/paper_wallet " + WALLET,
    }
    b.on_message(msg)
    assert f.wallet(2, 2) == WALLET
    msg["text"] = "/paper_balances " + TOKEN
    b.on_message(msg)
    assert "On-chain snapshot" in b._send.call_args.args[1]
    assert "not a PAPER position" in b._send.call_args.args[1]
    msg["chat"] = {"id": 1, "type": "private"}
    msg["text"] = "/paper_wallet " + "0x" + "33" * 20
    b.on_message(msg)
    assert f.wallet(1, 1) == WALLET


def test_ui_default_off_has_no_provider_or_file_io(monkeypatch):
    from agora_tracker.config import (
        AlertConfig,
        ChainConfig,
        Config,
        LeaderboardConfig,
        TelegramConfig,
    )

    cfg = Config(
        chain=ChainConfig(rpc_urls=["https://fixture.invalid"]),
        leaderboard=LeaderboardConfig(),
        alerts=AlertConfig(),
        telegram=TelegramConfig(),
    )
    ctor = Mock(side_effect=AssertionError("unexpected private file I/O"))
    monkeypatch.setattr("agora_tracker.trading.journal.PaperJournal", ctor)
    b = IntelBot(cfg, Mock(), Mock(), Mock(), None)
    try:
        assert b.paper_trades.provider is None and not b.paper_trades.routes
        assert b.paper_market is None and not ctor.called
    finally:
        b.shutdown()


def test_ui_journal_failure_pauses_only_paper(monkeypatch):
    from agora_tracker.config import (
        AlertConfig,
        ChainConfig,
        Config,
        LeaderboardConfig,
        TelegramConfig,
    )

    cfg = Config(
        chain=ChainConfig(rpc_urls=["https://fixture.invalid"]),
        leaderboard=LeaderboardConfig(),
        alerts=AlertConfig(),
        telegram=TelegramConfig(),
        trading=TradingConfig(True, "/not-used/private/paper.db"),
    )
    monkeypatch.setattr(
        "agora_tracker.trading.journal.PaperJournal", Mock(side_effect=TradeRefused("private"))
    )
    b = IntelBot(cfg, Mock(), Mock(), Mock(), None)
    try:
        assert b.paper_trades.paused and b.paper_trades.provider is None
        assert b.sessions is not None
    finally:
        b.shutdown()
