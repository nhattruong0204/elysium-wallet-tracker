"""Cryptographic fixtures are public test identities, never funded/live wallet keys."""

import hashlib
import hmac
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from eth_keys import keys
from eth_keys.constants import SECPK1_N
from starlette.requests import Request

from agora_tracker.trading.auth import AuthService, AuthStore
from agora_tracker.trading.auth.service import COOKIE, digest
from agora_tracker.trading.auth.siwe import message_hash, recover, require_eoa
from agora_tracker.trading.auth.store import Challenge
from agora_tracker.trading.auth.telegram import AuthRefused, RateLimited, TelegramVerifier
from agora_tracker.trading.chain import ReadRpc
from agora_tracker.trading.paper import PaperFlow, TradeRefused

BOT = "123456:" + "b" * 32
KEY = keys.PrivateKey(b"\x01" * 32)  # public deterministic fixture ONLY
WALLET = KEY.public_key.to_address()
TOKEN = "0x" + "ab" * 20
ORIGIN = "https://auth.example"


def signed_init(uid=1, now=100, token=BOT, **changes):
    fields = {
        "auth_date": str(now),
        "user": json.dumps(
            {"id": uid, "first_name": "Fixture ☃"}, ensure_ascii=False, separators=(",", ":")
        ),
        "query_id": secrets.token_urlsafe(24),
    }
    fields.update(changes)
    secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
    fields["hash"] = hmac.digest(
        secret, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), "sha256"
    ).hex()
    return urlencode(fields)


def sign(text, key=KEY):
    sig = key.sign_msg_hash(message_hash(text))
    return (
        "0x"
        + sig.r.to_bytes(32, "big").hex()
        + sig.s.to_bytes(32, "big").hex()
        + bytes([sig.v + 27]).hex()
    )


class AuthRpc(ReadRpc):
    def __init__(self, clock):
        super().__init__("https://fixture.invalid", now=lambda: clock[0])
        self.clock, self.code, self.chain = clock, "0x", 99901
        self.methods = []

    def call(self, method, params):
        self.methods.append(method)
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "eth_getCode":
            assert params[1] == "0xa"
            return self.code
        assert method == "eth_getBlockByNumber"
        return {"number": "0xa", "timestamp": hex(int(self.clock[0])), "hash": "0x" + "12" * 32}


def request(action="session", cookie=None, csrf=None, origin=ORIGIN):
    headers = [
        (b"origin", origin.encode()),
        (b"host", b"auth.example"),
        (b"content-type", b"application/json"),
    ]
    if cookie:
        headers.append((b"cookie", f"{COOKIE}={cookie}".encode()))
    if csrf:
        headers.append((b"x-csrf-token", csrf.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/" + action,
            "headers": headers,
            "server": ("auth.example", 443),
            "client": ("127.0.0.1", 1234),
            "query_string": b"",
        }
    )


@pytest.fixture
def secured(tmp_path):
    clock = [100.0]
    flow = PaperFlow({1, 2}, now=lambda: clock[0])
    flow.set_wallet(1, 1, WALLET)
    intent = flow.create(1, 1, TOKEN, "buy", -100123, 55)
    rpc = AuthRpc(clock)
    store = AuthStore(str(tmp_path / "private" / "auth.db"))
    auth = AuthService(
        flow=flow,
        store=store,
        bot_token=BOT,
        bot_id=123456,
        origin=ORIGIN,
        rpc=rpc,
        now=lambda: clock[0],
    )
    return SimpleNamespace(clock=clock, flow=flow, intent=intent, rpc=rpc, store=store, auth=auth)


def bootstrap(p, raw=None):
    response = p.auth._bootstrap(
        request(), {"intent": p.intent.id, "init_data": raw or signed_init()}
    )
    data = json.loads(response.body)
    cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    return cookie, data["csrf"]


def challenge(p, cookie, csrf):
    response = p.auth._challenge(
        request(cookie=cookie, csrf=csrf),
        {"intent": p.intent.id, "wallet": WALLET, "chain_id": 99901},
    )
    return json.loads(response.body)


def verify(p, cookie, csrf, q, **changes):
    data = dict(
        intent=p.intent.id,
        wallet=WALLET,
        chain_id=99901,
        nonce=q["nonce"],
        signature=sign(q["message"]),
    )
    data.update(changes)
    return p.auth._verify(request(cookie=cookie, csrf=csrf), data)


def test_official_hmac_roundtrip_and_bot_binding():
    verifier = TelegramVerifier(BOT, 123456)
    raw = signed_init(signature="public-Telegram-signature", bot_id="123456")
    assert verifier.verify(raw, "s" * 16, 100).user_id == 1
    with pytest.raises(AuthRefused):
        TelegramVerifier("654321:" + "c" * 32, 654321).verify(raw, "s" * 16, 100)
    with pytest.raises(ValueError):
        TelegramVerifier(BOT, 999)


@pytest.mark.parametrize(
    "changes",
    [
        {"auth_date": "0"},
        {"auth_date": "-1"},
        {"auth_date": "100.0"},
        {"auth_date": "101"},
        {"auth_date": "220"},
        {"auth_date": "01"},
        {"auth_date": "NaN"},
        {"user": '{"id":1,"id":2}'},
        {"user": '{"id":true}'},
        {"user": '{"id":0}'},
        {"user": '{"id":-1}'},
        {"user": '{"id":1.0}'},
        {"user": '{"id":4503599627370496}'},
        {"user": '{"id":1,"is_bot":true}'},
        {"user": "[]"},
        {"user": "null"},
        {"query_id": ""},
        {"query_id": "x\nuser=2"},
        {"chat": '{"id":-1}'},
        {"receiver": '{"id":2}'},
        {"chat_type": "group"},
        {"chat_type": "private"},
        {"chat_instance": "123"},
        {"start_param": "forged"},
        {"bot_id": "654321"},
    ],
)
def test_signed_but_unsupported_launch_context_and_bad_identity_refused(changes):
    with pytest.raises(AuthRefused):
        TelegramVerifier(BOT, 123456).verify(signed_init(**changes), "s" * 16, 100)


@pytest.mark.parametrize(
    "suffix",
    [
        "&hash=" + "0" * 64,
        "&user=%7B%22id%22%3A2%7D",
        "&%75ser=x",
        "&auth_date=100",
        "&x=%zz",
        "&x=%ff",
        "&x=1\nuser=2",
        "&x=1%0Auser%3D2",
    ],
)
def test_duplicate_encoding_tamper_and_injection_refused(suffix):
    with pytest.raises(AuthRefused):
        TelegramVerifier(BOT, 123456).verify(signed_init() + suffix, "s" * 16, 100)


def test_auth_date_bounds_and_every_byte_mutation():
    verifier = TelegramVerifier(BOT, 123456)
    raw = signed_init()
    assert verifier.verify(raw, "s" * 16, 219).user_id == 1
    for now in (99, 220, 1000):
        with pytest.raises(AuthRefused):
            verifier.verify(raw, "s" * 16, now)
    # Property: changing any signed value byte without recomputing the HMAC refuses.
    prefix, signature = raw.rsplit("&hash=", 1)
    for index in range(len(prefix)):
        mutated = prefix[:index] + ("Z" if prefix[index] != "Z" else "Y") + prefix[index + 1 :]
        with pytest.raises(AuthRefused):
            verifier.verify(mutated + "&hash=" + signature, "s" * 16, 100)


def test_full_proof_scoped_rotated_and_watch_wallet_does_not_authenticate(secured):
    p = secured
    cookie, csrf = bootstrap(p)
    r = request(cookie=cookie, csrf=csrf)
    r.state.miniapp_intent = p.intent.id
    assert p.auth._authenticate(r, "", WALLET).verified_wallet is None
    q = challenge(p, cookie, csrf)
    assert q["message"].startswith("auth.example wants you to sign in")
    assert "Chain ID: 99901" in q["message"] and "No transactions or approvals" in q["message"]
    assert "0x" not in q["nonce"] and len(q["nonce"]) == 64
    assert recover(q["message"], sign(q["message"])) == WALLET
    response = verify(p, cookie, csrf, q)
    newcookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    newcsrf = json.loads(response.body)["csrf"]
    assert cookie != newcookie and csrf != newcsrf
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag in response.headers["set-cookie"]
    with pytest.raises(AuthRefused):
        p.store.get(digest(cookie), 100)
    r = request(cookie=newcookie, csrf=newcsrf)
    r.state.miniapp_intent = p.intent.id
    principal = p.auth._authenticate(r, "", WALLET)
    assert principal.user_id == 1 and principal.verified_wallet == WALLET
    assert principal.intent_id == p.intent.id and not principal.fixture
    # An attempted account change revokes proof, even if that address is also an EOA.
    assert p.auth._authenticate(r, "", "0x" + "22" * 20).verified_wallet is None
    assert p.auth._authenticate(r, "", WALLET).verified_wallet is None
    assert set(p.rpc.methods) <= {"eth_chainId", "eth_getBlockByNumber", "eth_getCode"}


def test_launch_replay_atomic_and_survives_store_reopen(secured):
    p = secured
    raw = signed_init()

    def attempt(_):
        try:
            bootstrap(p, raw)
            return True
        except AuthRefused:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(4))) == 1
    p.auth.store = AuthStore(str(p.store.path))
    with pytest.raises(AuthRefused):
        bootstrap(p, raw)


def test_nonce_consume_race(secured):
    p = secured
    c, csrf = bootstrap(p)
    q = challenge(p, c, csrf)
    # Two store objects model separate workers using one shared private SQLite file.
    stores = [p.store, AuthStore(str(p.store.path))]

    def attempt(index):
        try:
            stores[index % 2].consume(digest(c), q["nonce"], WALLET, 100)
            return True
        except AuthRefused:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(20))) == 1


@pytest.mark.parametrize(
    "change", ["signature", "domain", "uri", "chain", "nonce", "issued", "expiration", "binding"]
)
def test_signature_binds_every_message_field_and_burns_nonce(secured, change):
    p = secured
    c, csrf = bootstrap(p)
    q = challenge(p, c, csrf)
    replacements = {
        "domain": ("auth.example", "evil.example"),
        "uri": ("URI: https://auth.example/", "URI: https://auth.example/evil"),
        "chain": ("Chain ID: 99901", "Chain ID: 1"),
        "nonce": (q["nonce"], "a" * 64),
        "issued": ("Issued At: 1970-01-01T00:01:40Z", "Issued At: 1970-01-01T00:01:41Z"),
        "expiration": ("Expiration Time:", "Expiration-Time:"),
        "binding": (q["request_id"], "b" * 64),
    }
    text = q["message"]
    signature = (
        sign("I own this wallet")
        if change == "signature"
        else sign(text.replace(*replacements[change]))
    )
    with pytest.raises(AuthRefused):
        verify(p, c, csrf, q, signature=signature)
    with pytest.raises(AuthRefused):
        verify(p, c, csrf, q)


def test_signature_shapes_and_malleability(secured):
    p = secured
    c, csrf = bootstrap(p)
    q = challenge(p, c, csrf)
    sig = KEY.sign_msg_hash(message_hash(q["message"]))
    high = (
        "0x"
        + sig.r.to_bytes(32, "big").hex()
        + (SECPK1_N - sig.s).to_bytes(32, "big").hex()
        + bytes([28 - sig.v]).hex()
    )
    for value in ("", None, "0x00", "0x" + "00" * 65, high, sign(q["message"])[:-2] + "00"):
        with pytest.raises(AuthRefused):
            recover(q["message"], value)


@pytest.mark.parametrize("code", ["0x00", "0xef0100" + "11" * 20, "0x1234", None, "", "0x0"])
def test_contract_delegated_and_malformed_rpc_refused(secured, code):
    p = secured
    p.rpc.code = code
    with pytest.raises(AuthRefused):
        require_eoa(p.rpc, WALLET)


def test_wrong_chain_rpc_and_changed_code_at_verification(secured):
    p = secured
    p.rpc.chain = 1
    with pytest.raises(TradeRefused):
        require_eoa(p.rpc, WALLET)
    p.rpc.chain = 99901
    c, csrf = bootstrap(p)
    q = challenge(p, c, csrf)
    p.rpc.code = "0x6000"
    with pytest.raises(AuthRefused):
        verify(p, c, csrf, q)
    p.rpc.code = "0x"
    with pytest.raises(AuthRefused):
        verify(p, c, csrf, q)


def test_csrf_user_session_origin_and_intent_separation(secured):
    p = secured
    c, csrf = bootstrap(p)
    for req in (
        request(cookie=c),
        request(csrf=csrf),
        request(cookie=c, csrf="a" * 43),
        request(cookie=c, csrf=csrf, origin="https://evil.example"),
    ):
        with pytest.raises(AuthRefused):
            p.auth._session(req)
    q = challenge(p, c, csrf)
    c2, csrf2 = bootstrap(p)
    with pytest.raises(AuthRefused):
        verify(p, c2, csrf2, q)
    with pytest.raises(AuthRefused):
        p.auth._intent(p.auth._session(request(cookie=c, csrf=csrf)), "s" * 16)
    other = p.flow.create(2, 2, TOKEN, "buy", -100123, 55)
    with pytest.raises(TradeRefused):
        p.auth._bootstrap(request(), {"intent": other.id, "init_data": signed_init(uid=1)})
    with pytest.raises(TradeRefused):
        p.auth._bootstrap(request(), {"intent": p.intent.id, "init_data": signed_init(uid=2)})


def test_expiry_rate_limits_and_logout_verification_race(secured):
    p = secured
    c, csrf = bootstrap(p)
    q = challenge(p, c, csrf)
    p.clock[0] = 160
    with pytest.raises(AuthRefused):
        verify(p, c, csrf, q)
    p.clock[0] = 100
    q = challenge(p, c, csrf)
    original = p.auth.rpc.call

    def logout_during_rpc(method, params):
        if method == "eth_getCode":
            p.store.revoke(digest(c), 100, logout=True)
        return original(method, params)

    p.auth.rpc.call = logout_during_rpc
    with pytest.raises(AuthRefused):
        verify(p, c, csrf, q)
    p.store.limit("fixture-limit", 1, 100)
    with pytest.raises(RateLimited):
        p.store.limit("fixture-limit", 1, 100)
    p.store.limit("fixture-limit", 1, 160)


def test_private_store_only_hashes_and_no_signature_persistence(secured):
    p = secured
    c, csrf = bootstrap(p)
    q = challenge(p, c, csrf)
    response = verify(p, c, csrf, q)
    newcookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    data = p.store.path.read_bytes()
    for secret in (BOT, c, csrf, signed_init(), sign(q["message"])):
        assert secret.encode() not in data
    assert p.store.path.stat().st_mode & 0o777 == 0o600
    assert p.store.path.parent.stat().st_mode & 0o777 == 0o700
    assert hashlib.sha256(newcookie.encode()).hexdigest().encode() in data
    p.store.path.chmod(0o644)
    with pytest.raises(TradeRefused):
        p.store.get(digest(c), 100)


def test_concurrent_full_verification_has_one_winner(secured):
    p = secured
    cookie, csrf = bootstrap(p)
    q = challenge(p, cookie, csrf)

    def attempt(_):
        try:
            verify(p, cookie, csrf, q)
            return True
        except AuthRefused:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    with p.store._connect() as c:
        assert (
            c.execute("SELECT count(*) FROM auth_sessions WHERE wallet IS NOT NULL").fetchone()[0]
            == 1
        )


def test_superseded_challenges_and_unconsumed_completion_refused(secured):
    p = secured
    cookie, csrf = bootstrap(p)
    first = challenge(p, cookie, csrf)
    session = p.store.get(digest(cookie), 100)
    with p.store._connect() as c:
        import sqlite3

        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM auth_challenges WHERE nonce=?", (first["nonce"],)).fetchone()
    with pytest.raises(AuthRefused):
        p.store.complete(session, Challenge(**dict(row)), digest("new"), digest("csrf"), 100)
    second = challenge(p, cookie, csrf)
    with pytest.raises(AuthRefused):
        verify(p, cookie, csrf, first)
    assert verify(p, cookie, csrf, second).status_code == 200


def test_malformed_account_attempt_revokes_existing_ownership(secured):
    p = secured
    cookie, csrf = bootstrap(p)
    response = verify(p, cookie, csrf, challenge(p, cookie, csrf))
    cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    csrf = json.loads(response.body)["csrf"]
    r = request(cookie=cookie, csrf=csrf)
    r.state.miniapp_intent = p.intent.id
    with pytest.raises(AuthRefused):
        p.auth._authenticate(r, "", "not-an-address")
    assert p.store.get(digest(cookie), 100).wallet is None


def test_duplicate_security_headers_and_absent_origin_refused(secured):
    p = secured
    cookie, csrf = bootstrap(p)
    for header in (b"cookie", b"origin", b"x-csrf-token", b"host"):
        r = request(cookie=cookie, csrf=csrf)
        headers = list(r.scope["headers"])
        duplicate = next(value for name, value in headers if name == header)
        r.scope["headers"] = headers + [(header, duplicate)]
        with pytest.raises(AuthRefused):
            p.auth._session(r)
    r = request(cookie=cookie, csrf=csrf)
    r.scope["headers"] = [(k, v) for k, v in r.scope["headers"] if k != b"origin"]
    with pytest.raises(AuthRefused):
        p.auth._session(r)


def test_clock_rollback_and_session_expiry_refused(secured):
    p = secured
    cookie, csrf = bootstrap(p)
    for now in (99, 220):
        p.clock[0] = now
        with pytest.raises(AuthRefused):
            p.auth._session(request(cookie=cookie, csrf=csrf))


def test_published_eth_account_utf8_recovery_vector():
    # Primary reference: https://eth-account.readthedocs.io/en/stable/eth_account.html#eth_account.account.Account.recover_message
    signature = (
        "0xe6ca9bba58c88611fad66a6ce8f996908195593807c4b38bd528d2cff09d4eb3"
        "3e5bfbbf4d3e39b1a2fd816a7680c19ebebaf3a141b239934ad43cb33fcec8ce1c"
    )
    assert (
        message_hash("I♥SF").hex()
        == "1476abb745d423bf09273f1afd887d951181d25adc66c4834a70491911b7f750"
    )
    assert recover("I♥SF", signature) == "0x5ce9454909639d2d17a3f753ce7d93fa0b9ab12e"


def test_bot_secret_rotation_invalidates_existing_cookie(secured):
    p = secured
    cookie, csrf = bootstrap(p)
    rotated = AuthService(
        flow=p.flow,
        store=p.store,
        bot_token="123456:" + "c" * 32,
        bot_id=123456,
        origin=ORIGIN,
        rpc=p.rpc,
        now=lambda: 100,
    )
    with pytest.raises(AuthRefused):
        rotated._session(request(cookie=cookie, csrf=csrf))
    assert p.auth._session(request(cookie=cookie, csrf=csrf)).uid == 1
    with pytest.raises(AuthRefused):
        rotated.telegram.verify(signed_init(), p.intent.id, 100)
