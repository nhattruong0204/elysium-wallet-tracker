# ruff: noqa: F811
"""Browser evidence with a MOCK EIP-1193 wallet against the real wallet routes and PG ledger.

This is mock evidence of the page logic, not a claim about any real wallet, extension or phone."""

from __future__ import annotations

import json

import pytest
from conftest import TEST_URL  # noqa: F401
from playwright.sync_api import expect, sync_playwright
from starlette.applications import Starlette
from test_trading_ledger import (  # noqa: F401
    NOW0,
    U1,
    clock,
    ledger,
    live,
    owner_engine,
    rw_engine,
)
from test_wallet_api import SID, StubAuth, StubPaper, StubPlans
from walletkit import E18, WALLET, FakeChain, Served, make_plan

from agora_tracker.trading.wallet.api import WalletRuntime, wallet_routes
from agora_tracker.trading.wallet.approvals import ApprovalPlanner
from agora_tracker.trading.wallet.flow import WalletFlow

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")
BASE = ""
HASH = "0x" + "cd" * 32

MOCK = """
window.rpc = []; window.mode = 'hash'; window.chain = '0x1863d'; window.refuseSwitch = false;
window.ethereum = {
  request: async ({method, params}) => {
    window.rpc.push({method, params});
    if (method === 'eth_requestAccounts' || method === 'eth_accounts') return ['WALLET'];
    if (method === 'eth_chainId') return window.chain;
    if (method === 'wallet_switchEthereumChain') {
      if (window.refuseSwitch) throw Object.assign(new Error('no'), {code: 4001});
      window.chain = params[0].chainId; return null;
    }
    if (method === 'eth_sendTransaction') {
      if (window.mode === 'reject') throw Object.assign(new Error('User rejected'), {code: 4001});
      if (window.mode === 'error') throw Object.assign(new Error('https://x/?key=SECRET'), {code: -32603});
      await new Promise(r => setTimeout(r, 150));
      return 'HASH';
    }
    throw new Error('FORBIDDEN: ' + method);
  }
};
""".replace("WALLET", WALLET).replace("HASH", HASH)
ALLOWED = {"eth_requestAccounts", "eth_accounts", "eth_chainId", "wallet_switchEthereumChain",
           "eth_sendTransaction"}


@pytest.fixture
def env(live):
    plans = StubPlans()
    flow = WalletFlow(live, ApprovalPlanner(FakeChain(), clock=live.clock), clock=live.clock)
    app = Starlette(routes=wallet_routes(WalletRuntime(live, flow, plans), StubAuth(), StubPaper()))
    served = Served(app)
    global BASE
    BASE = served.base
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        ctx.add_init_script(MOCK)
        errors: list = []
        hooks: dict = {}

        def forward(route):
            """The page talks to the real server; only auth (a header the Telegram/SIWE layer
            would supply) is added, and a test can swap in a hostile response body."""
            req = route.request
            path = req.url[len(BASE):]
            res = route.fetch(headers={**req.headers, "x-user": str(U1)})
            body = res.body()
            hook = hooks.get(path)
            if hook is not None:                 # a hostile/compromised server response
                body = json.dumps(hook(res.json())).encode()
            route.fulfill(response=res, body=body)

        ctx.route(BASE + "/wallet/api/*", forward)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(e))
        yield page, plans, live, hooks
        assert not errors
        assert {c["method"] for c in page.evaluate("window.rpc")} <= ALLOWED
        ctx.close()
        browser.close()
    served.close()


def open_page(page, plan):
    page.goto(f"{BASE}/wallet#intent={SID}&plan={plan.id}")
    page.click("#connect")
    expect(page.locator("#create")).to_be_enabled()


def sends(page):
    return [c for c in page.evaluate("window.rpc") if c["method"] == "eth_sendTransaction"]


def test_buy_happy_path_sends_exactly_the_reviewed_transaction_once(env):
    page, plans, lg, _hooks = env
    p = make_plan("buy", uid=U1, now=lg.clock())
    plans.add(p)
    open_page(page, p)
    page.click("#create")
    expect(page.locator("#send")).to_be_enabled()
    expect(page.locator("#request-title")).to_have_text("Swap")
    page.dblclick("#send")                       # a double click must not double send
    expect(page.locator("#status")).to_contain_text("not a fill")
    (call,) = sends(page)
    tx = call["params"][0]
    assert (tx["from"], tx["to"], tx["data"]) == (WALLET, p.transaction.target, p.transaction.calldata)
    assert int(tx["value"], 16) == p.amount_in_wei
    assert lg.list_orders(U1)[0].state == "submitted"
    expect(page.locator("#send")).to_be_disabled()


def test_rejection_cancels_and_never_resends(env):
    page, plans, lg, _hooks = env
    p = make_plan("buy", uid=U1, now=lg.clock())
    plans.add(p)
    open_page(page, p)
    page.evaluate("window.mode = 'reject'")
    page.click("#create")
    page.click("#send")
    expect(page.locator("#status")).to_contain_text("rejected")
    assert len(sends(page)) == 1 and lg.list_orders(U1)[0].state == "cancelled"


def test_wallet_error_text_is_never_sent_to_the_server_or_audit(env):
    page, plans, lg, _hooks = env
    p = make_plan("buy", uid=U1, now=lg.clock())
    plans.add(p)
    open_page(page, p)
    page.evaluate("window.mode = 'error'")
    page.click("#create")
    page.click("#send")
    expect(page.locator("#status")).to_contain_text("error")
    assert "SECRET" not in json.dumps(lg.get_events(U1, lg.list_orders(U1)[0].id))
    assert lg.list_orders(U1)[0].state == "ambiguous"       # it may have been sent: not "failed"


def sell_plan(plans, lg):
    p = make_plan("sell", status="approval_required", amount_in=E18 // 2, out=E18 * 8 // 10,
                  uid=U1, now=lg.clock())
    plans.add(p)
    return p


def test_sell_shows_a_finite_approval_and_sends_only_that(env):
    page, plans, lg, _hooks = env
    p = sell_plan(plans, lg)
    open_page(page, p)
    page.click("#create")
    expect(page.locator("#request-title")).to_have_text("Finite token approval")
    page.click("#send")
    expect(page.locator("#status")).to_contain_text("not a fill")
    (call,) = sends(page)
    assert call["params"][0]["data"].startswith("0x095ea7b3") and call["params"][0]["to"] == p.token
    assert lg.list_orders(U1)[0].state == "requires_approval"      # never silently a swap


@pytest.mark.parametrize("tamper", [
    "r.eip1193.params[0].data = r.eip1193.params[0].data.replace('3c5caa33d02c78935fc682a8eb295f0e67b93ced','00000000000000000000000000000000000000ff')",
    "r.eip1193.params[0].data = r.eip1193.params[0].data.slice(0, 74) + 'f'.repeat(64)",
    "r.eip1193.params[0].to = '0x' + '99'.repeat(20)",
    "r.eip1193.params[0].from = '0x' + '99'.repeat(20)",
    "delete r.eip1193.params[0].gasPrice",
])
def test_tampered_server_response_is_refused_before_the_wallet_sees_it(env, tamper):
    page, plans, lg, hooks = env
    p = sell_plan(plans, lg)

    hooks["/wallet/api/prompt"] = lambda body: {**body, "request": _apply(tamper, body["request"])}
    open_page(page, p)
    page.click("#create")
    expect(page.locator("#request-warning")).to_contain_text("Refusing to send")
    expect(page.locator("#send")).to_be_disabled()
    assert sends(page) == []


def _apply(expr: str, r: dict) -> dict:
    tx = r["eip1193"]["params"][0]
    if "replace(" in expr:
        tx["data"] = tx["data"].replace("3c5caa33d02c78935fc682a8eb295f0e67b93ced", "0" * 38 + "ff")
    elif "slice(0, 74)" in expr:
        tx["data"] = tx["data"][:74] + "f" * 64
    elif ".to =" in expr:
        tx["to"] = "0x" + "99" * 20
    elif ".from =" in expr:
        tx["from"] = "0x" + "99" * 20
    else:
        tx.pop("gasPrice", None)
    return r


def test_wrong_chain_that_wont_switch_makes_no_server_calls(env):
    page, plans, lg, _hooks = env
    p = make_plan("buy", uid=U1, now=lg.clock())
    plans.add(p)
    page.add_init_script("window.chain = '0x1'; window.refuseSwitch = true;")
    page.goto(f"{BASE}/wallet#intent={SID}&plan={p.id}")
    page.click("#connect")
    expect(page.locator("#status")).to_contain_text("Agora chain")
    expect(page.locator("#create")).to_be_hidden()
    assert lg.list_orders(U1) == []


def test_no_provider_is_explained_not_faked(env):
    page, plans, lg, hooks = env
    p = make_plan("buy", uid=U1, now=lg.clock())
    plans.add(p)
    page.add_init_script("delete window.ethereum;")
    page.goto(f"{BASE}/wallet#intent={SID}&plan={p.id}")
    page.click("#connect")
    expect(page.locator("#status")).to_contain_text("No wallet found")
    assert lg.list_orders(U1) == []
