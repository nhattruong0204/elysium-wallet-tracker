"""Actual HTTPS ASGI integration; self-signed TLS and bot/EOA identities are fixtures."""

import socket
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
import requests
import uvicorn
from test_trading_auth import BOT, WALLET, secured, sign, signed_init  # noqa: F401

from agora_tracker.trading.auth import AuthService
from agora_tracker.trading.auth.service import COOKIE, digest
from agora_tracker.trading.miniapp import create_app

pytestmark = pytest.mark.filterwarnings("ignore:Unverified HTTPS request")


@pytest.fixture
def auth_live(secured, tmp_path):
    p = secured
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"https://127.0.0.1:{sock.getsockname()[1]}"
    auth = AuthService(
        flow=p.flow,
        store=p.store,
        bot_token=BOT,
        bot_id=123456,
        origin=origin,
        rpc=p.rpc,
        now=lambda: p.clock[0],
    )
    cert, key = tmp_path / "cert.pem", tmp_path / "tls-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(p.flow, auth),
            log_level="warning",
            lifespan="off",
            proxy_headers=False,
            timeout_keep_alive=1,
            timeout_graceful_shutdown=2,
            ssl_keyfile=str(key),
            ssl_certfile=str(cert),
        )
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    client = requests.Session()
    client.verify = False  # test-only self-signed certificate; production uses browser trust
    client.headers["Origin"] = origin
    client.headers["Connection"] = "close"

    def post(action, **data):
        response = client.post(
            origin + "/api/" + action, json={"intent": p.intent.id, **data}, timeout=10
        )
        if response.ok and "csrf" in response.json():
            client.headers["X-CSRF-Token"] = response.json()["csrf"]
        return response

    yield SimpleNamespace(**vars(p), base=origin, service=auth, client=client, post=post)
    client.close()
    server.should_exit = True
    thread.join(5)
    assert not thread.is_alive()
    sock.close()


def prove(p):
    assert p.post("auth/bootstrap", init_data=signed_init()).status_code == 200
    q = p.post("auth/challenge", wallet=WALLET, chain_id=99901).json()
    response = p.post(
        "auth/verify", wallet=WALLET, chain_id=99901, nonce=q["nonce"], signature=sign(q["message"])
    )
    assert response.status_code == 200, response.text
    return q


def test_https_scoped_preview_and_logout_no_execution(auth_live):
    p = auth_live
    assert p.post("session", wallet=WALLET).status_code == 401
    assert p.post("auth/bootstrap", init_data=signed_init()).status_code == 200
    assert p.post("session", wallet=WALLET).json()["auth"]["wallet_verified"] is False
    assert p.post("quote", wallet=WALLET, amount="0.1", slippage_bps=50).status_code == 403
    q = p.post("auth/challenge", wallet=WALLET, chain_id=99901).json()
    assert (
        p.post(
            "auth/verify",
            wallet=WALLET,
            chain_id=99901,
            nonce=q["nonce"],
            signature=sign(q["message"]),
        ).status_code
        == 200
    )
    response = p.post("session", wallet=WALLET)
    assert response.json()["auth"]["wallet_verified"] is True
    assert response.json()["signing_enabled"] is False
    assert response.headers["Cache-Control"] == "no-store"
    assert (
        p.post("quote", wallet=WALLET, amount="0.1", slippage_bps=50).status_code == 404
    )  # no provider
    for action in ("sign", "send", "approve", "broadcast", "auth/sign", "auth/arbitrary"):
        assert p.post(action).status_code == 404
    assert p.post("auth/logout").status_code == 200
    assert p.post("session", wallet=WALLET).status_code == 401
    assert p.client.cookies.get(COOKIE) is None


def test_csrf_origin_duplicate_json_and_private_endpoints(auth_live):
    p = auth_live
    assert p.post("auth/bootstrap", init_data=signed_init()).status_code == 200
    csrf = p.client.headers.pop("X-CSRF-Token")
    assert p.post("auth/challenge", wallet=WALLET, chain_id=99901).status_code == 401
    p.client.headers["X-CSRF-Token"] = csrf
    for origin in ("https://evil.example", "null", "http://127.0.0.1"):
        p.client.headers["Origin"] = origin
        assert p.post("auth/challenge", wallet=WALLET, chain_id=99901).status_code == 401
    p.client.headers["Origin"] = p.base
    response = p.client.post(
        p.base + "/api/auth/bootstrap",
        data='{"intent":"' + p.intent.id + '","init_data":"x","init_data":"y"}',
        headers={"Content-Type": "application/json"},
        timeout=5,
    )
    assert response.status_code == 401
    for path in ("/auth.db", "/api/auth/sessions", "/api/wallets", "/static/../auth.db"):
        assert p.client.get(p.base + path, timeout=5).status_code == 404
    assert p.client.get(p.base + "/api/auth/challenge", timeout=5).status_code == 405
    assert p.client.options(p.base + "/api/auth/challenge", timeout=5).status_code == 405
    assert (
        p.post("auth/challenge", wallet=WALLET, chain_id=99901, message="sign anything").status_code
        == 401
    )


def test_session_intent_cookie_replay_and_changed_account(auth_live):
    p = auth_live
    q = prove(p)
    assert (
        p.post(
            "auth/verify",
            wallet=WALLET,
            chain_id=99901,
            nonce=q["nonce"],
            signature=sign(q["message"]),
        ).status_code
        == 401
    )
    assert p.post("session", wallet=WALLET, intent="s" * 16).status_code == 401
    assert p.post("session", wallet="0x" + "22" * 20).json()["auth"]["wallet_verified"] is False
    assert p.post("session", wallet=WALLET).json()["auth"]["wallet_verified"] is False
    p.client.headers["Cookie"] = f"{COOKIE}=a; {COOKIE}=b"
    assert p.post("session", wallet=WALLET).status_code == 401


def test_logout_after_cancel_and_replay_after_restarting_adapter(auth_live):
    p = auth_live
    raw = signed_init()
    assert p.post("auth/bootstrap", init_data=raw).status_code == 200
    assert p.post("cancel").status_code == 200
    assert p.post("auth/logout").status_code == 200
    # Revoking a session does not make its used Telegram launch reusable.
    assert p.post("auth/bootstrap", init_data=raw).status_code == 401


def test_persistence_failure_never_authenticates_or_leaks(auth_live, monkeypatch):
    p = auth_live

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("sensitive backend detail")

    monkeypatch.setattr(p.store, "create", unavailable)
    response = p.post("auth/bootstrap", init_data=signed_init())
    assert response.status_code == 503
    assert "sensitive" not in response.text and BOT not in response.text
    assert p.client.cookies.get(COOKIE) is None


def test_verify_persistence_failure_consumes_nonce_without_proof(auth_live, monkeypatch):
    p = auth_live
    assert p.post("auth/bootstrap", init_data=signed_init()).status_code == 200
    q = p.post("auth/challenge", wallet=WALLET, chain_id=99901).json()
    with monkeypatch.context() as m:

        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("sensitive")

        m.setattr(p.store, "complete", unavailable)
        assert (
            p.post(
                "auth/verify",
                wallet=WALLET,
                chain_id=99901,
                nonce=q["nonce"],
                signature=sign(q["message"]),
            ).status_code
            == 503
        )
    assert p.post("session", wallet=WALLET).json()["auth"]["wallet_verified"] is False
    assert (
        p.post(
            "auth/verify",
            wallet=WALLET,
            chain_id=99901,
            nonce=q["nonce"],
            signature=sign(q["message"]),
        ).status_code
        == 401
    )


def test_auth_rate_limit_is_enforced_on_http(auth_live):
    p = auth_live
    statuses = [p.post("auth/bootstrap", init_data="forged").status_code for _ in range(11)]
    assert statuses == [401] * 10 + [429]
    assert p.client.cookies.get(COOKIE) is None


def test_revoke_during_http_verification_blocks_elevation(auth_live):
    p = auth_live
    assert p.post("auth/bootstrap", init_data=signed_init()).status_code == 200
    q = p.post("auth/challenge", wallet=WALLET, chain_id=99901).json()
    token = digest(p.client.cookies.get(COOKIE))
    original = p.rpc.call

    def revoke_during_read(method, params):
        if method == "eth_getCode":
            p.store.revoke(token, 100)
        return original(method, params)

    p.rpc.call = revoke_during_read
    assert (
        p.post(
            "auth/verify",
            wallet=WALLET,
            chain_id=99901,
            nonce=q["nonce"],
            signature=sign(q["message"]),
        ).status_code
        == 401
    )
    assert p.store.get(token, 100).wallet is None


def test_quote_does_not_release_data_after_ownership_revocation(auth_live, monkeypatch):
    p = auth_live
    prove(p)
    token = digest(p.client.cookies.get(COOKIE))

    def revoke_during_review(*args):
        p.store.revoke(token, 100)
        return object()  # must be discarded before quote serialization

    monkeypatch.setattr(p.flow, "review", revoke_during_review)
    response = p.post("quote", wallet=WALLET, amount="0.1", slippage_bps=50)
    assert response.status_code == 401
    assert "quote" not in response.json()
    assert p.store.get(token, 100).wallet is None
