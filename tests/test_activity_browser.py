"""Activity monitor in a real browser: polling, scroll preservation, pause, visibility,
failure handling, CSP-clean rendering and mobile layout. Skipped when Chromium is missing."""

from __future__ import annotations

import json
import os
import re
import time

import pytest
from test_activity import add, kinds, many  # noqa: F401
from test_dashboard_db import NOW, PUB1, seeded  # noqa: F401
from test_web import make_cfg, make_live  # noqa: F401

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=os.environ.get("TEST_CHROMIUM_EXECUTABLE"))
        except Exception as e:  # noqa: BLE001 - no browser installed: skip, do not fail
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture(autouse=True)
def fresh_heartbeat(request):
    """`seeded` stamps the heartbeat at import time; in a long suite that is minutes old by the
    time a test runs and the scanner would read as stale. Stamp it again right before the test."""
    if "kinds" in request.fixturenames or "many" in request.fixturenames:
        store = request.getfixturevalue("kinds" if "kinds" in request.fixturenames else "many")
        store.set_kv("heartbeat", str(int(time.time())))


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
    # small fake-clock steps with real time in between: a 30 s jump would also fire the page's
    # 10 s request timeout before the (real) server has answered
    for _ in range(400):
        advance(page, 2000)
        page.wait_for_timeout(250)
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


# --- HYPE amount filter -------------------------------------------------------------------------
def plain_page(ctx, lv, query):
    """A page without the fake clock, for tests that navigate (form submits, links)."""
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.goto(f"{lv.base}/activity?{query}")
    page.wait_for_selector("form#act-form")
    return page


def test_amount_filter_keeps_polling_filtered(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv, "range=all&min_hype=2")
    assert page.input_value("input[name=min_hype]") == "2"
    calls: list[str] = []
    page.on("request", lambda r: calls.append(r.url) if "/api/v1/activity" in r.url else None)
    small = add(kinds, "BUY", "0xd1", seconds_ago=2, block=9910, hype=0.2)
    big = add(kinds, "BUY", "0xd2", seconds_ago=1, block=9911, hype=8)
    advance(page, 7000)
    wait_row(page, big)
    assert small not in row_ids(page)                              # filtered out of live updates too
    assert calls and all("min_hype=2" in c and "since_id=" in c for c in calls)
    assert page.errors == []


def test_amount_filter_preset_and_reset(kinds, make_live, ctx):
    lv = live(make_live)
    page = plain_page(ctx, lv, "range=all&min_hype=2&action=buy")
    page.click('a.btn.sm:text-is("1+")')
    page.wait_for_url(re.compile(r"min_hype=1(&|$)"))
    assert "range=all" in page.url and "action=buy" in page.url          # other filters survive
    assert page.input_value("input[name=min_hype]") == "1"
    assert "amount at least 1 HYPE" in page.inner_text("body")
    page.click("a >> text=clear amount")
    page.wait_for_url(re.compile(r"^(?!.*min_hype)"))
    assert "action=buy" in page.url and page.input_value("input[name=min_hype]") == ""
    page.goto(f"{lv.base}/activity?range=all&min_hype=2&max_hype=9")
    page.click("a.btn >> text=Reset")
    page.wait_for_url(re.compile(r"/activity$"))
    assert page.input_value("input[name=min_hype]") == "" and page.input_value("input[name=max_hype]") == ""
    assert page.errors == []


def test_amount_filter_custom_range_submit(kinds, make_live, ctx):
    lv = live(make_live)
    page = plain_page(ctx, lv, "range=all")
    page.fill("input[name=min_hype]", "1")
    page.fill("input[name=max_hype]", "3")
    page.click("#act-form button[type=submit]")
    page.wait_for_url(re.compile(r"min_hype=1"))
    assert "max_hype=3" in page.url
    assert "amount 1 to 3 HYPE" in page.inner_text("body")
    # an invalid value is stopped by the browser before the server sees it
    page.fill("input[name=min_hype]", "1e3")
    assert page.eval_on_selector("input[name=min_hype]", "e => e.validity.patternMismatch") is True
    assert page.errors == []


def test_unread_unique_batches_acknowledge_and_sound(kinds, make_live, ctx):
    page = open_activity(ctx, live(make_live))
    title = page.title()
    page.evaluate("""() => {
      Object.defineProperty(document, 'hasFocus', {configurable:true, value:()=>false});
      window.tones=0;
      window.AudioContext=class {
        constructor(){this.state='suspended';this.currentTime=0;this.destination={};}
        resume(){this.state='running';return Promise.resolve();}
        createOscillator(){return {frequency:{},connect(){},start(){window.tones++;},stop(){}};}
        createGain(){return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}};}
      };
    }""")
    page.click('#act-sound')
    assert page.inner_text('#act-sound') == 'Sound: ON'
    for i in range(3):
        add(kinds, 'BUY', f'0xa79{i}', seconds_ago=1, block=9950+i)
    page.evaluate('ActivityMonitor.poll()')
    until(page, "document.getElementById('act-unread').textContent === '3 new'")
    assert page.title() == '(3) ' + title
    assert page.evaluate('window.tones') == 1
    page.evaluate('ActivityMonitor.poll()')
    advance(page, 1000)
    assert page.title() == '(3) ' + title
    assert page.evaluate('window.tones') == 1
    page.click('#act-unread')
    assert page.title() == title
    assert page.locator('#act-unread').is_hidden()
    assert page.errors == []


def test_sound_rejection_keeps_visual_notifications(kinds, make_live, ctx):
    page = open_activity(ctx, live(make_live))
    assert page.inner_text('#act-sound') == 'Sound: OFF'
    page.evaluate("""() => {window.AudioContext=class {
      resume(){return Promise.reject(new Error('blocked'));}
    }; }""")
    page.click('#act-sound')
    until(page, "document.getElementById('act-audio-status').textContent.includes('blocked')")
    assert page.get_attribute('#act-sound', 'aria-pressed') == 'false'


def test_launchpad_ssr_live_parity(kinds, make_live, ctx):
    page = open_activity(ctx, live(make_live))
    ssr = page.locator('#act-body td:nth-child(4) a.lp').first
    assert ssr.get_attribute('href').startswith('https://')
    first = page.locator('#act-body tr[data-id]').first.get_attribute('data-id')
    item = page.request.get(page.url.replace('/activity?', '/api/v1/activity?')).json()['items'][0]
    page.evaluate('(it) => {document.querySelector("#act-body").prepend(ActivityMonitor.renderRow(it));}', item)
    links = page.locator(f'#act-body tr[data-id="{first}"] td:nth-child(4) a.lp')
    assert links.count() == 2
    assert links.nth(0).get_attribute('href') == links.nth(1).get_attribute('href')
    assert links.nth(0).inner_text() == links.nth(1).inner_text()
    assert links.nth(0).get_attribute('rel') == 'noopener noreferrer'


def test_fomo_column_ssr_matches_live_rows_and_never_links_wrong_chain(kinds, make_live, ctx):
    lv = live(make_live)
    page = open_activity(ctx, lv)
    assert page.locator('#act-table thead th').all_inner_texts().count('Fomo') == 1
    api = page.request.get(page.url.replace('/activity?', '/api/v1/activity?')).json()
    item = api['items'][0]
    expected = f"https://fomo.family/coin?address={item['token']}&chainId=99901"
    assert item['fomo_url'] == expected
    original = page.locator('#act-body tr[data-id]').first
    assert original.locator('td:nth-child(5) a.fomo').get_attribute('href') == expected
    assert original.locator('td:nth-child(5) a.fomo').get_attribute('rel') == 'noopener noreferrer'
    client = page.evaluate('(it) => ActivityMonitor.renderRow(it).outerHTML', item)
    assert f'href="{expected}"' in client
    forged = {**item, 'fomo_url': 'https://evil.example/redirect'}
    forged_row = page.evaluate('(it) => ActivityMonitor.renderRow(it).outerHTML', forged)
    assert 'evil.example' not in forged_row
    assert '<td><span class="faint">–</span></td>' in forged_row


def test_fomo_link_carries_into_incremental_activity_api(kinds, make_live):
    lv = live(make_live)
    all_items = lv.get('/api/v1/activity', params={'range': 'all'}).json()['items']
    assert all_items
    data = lv.get('/api/v1/activity', params={'range': 'all', 'since_id': 0}).json()
    assert data['items']
    assert all(it['fomo_url'] ==
               f"https://fomo.family/coin?address={it['token']}&chainId=99901"
               for it in data['items'])


def test_duplicate_poll_payload_and_stale_response_do_not_notify(kinds, make_live, ctx):
    page = open_activity(ctx, live(make_live))
    original = page.title()
    item = page.request.get(page.url.replace('/activity?', '/api/v1/activity?')).json()['items'][0]
    item['id'] = 987654
    payload = {'items': [item, item, item], 'head_id': 987654, 'meta': {'stale': True}}
    page.route(re.compile(r'.*/api/v1/activity\?.*since_id.*'),
               lambda route: route.fulfill(json=payload))
    page.evaluate("Object.defineProperty(document, 'hasFocus', {configurable:true,value:()=>false})")
    page.evaluate('ActivityMonitor.poll()')
    page.wait_for_timeout(200)
    assert page.title() == original
    payload['meta']['stale'] = False
    page.evaluate('ActivityMonitor.poll()')
    until(page, "document.getElementById('act-unread').textContent === '1 new'")
    page.evaluate('ActivityMonitor.poll()')
    page.wait_for_timeout(200)
    assert page.title() == '(1) ' + original
    assert page.inner_text('#act-sound') == 'Sound: OFF'
    page.click('#act-unread')
    assert page.title() == original


def test_overflow_preserves_unread_until_reload(kinds, make_live, ctx):
    page = open_activity(ctx, live(make_live))
    title = page.title()
    item = page.request.get(page.url.replace('/activity?', '/api/v1/activity?')).json()['items'][0]
    item['id'] = 987654
    page.route(re.compile(r'.*/api/v1/activity\?.*since_id.*'),
               lambda route: route.fulfill(json={'items': [item], 'overflow': True, 'head_id': 987654}))
    page.evaluate("Object.defineProperty(document, 'hasFocus', {configurable:true,value:()=>false})")
    page.evaluate('ActivityMonitor.poll()')
    until(page, "document.getElementById('act-unread').textContent === '1 new'")
    page.click('#act-unread')
    assert page.title() == '(1) ' + title
    assert page.locator('#act-new a').inner_text() == 'Reload to see the newest activity'


def test_multiple_tabs_coalesce_sound(kinds, make_live, ctx):
    lv = live(make_live)
    pages = [open_activity(ctx, lv), open_activity(ctx, lv)]
    for page in pages:
        page.evaluate("""() => {
          Object.defineProperty(document,'hasFocus',{configurable:true,value:()=>false});
          window.tones=0;
          window.AudioContext=class {
            constructor(){this.state='running';this.currentTime=0;this.destination={};}
            resume(){return Promise.resolve();}
            createOscillator(){return {frequency:{},connect(){},start(){window.tones++;},stop(){}};}
            createGain(){return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}};}
          };
        }""")
        page.click('#act-sound')
    add(kinds, 'BUY', '0xa7988', seconds_ago=1, block=9970)
    for page in pages:
        page.evaluate('ActivityMonitor.poll()')
        until(page, "document.getElementById('act-unread').textContent === '1 new'")
    assert sum(p.evaluate('window.tones') for p in pages) == 1
    for page in pages:
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        assert page.title().startswith('(1) ')


def test_saved_sound_preference_requires_new_gesture(kinds, make_live, ctx):
    page = open_activity(ctx, live(make_live))
    page.evaluate("localStorage.setItem('act.sound', '1')")
    page.reload()
    page.wait_for_selector('#act-sound')
    assert page.inner_text('#act-sound') == 'Enable sound'
    assert page.get_attribute('#act-sound', 'aria-pressed') == 'false'
    assert 'click to enable' in page.inner_text('#act-audio-status')


@pytest.mark.parametrize(('pad', 'label', 'base', 'kind'), [
    ('ascend', 'Ascend', 'https://app.ascendlaunch.xyz/', 'platform'),
    ('montra', 'Montra', 'https://montramarkets.xyz/token/', 'token'),
    ('obol', 'Obol', 'https://tryobol.world/coin/', 'token'),
])
def test_platform_homepage_ssr_and_live_parity(kinds, make_live, ctx, pad, label, base, kind):
    import sqlalchemy as sa
    with kinds.tx() as conn:
        conn.execute(sa.text("UPDATE token_profiles SET launchpad=:pad"), {"pad": pad})
    page = open_activity(ctx, live(make_live))
    ssr = page.locator('#act-body a.lp').first
    expected_title = f'Open {label} platform (homepage)' if kind == 'platform' else f'Open token on {label}'
    assert ssr.get_attribute('href').startswith(base)
    assert ssr.get_attribute('title') == expected_title
    item = page.request.get(page.url.replace('/activity?', '/api/v1/activity?')).json()['items'][0]
    assert item['launchpad_url_kind'] == kind
    assert item['launchpad_url'] == base + (item['token'] if kind == 'token' else '')
    page.evaluate('(it) => {document.querySelector("#act-body").prepend(ActivityMonitor.renderRow(it));}', item)
    new = page.locator('#act-body a.lp').first
    assert new.get_attribute('href') == ssr.get_attribute('href')
    assert new.get_attribute('title') == expected_title
    assert new.get_attribute('rel') == 'noopener noreferrer'
