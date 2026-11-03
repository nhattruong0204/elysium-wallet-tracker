"""Public dashboard: HTTP behaviour, privacy, validation, caching, failure modes.

Runs the real ASGI app in uvicorn on a loopback port, talks to it with `requests`, and
connects to PostgreSQL as the real `dashboard_ro` role. Needs TEST_DATABASE_URL.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import requests
import sqlalchemy as sa
import uvicorn
from conftest import CHAIN, TEST_URL, truncate_all
from test_dashboard_db import (  # noqa: F401  (fixtures + seed constants)
    NOW,
    PRIV,
    PRIVATE_LABEL,
    PUB1,
    PUB2,
    SECRET_TEXT,
    TOK_PRIV,
    TOK_PUB,
    ago,
    seeded,
)

from agora_tracker import schema as t
from agora_tracker.classify import TradeEvent
from agora_tracker.config import (
    AlertConfig,
    ChainConfig,
    Config,
    LeaderboardConfig,
    TelegramConfig,
    WebConfig,
)
from agora_tracker.timerange import RangeError
from agora_tracker.web.app import RateLimiter
from agora_tracker.web.cache import TTLCache
from agora_tracker.web.params import list_params, paginate, parse_range
from agora_tracker.web.run import build

UTC = timezone.utc
E18 = 10 ** 18
DASH_URL = str(sa.engine.make_url(TEST_URL).set(username="dashboard_ro", password=None)
               .render_as_string(hide_password=False)) if TEST_URL else ""
PRIVATE_MARKERS = (PRIV, TOK_PRIV, PRIVATE_LABEL, SECRET_TEXT, "HIDDEN", "123456789",
                   "987654321", "tg_offset")


def make_cfg(**web) -> Config:
    web.setdefault("rate_limit_per_ip", 0)
    return Config(chain=ChainConfig(rpc_urls=["https://rpc.example"], chain_id=CHAIN),
                  leaderboard=LeaderboardConfig(), alerts=AlertConfig(),
                  telegram=TelegramConfig(), web=WebConfig(**web))


class Live:
    def __init__(self, app, engine):
        self.app, self.engine = app, engine
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                                    log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.s = requests.Session()

    def start(self) -> Live:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    def get(self, path: str, **kw) -> requests.Response:
        kw.setdefault("allow_redirects", False)
        kw.setdefault("timeout", 15)
        return self.s.get(self.base + path, **kw)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)
        self.engine.dispose()


@pytest.fixture
def make_live(pg_engine):
    started: list[Live] = []

    def factory(cfg: Config | None = None, url: str | None = None) -> Live:
        app, engine = build(cfg or make_cfg(), url or DASH_URL)
        lv = Live(app, engine).start()
        started.append(lv)
        return lv
    yield factory
    for lv in started:
        lv.stop()


@pytest.fixture
def web(seeded, make_live):
    return make_live()


def seed_estimated(store):
    with store.tx() as c:
        ev = TradeEvent("BUY", "0xe5".ljust(66, "0"), 5000, PUB2, TOK_PUB, 10 * E18,
                        9 * E18, confidence="estimated", hype_method="estimate")
        store.insert_trade(c, ev, ago(minutes=20))


# --- pages render with real data --------------------------------------------------------------
PAGES = ["/", "/tokens", "/smart-money", "/early", "/scanner", "/about",
         f"/tokens/{TOK_PUB}", f"/wallets/{PUB1}", "/smart-money?view=clusters",
         "/smart-money?view=traders-agora", "/smart-money?view=traders-realized",
         "/smart-money?view=exits", "/early?sort=smart", "/?range=7d", "/tokens?range=15m"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(web, path):
    r = web.get(path)
    assert r.status_code == 200, r.text[:400]
    assert "text/html" in r.headers["content-type"]
    assert "<h1" in r.text


def test_overview_shows_public_data_only(web):
    html = web.get("/?range=24h").text
    assert "PUB" in html or TOK_PUB[:6] in html
    for m in PRIVATE_MARKERS:
        assert m not in html, m


def test_nothing_private_on_any_page_or_api_even_after_crawling(web):
    seen, todo = set(), ["/", "/tokens", "/smart-money", "/early", "/scanner",
                         "/api/v1/overview", "/api/v1/tokens", "/api/v1/early",
                         "/api/v1/health", "/api/v1/smart-money?view=clusters"]
    while todo and len(seen) < 60:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        r = web.get(p)
        assert r.status_code in (200, 303), p
        for m in PRIVATE_MARKERS:
            assert m not in r.text, f"{m} leaked on {p}"
        for href in re.findall(r'href="(/(?:tokens|wallets)/0x[0-9a-f]{40})"', r.text):
            todo.append(href)
    assert any(s.startswith("/wallets/") for s in seen) and any(s.startswith("/tokens/0x") for s in seen)


def test_private_token_and_wallet_are_not_found(web):
    for p in (f"/tokens/{TOK_PRIV}", f"/wallets/{PRIV}", f"/api/v1/tokens/{TOK_PRIV}",
              f"/api/v1/wallets/{PRIV}"):
        r = web.get(p)
        assert r.status_code == 404, p
        for m in PRIVATE_MARKERS:
            assert m not in r.text.replace(PRIV[:0], ""), (p, m)
    # a token that is public via other wallets hides the private wallet's share
    html = web.get(f"/tokens/{TOK_PUB}?range=24h").text
    assert PRIV not in html


def test_search_never_confirms_private_things(web):
    r = web.get(f"/search?q={PRIV}")
    assert r.status_code == 404 and "Nothing found" in r.text
    r = web.get(f"/search?q={TOK_PRIV}")
    assert r.status_code in (404,) or TOK_PRIV not in r.headers.get("location", "")
    r = web.get("/search?q=HIDDEN")
    assert r.status_code == 404
    r = web.get("/search?q=PUB")
    assert r.status_code == 303 and r.headers["location"] == f"/tokens/{TOK_PUB}"
    r = web.get(f"/search?q={PUB1}")
    assert r.status_code == 303 and r.headers["location"] == f"/wallets/{PUB1}"
    assert web.get("/search?q=").status_code == 303
    assert web.get("/search?q=" + "x" * 500).status_code == 404


# --- time ranges -------------------------------------------------------------------------------
@pytest.mark.parametrize("qs", [
    "range=bogus", "range=custom", "range=custom&start=2026-11-02&end=2026-11-01",
    "range=custom&start=2099-01-01&end=2099-01-02", "range=custom&start=2020-01-01&end=2020-01-02",
    "range=custom&start=2026-01-01&end=2026-06-01", "range=custom&start=garbage&end=nonsense",
    "range=custom&start=2026-11-01T00:00&end=2026-11-01T00:00",
])
def test_invalid_ranges_are_rejected_with_a_message(web, qs):
    for base in ("/", "/tokens", "/smart-money", "/early", "/api/v1/overview"):
        r = web.get(f"{base}?{qs}")
        assert r.status_code == 400, (base, qs, r.status_code)
        assert "Invalid time range" in r.text or "Invalid time range" in r.json().get("error", "")


def test_custom_range_is_half_open_and_labelled_utc(web, seeded):
    start = (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    end = (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    r = web.get(f"/?range=custom&start={start}&end={end}")
    assert r.status_code == 200 and "[start, end)" in r.text and "UTC" in r.text
    j = web.get(f"/api/v1/overview?range=custom&start={start}&end={end}").json()
    assert j["meta"]["range"]["preset"] is None
    assert j["meta"]["range"]["start"].endswith("Z")


def test_parse_range_unit():
    now = datetime(2026, 11, 3, 12, 0, 17, tzinfo=UTC)
    tr = parse_range({"range": "6h"}, now)
    assert tr.end == datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)          # 30 s grid
    assert tr.end - tr.start == timedelta(hours=6) and tr.preset == "6h"
    assert parse_range({}, now).preset == "24h"
    c = parse_range({"range": "custom", "start": "2026-11-01 08:00", "end": "2026-11-01T09:30:00Z"}, now)
    assert (c.start, c.end, c.preset) == (datetime(2026, 11, 1, 8, tzinfo=UTC),
                                           datetime(2026, 11, 1, 9, 30, tzinfo=UTC), None)
    with pytest.raises(RangeError):
        parse_range({"range": "custom", "start": "2026-10-01", "end": "2026-11-03"}, now, 30)


def test_trade_on_the_end_instant_is_excluded(seeded, make_live):
    boundary = NOW - timedelta(minutes=45)
    with seeded.tx() as c:
        ev = TradeEvent("BUY", "0xb0".ljust(66, "0"), 7000, PUB1, TOK_PUB, E18, 5 * E18,
                        confidence="high", hype_method="balance_diff")
        seeded.insert_trade(c, ev, int(boundary.timestamp()))
    lv = make_live()
    s0 = (boundary - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
    at = boundary.strftime("%Y-%m-%dT%H:%M:%S")
    excl = lv.get(f"/api/v1/overview?range=custom&start={s0}&end={at}").json()
    incl = lv.get(f"/api/v1/overview?range=custom&start={at}&end="
                  f"{(boundary + timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S')}").json()
    assert Decimal(excl["totals"]["buy"]) == 0          # trade at `end` is not in [start, end)
    assert Decimal(incl["totals"]["buy"]) == 5          # trade at `start` is


# --- tokens: search, sort, pagination --------------------------------------------------------------
@pytest.fixture
def many_tokens(seeded):
    with seeded.tx() as c:
        for i in range(60):
            tok = "0x" + f"{0xc000 + i:04x}" * 10
            c.execute(sa.insert(t.token_metadata).values(chain_id=CHAIN, address=tok,
                                                         symbol=f"T{i:02d}", decimals=18))
            ev = TradeEvent("BUY", f"0x{i:02x}ee".ljust(66, "0"), 9000 + i, PUB1, tok, E18,
                            (i + 1) * E18 // 10, confidence="high", hype_method="balance_diff")
            seeded.insert_trade(c, ev, ago(minutes=i + 1))
    return seeded


def test_tokens_pagination_search_and_sort(many_tokens, make_live):
    lv = make_live()
    j = lv.get("/api/v1/tokens").json()
    assert j["totals"]["tokens"] == 61 and j["page"]["size"] == 25 and j["page"]["pages"] == 3
    assert len(j["page"]["items"]) == 25
    p3 = lv.get("/api/v1/tokens?page=3").json()["page"]
    assert len(p3["items"]) == 11 and p3["page"] == 3
    assert lv.get("/api/v1/tokens?page=999").json()["page"]["page"] == 3        # clamped
    assert lv.get("/api/v1/tokens?page=abc").json()["page"]["page"] == 1
    q = lv.get("/api/v1/tokens?q=t07").json()
    assert q["totals"]["matching"] == 1 and q["page"]["items"][0]["symbol"] == "T07"
    byaddr = lv.get(f"/api/v1/tokens?q={TOK_PUB[:10]}").json()
    assert byaddr["totals"]["matching"] == 1
    asc = lv.get("/api/v1/tokens?sort=buy&dir=asc").json()["page"]["items"]
    desc = lv.get("/api/v1/tokens?sort=buy&dir=desc").json()["page"]["items"]
    assert float(asc[0]["buy"]) <= float(asc[-1]["buy"]) and float(desc[0]["buy"]) >= float(desc[-1]["buy"])
    assert lv.get("/api/v1/tokens?sort=DROP%20TABLE").status_code == 200       # falls back
    html = lv.get("/tokens?q=t0&sort=symbol&dir=asc").text
    assert 'aria-sort="ascending"' in html and "T00" in html
    assert lv.get("/tokens?q=zzzzzz").text.count("No tokens match") == 1


def test_list_params_and_paginate_unit():
    lp = list_params({"q": "  a\x00b ", "sort": "nope", "dir": "asc", "page": "-4"},
                     ("x", "y"), "y")
    assert (lp.q, lp.sort, lp.desc, lp.page) == ("ab", "y", False, 1)
    p = paginate(list(range(53)), 3, 25)
    assert p["items"] == [50, 51, 52] and p["pages"] == 3 and p["total"] == 53
    assert paginate([], 5, 25)["page"] == 1


# --- token / wallet detail -------------------------------------------------------------------
def test_token_page_content_and_address_validation(web):
    r = web.get(f"/tokens/{TOK_PUB.upper().replace('0X', '0x')}")
    assert r.status_code == 200 and "Tracked wallet participation" in r.text
    assert "Market snapshot" in r.text and "stored, not live" in r.text
    assert "elyscan.live/token/" in r.text and "noopener" in r.text
    assert web.get("/tokens/0xnotanaddress").status_code == 404
    assert web.get("/tokens/" + "0x" + "00" * 20).status_code == 404
    j = web.get(f"/api/v1/tokens/{TOK_PUB}").json()
    assert j["token"] == TOK_PUB and j["flow"]["q_buyers"] == 2
    wallets = {b["wallet"] for b in j["buyers"]}
    assert wallets == {PUB1, PUB2}


def test_wallet_page_keeps_agora_and_measured_pnl_apart(web):
    html = web.get(f"/wallets/{PUB1}").text
    assert "Agora PnL (all-time)" in html and "Measured realized PnL" in html
    j = web.get(f"/api/v1/wallets/{PUB1}").json()
    assert j["rank"] == 1 and Decimal(j["agora_pnl"]) == Decimal("500")
    assert Decimal(j["realized"]["measured"]["pnl"]) == Decimal("0.5")
    assert j["trades"]["size"] == 25


# --- verified vs estimated, accuracy against direct SQL ------------------------------------------
def test_estimated_is_separate_and_numbers_match_the_database(seeded, make_live):
    seed_estimated(seeded)
    lv = make_live()
    j = lv.get("/api/v1/overview?range=24h").json()
    eng = sa.create_engine(DASH_URL)
    with eng.connect() as c:
        verified = c.execute(sa.text(
            "SELECT coalesce(sum(hype_wei),0)/1e18 FROM trades WHERE kind='BUY' AND "
            "confidence='high' AND block_time >= :s AND block_time < :e"),
            {"s": datetime.fromisoformat(j["meta"]["range"]["start"].replace("Z", "+00:00")),
             "e": datetime.fromisoformat(j["meta"]["range"]["end"].replace("Z", "+00:00"))}
        ).scalar()
        est = c.execute(sa.text("SELECT coalesce(sum(hype_wei),0)/1e18 FROM trades "
                                "WHERE confidence='estimated'")).scalar()
        n_wallets = c.execute(sa.text("SELECT count(DISTINCT wallet) FROM trades")).scalar()
    eng.dispose()
    assert Decimal(j["totals"]["buy"]) == Decimal(verified) == Decimal("3")     # estimate excluded
    assert Decimal(j["totals"]["buy_est"]) == Decimal(est) == Decimal("9")
    assert j["totals"]["wallets"] == n_wallets == 2
    html = lv.get("/").text
    assert "Estimated (not measured)" in html and "excluded above" in html


# --- empty, stale, partial -------------------------------------------------------------------------
def test_empty_database_renders_honest_empty_states(pg_engine, make_live):
    truncate_all(pg_engine)
    lv = make_live()
    for p in ("/", "/tokens", "/smart-money", "/early", "/scanner", "/smart-money?view=clusters",
              "/smart-money?view=traders-agora"):
        r = lv.get(p)
        assert r.status_code == 200, p
    assert "No tokens match" in lv.get("/tokens").text
    assert "Nothing to show" in lv.get("/smart-money").text
    h = lv.get("/api/v1/health").json()["health"]
    assert h["status"] in ("down", "degraded")
    assert lv.get("/").text.count("Partial coverage") == 1


def test_stale_heartbeat_marks_scanner_down_and_overview_warns(seeded, make_live):
    seeded.set_kv("heartbeat", str(int(NOW.timestamp()) - 3600))
    lv = make_live()
    h = lv.get("/api/v1/health").json()["health"]
    assert h["status"] == "down"
    assert any("Indexer heartbeat" in r for r in h["reasons"])
    assert "Scanner is down" in lv.get("/").text or "Scanner problem" in lv.get("/scanner").text
    seeded.set_kv("heartbeat", str(int(time.time())))
    lv2 = make_live()
    assert lv2.get("/api/v1/health").json()["health"]["checks"][0]["state"] == "ok"


def test_health_page_is_public_safe(web):
    html = web.get("/scanner").text
    assert "rpc.example" not in html and "http" not in re.sub(r'href="[^"]*"', "", html).replace(
        "http-equiv", "").lower().replace("https://onchainflow", "")
    j = json.dumps(web.get("/api/v1/health").json())
    for bad in ("disk", "postgres", "password", "tg_offset", "db_bytes", "token", "secret"):
        assert bad not in j.lower().replace("tokens", ""), bad


# --- failure modes -----------------------------------------------------------------------------------
def test_database_outage_is_a_503_with_retry_and_healthz_fails(make_live, monkeypatch):
    from sqlalchemy import exc as sa_exc
    lv = make_live()
    assert lv.get("/healthz").status_code == 200

    def down(*a, **k):
        raise sa_exc.OperationalError("connect", {}, Exception("connection refused"))
    monkeypatch.setattr(lv.engine, "connect", down)
    r = lv.get("/tokens")
    assert r.status_code == 503 and "Data temporarily unavailable" in r.text
    assert r.headers.get("retry-after")
    assert "connection refused" not in r.text                       # internals stay private
    assert lv.get("/healthz").status_code == 503
    j = lv.get("/api/v1/tokens")
    assert j.status_code == 503 and j.json()["error"]


def test_statement_timeout_becomes_a_503_not_a_hang(seeded, make_live, monkeypatch):
    from agora_tracker.web.reports import PublicReports
    real = PublicReports.market_overview

    def slow(self, tr):
        with self.ro() as c:
            c.execute(sa.text("SELECT pg_sleep(2)"))
        return real(self, tr)
    monkeypatch.setattr(PublicReports, "market_overview", slow)
    lv = make_live(make_cfg(query_timeout_ms=150))
    t0 = time.time()
    r = lv.get("/")
    assert r.status_code == 503 and time.time() - t0 < 1.5
    assert "ref " in r.text


def test_stale_page_is_served_when_the_database_fails_after_a_good_result():
    now = [0.0]
    cache = TTLCache(ttl_s=30, stale_s=300, clock=lambda: now[0])
    assert cache.get("k", lambda: {"v": 1}).value == {"v": 1}
    now[0] = 100.0                                  # expired, within the stale window

    def boom():
        raise RuntimeError("db down")
    hit = cache.get("k", boom)
    assert hit.stale and hit.value == {"v": 1} and cache.stale_served == 1
    now[0] = 1000.0                                 # too old: error propagates
    with pytest.raises(RuntimeError):
        cache.get("k", boom)


def test_cache_is_single_flight_and_bounded():
    calls = []
    cache = TTLCache(ttl_s=30, max_entries=4)
    gate = threading.Event()

    def compute():
        calls.append(1)
        gate.wait(1)
        return "x"
    ts = [threading.Thread(target=lambda: cache.get("k", compute)) for _ in range(8)]
    for th in ts:
        th.start()
    time.sleep(0.2)
    gate.set()
    for th in ts:
        th.join()
    assert len(calls) == 1 and cache.hits == 7
    for i in range(20):
        cache.get(i, lambda i=i: i)
    assert len(cache) <= 4


def test_concurrent_requests_share_one_computation(seeded, make_live, monkeypatch):
    from agora_tracker.web.reports import PublicReports
    real, n = PublicReports.market_overview, []

    def counted(self, tr):
        n.append(1)
        return real(self, tr)
    monkeypatch.setattr(PublicReports, "market_overview", counted)
    lv = make_live()
    out: list[int] = []

    def hit():
        out.append(lv.get("/").status_code)
    ts = [threading.Thread(target=hit) for _ in range(12)]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    assert out == [200] * 12 and len(n) == 1


def test_refuses_to_start_with_a_privileged_database_role(pg_engine):
    with pytest.raises(SystemExit, match="not the read-only dashboard role"):
        build(make_cfg(), TEST_URL)


# --- headers, assets, escaping, rate limit -----------------------------------------------------------
def test_security_headers_and_no_external_resources(web):
    r = web.get("/", headers={"X-Forwarded-Proto": "https"})
    h = r.headers
    assert "default-src 'none'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["strict-transport-security"].startswith("max-age=")
    for page_path in PAGES + ["/search?q=zzz", "/nope"]:      # CSP forbids inline style/script
        body = web.get(page_path).text
        assert "<script>" not in body and "onclick" not in body, page_path
        assert 'style="' not in body, f"inline style on {page_path} would be blocked by the CSP"
    assert not re.findall(r'(?:src|href)="https?://(?!elyscan|www\.agoradex|www\.thekleos|boxa|elysp)', r.text)
    css = web.get("/static/app.css?v=x")
    assert css.status_code == 200 and "immutable" in css.headers["cache-control"]
    assert web.get("/static/../../etc/passwd").status_code == 404


def test_user_input_and_stored_text_are_escaped(seeded, make_live):
    evil = "<img src=x onerror=alert(1)>"
    with seeded.tx() as c:
        c.execute(sa.text("UPDATE token_profiles SET symbol = :s, name = :s WHERE address = :a"),
                  {"s": evil, "a": TOK_PUB})
    lv = make_live()
    for p in ("/tokens", f"/tokens/{TOK_PUB}", "/", "/early", "/smart-money"):
        assert "<img src=x" not in lv.get(p).text, p
    assert "&lt;img" in lv.get(f"/tokens/{TOK_PUB}").text
    r = lv.get("/search?q=%3Cscript%3Ealert(1)%3C/script%3E")
    assert "<script>alert" not in r.text


def test_rate_limiter_and_loopback_exemption(make_live, seeded):
    rl = RateLimiter(rate=2.0, burst=3)
    got = [rl.allow("1.2.3.4", now=100.0) for _ in range(5)]
    assert got == [True, True, True, False, False]
    assert rl.allow("1.2.3.4", now=101.0)                       # refilled
    assert rl.allow("5.6.7.8", now=100.0)                       # separate bucket
    lv = make_live(make_cfg(rate_limit_per_ip=0.001, rate_burst=2))
    assert all(lv.get("/about").status_code == 200 for _ in range(10))   # loopback: exempt
    codes = [lv.get("/about", headers={"X-Forwarded-For": "9.9.9.9"}).status_code
             for _ in range(5)]
    assert codes[:2] == [200, 200] and 429 in codes
    assert lv.get("/healthz", headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 200


def test_healthz_and_meta(web):
    r = web.get("/healthz")
    assert r.status_code == 200 and r.json()["database"] == "ok"
    m = web.get("/api/v1/meta").json()
    assert m["interval"] == "[start, end)" and "24h" in m["ranges"] and m["chain_id"] == CHAIN
    assert web.get("/robots.txt").text.count("Disallow: /api/") == 1
    assert web.get("/nope").status_code == 404
    assert web.get("/api/v1/nope").status_code == 404


def test_light_load_does_not_exhaust_the_connection_limit(web):
    codes: list[int] = []

    def worker():
        for p in ("/", "/tokens", "/smart-money", "/early", "/scanner"):
            codes.append(web.get(p).status_code)
    ts = [threading.Thread(target=worker) for _ in range(8)]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    assert codes.count(200) == len(codes) == 40
    with sa.create_engine(TEST_URL).connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM pg_stat_activity WHERE usename = 'dashboard_ro'")).scalar()
    assert n <= 5
