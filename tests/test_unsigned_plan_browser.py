"""Full mocked Telegram→SIWE→verified V2 exact review over the existing HTTPS UI."""

import json

import pytest
from playwright.sync_api import expect, sync_playwright
from test_miniapp_browser import ALLOWED, MOCK
from test_trading_auth import WALLET, secured, sign, signed_init  # noqa: F401
from test_unsigned_plan_api import plan_live  # noqa: F401


@pytest.fixture(params=["desktop", "mobile", "webkit-iphone"])
def exact_browser(request, plan_live):
    p = plan_live
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
            "fixtureSign", lambda value: sign(bytes.fromhex(value[2:]).decode())
        )
        context.add_init_script(
            MOCK.replace("0x" + "11" * 20, WALLET).replace(
                "'fixture-user-1'", json.dumps(signed_init())
            )
            + """
            const readRequest=window.ethereum.request;
            window.ethereum.request=async args=>{
                if(args.method!=='personal_sign')return readRequest(args);
                window.calls.push(args.method);return fixtureSign(args.params[0]);
            };
        """
        )
        context.route("https://telegram.org/**", lambda route: route.fulfill(body=""))
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(error))
        yield page, p
        assert not errors
        assert set(page.evaluate("window.calls")) <= ALLOWED | {"personal_sign"}
        context.close()
        browser.close()


def authenticate(page, p):
    page.goto(p.base + "/#intent=" + p.intent.id)
    expect(page.locator("#connect")).to_be_enabled()
    page.locator("#connect").click()
    expect(page.locator("#ownership-prepare")).to_be_enabled()
    page.locator("#ownership-prepare").click()
    expect(page.locator("#ownership-sign")).to_be_enabled()
    page.locator("#ownership-sign").click()
    expect(page.locator("#app")).to_have_attribute("data-state", "verified_wallet")


@pytest.mark.parametrize(
    "side,status", [("buy", "success"), ("sell", "success"), ("sell", "approval_required")]
)
def test_exact_buy_sell_review(exact_browser, side, status):
    page, p = exact_browser
    p.intent = p.flow.create(1, 1, p.intent.token, side, -100123, 56)
    p.plan_rpc.allowance = 0 if status == "approval_required" else 10**30
    authenticate(page, p)
    with page.expect_response("**/api/plan") as response:
        page.locator("#refresh").click()
    dto = response.value.json()
    assert response.value.status == 200, dto
    expect(page.locator("#app")).to_have_attribute("data-state", "quote_ready")
    assert dto["plan"]["simulation_status"] == status
    with page.expect_response("**/api/revalidate") as revalidation:
        page.locator("#review").click()
    assert revalidation.value.status == 200
    expect(page.locator("#app")).to_have_attribute("data-state", "unsigned_review")
    expect(page.locator("#calldata")).to_have_value(dto["plan"]["transaction_payload"]["data"])
    expect(page.locator("#signing-disabled")).to_be_disabled()
    expect(page.locator("#review-details")).to_contain_text(
        "CONDITIONAL" if status == "approval_required" else "Exact payload minimum enforced"
    )
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    assert not p.flow.orders(1, 1)


def test_changed_balance_or_forged_client_payload_never_reviews(exact_browser):
    page, p = exact_browser
    authenticate(page, p)
    page.locator("#refresh").click()
    expect(page.locator("#app")).to_have_attribute("data-state", "quote_ready")
    p.plan_rpc.native -= 1
    page.locator("#review").click()
    expect(page.locator("#app")).to_have_attribute("data-state", "quote_stale")
    expect(page.locator("#review-card")).to_be_hidden()


def test_altered_plan_response_cannot_pass_exact_review(exact_browser):
    page, p = exact_browser
    authenticate(page, p)

    def alter(route):
        response = route.fetch()
        data = response.json()
        data["plan"]["min_out_wei"] = "1"
        route.fulfill(response=response, json=data)

    page.route("**/api/plan", alter)
    page.locator("#refresh").click()
    expect(page.locator("#app")).to_have_attribute("data-state", "quote_ready")
    page.locator("#review").click()
    expect(page.locator("#app")).to_have_attribute("data-state", "quote_stale")
    expect(page.locator("#review-card")).to_be_hidden()
