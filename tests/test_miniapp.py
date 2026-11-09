"""C1 only: actual PAPER provider, explicit mock identity, no signing backend."""
import subprocess
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
import uvicorn
from test_verified_paper import E18, TOKEN, WALLET, RpcFixture, prepared

from agora_tracker.cards import Links
from agora_tracker.config import LinksConfig, TradingConfig
from agora_tracker.trading.miniapp import Principal, create_app
from agora_tracker.trading.telegram import callback, panel


class MockAuth:
    async def authenticate(self, request, init_data, claimed_wallet):
        if init_data == "fixture-user-1":
            return Principal(1, WALLET if claimed_wallet == WALLET else None, fixture=True)
        if init_data == "fixture-user-2":
            return Principal(2, fixture=True)
        return None


@pytest.fixture
def preview(tmp_path):
    started = []

    def factory(side="buy", auth=True, tls=False):
        rpc = RpcFixture(allowance=10**30)
        rpc.reserves = (100000 * E18, 1000 * E18)
        flow, intent, _ = prepared(side, rpc=rpc)
        app = create_app(flow, MockAuth() if auth else None)
        tls_options = {}
        if tls:
            cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1",
                            "-subj", "/CN=localhost"], check=True, capture_output=True)
            tls_options = {"ssl_keyfile": str(key), "ssl_certfile": str(cert)}
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                               log_level="warning", lifespan="off", **tls_options))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        assert server.started
        scheme = "https" if tls else "http"
        base = f"{scheme}://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
        started.append((server, thread))
        return SimpleNamespace(base=base, flow=flow, intent=intent, rpc=rpc)

    yield factory
    for server, thread in started:
        server.should_exit = True
        thread.join(5)


def post(p, action="session", **changes):
    body = dict(intent=p.intent.id, init_data="fixture-user-1", wallet=WALLET,
                amount="0.1", slippage_bps=50)
    body.update(changes)
    return requests.post(p.base + "/api/" + action, json=body, timeout=10)


def test_default_deny_and_no_execution_endpoints(preview):
    p = preview(auth=False)
    assert post(p).status_code == 401
    for action in ("send", "sign", "confirm", "approve", "broadcast"):
        assert post(p, action).status_code == 404
    response = requests.get(p.base, timeout=5)
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]


def test_bound_context_auth_separation_and_request_limits(preview):
    p = preview()
    data = post(p, token="forged", chain_id=1, source_message=999).json()
    assert (data["token"], data["chain_id"], data["source_message"]) == (TOKEN, 99901, 55)
    assert post(p, init_data="forged").status_code == 401
    assert post(p, init_data="fixture-user-2").status_code == 409
    assert post(p, "quote", wallet="0x" + "22"*20).status_code == 403
    assert requests.post(p.base+"/api/session", json={"x": "x"*9000}, timeout=5).status_code == 409
    assert requests.post(p.base+"/api/session", json={}, headers={"Origin": "https://evil.invalid"}, timeout=5).status_code == 409
    assert post(p, "quote", slippage_bps=True).status_code == 409
    assert post(p, "quote", amount="1e18").status_code == 409


@pytest.mark.parametrize("side,amount", [("buy", "0.1"), ("buy", "0.5"), ("buy", "1"),
                                        ("buy", "0.123"), ("sell", "25"), ("sell", "50"),
                                        ("sell", "100"), ("sell", "33.33")])
def test_actual_provider_unsigned_preview(preview, side, amount):
    p = preview(side)
    response = post(p, "quote", amount=amount)
    assert response.status_code == 200, response.text
    data = response.json()
    q = data["quote"]
    assert data["signing_enabled"] is False
    assert q["transaction"]["from"] == WALLET
    assert q["transaction"]["data"].startswith("0xb6f9de95" if side == "buy" else "0x791ac947")
    assert type(q["amount_in_wei"]) is str
    if side == "sell":
        assert int(q["amount_in_wei"]) == int(Decimal(amount)*100)*100*E18//10000
    assert not p.flow.orders(1, 1)


def test_expiry_cancel_unsupported(preview):
    p = preview()
    p.flow.now = lambda: 281.
    assert post(p).status_code == 409
    p = preview()
    assert post(p, "cancel").json()["cancelled"]
    assert post(p, "quote").status_code == 409
    p = preview()
    p.flow.provider = None
    assert post(p, "quote").json()["error"] == "unsupported_route"


def test_private_alert_launcher_is_opt_in_and_opaque(preview):
    p = preview()
    bot = SimpleNamespace(cfg=SimpleNamespace(trading=TradingConfig(paper_enabled=True,
        miniapp_url="https://preview.example.com/"), chain=SimpleNamespace(chain_id=99901)),
        paper_trades=p.flow, L=Links(LinksConfig()), _send=Mock(), _answer=Mock())
    callback(bot, {"id": "alert", "from": {"id": 1}, "message": {
        "chat": {"id": -100123, "type": "supergroup"}, "message_id": 55},
        "data": "tp|b|"+TOKEN})
    uid, _, markup = bot._send.call_args.args
    assert uid == 1
    url = markup["inline_keyboard"][-1][0]["web_app"]["url"]
    assert url == "https://preview.example.com/#intent="+p.intent.id
    assert TOKEN not in url
    _, existing = panel(p.flow, 1, 1, p.intent.id, bot.L)
    assert not any("web_app" in button for row in existing["inline_keyboard"] for button in row)
    for url in ("http://x/", "https://u:p@x/", "https://x/#x", "https://x/?token=x"):
        with pytest.raises(ValueError):
            TradingConfig(miniapp_url=url)
