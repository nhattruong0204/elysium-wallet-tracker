"""Mock Telegram/EIP-1193 browser evidence, not a physical iPhone compatibility claim."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import expect, sync_playwright
from test_miniapp import preview  # noqa: F401
from test_verified_paper import TOKEN, WALLET

from agora_tracker.cards import Links
from agora_tracker.config import LinksConfig, TradingConfig
from agora_tracker.trading.telegram import callback

MOCK = """
window.calls = []; window.listeners = {}; window.chain = '0x1';
window.accounts = ['WALLET'];
window.ethereum = {
  on: (name, fn) => { (window.listeners[name] ||= []).push(fn); },
  removeListener: (name, fn) => { window.listeners[name] = (window.listeners[name] || []).filter(x => x !== fn); },
  request: async ({method, params}) => {
    window.calls.push(method);
    if (method === 'eth_requestAccounts' || method === 'eth_accounts') return window.accounts;
    if (method === 'eth_chainId') return window.chain;
    if (method === 'wallet_switchEthereumChain') {
      if (window.decline) throw Object.assign(new Error('Declined'), {code:4001});
      if (window.unknown) { window.unknown = false; throw Object.assign(new Error('Unknown'), {code:4902}); }
      if (!window.wrongChain) window.chain = params[0].chainId;
      return null;
    }
    if (method === 'wallet_addEthereumChain') return null;
    throw new Error('FORBIDDEN: '+method);
  }
};
window.emit = (name, value) => (window.listeners[name] || []).forEach(fn => fn(value));
window.Telegram = { WebApp: { initData:'fixture-user-1', ready:() => {}, openLink:url => {window.externalLink=url;} } };
""".replace("WALLET", WALLET)
ALLOWED = {"eth_requestAccounts", "eth_accounts", "eth_chainId",
           "wallet_switchEthereumChain", "wallet_addEthereumChain"}


@pytest.fixture(params=["desktop", "mobile", "webkit-iphone"])
def browser(request):
    with sync_playwright() as pw:
        engine = pw.webkit if request.param == "webkit-iphone" else pw.chromium
        browser = engine.launch()
        options = pw.devices["iPhone 13"] if request.param == "webkit-iphone" else {
            "viewport": {"width": 390 if request.param == "mobile" else 1280, "height": 844}}
        context = browser.new_context(ignore_https_errors=True, **options)
        context.add_init_script(MOCK)
        # Block real Telegram network access; the SDK and initData are explicit fixtures.
        context.route("https://telegram.org/**", lambda route: route.fulfill(body=""))
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(error))
        page.clock.install()
        yield page
        assert not errors
        assert set(page.evaluate("window.calls || []")) <= ALLOWED
        context.close()
        browser.close()


def open_preview(page, p):
    bot = SimpleNamespace(cfg=SimpleNamespace(trading=TradingConfig(paper_enabled=True,
        miniapp_url="https://preview.example.com/"), chain=SimpleNamespace(chain_id=99901)),
        paper_trades=p.flow, L=Links(LinksConfig()), _send=Mock(), _answer=Mock())
    callback(bot, {"id": "fixture-alert", "from": {"id": 1}, "message": {
        "chat": {"id": -100123, "type": "supergroup"}, "message_id": 55},
        "data": "tp|"+("b" if p.intent.side == "buy" else "s")+"|"+TOKEN})
    markup = bot._send.call_args.args[2]
    fragment = markup["inline_keyboard"][-1][0]["web_app"]["url"].split("#", 1)[1]
    page.goto(p.base + "/#" + fragment + "&token=forged")
    expect(page.locator("#app")).to_have_attribute("data-state", "wallet_disconnected")
    assert page.evaluate("isSecureContext") is True  # loopback is a secure context
    assert page.evaluate("location.hash") == ""
    expect(page.locator("#token")).to_have_text(TOKEN)
    page.locator("#connect").click()
    expect(page.locator("#app")).to_have_attribute("data-state", "verified_wallet")


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_mocked_quote_exact_review_and_mobile_layout(browser, preview, side):
    p = preview(side)
    open_preview(browser, p)
    expect(browser.locator("#fixture")).to_be_visible()
    expect(browser.locator("#presets button")).to_have_count(3)
    browser.locator("#presets button").nth(1).click()
    browser.locator("#refresh").click()
    expect(browser.locator("#app")).to_have_attribute("data-state", "quote_ready")
    browser.locator("#amount").fill("0.123" if side == "buy" else "33.33")
    browser.locator("#slippage").select_option("100")
    expect(browser.locator("#quote-card")).to_be_hidden()
    browser.locator("#refresh").click()
    expect(browser.locator("#app")).to_have_attribute("data-state", "quote_ready")
    expected = p.flow.reviewed(1, 1, p.intent.id)
    browser.locator("#review").click()
    expect(browser.locator("#app")).to_have_attribute("data-state", "unsigned_review")
    expect(browser.locator("#calldata")).to_have_value(expected.transaction["data"])
    expect(browser.locator("#signing-disabled")).to_be_disabled()
    assert "wallet_switchEthereumChain" in browser.evaluate("window.calls")
    assert browser.evaluate("document.documentElement.scrollWidth <= innerWidth")
    browser.evaluate("window.emit('accountsChanged', ['0x'+'22'.repeat(20)])")
    expect(browser.locator("#review-card")).to_be_hidden()
    expect(browser.locator("#refresh")).to_be_disabled()
    browser.locator("#connect").click()
    expect(browser.locator("#app")).to_have_attribute("data-state", "verified_wallet")
    browser.locator("#refresh").click()
    expect(browser.locator("#quote-card")).to_be_visible()
    browser.evaluate("window.emit('chainChanged', '0x1')")
    expect(browser.locator("#quote-card")).to_be_hidden()
    browser.locator("#cancel").click()
    expect(browser.locator("#app")).to_have_attribute("data-state", "cancelled")
    assert browser.evaluate("Object.values(window.listeners).flat().length") == 0
    assert not p.flow.orders(1, 1)


def test_wrong_chain_decline_and_external_fallback(browser, preview):
    p = preview()
    browser.goto(p.base+"/#intent="+p.intent.id)
    expect(browser.locator("#connect")).to_be_enabled()
    browser.evaluate("window.wrongChain = true")
    browser.locator("#connect").click()
    expect(browser.locator("#app")).to_have_attribute("data-state", "wallet_unverified")
    expect(browser.locator("#refresh")).to_be_disabled()
    browser.evaluate("window.wrongChain=false; window.decline=true")
    browser.locator("#connect").click()
    expect(browser.locator("#status-message")).to_contain_text("declined")
    browser.evaluate("delete window.ethereum")
    browser.locator("#connect").click()
    expect(browser.locator("#fallback")).to_be_visible()
    browser.locator("#external-browser").click()
    assert browser.evaluate("window.externalLink") == p.base+"/#intent="+p.intent.id


def test_quote_and_intent_expiry_activity_and_injection(browser, preview):
    p = preview()
    open_preview(browser, p)
    browser.route("**/api/quote", lambda route: route.fulfill(status=404,
        content_type="application/json", body='{"error":"unsupported_route","message":"<img src=x onerror=alert(1)>","signing_enabled":false}'))
    browser.locator("#refresh").click()
    expect(browser.locator("#app")).to_have_attribute("data-state", "unsupported_route")
    expect(browser.locator("#status-message")).to_have_text("<img src=x onerror=alert(1)>")
    assert browser.locator("#status-message img").count() == 0
    expect(browser.locator("#dex")).to_have_attribute("href", "https://www.agoradex.app/token/"+TOKEN)
    browser.unroute("**/api/quote")
    browser.locator("#refresh").click()
    expect(browser.locator("#quote-card")).to_be_visible()
    browser.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, value:true}); document.dispatchEvent(new Event('visibilitychange'))")
    expect(browser.locator("#quote-card")).to_be_hidden()
    expect(browser.locator("#refresh")).to_be_disabled()
    browser.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, value:false})")
    browser.locator("#connect").click()
    expect(browser.locator("#refresh")).to_be_enabled()
    browser.locator("#refresh").click()
    expect(browser.locator("#quote-card")).to_be_visible()
    browser.clock.fast_forward(31000)
    expect(browser.locator("#app")).to_have_attribute("data-state", "quote_stale")
    browser.clock.fast_forward(151000)
    expect(browser.locator("#app")).to_have_attribute("data-state", "expired")
    expect(browser.locator("#connect")).to_be_disabled()


def test_https_switch_add_chain_and_unverified_account(browser, preview):
    p = preview(tls=True)
    browser.goto(p.base+"/#intent="+p.intent.id)
    expect(browser.locator("#connect")).to_be_enabled()
    assert browser.evaluate("isSecureContext && location.protocol === 'https:'")
    browser.evaluate("window.unknown=true")
    browser.locator("#connect").click()
    expect(browser.locator("#app")).to_have_attribute("data-state", "verified_wallet")
    assert "wallet_addEthereumChain" in browser.evaluate("window.calls")
    browser.evaluate("window.accounts=['0x'+'22'.repeat(20)]; window.emit('accountsChanged', window.accounts)")
    browser.locator("#connect").click()
    expect(browser.locator("#app")).to_have_attribute("data-state", "wallet_unverified")
    expect(browser.locator("#refresh")).to_be_disabled()


def test_delayed_quote_cannot_restore_invalidated_review(browser, preview):
    import threading

    p = preview()
    open_preview(browser, p)
    started, release = threading.Event(), threading.Event()
    original = p.flow.provider.quote

    def delayed(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    p.flow.provider.quote = delayed
    browser.locator("#refresh").click()
    assert started.wait(3)
    expect(browser.locator("#app")).to_have_attribute("data-state", "quote_loading")
    browser.evaluate("window.emit('accountsChanged', [])")
    with browser.expect_response("**/api/quote") as response:
        release.set()
    response.value.finished()
    browser.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    expect(browser.locator("#app")).to_have_attribute("data-state", "wallet_disconnected")
    expect(browser.locator("#quote-card")).to_be_hidden()
    expect(browser.locator("#review")).to_be_disabled()


def test_default_auth_is_denied_in_browser(browser, preview):
    p = preview(auth=False)
    browser.goto(p.base+"/#intent="+p.intent.id)
    expect(browser.locator("#app")).to_have_attribute("data-state", "not_authenticated")
    expect(browser.locator("#connect")).to_be_disabled()
    expect(browser.locator("#refresh")).to_be_disabled()
    assert browser.evaluate("window.calls") == []
