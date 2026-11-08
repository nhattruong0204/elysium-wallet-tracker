# ruff: noqa: F811
"""#95 F-16 end to end: the real application behind the real ingress, a mock Telegram session.

Identities are the public deterministic fixtures of test_trading_auth (never funded). There is no
signing endpoint, no broadcaster and no live RPC: the chain is the test double `AuthRpc`.
"""

from __future__ import annotations

import dataclasses
import socket

import pytest
import requests
from conftest import TEST_URL  # noqa: F401
from test_trading_auth import BOT, TOKEN, WALLET, AuthRpc, sign, signed_init  # noqa: F401
from test_trading_ledger import (  # noqa: F401
    clock,
    ledger,
    live,
    owner_engine,
    rw_engine,
)
from test_wallet_ingress import (
    GOOD,
    HOST,
    ORIGIN,
    SECRET,
    config,
    free_port,
)
from walletkit import FakeChain

from agora_tracker.trading.auth.service import COOKIE
from agora_tracker.trading.ingress import build_ingress
from agora_tracker.trading.journal import PaperJournal
from agora_tracker.trading.paper import PaperFlow
from agora_tracker.trading.v2 import DIRECT_ROUTE, DirectV2Provider

ENV = {"TRADING_INGRESS_SECRET": SECRET}


class World:
    """A flow with a mock Telegram user (id 1) who owns WALLET, plus a clock we control."""

    def __init__(self, tmp_path, name="priv"):
        self.clock = [100.0]
        self.rpc = AuthRpc(self.clock)
        self.flow = PaperFlow(
            {1, 2}, now=lambda: self.clock[0], routes=frozenset({DIRECT_ROUTE}),
            provider=DirectV2Provider(self.rpc, now=lambda: self.clock[0]),
            journal=PaperJournal(str(tmp_path / name / "paper.sqlite")))
        self.flow.set_wallet(1, 1, WALLET)
        self.intent = self.flow.create(1, 1, TOKEN, "buy", -100123, 55)

    def serve(self, tmp_path, port, _ledger=None, _rpc=None, **over):
        cfg = config(tmp_path, token=BOT, ingress_port=port, **over)
        server = build_ingress(cfg, flow=self.flow, ledger=_ledger, exec_rpc=_rpc,
                               environ=ENV, now=lambda: self.clock[0])
        server.start()
        return server


class Browser:
    """A Telegram WebView behind the reverse proxy: the proxy headers are always injected."""

    def __init__(self, server, intent, headers=None):
        self.base = f"http://127.0.0.1:{server.address[1]}"
        self.intent, self.cookie, self.csrf = intent, None, None
        self.extra = dict(headers or {})

    def post(self, path, *, origin=ORIGIN, proxy=True, csrf=True, cookie=True, **data):
        h = {**(GOOD if proxy else {}), "Origin": origin, **self.extra}
        if proxy is False:
            h["Host"] = HOST
        if self.cookie and cookie:
            h["Cookie"] = f"{COOKIE}={self.cookie}"
        if self.csrf and csrf:
            h["X-CSRF-Token"] = self.csrf
        r = requests.post(self.base + path, json={"intent": self.intent, **data}, headers=h, timeout=10)
        if r.ok and r.headers.get("set-cookie"):
            self.cookie = r.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        if r.ok and "csrf" in r.json():
            self.csrf = r.json()["csrf"]
        return r

    def auth(self, action, **data):
        return self.post("/api/auth/" + action, **data)

    def session(self, **kw):
        return self.post("/api/session", init_data="", wallet=WALLET, **kw)

    def login(self, uid=1):
        assert self.auth("bootstrap", init_data=signed_init(uid=uid)).status_code == 200
        q = self.auth("challenge", wallet=WALLET, chain_id=99901).json()
        r = self.auth("verify", wallet=WALLET, chain_id=99901, nonce=q["nonce"],
                      signature=sign(q["message"]))
        assert r.status_code == 200, r.text


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def served(world, tmp_path):
    port = free_port()
    server = world.serve(tmp_path, port)
    yield server
    server.stop(3)


def test_mock_telegram_session_wallet_ownership_and_unsigned_review(world, served):
    b = Browser(served, world.intent.id)
    assert b.session().status_code == 401                          # nothing without a login
    assert b.auth("bootstrap", init_data=signed_init()).status_code == 200
    ctx = b.session().json()
    assert ctx["auth"] == {"telegram_authenticated": True, "wallet_verified": False,
                           "wallet": None, "fixture": False}
    assert ctx["signing_enabled"] is False
    q = b.auth("challenge", wallet=WALLET, chain_id=99901).json()
    assert f"{HOST} wants you to sign in" in q["message"] and "Chain ID: 99901" in q["message"]
    assert b.auth("verify", wallet=WALLET, chain_id=99901, nonce=q["nonce"],
                  signature=sign(q["message"])).status_code == 200
    ctx = b.session().json()
    assert ctx["auth"]["wallet_verified"] is True and ctx["auth"]["wallet"] == WALLET
    assert ctx["signing_enabled"] is False and ctx["intent"] == world.intent.id
    # a wallet the user has not proven is never accepted for a private action
    other = "0x" + "22" * 20
    assert b.post("/api/quote", init_data="", wallet=other, amount="0.1", slippage_bps=50).status_code in (403, 409)


def test_unauthenticated_and_forged_launch_data_are_refused(world, served):
    b = Browser(served, world.intent.id)
    assert b.auth("bootstrap", init_data=signed_init(token="999999:" + "c" * 32)).status_code != 200
    assert b.auth("bootstrap", init_data="user=%7B%7D&hash=" + "0" * 64).status_code != 200
    assert b.auth("bootstrap", init_data="").status_code != 200
    assert b.session().status_code == 401


def test_per_user_authorization_is_enforced_for_intents_and_sessions(world, served):
    other = Browser(served, world.intent.id)
    assert other.auth("bootstrap", init_data=signed_init(uid=2)).status_code != 200   # not their intent
    mine = Browser(served, world.intent.id)
    mine.login()
    stranger = Browser(served, "Z" * 16)                       # a different intent id
    stranger.cookie, stranger.csrf = mine.cookie, mine.csrf
    assert stranger.session().status_code == 401               # the cookie is bound to ONE intent
    assert mine.session().status_code == 200


def test_csrf_origin_cookie_and_the_proxy_secret_are_each_required(world, served):
    b = Browser(served, world.intent.id)
    b.login()
    assert b.session().status_code == 200
    assert b.session(csrf=False).status_code in (401, 403)     # no CSRF header
    assert b.post("/api/session", init_data="", wallet=WALLET, origin="https://evil.example.test").status_code != 200
    assert b.session(cookie=False).status_code == 401           # no cookie
    # valid cookie + CSRF still need to arrive through the proxy
    assert b.post("/api/session", init_data="", wallet=WALLET, proxy=False).status_code == 404
    bad = Browser(served, world.intent.id, headers={"X-Elysium-Ingress": "wrong"})
    bad.cookie, bad.csrf = b.cookie, b.csrf
    assert bad.session().status_code == 404
    snap = served.snapshot()
    assert sum(snap["rejected"].values()) == 2 and snap["status"]["5xx"] == 0


def test_session_expiry_is_enforced_by_the_stored_clock(world, served):
    b = Browser(served, world.intent.id)
    b.login()
    assert b.session().status_code == 200
    world.clock[0] += 400                                      # past the (<=120 s) session and intent
    assert b.session().status_code == 401


def test_wallet_surface_is_absent_and_execution_stays_disabled_by_default(world, served):
    b = Browser(served, world.intent.id)
    b.login()
    assert served.snapshot()["wallet_routes"] is False
    for path in ("/wallet/api/order", "/wallet/api/prompt", "/wallet/api/report"):
        assert b.post(path, init_data="", wallet=WALLET).status_code == 404
    assert requests.get(b.base + "/wallet", headers=GOOD, timeout=10).status_code == 404


def test_restart_drops_in_memory_intents_so_old_cookies_fail_closed(world, tmp_path):
    port = free_port()
    s1 = world.serve(tmp_path, port)
    b = Browser(s1, world.intent.id)
    b.login()
    assert b.session().status_code == 200
    s1.stop(3)
    with pytest.raises(requests.ConnectionError):               # nothing listens while down
        b.session()
    reborn = World(tmp_path, name="priv2")                       # same auth store file, new memory
    reborn.clock = world.clock
    s2 = reborn.serve(tmp_path, port, paper_order_path=str(tmp_path / "priv2" / "paper.sqlite"))
    try:
        r = b.session()                                          # SQLite session survived, intent did not
        assert r.status_code == 401 and "not_authenticated" in r.text and s2.healthy()
        assert s2.snapshot()["status"]["5xx"] == 0
    finally:
        s2.stop(3)


@pytest.mark.parametrize("breakage", ["wrong_chain", "contract_wallet"])
def test_chain_dependency_failure_refuses_ownership_and_recovers(world, served, breakage):
    b = Browser(served, world.intent.id)
    assert b.auth("bootstrap", init_data=signed_init()).status_code == 200
    q = b.auth("challenge", wallet=WALLET, chain_id=99901).json()
    sig = sign(q["message"])
    if breakage == "wrong_chain":
        world.rpc.chain = 1
    else:
        world.rpc.code = "0x6001"
    r = b.auth("verify", wallet=WALLET, chain_id=99901, nonce=q["nonce"], signature=sig)
    assert 400 <= r.status_code < 500                           # refused, never a 5xx or a crash
    assert b.session().json()["auth"]["wallet_verified"] is False
    assert served.healthy() and served.snapshot()["status"]["5xx"] == 0
    world.rpc.chain, world.rpc.code = 99901, "0x"               # the dependency comes back
    b2 = Browser(served, world.intent.id)
    b2.login()
    assert b2.session().json()["auth"]["wallet_verified"] is True


def test_malformed_requests_are_refused_without_a_crash(world, served):
    b = Browser(served, world.intent.id)
    h = {**GOOD, "Origin": ORIGIN, "Content-Type": "application/json"}
    for path in ("/api/session", "/api/auth/bootstrap", "/api/auth/verify", "/api/plan"):
        for raw in (b"", b"{", b"[]", b"null", b'{"intent":"A","intent":"B"}', b"\xff\xfe\x00",
                    b'{"intent":' + b"1" * 5000 + b"}"):
            r = requests.post(b.base + path, data=raw, headers=h, timeout=10)
            assert r.status_code in (400, 401, 403, 409), (path, raw[:20], r.status_code)
    assert requests.post(b.base + "/api/session", data=b"x" * 40000, headers=h, timeout=10).status_code == 413
    assert requests.get(b.base + "/api/session", headers=GOOD, timeout=10).status_code == 405
    assert requests.post(b.base + "/nope", headers=GOOD, timeout=10).status_code == 404
    assert served.healthy() and served.snapshot()["status"]["5xx"] == 0


def test_wallet_routes_with_the_ledger_still_demand_a_proven_owner(world, tmp_path, ledger):
    port = free_port()
    server = world.serve(tmp_path, port, ledger_enabled=True, wallet_execution_enabled=True,
                         _ledger=ledger, _rpc=FakeChain())
    try:
        b = Browser(server, world.intent.id)
        fields = {"order": {"plan_id": "P" * 24}, "status": {"order_id": "nope"}}
        for name, extra in fields.items():
            assert b.post(f"/wallet/api/{name}", init_data="", wallet=WALLET, **extra).status_code == 401
        b.login()
        for name, extra in fields.items():                       # proven owner, but nothing to act on
            r = b.post(f"/wallet/api/{name}", init_data="", wallet=WALLET, **extra)
            assert 400 <= r.status_code < 500 and r.json().get("server_signs") is False
        assert server.snapshot()["status"]["5xx"] == 0
    finally:
        server.stop(3)


def test_two_listeners_cannot_share_a_port(world, tmp_path):
    port = free_port()
    s1 = world.serve(tmp_path, port)
    try:
        with pytest.raises(Exception, match="cannot bind"):
            World(tmp_path, name="priv3").serve(
                tmp_path, port, paper_order_path=str(tmp_path / "priv3" / "paper.sqlite"))
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        assert s1.healthy()
    finally:
        s1.stop(3)


def test_config_replace_keeps_ingress_off_for_other_services(tmp_path):
    c = config(tmp_path)
    off = dataclasses.replace(c, trading=dataclasses.replace(c.trading, ingress_enabled=False))
    assert build_ingress(off, flow=None, ledger=None, exec_rpc=None, environ={}) is None
