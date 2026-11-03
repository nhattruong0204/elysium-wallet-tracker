"""Activity monitor in a real browser: polling, scroll preservation, pause, visibility,
failure handling, CSP-clean rendering and mobile layout. Skipped when Chromium is missing."""

from __future__ import annotations

import json
import re

import pytest
from test_activity import add, kinds, many  # noqa: F401
from test_dashboard_db import NOW, PUB1, seeded  # noqa: F401
from test_web import make_cfg, make_live  # noqa: F401

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - no browser installed: skip, do not fail
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture
def ctx(browser):
    c = browser.new_context(viewport={"width": 1280, "height": 900})
    yield c
    c.close()


def open_activity(ctx, lv, query="range=all", poll=5, hidden=0, **kw):
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.clock.install()
    page.goto(f"{lv.base}/activity?{query}")
    page.wait_for_selector("#act-body")
    page.errors = errors
    return page


def live(make_live, **kw):
    kw.setdefault("activity_cache_ttl_s", 0)
    kw.setdefault("activity_poll_s", 5)
    kw.setdefault("activity_hidden_poll_s", 0)
    return make_live(make_cfg(**kw))


def row_ids(page):
    return [int(x) for x in page.eval_on_selector_all("#act-body tr[data-id]",
                                                      "els => els.map(e => e.dataset.id)")]


def until(page, expr, timeout=5.0):
    """Poll a JS expression with evaluate (wait_for_function needs eval, which the page's CSP
    rightly forbids)."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        if page.evaluate(expr):
            return
        time.sleep(0.05)
    raise AssertionError(f"condition never became true: {expr}")


def advance(page, ms):
    page.clock.run_for(ms)


def wait_row(page, rid):
    page.wait_for_selector(f'#act-body tr[data-id="{rid}"]', timeout=5000)


def test_new_rows_appear_at_the_top_without_reload(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    before = row_ids(page)
    marker = page.evaluate("() => { window.__same = 1; return 1 }")
    new = add(kinds, "BUY", "0xb1", seconds_ago=1, block=9900, hype=7.5)
    advance(page, 7000)
    wait_row(page, new)
    ids = row_ids(page)
    assert ids[0] == new and ids[1:] == before
    assert page.evaluate("() => window.__same") == marker      # same document: no page reload
    assert page.locator("tr.fresh-row").count() >= 1
    assert "7.500" in page.inner_text(f'tr[data-id="{new}"]')
    assert page.locator("#act-new").is_hidden()
    assert page.errors == []                                   # no CSP violations or JS errors


def test_scrolled_down_users_keep_their_place_and_get_a_banner(many, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv, "range=all&limit=100")
    page.evaluate("window.scrollTo(0, 900)")
    until(page, "window.scrollY > 500")
    y0 = page.evaluate("window.scrollY")
    first_before = row_ids(page)[0]
    for i in range(3):
        add(many, "BUY", f"0xb{i}", seconds_ago=1, block=9900 + i)
    advance(page, 7000)
    page.wait_for_selector("#act-new:not([hidden])")
    assert page.inner_text("#act-new-n") == "3"
    assert "new activities" in page.inner_text("#act-new")
    assert abs(page.evaluate("window.scrollY") - y0) <= 2      # scroll position untouched
    assert row_ids(page)[0] == first_before                    # nothing inserted above the fold
    # more arrivals just raise the count
    add(many, "SELL", "0xb9", seconds_ago=1, block=9990)
    advance(page, 12000)
    until(page, "document.getElementById('act-new-n').textContent === '4'")
    page.click("#act-show")
    until(page, "window.scrollY < 5")
    assert row_ids(page)[0] > first_before and len(set(row_ids(page))) == len(row_ids(page))
    assert page.locator("#act-new").is_hidden()


def test_pending_rows_flush_when_the_user_returns_to_the_top(many, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv, "range=all&limit=100")
    page.evaluate("window.scrollTo(0, 900)")
    until(page, "window.scrollY > 500")
    new = add(many, "BUY", "0xba", seconds_ago=1, block=9991)
    advance(page, 7000)
    page.wait_for_selector("#act-new:not([hidden])")
    page.evaluate("window.scrollTo(0, 0)")
    advance(page, 400)
    wait_row(page, new)


def test_pause_and_resume(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    calls: list[str] = []
    page.on("request", lambda r: calls.append(r.url) if "/api/v1/activity" in r.url else None)
    page.click("#act-auto")
    assert page.get_attribute("#act-auto", "aria-pressed") == "false"
    assert "OFF" in page.inner_text("#act-auto")
    new = add(kinds, "BUY", "0xb2", seconds_ago=1, block=9901)
    advance(page, 120000)
    assert calls == [] and new not in row_ids(page)           # paused: no traffic at all
    page.click("#act-auto")
    wait_row(page, new)
    assert len(calls) >= 1 and "ON" in page.inner_text("#act-auto")
    assert "since_id=" in calls[0]


def test_hidden_tab_stops_polling_and_catches_up_when_visible(kinds, make_live, ctx):
    lv = live(make_live, activity_hidden_poll_s=0)
    page = open_activity(ctx, lv)
    calls: list[str] = []
    page.on("request", lambda r: calls.append(r.url) if "/api/v1/activity" in r.url else None)

    def set_hidden(v):
        page.evaluate("""(v) => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => v});
                                  document.dispatchEvent(new Event('visibilitychange')); }""", v)
    set_hidden(True)
    new = add(kinds, "BUY", "0xb3", seconds_ago=1, block=9902)
    advance(page, 600000)
    assert calls == []
    set_hidden(False)
    wait_row(page, new)
    assert len(calls) == 1


def test_hidden_tab_can_poll_slowly_when_configured(kinds, make_live, ctx):
    lv = live(make_live, activity_hidden_poll_s=120)
    page = open_activity(ctx, lv)
    calls: list[str] = []
    page.on("request", lambda r: calls.append(r.url) if "/api/v1/activity" in r.url else None)
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                             document.dispatchEvent(new Event('visibilitychange')); }""")
    advance(page, 60000)
    n_fast = len(calls)
    assert n_fast <= 1                                          # 120 s cadence, not 5 s


def test_failures_back_off_then_recover(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    hits = []
    mode = {"fail": True}

    def handler(route):
        hits.append(1)
        if mode["fail"]:
            route.fulfill(status=503, body='{"error":"x"}', content_type="application/json")
        else:
            route.continue_()
    page.route(re.compile(r".*/api/v1/activity\?.*since_id.*"), handler)
    advance(page, 7000)
    page.wait_for_selector("#act-err:not([hidden])")
    assert "Connection problem" in page.inner_text("#act-err")
    n1 = len(hits)
    advance(page, 60000)
    assert len(hits) - n1 <= 6                                   # backing off, not hammering
    assert len(hits) - n1 < 12                                   # 5 s fixed would be 12 polls
    mode["fail"] = False
    new = add(kinds, "BUY", "0xb4", seconds_ago=1, block=9903)
    for _ in range(40):
        advance(page, 30000)
        if page.locator(f'#act-body tr[data-id="{new}"]').count():
            break
    wait_row(page, new)
    assert page.locator("#act-err").is_hidden()


def test_rate_limit_is_respected(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    hits = []

    def handler(route):
        hits.append(1)
        route.fulfill(status=429, headers={"Retry-After": "30"}, content_type="application/json",
                      body='{"error":"Too many requests"}')
    page.route(re.compile(r".*/api/v1/activity\?.*since_id.*"), handler)
    advance(page, 7000)
    page.wait_for_selector("#act-err:not([hidden])")
    assert "Rate limited" in page.inner_text("#act-err")
    n = len(hits)
    advance(page, 20000)
    assert len(hits) == n                                       # waited out Retry-After


def test_requests_never_overlap(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    state = {"open": 0, "max": 0, "n": 0}

    def handler(route):
        state["n"] += 1
        state["open"] += 1
        state["max"] = max(state["max"], state["open"])
        route.continue_()
        state["open"] -= 1
    page.on("request", lambda r: None)
    page.route(re.compile(r".*/api/v1/activity\?.*since_id.*"), handler)
    # a slow response: keep the request open far longer than the poll interval
    page.evaluate("""() => { const f = window.fetch; window.__inflight = 0; window.__max = 0;
        window.fetch = (...a) => { window.__inflight++; window.__max = Math.max(window.__max, window.__inflight);
          return f(...a).then(r => new Promise(res => setTimeout(() => { window.__inflight--; res(r); }, 8000))); }; }""")
    for _ in range(60):
        advance(page, 1000)
    assert page.evaluate("window.__max") == 1
    assert page.evaluate("window.__inflight") <= 1


def test_duplicates_from_the_server_are_ignored(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    existing = lv.get("/api/v1/activity", params={"range": "all", "limit": 5}).json()

    def handler(route):
        body = {**existing, "overflow": False, "count": 5, "head_id": 10 ** 9}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    page.route(re.compile(r".*/api/v1/activity\?.*since_id.*"), handler)
    n = len(row_ids(page))
    advance(page, 7000)
    advance(page, 7000)
    assert len(row_ids(page)) == n and len(set(row_ids(page))) == n


def test_scanner_going_stale_turns_the_indicator_red(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    assert "sdot live" in page.inner_html("#act-status") or "sdot delayed" in page.inner_html("#act-status")
    kinds.set_kv("heartbeat", str(int(NOW.timestamp()) - 3600))
    advance(page, 7000)
    page.wait_for_selector("#act-dot.stale")
    assert "stale" in page.inner_text("#act-scanner")
    assert page.locator("#act-stale").count() == 1
    kinds.set_kv("heartbeat", str(int(__import__("time").time())))
    advance(page, 40000)
    until(page, "!document.getElementById('act-dot').classList.contains('stale')")
    assert page.locator("#act-stale").count() == 0


def norm(h):
    return re.sub(r"\s+", " ", re.sub(r">\s+<", "><", h)).replace("&amp;", "&").replace("&#39;", "'").strip()


def test_js_rows_match_server_rows(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    api = lv.get("/api/v1/activity", params={"range": "all", "limit": 100}).json()["items"]
    mismatches = []
    for it in api:
        server = page.eval_on_selector(f'#act-body tr[data-id="{it["id"]}"]', "e => e.outerHTML")
        client = page.evaluate("(it) => ActivityMonitor.renderRow(it).outerHTML", it)
        if norm(server) != norm(client):
            mismatches.append((it["kind"], norm(server), norm(client)))
    assert not mismatches, (mismatches[0][0], mismatches[0][1], mismatches[0][2])


def test_local_time_toggle_is_display_only(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    t0 = page.inner_text("#act-body tr td.nw")
    assert t0.endswith("UTC")
    page.check("#act-local")
    t1 = page.inner_text("#act-body tr td.nw")
    assert "UTC" not in t1 or page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == "UTC"
    assert page.get_attribute("#act-body tr time", "datetime").endswith("Z")   # stored value stays UTC
    page.uncheck("#act-local")
    assert page.inner_text("#act-body tr td.nw") == t0


def test_load_older_appends_without_navigating(many, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv, "range=all&limit=25")
    page.evaluate("window.__same = 7")
    assert len(row_ids(page)) == 25
    page.click("#act-older")
    until(page, "document.querySelectorAll('#act-body tr').length === 50")
    ids = row_ids(page)
    assert len(set(ids)) == 50 and page.evaluate("window.__same") == 7
    full = [r["id"] for r in lv.get("/api/v1/activity", params={"range": "all", "limit": 50}).json()["items"]]
    assert ids == full


def test_non_live_views_do_not_poll(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv, "range=all&sort=oldest")
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/api/v1/activity" in r.url else None)
    advance(page, 120000)
    assert calls == [] and page.locator("#act-auto").count() == 0


def test_mobile_layout_has_no_page_level_horizontal_scroll(kinds, make_live, browser):
    lv = live(make_live)
    c = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    try:
        page = c.new_page()
        page.goto(f"{lv.base}/activity?range=all")
        page.wait_for_selector("#act-body")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
        assert page.is_visible("#act-auto")
        assert page.is_visible("#act-status")
    finally:
        c.close()


def test_other_pages_still_load_cleanly_with_usd(kinds, make_live, ctx):
    lv = live(make_live)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    for path in ("/", "/tokens", "/early", "/smart-money", "/scanner"):
        page.goto(lv.base + path)
        assert page.locator("h1").count() == 1
    assert errors == []
