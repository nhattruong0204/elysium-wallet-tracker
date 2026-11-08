# ruff: noqa: F811
"""#95 F-16: the only production entrypoint of the private Mini App / wallet routes.

The proxy is emulated faithfully: requests go over a real loopback socket with the headers the
reverse proxy must inject (Host, X-Forwarded-Proto, the shared ingress secret). No real wallet,
no signing, no broadcasting; the Telegram API is never contacted.
"""

from __future__ import annotations

import dataclasses
import http.client
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from conftest import TEST_URL  # noqa: F401
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from test_trading_ledger import (  # noqa: F401
    clock,
    ledger,
    live,
    owner_engine,
    rw_engine,
)
from test_wallet_worker import _run_ui_harness
from walletkit import FakeChain

import agora_tracker
from agora_tracker.config import TradingConfig, load_config
from agora_tracker.trading import ingress as ing
from agora_tracker.trading.chain import ReadRpc
from agora_tracker.trading.ingress import (
    IngressGuard,
    IngressServer,
    IngressSetupError,
    IngressStats,
    build_ingress,
)
from agora_tracker.trading.journal import PaperJournal
from agora_tracker.trading.paper import PaperFlow
from agora_tracker.trading.v2 import DIRECT_ROUTE, DirectV2Provider

HOST = "app.example.test"
ORIGIN = "https://" + HOST
SECRET = "s3cret-" + "x" * 40
BOT = "123456:" + "b" * 32
GOOD = {"Host": HOST, "X-Forwarded-Proto": "https", "X-Elysium-Ingress": SECRET}
EXAMPLE = Path(agora_tracker.__file__).parent.parent / "config.example.yaml"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def tcfg(tmp_path, **over):
    kw = dict(paper_enabled=True, paper_order_path=str(tmp_path / "priv" / "paper.sqlite"),
              ingress_enabled=True, ingress_origin=ORIGIN, miniapp_url=ORIGIN + "/",
              ingress_auth_path=str(tmp_path / "authdir" / "auth.sqlite"),
              ingress_port=free_port())
    kw.update(over)
    return TradingConfig(**kw)


def config(tmp_path, token=BOT, **over):
    base = load_config(EXAMPLE)
    return dataclasses.replace(base, telegram=dataclasses.replace(base.telegram, bot_token=token),
                               trading=tcfg(tmp_path, **over))


def mkflow(tmp_path, *, provider=True, journal=True):
    jr = PaperJournal(str(tmp_path / "priv" / "paper.sqlite")) if journal else None
    prov = DirectV2Provider(ReadRpc("https://rpc.invalid")) if provider else None
    return PaperFlow({1, 2}, provider=prov, journal=jr,
                     routes=frozenset({DIRECT_ROUTE}) if prov else frozenset())


ENV = {"TRADING_INGRESS_SECRET": SECRET}


# ------------------------------------------------------------------ the guard + server alone
class Counter:
    calls = 0


def fake_app():
    import anyio
    import anyio.to_thread

    async def index(request):
        Counter.calls += 1
        return PlainTextResponse("ok")

    async def slow(request):
        Counter.calls += 1
        await anyio.sleep(float(request.query_params.get("s", "3")))
        return PlainTextResponse("late")

    async def threads(request):
        return JSONResponse({"tokens": anyio.to_thread.current_default_thread_limiter().total_tokens})

    async def echo(request):
        Counter.calls += 1
        return JSONResponse({"n": len(await request.body())})

    return Starlette(routes=[Route("/", index), Route("/slow", slow), Route("/threads", threads),
                             Route("/echo", echo, methods=["POST"])])


@pytest.fixture
def served():
    made = []

    def make(**over):
        Counter.calls = 0
        t = TradingConfig(**{"ingress_request_timeout_s": 20, **over})
        stats = IngressStats()
        guard = IngressGuard(fake_app(), host=HOST, secret=SECRET, max_body=t.ingress_max_body_bytes,
                             timeout_s=t.ingress_request_timeout_s, threads=t.ingress_threads,
                             stats=stats)
        sock = ing._bind_loopback("127.0.0.1", 0)
        server = IngressServer(guard, sock, origin=ORIGIN, cfg=SimpleNamespace(trading=t),
                               stats=stats, wallet_routes=False)
        server.start()
        made.append(server)
        return server, f"http://127.0.0.1:{server.address[1]}", stats

    yield make
    for s in made:
        s.stop(3)


def get(base, path="/", headers=GOOD, **kw):
    return requests.get(base + path, headers=headers, timeout=10, **kw)


def test_request_through_the_proxy_reaches_the_app_with_security_headers(served):
    server, base, stats = served()
    r = get(base)
    assert r.status_code == 200 and r.text == "ok"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert r.headers["x-content-type-options"] == "nosniff" and "server" not in r.headers
    assert server.healthy() and server.address[0] == "127.0.0.1"


@pytest.mark.parametrize("headers,reason", [
    ({k: v for k, v in GOOD.items() if k != "X-Elysium-Ingress"}, "secret"),
    ({**GOOD, "X-Elysium-Ingress": SECRET + "x"}, "secret"),
    ({**GOOD, "X-Elysium-Ingress": SECRET[:-1]}, "secret"),
    ({**GOOD, "X-Elysium-Ingress": ""}, "secret"),
    ({**GOOD, "Host": "evil.example.test"}, "host"),
    ({**GOOD, "Host": HOST + ":8765"}, "host"),
    ({k: v for k, v in GOOD.items() if k != "X-Forwarded-Proto"}, "scheme"),
    ({**GOOD, "X-Forwarded-Proto": "http"}, "scheme"),
])
def test_anything_not_forwarded_by_the_configured_proxy_never_reaches_the_app(served, headers, reason):
    server, base, stats = served()
    r = get(base, headers=headers)
    assert r.status_code == 404 and r.content == b""        # nothing to learn from the refusal
    assert Counter.calls == 0 and stats.snapshot()["rejected"] == {reason: 1}
    assert SECRET not in r.text and server.healthy()


def test_duplicate_secret_header_is_refused(served):
    server, base, stats = served()
    c = http.client.HTTPConnection("127.0.0.1", server.address[1], timeout=10)
    c.putrequest("GET", "/", skip_host=True)
    for k, v in (("Host", HOST), ("X-Forwarded-Proto", "https"), ("X-Elysium-Ingress", SECRET),
                 ("X-Elysium-Ingress", SECRET)):
        c.putheader(k, v)
    c.endheaders()
    assert c.getresponse().status == 404 and Counter.calls == 0
    c.close()


def test_duplicate_host_header_is_refused(served):
    server, base, _ = served()
    c = http.client.HTTPConnection("127.0.0.1", server.address[1], timeout=10)
    c.putrequest("GET", "/", skip_host=True)
    for k, v in (("Host", HOST), ("Host", HOST), ("X-Forwarded-Proto", "https"),
                 ("X-Elysium-Ingress", SECRET)):
        c.putheader(k, v)
    c.endheaders()
    assert c.getresponse().status in (400, 404) and Counter.calls == 0   # h11 or the guard
    c.close()


def test_methods_body_size_and_malformed_length_are_bounded_before_the_app(served):
    server, base, stats = served(ingress_max_body_bytes=2048)
    assert requests.put(base + "/echo", headers=GOOD, data=b"x", timeout=10).status_code == 405
    assert requests.delete(base + "/", headers=GOOD, timeout=10).status_code == 405
    assert requests.post(base + "/echo", headers=GOOD, data=b"x" * 2049, timeout=10).status_code == 413
    assert Counter.calls == 0
    assert requests.post(base + "/echo", headers=GOOD, data=b"x" * 2048, timeout=10).json() == {"n": 2048}
    c = http.client.HTTPConnection("127.0.0.1", server.address[1], timeout=10)
    c.putrequest("POST", "/echo", skip_host=True)
    for k, v in {**GOOD, "Content-Length": "12abc"}.items():
        c.putheader(k, v)
    c.endheaders()
    assert c.getresponse().status in (400, 404)                # uvicorn or the guard refuses it
    c.close()
    assert stats.snapshot()["rejected"].get("method") == 2 and server.healthy()


def test_request_timeout_returns_504_and_frees_the_slot(served):
    server, base, stats = served(ingress_request_timeout_s=1)
    t0 = time.monotonic()
    r = get(base, "/slow?s=5")
    assert r.status_code == 504 and time.monotonic() - t0 < 4
    snap = stats.snapshot()
    assert snap["timeouts"] == 1 and snap["in_flight"] == 0
    assert get(base).status_code == 200                        # still serving


def test_blocking_work_is_capped_to_the_configured_thread_pool(served):
    _, base, _ = served(ingress_threads=3)
    assert get(base, "/threads").json() == {"tokens": 3}


def test_connection_cap_sheds_load_instead_of_queueing_forever(served):
    # uvicorn counts open connections INCLUDING the one asking, so a cap of 3 serves 2 at once
    server, base, stats = served(ingress_max_concurrency=3)
    with ThreadPoolExecutor(2) as pool:
        busy = [pool.submit(lambda: get(base, "/slow?s=1.5").status_code) for _ in range(2)]
        for _ in range(100):                                    # both slots are now occupied
            if stats.snapshot()["in_flight"] == 2:
                break
            time.sleep(0.02)
        assert get(base).status_code == 503                      # shed, not queued
        assert [f.result() for f in busy] == [200, 200]
    assert get(base).status_code == 200 and server.healthy()


def test_only_loopback_proxy_headers_are_trusted_and_no_access_log(served):
    server, _, _ = served()
    c = server._server.config
    assert c.proxy_headers is True and c.forwarded_allow_ips == "127.0.0.1"
    assert c.access_log is False and c.server_header is False and c.ws == "none"
    assert c.limit_concurrency == 32 and c.h11_max_incomplete_event_size == 16384


def test_websocket_upgrades_are_closed(served):
    import anyio

    async def go():
        sent = []
        guard = IngressGuard(fake_app(), host=HOST, secret=SECRET, max_body=1, timeout_s=1,
                             threads=1, stats=IngressStats())

        async def receive():
            return {"type": "websocket.connect"}

        async def send(m):
            sent.append(m)
        await guard({"type": "websocket", "headers": []}, receive, send)
        return sent
    assert anyio.run(go) == [{"type": "websocket.close", "code": 1008}]


def test_stop_releases_the_port_and_a_restart_binds_again(served):
    server, base, _ = served()
    port = server.address[1]
    server.stop(3)
    assert not server.healthy() and server.snapshot()["state"] == "stopped"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    sock = ing._bind_loopback("127.0.0.1", port)                # the port is free again
    sock.close()


def test_bind_failure_is_fatal_and_leaks_nothing():
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    before = {t.name for t in threading.enumerate()}
    with pytest.raises(IngressSetupError, match="cannot bind"):
        ing._bind_loopback("127.0.0.1", taken.getsockname()[1])
    taken.close()
    assert {t.name for t in threading.enumerate()} == before


def test_health_snapshot_and_render_never_contain_the_secret(served):
    server, base, _ = served()
    get(base)
    get(base, headers={**GOOD, "X-Elysium-Ingress": "bad"})
    snap = server.snapshot()
    text = ing.render_text(snap)
    assert snap["state"] == "running" and snap["requests"] == 1 and snap["rejected"] == {"secret": 1}
    assert SECRET not in repr(snap) and SECRET not in text and "wallet routes: not mounted" in text


# ------------------------------------------------------------------ config
def test_default_config_has_no_listener_and_validation_is_strict(tmp_path):
    t = TradingConfig()
    assert t.ingress_enabled is False and t.wallet_execution_enabled is False
    assert t.ingress_bind_host == "127.0.0.1"
    bad = [dict(ingress_bind_host="0.0.0.0"), dict(ingress_bind_host="::"), dict(ingress_bind_host="10.0.0.5"),
           dict(ingress_bind_host="localhost"), dict(ingress_port=80), dict(ingress_port=70000),
           dict(ingress_origin="http://a.example"), dict(ingress_origin="https://A.example"),
           dict(ingress_origin="https://a.example/path"), dict(ingress_origin="https://a.example:443?x=1"),
           dict(ingress_threads=0), dict(ingress_threads=64), dict(ingress_max_concurrency=0),
           dict(ingress_max_body_bytes=10), dict(ingress_request_timeout_s=0),
           dict(ingress_secret_env="lower"), dict(ingress_enabled="yes")]
    for kw in bad:
        with pytest.raises(ValueError):
            TradingConfig(**kw)


@pytest.mark.parametrize("drop", ["ingress_origin", "paper_order_path", "ingress_auth_path", "miniapp_url"])
def test_enabling_without_a_required_setting_is_a_config_error(tmp_path, drop):
    kw = dict(paper_enabled=True, paper_order_path="/x/p.sqlite", ingress_enabled=True,
              ingress_origin=ORIGIN, miniapp_url=ORIGIN + "/", ingress_auth_path="/x/a.sqlite")
    kw[drop] = None
    with pytest.raises(ValueError):
        TradingConfig(**kw)


def test_the_telegram_launcher_must_point_at_the_listener_origin(tmp_path):
    with pytest.raises(ValueError, match="miniapp_url"):
        tcfg(tmp_path, miniapp_url="https://elsewhere.example.test/")
    with pytest.raises(ValueError, match="miniapp_url"):
        tcfg(tmp_path, miniapp_url=ORIGIN + "/deep/path")
    tcfg(tmp_path, miniapp_url=ORIGIN)


def test_enabling_the_listener_does_not_enable_wallet_execution(tmp_path):
    t = tcfg(tmp_path)
    assert t.ingress_enabled and not t.wallet_execution_enabled and not t.ledger_enabled


# ------------------------------------------------------------------ build_ingress: fail closed
@pytest.fixture
def no_bind(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("a socket must not be bound before every requirement is met")
    monkeypatch.setattr(ing, "_bind_loopback", boom)
    return calls


def test_disabled_by_default_builds_nothing(tmp_path, no_bind):
    c = config(tmp_path)
    c = dataclasses.replace(c, trading=TradingConfig())
    assert build_ingress(c, flow=None, ledger=None, exec_rpc=None, environ={}) is None
    assert no_bind == []


@pytest.mark.parametrize("what", [
    "no_token", "bad_token", "no_flow", "no_journal", "no_provider", "no_secret", "short_secret",
    "space_secret", "newline_secret", "bad_auth_dir", "wallet_no_ledger", "wallet_no_rpc",
])
def test_every_missing_dependency_is_fatal_before_a_socket_exists(tmp_path, no_bind, what):
    over, env, flow_kw, flow_none, ledger_, rpc = {}, dict(ENV), {}, False, None, None
    token = BOT
    if what == "no_token":
        token = ""
    elif what == "bad_token":
        token = "not-a-token"
    elif what == "no_flow":
        flow_none = True
    elif what == "no_journal":
        flow_kw["journal"] = False
    elif what == "no_provider":
        flow_kw["provider"] = False
    elif what == "no_secret":
        env = {}
    elif what == "short_secret":
        env = {"TRADING_INGRESS_SECRET": "x" * 31}
    elif what == "space_secret":
        env = {"TRADING_INGRESS_SECRET": "a" * 20 + " " + "b" * 20}
    elif what == "newline_secret":
        env = {"TRADING_INGRESS_SECRET": "a" * 40 + "\n"}
    elif what == "bad_auth_dir":
        d = tmp_path / "wide"
        d.mkdir()
        d.chmod(0o755)
        over["ingress_auth_path"] = str(d / "auth.sqlite")
    elif what in ("wallet_no_ledger", "wallet_no_rpc"):
        over.update(ledger_enabled=True, wallet_execution_enabled=True)
        rpc = None if what == "wallet_no_rpc" else FakeChain()
    c = config(tmp_path, token=token, **over)
    flow = None if flow_none else mkflow(tmp_path, **flow_kw)
    with pytest.raises(IngressSetupError) as e:
        build_ingress(c, flow=flow, ledger=ledger_, exec_rpc=rpc, environ=env)
    assert SECRET not in str(e.value) and no_bind == []


def test_occupied_port_is_fatal(tmp_path):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    c = config(tmp_path, ingress_port=taken.getsockname()[1])
    try:
        with pytest.raises(IngressSetupError, match="cannot bind"):
            build_ingress(c, flow=mkflow(tmp_path), ledger=None, exec_rpc=None, environ=ENV)
    finally:
        taken.close()


def started(tmp_path, **kw):
    c = config(tmp_path, **kw.pop("over", {}))
    s = build_ingress(c, flow=kw.pop("flow", None) or mkflow(tmp_path), ledger=kw.pop("ledger", None),
                      exec_rpc=kw.pop("exec_rpc", None), environ=ENV)
    s.start()
    return s, f"http://127.0.0.1:{s.address[1]}"


def test_real_app_is_served_only_behind_the_proxy_and_private_api_denies_by_default(tmp_path):
    s, base = started(tmp_path)
    try:
        assert get(base, headers={"Host": HOST}).status_code == 404       # bypassing the proxy
        idx = get(base)
        assert idx.status_code == 200 and 'data-auth-mode="telegram-siwe-v1"' in idx.text
        assert "frame-ancestors" in idx.headers["content-security-policy"]
        body = {"intent": "A" * 16, "init_data": "", "wallet": None}
        r = requests.post(base + "/api/session", json=body, headers={**GOOD, "Origin": ORIGIN}, timeout=10)
        assert r.status_code == 401 and r.json()["error"] == "not_authenticated"
        # the wallet surface is not mounted unless wallet execution is also configured
        assert s.snapshot()["wallet_routes"] is False
        assert get(base, "/wallet").status_code == 404
        assert requests.post(base + "/wallet/api/order", json=body, headers=GOOD, timeout=10).status_code == 404
        # malformed requests are refused, never a 500 and never a crash
        for raw in (b"{", b"[]", b'{"intent":"A","intent":"B"}', b"\xff\xfe", b""):
            r = requests.post(base + "/api/session", data=raw, headers={**GOOD, "Origin": ORIGIN}, timeout=10)
            assert r.status_code in (401, 409), (raw, r.status_code)
        big = requests.post(base + "/api/session", data=b"x" * 20000, headers=GOOD, timeout=10)
        assert big.status_code == 413
        assert s.healthy()
    finally:
        s.stop(3)


def test_wallet_routes_are_mounted_only_with_the_second_opt_in_and_still_need_auth(tmp_path, live):
    over = dict(ledger_enabled=True, wallet_execution_enabled=True)
    s, base = started(tmp_path, over=over, ledger=live, exec_rpc=FakeChain())
    try:
        assert s.snapshot()["wallet_routes"] is True
        assert get(base, "/wallet").status_code == 200
        base_body = {"intent": "A" * 16, "init_data": "", "wallet": "0x" + "11" * 20}
        fields = {"order": {"plan_id": "P" * 24}, "prompt": {"order_id": "o", "plan_id": "P" * 24},
                  "report": {"request_id": "r", "outcome": "rejected", "tx_hash": None, "reason": None},
                  "status": {"order_id": "o"}, "cancel": {"order_id": "o"}}
        for name, extra in fields.items():             # well-formed, but nobody is authenticated
            r = requests.post(base + f"/wallet/api/{name}", json={**base_body, **extra},
                              headers={**GOOD, "Origin": ORIGIN}, timeout=10)
            assert r.status_code == 401, (name, r.status_code)
        assert live.engine is not None
    finally:
        s.stop(3)


# ------------------------------------------------------------------ run_ui lifecycle
def _bot_factory(flow, seen):
    class Bot:
        worker = None
        ingress = None

        def __init__(self, *a, **k):
            self.paper_trades = flow
            seen["bot"] = self

        def run(self, stop):
            seen["runs"] += 1                      # == number of getUpdates consumers started
            seen["during"] = None
            if self.ingress is not None:
                port = self.ingress.address[1]
                seen["during"] = (self.ingress.healthy(),
                                  requests.get(f"http://127.0.0.1:{port}/", headers=GOOD, timeout=5).status_code,
                                  [t.name for t in threading.enumerate() if t.name == "ingress"])
            time.sleep(0.1)

        def shutdown(self):
            seen["shutdown"] = True
    return Bot


def _ui(monkeypatch, tmp_path, *, trading=None, env=None, flow=True):
    import agora_tracker.tgui as tgui
    over = dict(paper_enabled=True, paper_order_path=str(tmp_path / "priv" / "paper.sqlite"),
                ingress_enabled=True, ingress_origin=ORIGIN, miniapp_url=ORIGIN + "/",
                ingress_auth_path=str(tmp_path / "authdir" / "auth.sqlite"), ingress_port=free_port())
    over.update(trading or {})
    main, c, seen = _run_ui_harness(monkeypatch, trading=over, env={**ENV, **(env or {})})
    c = dataclasses.replace(c, telegram=dataclasses.replace(c.telegram, bot_token=BOT))
    monkeypatch.setattr(tgui, "IntelBot", _bot_factory(mkflow(tmp_path) if flow else None, seen))
    return main, c, seen, over["ingress_port"]


def test_run_ui_starts_no_listener_by_default(monkeypatch, tmp_path):
    main, c, seen, _ = _ui(monkeypatch, tmp_path, trading={"ingress_enabled": False, "miniapp_url": None,
                                                           "ingress_origin": None, "ingress_auth_path": None})
    main.run_ui(c)
    assert seen["runs"] == 1 and seen["bot"].ingress is None
    assert not [t for t in threading.enumerate() if t.name == "ingress"]


def test_run_ui_serves_beside_the_single_update_consumer_and_shuts_down_cleanly(monkeypatch, tmp_path):
    main, c, seen, port = _ui(monkeypatch, tmp_path)
    main.run_ui(c)
    assert seen["runs"] == 1                                      # exactly one getUpdates consumer
    assert seen["during"] == (True, 200, ["ingress"])             # served while polling
    assert seen["shutdown"] is True and seen["bot"].ingress.healthy() is False
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)  # the listener is gone
    assert not [t for t in threading.enumerate() if t.name == "ingress"]


def test_run_ui_restart_rebinds_the_same_port_and_never_double_polls(monkeypatch, tmp_path):
    main, c, seen, port = _ui(monkeypatch, tmp_path)
    main.run_ui(c)
    main.run_ui(c)
    assert seen["runs"] == 2 and seen["during"][:2] == (True, 200)


@pytest.mark.parametrize("trading,env,flow,match", [
    ({}, {"TRADING_INGRESS_SECRET": ""}, True, "ingress misconfigured"),
    ({}, {"TRADING_INGRESS_SECRET": "short"}, True, "ingress misconfigured"),
    ({}, {}, False, "ingress misconfigured"),
])
def test_run_ui_misconfigured_ingress_is_fatal_before_polling_starts(monkeypatch, tmp_path, trading, env, flow, match):
    main, c, seen, _ = _ui(monkeypatch, tmp_path, trading=trading, env=env, flow=flow)
    with pytest.raises(SystemExit, match=match):
        main.run_ui(c)
    assert seen["runs"] == 0
    assert not [t for t in threading.enumerate() if t.name == "ingress"]


def test_run_ui_port_in_use_is_fatal_before_polling_starts(monkeypatch, tmp_path):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    main, c, seen, _ = _ui(monkeypatch, tmp_path, trading={"ingress_port": taken.getsockname()[1]})
    try:
        with pytest.raises(SystemExit, match="cannot bind"):
            main.run_ui(c)
    finally:
        taken.close()
    assert seen["runs"] == 0


def test_run_ui_wallet_stack_failure_precedes_any_listener(monkeypatch, tmp_path):
    main, c, seen, port = _ui(monkeypatch, tmp_path,
                              trading={"ledger_enabled": True, "wallet_execution_enabled": True})
    with pytest.raises(SystemExit, match="worker misconfigured"):      # no TRADING_EXEC_RPC_URL
        main.run_ui(c)
    assert seen["runs"] == 0
    sock = ing._bind_loopback("127.0.0.1", port)                        # nothing was ever bound
    sock.close()


def test_run_ui_with_wallet_execution_mounts_wallet_routes_on_the_same_listener(monkeypatch, tmp_path):
    main, c, seen, _ = _ui(monkeypatch, tmp_path,
                           trading={"ledger_enabled": True, "wallet_execution_enabled": True},
                           env={"TRADING_EXEC_RPC_URL": "https://exec.example/KEY"})
    main.run_ui(c)
    assert seen["runs"] == 1 and seen["bot"].worker is not None
    snap = seen["bot"].ingress.snapshot()
    assert snap["wallet_routes"] is True and snap["state"] == "stopped"


# ------------------------------------------------------------------ one getUpdates consumer
def test_ingress_never_talks_to_telegram_and_getupdates_has_a_single_owner():
    root = Path(agora_tracker.__file__).parent
    owners = sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                    if re.search(r"""["']getUpdates["']""", p.read_text()))
    assert owners == ["tgui.py"]
    src = (root / "trading" / "ingress.py").read_text()
    assert not re.search(r"TelegramApi|IntelBot|setWebhook|\.call\(", src)


def test_wallet_health_is_owner_only_and_private():
    import agora_tracker.tgui as tgui
    sent = []

    class B:
        ledger = None
        worker = None
        cfg = load_config(EXAMPLE)
        ingress = SimpleNamespace(snapshot=lambda: {
            "state": "running", "thread_alive": True, "origin": ORIGIN, "bind": "127.0.0.1:1",
            "wallet_routes": False, "error": None, "requests": 0, "in_flight": 0, "max_in_flight": 0,
            "rejected": {}, "status": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}, "timeouts": 0})

        def _send(self, chat, text, kb):
            sent.append((chat, text))
    b = B()
    b.cfg = dataclasses.replace(b.cfg, telegram=dataclasses.replace(b.cfg.telegram, chat_id="7"))
    private = {"chat": {"type": "private"}}
    tgui.IntelBot._worker_health(b, 7, 7, private)               # owner, private chat
    tgui.IntelBot._worker_health(b, 8, 8, private)               # not an owner
    tgui.IntelBot._worker_health(b, -5, 7, {"chat": {"type": "group"}})   # owner, but in a group
    assert "Mini App ingress" in sent[0][1] and "running" in sent[0][1]
    assert sent[1][1] == "Not available." and sent[2][1] == "Not available."
