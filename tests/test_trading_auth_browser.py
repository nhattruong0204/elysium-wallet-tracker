"""Real HMAC/SIWE verification over HTTPS; wallet/browser identity remains a fixture."""

import json

import pytest
from playwright.sync_api import expect, sync_playwright
from test_miniapp_browser import ALLOWED, MOCK
from test_trading_auth import WALLET, secured, sign, signed_init  # noqa: F401
from test_trading_auth_http import auth_live  # noqa: F401

from agora_tracker.trading.auth.service import COOKIE, digest


@pytest.fixture(params=["desktop", "mobile", "webkit-iphone"])
def authenticated_browser(request, auth_live):
    p = auth_live
    with sync_playwright() as pw:
        engine = pw.webkit if request.param == "webkit-iphone" else pw.chromium
        browser = engine.launch()
        options = (
            pw.devices["iPhone 13"]
            if request.param == "webkit-iphone"
            else {"viewport": {"width": 390 if request.param == "mobile" else 1280, "height": 844}}
        )
        context = browser.new_context(ignore_https_errors=True, **options)
        context.expose_function(
            "fixtureSign", lambda hex_message: sign(bytes.fromhex(hex_message[2:]).decode())
        )
        mock = MOCK.replace("0x" + "11" * 20, WALLET).replace(
            "'fixture-user-1'", json.dumps(signed_init())
        )
        context.add_init_script(
            mock
            + """
          window.Telegram.WebApp.initDataUnsafe = {user:{id:999}};
          const readRequest = window.ethereum.request;
          window.ethereum.request = async args => {
            if (args.method !== 'personal_sign') return readRequest(args);
            window.calls.push(args.method); window.signatureText = args.params[0];
            if (window.declineProof) throw Object.assign(new Error('Declined'), {code:4001});
            if (window.changeDuringSign) {
              window.accounts=['0x'+'22'.repeat(20)];
              window.emit('accountsChanged', window.accounts);
            }
            return window.fixtureSign(args.params[0]);
          };
        """
        )
        context.route("https://telegram.org/**", lambda route: route.fulfill(body=""))
        page = context.new_page()
        page.auth_responses = []
        page.on(
            "response",
            lambda response: (
                page.auth_responses.append((response.url.split("/api/")[-1], response.status))
                if "/api/" in response.url
                else None
            ),
        )
        errors = []
        page.on("pageerror", lambda error: errors.append(error))
        page.goto(p.base + "/#intent=" + p.intent.id)
        expect(page.locator("#app")).to_have_attribute("data-state", "wallet_disconnected")
        assert page.evaluate("isSecureContext")
        assert p.client.cookies.get(COOKIE) is None  # separate HTTP client never bootstrapped
        yield page, p
        assert not errors
        assert set(page.evaluate("window.calls")) <= ALLOWED | {"personal_sign"}
        context.close()
        browser.close()


def connect(page):
    page.locator("#connect").click()
    expect(page.locator("#app")).to_have_attribute("data-state", "wallet_unverified")
    expect(page.locator("#refresh")).to_be_disabled()
    expect(page.locator("#ownership-prepare")).to_be_enabled()


def prove(page):
    connect(page)
    page.locator("#ownership-prepare").click()
    expect(page.locator("#ownership-card")).to_be_visible()
    assert "personal_sign" not in page.evaluate("window.calls")
    page.locator("#ownership-sign").click()
    try:
        expect(page.locator("#app")).to_have_attribute("data-state", "verified_wallet")
    except AssertionError:
        pytest.fail(
            f"Ownership state: {page.locator('#status-message').inner_text()}; HTTP: {page.auth_responses}"
        )
    expect(page.locator("#refresh")).to_be_enabled()


def test_crypto_authenticated_scoped_session_revoke_and_logout(authenticated_browser):
    page, p = authenticated_browser
    page.evaluate("Date.now = () => 0")  # browser wall-clock changes cannot invalidate/revive proof
    prove(page)
    cookies = page.context.cookies()
    cookie = next(c for c in cookies if c["name"] == COOKIE)
    assert cookie["httpOnly"] and cookie["secure"] and cookie["sameSite"] == "Strict"
    assert COOKIE not in page.evaluate("document.cookie")
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    s = p.store.get(digest(cookie["value"]), 100)
    assert s.uid == 1 and s.intent == p.intent.id and s.wallet == WALLET
    text = bytes.fromhex(page.evaluate("window.signatureText")[2:]).decode()
    assert text.startswith(p.service.domain + " wants you to sign in with your Ethereum account:")
    assert "URI: " + p.base + "/" in text and "Chain ID: 99901" in text
    expect(page.locator("#signing-disabled")).to_be_disabled()
    with page.expect_response("**/api/auth/revoke") as response:
        page.evaluate("window.emit('chainChanged','0x1')")
    assert response.value.status == 200
    assert p.store.get(digest(cookie["value"]), 100).wallet is None
    expect(page.locator("#refresh")).to_be_disabled()
    page.locator("#logout").click()
    expect(page.locator("#app")).to_have_attribute("data-state", "cancelled")
    assert not any(c["name"] == COOKIE for c in page.context.cookies())


def test_forged_arbitrary_message_never_reaches_wallet(authenticated_browser):
    page, p = authenticated_browser
    connect(page)

    def mutate(route):
        response = route.fetch()
        data = response.json()
        data["message"] = data["message"].replace(
            "URI: " + p.base + "/", "URI: https://evil.example/"
        )
        route.fulfill(response=response, json=data)

    page.route("**/api/auth/challenge", mutate)
    page.locator("#ownership-prepare").click()
    expect(page.locator("#ownership-card")).to_be_hidden()
    expect(page.locator("#refresh")).to_be_disabled()
    assert "personal_sign" not in page.evaluate("window.calls")


def test_signature_decline_and_account_change_discard_proof(authenticated_browser):
    page, p = authenticated_browser
    connect(page)
    page.locator("#ownership-prepare").click()
    expect(page.locator("#ownership-sign")).to_be_enabled()
    page.evaluate("window.declineProof=true")
    page.locator("#ownership-sign").click()
    expect(page.locator("#status-message")).to_contain_text("refused or declined")
    expect(page.locator("#refresh")).to_be_disabled()
    page.evaluate("window.declineProof=false")
    page.locator("#ownership-prepare").click()
    expect(page.locator("#ownership-sign")).to_be_enabled()
    page.evaluate("window.changeDuringSign=true")
    with page.expect_response("**/api/auth/revoke") as response:
        page.locator("#ownership-sign").click()
    assert response.value.status == 200
    expect(page.locator("#refresh")).to_be_disabled()
    cookie = next(c for c in page.context.cookies() if c["name"] == COOKIE)
    assert p.store.get(digest(cookie["value"]), 100).wallet is None


def test_nonce_expiry_and_replay_after_reload_refused(authenticated_browser):
    page, p = authenticated_browser
    connect(page)
    page.locator("#ownership-prepare").click()
    expect(page.locator("#ownership-sign")).to_be_enabled()
    p.clock[0] = 161.0
    page.locator("#ownership-sign").click()
    expect(page.locator("#refresh")).to_be_disabled()
    # CSRF stays in memory only; old initData cannot re-exchange on reload.
    page.goto("about:blank")
    page.goto(p.base + "/#intent=" + p.intent.id)
    expect(page.locator("#app")).to_have_attribute("data-state", "not_authenticated")
    expect(page.locator("#connect")).to_be_disabled()


def test_contract_wallet_refuses_before_signature_request(authenticated_browser):
    page, p = authenticated_browser
    connect(page)
    p.rpc.code = "0x6000"
    page.locator("#ownership-prepare").click()
    expect(page.locator("#app")).to_have_attribute("data-state", "not_authenticated")
    expect(page.locator("#refresh")).to_be_disabled()
    assert "personal_sign" not in page.evaluate("window.calls")
