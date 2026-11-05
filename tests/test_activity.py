"""Activity monitor: filters, classification, keyset pagination, incremental updates, privacy.

Everything runs through the real ASGI app against PostgreSQL as the `dashboard_ro` role.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from conftest import CHAIN, role_url
from test_dashboard_db import NOW, PRIV, PUB1, PUB2, TOK_PRIV, TOK_PUB, ago, seeded  # noqa: F401
from test_web import PRIVATE_MARKERS, make_cfg, make_live, web  # noqa: F401

from agora_tracker import schema as t
from agora_tracker.classify import TradeEvent
from agora_tracker.web import activity as A

E18 = 10 ** 18
API = "/api/v1/activity"
TOK_B = "0x" + "2b" * 20


def add(store, kind, tx, wallet=PUB1, token=TOK_PUB, hype=1.0, seconds_ago=60, block=5000,
        confidence="high", method="balance_diff"):
    wei = None if hype is None else int(Decimal(str(hype)) * E18)
    with store.tx() as c:
        return store.insert_trade(
            c, TradeEvent(kind, tx.ljust(66, "0"), block, wallet, token, 10 * E18, wei,
                          confidence=confidence, hype_method=method),
            int(NOW.timestamp()) - seconds_ago)


@pytest.fixture
def kinds(seeded):
    """One leg of every stored kind, plus two legs of a single tx (distinct tokens)."""
    with seeded.tx() as c:
        c.execute(sa.insert(t.token_profiles).values(chain_id=CHAIN, address=TOK_B, symbol="BEE",
                                                     name="Bee", launchpad="boxa"))
    add(seeded, "BUY", "0xa1", hype=3.0, seconds_ago=50, block=6001)
    add(seeded, "SELL", "0xa2", hype=2.0, seconds_ago=49, block=6002)
    add(seeded, "SWAP", "0xa3", hype=None, seconds_ago=48, block=6003)
    add(seeded, "TRANSFER_IN", "0xa4", hype=None, seconds_ago=47, block=6004)
    add(seeded, "TRANSFER_OUT", "0xa5", hype=None, seconds_ago=46, block=6005,
        confidence="unknown", method="none")
    add(seeded, "MULTI", "0xa6", hype=None, seconds_ago=45, block=6006, confidence="unknown",
        method="none")
    add(seeded, "BUY", "0xa7", token=TOK_PUB, hype=1.0, seconds_ago=44, block=6007,
        confidence="estimated", method="estimate")
    add(seeded, "BUY", "0xa8", token=TOK_PUB, hype=0.5, seconds_ago=43, block=6008)
    add(seeded, "BUY", "0xa8", token=TOK_B, hype=0.25, seconds_ago=43, block=6008)   # same tx
    return seeded


def get(web, **q):
    return web.get(API, params=q)


def items(web, **q):
    r = get(web, **q)
    assert r.status_code == 200, r.text
    return r.json()["items"]


# --- classification and the supported kinds --------------------------------------------------
def test_every_stored_kind_is_listed_with_its_own_action(kinds, make_live):
    lv = make_live()
    rows = items(lv, range="all", limit=100)
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    assert {"BUY", "SELL", "SWAP", "TRANSFER_IN", "TRANSFER_OUT", "MULTI"} <= set(by_kind)
    for kind, action in (("BUY", "buy"), ("SELL", "sell"), ("SWAP", "swap"),
                         ("TRANSFER_IN", "transfer_in"), ("TRANSFER_OUT", "transfer_out"),
                         ("MULTI", "other")):
        assert {r["action"] for r in by_kind[kind]} == {action}


def test_transfers_are_never_buys_or_sells(kinds, make_live):
    lv = make_live()
    for action in ("transfer_in", "transfer_out"):
        rows = items(lv, range="all", action=action)
        assert rows and all(r["kind"].startswith("TRANSFER") for r in rows)
        assert all(r["amount_hype"] is None for r in rows)
    buys = items(lv, range="all", action="buy")
    assert buys and all(r["kind"] == "BUY" for r in buys)
    html = lv.get("/activity?range=all&action=transfer_in").text
    assert "no HYPE leg" in html and "act-transfer_in" in html and "act-buy" not in html


def test_verified_estimated_unknown_stay_distinct(kinds, make_live):
    lv = make_live()
    basis = {r["tx_hash"][:4]: r["basis"] for r in items(lv, range="all", limit=100)}
    assert basis["0xa1"] == "verified" and basis["0xa7"] == "estimated"
    assert basis["0xa5"] == "unknown"
    assert {r["basis"] for r in items(lv, range="all", basis="estimated")} == {"estimated"}
    assert {r["basis"] for r in items(lv, range="all", basis="verified")} == {"verified"}
    assert {r["basis"] for r in items(lv, range="all", basis="unknown")} == {"unknown"}


def test_multiple_legs_of_one_tx_are_distinct_records(kinds, make_live):
    rows = [r for r in items(make_live(), range="all", limit=100) if r["tx_hash"].startswith("0xa8")]
    assert len(rows) == 2 and {r["token"] for r in rows} == {TOK_PUB, TOK_B}
    assert len({r["id"] for r in rows}) == 2


def test_item_contract(kinds, make_live):
    r = items(make_live(), range="all", action="buy", limit=1)[0]
    assert set(r) >= {"id", "block_time", "block_number", "tx_hash", "wallet", "token", "symbol",
                      "kind", "action", "basis", "amount_hype", "tx_url", "qty_units"}
    assert isinstance(r["id"], int) and r["block_time"].endswith("Z")
    assert r["tx_url"].startswith("https://") and r["tx_hash"] in r["tx_url"]
    assert not any(k.startswith("_") for k in r)


# --- filters ----------------------------------------------------------------------------------
def test_time_ranges_and_custom_half_open(kinds, make_live):
    lv = make_live()
    assert len(items(lv, range="15m", limit=100)) >= 9
    # the seeded fixture has legs up to 2 h old: 15m excludes them, 4h includes them
    assert len(items(lv, range="4h", limit=100)) > len(items(lv, range="15m", limit=100))
    start = (NOW - timedelta(seconds=50)).strftime("%Y-%m-%d %H:%M:%S")
    end = (NOW - timedelta(seconds=47)).strftime("%Y-%m-%d %H:%M:%S")      # excludes the 47s-old leg
    got = items(lv, range="custom", start=start, end=end, limit=100)
    assert {r["tx_hash"][:4] for r in got} == {"0xa1", "0xa2", "0xa3"}
    for rng in ("15m", "1h", "4h", "12h", "24h", "7d", "30d", "all"):
        assert get(lv, range=rng).status_code == 200


def test_search_by_symbol_address_and_hash(kinds, make_live):
    lv = make_live()
    assert {r["token"] for r in items(lv, range="all", q="bee")} == {TOK_B}           # exact, any case
    assert {r["token"] for r in items(lv, range="all", q="$BE")} == {TOK_B}          # partial symbol
    assert items(lv, range="all", q="zzzz") == []
    assert {r["token"] for r in items(lv, range="all", q=TOK_B)} == {TOK_B}          # token address
    assert {r["wallet"] for r in items(lv, range="all", q=PUB2)} == {PUB2}           # wallet address
    full = items(lv, range="all", action="sell")[0]["tx_hash"]
    assert [r["tx_hash"] for r in items(lv, range="all", q=full)] == [full]          # exact hash
    assert [r["tx_hash"] for r in items(lv, range="all", q=full[:14])] == [full]     # partial hash
    assert [r["tx_hash"] for r in items(lv, range="all", tx=full)] == [full]
    assert {r["wallet"] for r in items(lv, range="all", wallet=PUB2)} == {PUB2}
    assert {r["token"] for r in items(lv, range="all", token=TOK_B)} == {TOK_B}


def test_like_wildcards_in_search_are_literal(kinds, make_live):
    lv = make_live()
    assert get(lv, range="all", q="%").status_code == 400        # not a valid search at all
    assert get(lv, range="all", q="b_e").status_code == 200
    assert items(lv, range="all", q="b_e") == []          # '_' is not a wildcard


def test_private_wallets_never_appear_whatever_the_filter(kinds, make_live):
    lv = make_live()
    priv_tx = "0x04".ljust(66, "0")                        # PRIV's leg in the seed
    for q in ({"wallet": PRIV}, {"q": PRIV}, {"token": TOK_PRIV}, {"q": TOK_PRIV},
              {"tx": priv_tx}, {"q": priv_tx}, {"q": priv_tx[:12]}, {"q": "HIDDEN"},
              {"q": "0x" + "b9" * 3}):
        r = get(lv, range="all", **q)
        assert r.status_code == 200, (q, r.text)
        assert r.json()["items"] == [], q
        assert r.json()["total"]["count"] == 0, q
    everything = lv.get(API, params={"range": "all", "limit": 100}).text
    for marker in PRIVATE_MARKERS:
        assert marker not in everything
    assert PRIV not in lv.get("/activity?range=all").text


def test_sorting(kinds, make_live):
    lv = make_live()
    newest = items(lv, range="all", limit=100)
    assert [r["block_time"] for r in newest] == sorted((r["block_time"] for r in newest), reverse=True)
    oldest = items(lv, range="all", sort="oldest", limit=100)
    assert [r["id"] for r in oldest] == [r["id"] for r in sorted(newest, key=lambda r: (r["block_time"], r["id"]))]
    big = items(lv, range="all", sort="largest_buy", limit=100)
    amounts = [Decimal(r["amount_hype"]) for r in big]
    assert amounts == sorted(amounts, reverse=True)
    assert {r["kind"] for r in big} == {"BUY"} and {r["basis"] for r in big} == {"verified"}
    assert Decimal(big[0]["amount_hype"]) >= Decimal("3")
    sells = items(lv, range="all", sort="largest_sell", limit=100)
    assert {r["kind"] for r in sells} == {"SELL"}


# --- validation --------------------------------------------------------------------------------
@pytest.mark.parametrize("q", [
    {"action": "hodl"}, {"range": "3w"}, {"sort": "random"}, {"basis": "maybe"},
    {"range": "custom"}, {"range": "custom", "start": "2026-01-02", "end": "2026-01-01"},
    {"range": "custom", "start": "garbage", "end": "2026-01-01"},
    {"wallet": "0x123"}, {"token": "nothex"}, {"tx": "0xabc"}, {"limit": "0"}, {"limit": "1000"},
    {"limit": "x"}, {"cursor": "!!!"}, {"cursor": "e30"}, {"dir": "sideways"},
    {"q": "0x12"}, {"q": "x" * 200}, {"q": "a b c"}, {"since_id": "abc"}, {"since_id": "-5x"},
    {"sort": "largest_buy", "action": "sell"},
])
def test_invalid_input_is_a_400_with_json(kinds, make_live, q):
    r = get(make_live(), **q)
    assert r.status_code == 400, (q, r.text[:200])
    body = r.json()
    assert body["error"] and body["detail"]
    assert "Traceback" not in r.text


def test_cursor_is_bound_to_its_sort(kinds, make_live):
    lv = make_live()
    cur = lv.get(API, params={"range": "all", "limit": 2}).json()["page"]["next_cursor"]
    assert get(lv, range="all", limit=2, cursor=cur).status_code == 200
    assert get(lv, range="all", limit=2, cursor=cur, sort="oldest").status_code == 400
    forged = base64.urlsafe_b64encode(json.dumps({"s": "newest", "k": "x", "i": 1}).encode()).decode()
    assert get(lv, range="all", cursor=forged).status_code == 400
    sql_ish = base64.urlsafe_b64encode(json.dumps(
        {"s": "newest", "k": "1; DROP TABLE trades", "i": 1}).encode()).decode()
    assert get(lv, range="all", cursor=sql_ish).status_code == 400
    assert get(lv, range="all", since_id=1, cursor=cur).status_code == 400
    assert get(lv, range="all", since_id=1, sort="oldest").status_code == 400


def test_sql_injection_attempts_are_inert(kinds, make_live):
    lv = make_live()
    for q in ("'; DROP TABLE trades; --", "x' OR '1'='1", "BEE' UNION SELECT 1 --"):
        r = get(lv, range="all", q=q)
        assert r.status_code in (200, 400)
        if r.status_code == 200:
            assert r.json()["items"] == []
    assert items(lv, range="all")                         # table is intact


# --- pagination ---------------------------------------------------------------------------------
@pytest.fixture
def many(seeded):
    """130 legs, many sharing the same block_time (the hard case for keyset paging)."""
    with seeded.tx() as c:
        for i in range(130):
            seeded.insert_trade(c, TradeEvent(
                "BUY" if i % 3 else "SELL", f"0xc{i:03d}".ljust(66, "0"), 7000 + i // 5, PUB1,
                TOK_PUB, E18, (i + 1) * E18 // 10, confidence="high", hype_method="balance_diff"),
                int(NOW.timestamp()) - 3000 - (i // 5) * 10)       # five legs per timestamp
    return seeded


def walk(lv, direction_key="next_cursor", dir_="next", start=None, **q):
    out, cur, pages = [], start, 0
    while True:
        params = {"range": "all", "limit": 25, **q}
        if cur:
            params.update(cursor=cur, dir=dir_)
        j = lv.get(API, params=params).json()
        out.append([r["id"] for r in j["items"]])
        pages += 1
        cur = j["page"][direction_key]
        if not cur or pages > 50:
            return out


def test_pages_cover_everything_once_in_order(many, make_live):
    lv = make_live()
    full = items(lv, range="all", limit=100)
    allids = []
    j = lv.get(API, params={"range": "all", "limit": 100}).json()
    while True:
        allids += [r["id"] for r in j["items"]]
        if not j["page"]["next_cursor"]:
            break
        j = lv.get(API, params={"range": "all", "limit": 100, "cursor": j["page"]["next_cursor"]}).json()
    pages = walk(lv)
    flat = [i for p in pages for i in p]
    assert flat == allids and len(flat) == len(set(flat))
    assert len(flat) >= 130 and len(pages[0]) == 25
    # oldest first is the exact reverse
    rev = [i for p in walk(lv, sort="oldest") for i in p]
    assert rev == list(reversed(flat))
    assert full[0]["id"] == flat[0]


def test_going_back_returns_the_same_rows(many, make_live):
    lv = make_live()
    j1 = lv.get(API, params={"range": "all", "limit": 25}).json()
    j2 = lv.get(API, params={"range": "all", "limit": 25, "cursor": j1["page"]["next_cursor"]}).json()
    assert j2["page"]["has_prev"] and not j1["page"]["has_prev"]
    back = lv.get(API, params={"range": "all", "limit": 25, "cursor": j2["page"]["prev_cursor"],
                               "dir": "prev"}).json()
    assert [r["id"] for r in back["items"]] == [r["id"] for r in j1["items"]]
    assert not back["page"]["has_prev"] and back["page"]["has_next"]


def test_no_gaps_or_duplicates_when_rows_arrive_between_pages(many, make_live):
    lv = make_live()
    j1 = lv.get(API, params={"range": "all", "limit": 25}).json()
    seen = [r["id"] for r in j1["items"]]
    for k in range(3):                                           # live inserts at the head
        add(many, "BUY", f"0xd{k}", seconds_ago=1, block=9000 + k)
    cur = j1["page"]["next_cursor"]
    while cur:
        j = lv.get(API, params={"range": "all", "limit": 25, "cursor": cur}).json()
        seen += [r["id"] for r in j["items"]]
        cur = j["page"]["next_cursor"]
    assert len(seen) == len(set(seen))                           # no duplicate
    baseline = [i for p in walk(lv, sort="oldest") for i in p]
    new_ids = set(baseline) - set(seen)
    assert len(new_ids) == 3                                     # only the three new head rows missing


def test_largest_sort_pages_without_gaps_on_ties(many, make_live):
    lv = make_live()
    flat = [i for p in walk(lv, sort="largest_buy") for i in p]
    expected = [r["id"] for r in sorted(
        (r for r in items(lv, range="all", limit=100) if r["kind"] == "BUY"), key=lambda r: r["id"])]
    assert len(flat) == len(set(flat)) and set(flat) >= set(expected)


def test_total_count_is_exact_and_capped(many, make_live, monkeypatch):
    lv = make_live()
    j = lv.get(API, params={"range": "all", "limit": 5}).json()
    assert j["total"] == {"count": len(walk_all(lv)), "capped": False}
    monkeypatch.setattr("agora_tracker.web.reports.TOTAL_CAP", 40)
    lv2 = make_live(make_cfg(activity_cache_ttl_s=0))
    j = lv2.get(API, params={"range": "all", "limit": 5}).json()
    assert j["total"] == {"count": 40, "capped": True}
    assert "40+" in lv2.get("/activity?range=all").text


def walk_all(lv):
    return [i for p in walk(lv) for i in p]


def test_window_reports_coverage(kinds, make_live):
    j = make_live().get(API, params={"range": "all"}).json()
    assert j["window"]["range"] == "all" and j["window"]["retention_start"].endswith("Z")
    old = make_live().get(API, params={"range": "custom", "start": "2025-02-01", "end": "2025-02-02"}).json()
    assert old["items"] == [] and old["window"]["coverage"] == "partial"
    assert "tracking began" in old["window"]["note"]


# --- incremental updates ----------------------------------------------------------------------
def test_since_id_returns_only_new_matching_rows(kinds, make_live):
    lv = make_live(make_cfg(activity_cache_ttl_s=0))
    base = lv.get(API, params={"range": "all"}).json()
    head = base["head_id"]
    assert lv.get(API, params={"range": "all", "since_id": head}).json()["items"] == []
    a = add(kinds, "BUY", "0xe1", seconds_ago=2, block=9100)
    b = add(kinds, "SELL", "0xe2", seconds_ago=1, block=9101)
    inc = lv.get(API, params={"range": "all", "since_id": head}).json()
    assert [r["id"] for r in inc["items"]] == [b, a] and inc["count"] == 2
    assert inc["head_id"] == b and inc["overflow"] is False
    only_buys = lv.get(API, params={"range": "all", "since_id": head, "action": "buy"}).json()
    assert [r["id"] for r in only_buys["items"]] == [a]
    again = lv.get(API, params={"range": "all", "since_id": b}).json()
    assert again["items"] == [] and again["head_id"] == b
    # a private wallet's new leg is invisible to the poll
    priv = add(kinds, "BUY", "0xe3", wallet=PRIV, seconds_ago=1, block=9102)
    assert priv > b
    assert lv.get(API, params={"range": "all", "since_id": b}).json()["items"] == []


def test_since_overflow_is_flagged(kinds, make_live):
    lv = make_live(make_cfg(activity_cache_ttl_s=0, activity_since_limit=5))
    head = lv.get(API, params={"range": "all"}).json()["head_id"]
    ids = [add(kinds, "BUY", f"0xf{i}", seconds_ago=1, block=9200 + i) for i in range(8)]
    j = lv.get(API, params={"range": "all", "since_id": head}).json()
    assert j["overflow"] is True and j["count"] == 5
    assert j["head_id"] == max(ids)                       # the cursor still advances


def test_late_indexed_old_block_is_caught_by_the_watermark(kinds, make_live):
    lv = make_live(make_cfg(activity_cache_ttl_s=0))
    head = lv.get(API, params={"range": "all"}).json()["head_id"]
    late = add(kinds, "BUY", "0xe9", seconds_ago=3000, block=100)   # older block, higher id
    j = lv.get(API, params={"range": "all", "since_id": head}).json()
    assert [r["id"] for r in j["items"]] == [late]


def test_api_is_cached_briefly_and_supports_conditional_requests(kinds, make_live):
    lv = make_live()
    r1 = lv.get(API, params={"range": "all", "limit": 5})
    etag = r1.headers["ETag"]
    assert r1.headers["Cache-Control"] == "private, no-cache"
    r2 = lv.s.get(lv.base + API, params={"range": "all", "limit": 5},
                  headers={"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b""
    add(kinds, "BUY", "0xe5", seconds_ago=1, block=9300)
    lv3 = make_live(make_cfg(activity_cache_ttl_s=0))
    r3 = lv3.s.get(lv3.base + API, params={"range": "all", "limit": 5}, headers={"If-None-Match": etag})
    assert r3.status_code == 200


# --- freshness -------------------------------------------------------------------------------------
def test_freshness_blocks_are_separate_and_honest(kinds, make_live):
    fr = make_live().get(API, params={"range": "all"}).json()["freshness"]
    for k in ("server_time", "scanner_status", "scanner_note", "indexer_heartbeat_age_s",
              "last_indexed_block", "indexer_checkpoint_at", "last_observed_transaction_at",
              "last_observed_transaction_age_s", "head_id", "chain_head_block", "lag_blocks"):
        assert k in fr
    assert fr["scanner_status"] in ("live", "delayed")
    assert fr["last_indexed_block"] == 2000


@pytest.mark.parametrize("hb_age,ckpt_age,status", [
    (10, 10, "live"), (150, 10, "delayed"), (10, 400, "delayed"), (400, 10, "stale"),
    (10, 1200, "stale"), (None, 10, "unknown")])
def test_scanner_status_is_never_live_when_indexing_stopped(kinds, make_live, hb_age, ckpt_age, status):
    if hb_age is None:
        with kinds.tx() as c:
            c.execute(sa.text("DELETE FROM service_state WHERE key = 'heartbeat'"))
    else:
        kinds.set_kv("heartbeat", str(int(NOW.timestamp()) + 0))
    from datetime import datetime, timezone

    from agora_tracker.web.service import activity_freshness
    now = datetime.now(timezone.utc)
    head = {"heartbeat_age_s": hb_age, "cursor": 5, "chain_head": 6, "lag_blocks": 1,
            "cursor_at": now - timedelta(seconds=ckpt_age), "last_trade_at": now - timedelta(hours=2),
            "head_id": 9}
    assert activity_freshness(head, now)["scanner_status"] == status


def test_stale_scanner_shows_a_warning_and_never_green(kinds, make_live):
    kinds.set_kv("heartbeat", str(int(NOW.timestamp()) - 3600))
    html = make_live().get("/activity").text
    assert "Scanner: stale" in html and "sdot stale" in html and "The scanner is not reporting" in html
    assert "sdot live" not in html


# --- pages -------------------------------------------------------------------------------------------
def test_activity_page_renders_filters_pager_and_status(kinds, make_live):
    lv = make_live()
    html = lv.get("/activity?range=24h").text
    assert 'aria-current="page">Activity</a>' in html
    for needle in ("Auto-refresh:", "Last indexed block", "Heartbeat", "Last observed transaction",
                   'id="act-body"', "/static/activity.js", 'name="q"', "Transfer in", "Multi / other",
                   "Largest verified buy", "All history"):
        assert needle in html, needle
    assert 'data-live="1"' in html
    assert 'data-live="0"' in lv.get("/activity?range=all&sort=oldest").text
    assert 'data-live="0"' in lv.get("/activity?range=custom&start=2026-11-01&end=2026-11-02").text


def test_nav_and_overview_link(kinds, make_live):
    lv = make_live()
    nav = lv.get("/").text
    assert 'href="/activity">Activity</a>' in nav and "View All Activity" in nav
    assert "/activity?range=24h" in nav
    assert "/activity?range=12h" in lv.get("/?range=6h").text
    assert "View All Activity" in lv.get("/?range=custom&start=2026-11-03T00:00&end=2026-11-03T01:00").text
    assert "/activity?token=" in lv.get(f"/tokens/{TOK_PUB}").text
    assert f"/activity?wallet={PUB1}" in lv.get(f"/wallets/{PUB1}").text


def test_overview_keeps_the_newest_15_preview(many, make_live):
    html = make_live().get("/?range=24h").text
    seg = html.split('id="rec"')[1]
    assert seg.count("<tr>") - 1 <= 15


def test_page_errors_are_html_not_json(kinds, make_live):
    r = make_live().get("/activity?action=bogus")
    assert r.status_code == 400 and "text/html" in r.headers["content-type"]
    assert "Invalid parameter" in r.text


def test_pager_links_carry_filters_and_cursors(many, make_live):
    lv = make_live()
    html = lv.get("/activity?range=all&action=buy&limit=25").text
    assert "cursor=" in html and "dir=next" in html and "action=buy" in html
    j = lv.get(API, params={"range": "all", "action": "buy", "limit": 25}).json()
    page2 = lv.get("/activity", params={"range": "all", "action": "buy", "limit": 25,
                                        "cursor": j["page"]["next_cursor"], "dir": "next"})
    assert page2.status_code == 200 and "← Newer" in page2.text and 'rel="prev"' in page2.text


# --- empty and failure modes -----------------------------------------------------------------------------
def test_empty_database_renders_an_empty_state(pg_engine, make_live):
    from conftest import truncate_all
    truncate_all(pg_engine)
    lv = make_live()
    r = lv.get("/activity")
    assert r.status_code == 200 and "No matching activity" in r.text
    j = lv.get(API).json()
    assert j["items"] == [] and j["total"]["count"] == 0
    assert j["page"]["next_cursor"] is None and j["freshness"]["scanner_status"] == "unknown"
    assert lv.get(API, params={"since_id": 5}).json()["items"] == []


def test_rate_limit_on_the_api_is_json_429(kinds, make_live):
    lv = make_live(make_cfg(rate_limit_per_ip=0.001, rate_burst=2))
    codes = []
    for _ in range(6):
        r = lv.s.get(lv.base + API, headers={"X-Forwarded-For": "203.0.113.9"})
        codes.append(r.status_code)
        if r.status_code == 429:
            assert r.headers["Retry-After"] and r.json()["retry_after_s"] >= 1
    assert 429 in codes and codes[0] == 200


def test_database_outage_is_a_503(kinds, make_live, monkeypatch):
    lv = make_live(make_cfg(activity_cache_ttl_s=0))
    def boom(self, p):
        raise sa.exc.OperationalError("x", {}, Exception("down"))
    monkeypatch.setattr("agora_tracker.web.reports.PublicReports.activity_page", boom)
    r = get(lv, range="all")
    assert r.status_code == 503 and r.json()["error"]


def test_web_role_still_cannot_write(kinds):
    eng = sa.create_engine(role_url())
    try:
        for sql in ("INSERT INTO public_api.trades (id) VALUES (1)", "DELETE FROM public_api.trades",
                    "UPDATE public_api.trades SET kind = 'BUY'"):
            with pytest.raises(sa.exc.DBAPIError), eng.begin() as c:
                c.execute(sa.text(sql))
    finally:
        eng.dispose()


# --- pure units ----------------------------------------------------------------------------------------------
def test_parse_search_classification():
    assert A.parse_search("0x" + "ab" * 32).kind == "tx"
    assert A.parse_search("0x" + "ab" * 20).kind == "address"
    assert A.parse_search("0xabcdef").kind == "hex"
    assert A.parse_search("$Hype").value == "hype"
    assert A.parse_search("  ") is None
    for bad in ("0xabc", "0x" + "ab" * 40, "no spaces!", "a;b"):
        with pytest.raises(A.ParamError):
            A.parse_search(bad)


def test_cursor_round_trip_and_rejects_garbage():
    c = A.encode_cursor("newest", 1_700_000_000_123_456, 42)
    assert A.decode_cursor(c, "newest") == (1_700_000_000_123_456, 42)
    big = A.encode_cursor("largest_buy", "123456789012345678901234567890", 7)
    assert A.decode_cursor(big, "largest_buy") == ("123456789012345678901234567890", 7)
    for bad in ("", "x", "e30", c[:-3], c + "AAAA" * 80):
        with pytest.raises(A.ParamError):
            A.decode_cursor(bad, "newest")


# --- market cap at the time of the row, and launchpad ------------------------------------------
def test_market_cap_is_valued_at_the_rows_block_time(kinds, make_live):
    from agora_tracker.web.usd import format_usd
    with kinds.tx() as c:
        c.execute(sa.insert(t.hype_usd_rates).values(observed_at=NOW - timedelta(minutes=1),
                                                     hype_usd=Decimal("40"), source="test"))
        # TOK_B has only an old snapshot: no market data near the trade, but a known supply
        c.execute(sa.insert(t.token_market_snapshots).values(
            chain_id=CHAIN, token=TOK_B, captured_at=NOW - timedelta(days=2), source="agora",
            price_hype=Decimal("0.01"), mcap_hype=100))
    rows = items(make_live(), range="all", limit=100)
    pub = next(r for r in rows if r["tx_hash"].startswith("0xa1"))
    assert Decimal(str(pub["market_cap_usd"])) == Decimal("40000")     # 1000 HYPE x $40, snapshot
    assert pub["market_cap_source"] == "snapshot" and pub["market_cap_text"] == format_usd(40000)
    bee = next(r for r in rows if r["tx_hash"].startswith("0xa8") and r["token"] == TOK_B)
    # 0.25 HYPE for 10 tokens = 0.025/token x 10,000 supply = 250 HYPE x $40
    assert Decimal(str(bee["market_cap_usd"])) == Decimal("10000")
    assert bee["market_cap_source"] == "trade_price"
    assert bee["launchpad"] == "boxa" and bee["launchpad_label"] == "BOXA"
    transfer = next(r for r in rows if r["kind"] == "TRANSFER_IN")
    assert transfer["market_cap_source"] == "snapshot"          # TOK_PUB snapshot is near
    page = make_live().get("/activity", params={"range": "all"}).text
    assert "MC (USD)" in page and "Launchpad" in page and ">Tx<" not in page
    assert "BOXA" in page and 'class="txl"' in page


def test_market_cap_missing_without_a_rate(kinds, make_live):
    rows = items(make_live(), range="all", limit=100)
    assert all(r["market_cap_usd"] is None and r["market_cap_text"] is None for r in rows)


def test_muted_tokens_are_hidden_unless_filtered_for(kinds, make_live):
    cfg = make_cfg()
    cfg.alerts.mute_tokens = [TOK_B]
    lv = make_live(cfg)
    rows = items(lv, range="all", limit=100)
    assert rows and all(r["token"] != TOK_B for r in rows)
    assert get(lv, range="all").json()["total"]["count"] == len(rows)
    only = items(lv, range="all", token=TOK_B)
    assert only and all(r["token"] == TOK_B for r in only)           # explicit filter still works
    assert TOK_B[:8] not in lv.get("/activity", params={"range": "all"}).text


def test_ascend_label_comes_from_the_chain_not_agora(kinds, make_live):
    with kinds.tx() as c:
        c.execute(sa.insert(t.ascend_launches).values(
            chain_id=CHAIN, token=TOK_B, pool_id="0x" + "ab" * 32,
            quote="0xba876237b0f021a6c6d852d6c26e107997cc5e81", token_is0=True, fee_pips=10000,
            builder_fee_pips=0, supply_seeded=10**27, dev_buy_tokens=0, creator="0x" + "c1" * 20,
            block_number=1, tx_hash="0x" + "cd" * 32))
    rows = items(make_live(), range="all", limit=100)
    bee = next(r for r in rows if r["token"] == TOK_B)
    assert bee["launchpad"] == "ascend" and bee["launchpad_label"] == "Ascend"   # Agora said boxa
