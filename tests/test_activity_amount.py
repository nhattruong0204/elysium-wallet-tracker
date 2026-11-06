"""Activity HYPE amount filter (min_hype / max_hype): parsing, exact boundaries, NULL semantics,
combination with the other filters, keyset pagination, live polling and cache isolation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from test_activity import API, E18, NOW, TOK_B, TOK_PUB, add, get, items, kinds  # noqa: F401
from test_dashboard_db import PUB1, PUB2, seeded  # noqa: F401
from test_web import make_cfg, make_live, web  # noqa: F401

from agora_tracker.web import amounts as AM


def wei(s: str) -> int:
    return int(Decimal(s) * E18)


@pytest.fixture
def amounts(kinds):  # noqa: F811
    """Boundary rows on top of the shared fixture. Tx names b0..bN are the ones asserted on."""
    add(kinds, "BUY", "0xb0", hype=0, seconds_ago=40, block=7000)                    # zero is known, not NULL
    add(kinds, "BUY", "0xb1", hype="0.1", seconds_ago=39, block=7001)
    add(kinds, "SELL", "0xb2", hype="0.099999999999999999", seconds_ago=38, block=7002)
    add(kinds, "BUY", "0xb3", hype="100", seconds_ago=37, block=7003)
    add(kinds, "BUY", "0xb4", hype="100.000000000000000001", seconds_ago=36, block=7004)
    add(kinds, "BUY", "0xb5", hype="5", seconds_ago=35, block=7005, confidence="estimated", method="estimate")
    add(kinds, "BUY", "0xb6", hype="10", seconds_ago=34, block=7006, confidence="legacy", method="none")
    add(kinds, "SELL", "0xb7", hype="10", seconds_ago=33, block=7007, confidence="unknown", method="none")
    return kinds


def bset(rows):
    return {r["tx_hash"][:4] for r in rows if r["tx_hash"].startswith("0xb")}


def aset(rows):
    return {r["tx_hash"][:4] for r in rows if r["tx_hash"].startswith("0xa")}


# --- parser -----------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expect", [
    (None, None), ("", None), ("  ", None), ("0", Decimal(0)), ("0.5", Decimal("0.5")),
    ("1", Decimal(1)), ("100", Decimal(100)), ("0.000000000000000001", Decimal("1e-18")),
    ("1000000000", Decimal(10 ** 9)),
])
def test_parse_accepts(raw, expect):
    assert AM.parse_hype(raw, "min_hype") == expect


@pytest.mark.parametrize("raw", [
    "-1", "+1", "1e3", "1E3", "0x10", "NaN", "inf", "1,5", "1 000", "1.", ".5", "1.2.3", "abc",
    "1.0000000000000000001", "1000000001", "٣", "１", "1;DROP TABLE trades", "1' OR '1'='1", "9" * 40,
])
def test_parse_rejects(raw):
    with pytest.raises(AM.AmountError):
        AM.parse_hype(raw, "min_hype")


def test_parse_range_orders_and_wei():
    r = AM.parse_range("0.5", "10")
    assert r.min_wei == wei("0.5") and r.max_wei == wei("10") and r.active
    assert AM.parse_range("5", "5").active                           # equal bounds are allowed
    with pytest.raises(AM.AmountError):
        AM.parse_range("10", "5")
    assert not AM.parse_range(None, None).active


def test_decimal_never_goes_through_float():
    assert AM.to_wei(Decimal("0.1")) == 10 ** 17
    assert AM.to_wei(Decimal("0.000000000000000001")) == 1
    assert AM.to_wei(Decimal("123456789.123456789123456789")) == 123456789123456789123456789


# --- semantics --------------------------------------------------------------------------------
def test_min_is_inclusive_and_exact(amounts, make_live):  # noqa: F811
    lv = make_live()
    got = bset(items(lv, range="all", limit=100, min_hype="0.1"))
    assert "0xb1" in got                      # exactly 0.1 matches
    assert "0xb2" not in got                  # 0.1 - 1 wei does not
    assert "0xb0" not in got                  # zero is below any positive minimum
    assert "0xb4" in got


def test_max_is_inclusive_and_exact(amounts, make_live):  # noqa: F811
    lv = make_live()
    got = bset(items(lv, range="all", limit=100, max_hype="100"))
    assert "0xb3" in got and "0xb4" not in got        # 100 + 1 wei excluded
    assert {"0xb0", "0xb1", "0xb2"} <= got            # small and zero amounts are known


def test_zero_is_a_known_amount_but_null_is_not(amounts, make_live):  # noqa: F811
    lv = make_live()
    only_max = items(lv, range="all", limit=100, max_hype="0")
    assert bset(only_max) == {"0xb0"}
    assert aset(only_max) == set()            # the NULL rows (swap/transfers) are never read as 0
    mn = items(lv, range="all", limit=100, min_hype="0")
    assert not {"0xa3", "0xa4", "0xa5", "0xa6"} & aset(mn)   # min=0 still needs a known amount
    assert "0xb0" in bset(mn)


def test_verified_and_estimated_count_unknown_and_legacy_do_not(amounts, make_live):  # noqa: F811
    lv = make_live()
    got = items(lv, range="all", limit=100, min_hype="5", max_hype="10")
    assert "0xb5" in bset(got)                                     # estimated 5
    assert "0xb6" not in bset(got) and "0xb7" not in bset(got)     # legacy / unknown 10
    assert {r["basis"] for r in got} <= {"verified", "estimated"}
    # basis is a separate, composable filter
    est = items(lv, range="all", limit=100, min_hype="1", basis="estimated")
    assert {r["basis"] for r in est} == {"estimated"} and {"0xa7", "0xb5"} <= aset(est) | bset(est)
    ver = items(lv, range="all", limit=100, min_hype="1", basis="verified")
    assert {r["basis"] for r in ver} == {"verified"}


@pytest.mark.parametrize("lo,expected", [
    ("0.1", {"0xa1", "0xa2", "0xa7", "0xa8"}), ("0.5", {"0xa1", "0xa2", "0xa7", "0xa8"}),
    ("1", {"0xa1", "0xa2", "0xa7"}), ("2", {"0xa1", "0xa2"}), ("3", {"0xa1"}),
    ("3.000000000000000001", set()),
])
def test_boundaries_on_seeded_amounts(amounts, make_live, lo, expected):  # noqa: F811
    assert aset(items(make_live(), range="all", limit=100, min_hype=lo)) == expected


@pytest.mark.parametrize("v", AM.PRESETS)
def test_every_preset_is_accepted(amounts, make_live, v):  # noqa: F811
    r = get(make_live(), range="all", min_hype=v)
    assert r.status_code == 200
    assert all(Decimal(x["amount_hype"]) >= Decimal(v) for x in r.json()["items"])


def test_min_max_window(amounts, make_live):  # noqa: F811
    got = items(make_live(), range="all", limit=100, min_hype="1", max_hype="3")
    amts = [Decimal(r["amount_hype"]) for r in got]
    assert amts and all(Decimal(1) <= a <= Decimal(3) for a in amts)
    assert aset(got) == {"0xa1", "0xa2", "0xa7"}


def test_combines_with_action_search_and_range(amounts, make_live):  # noqa: F811
    lv = make_live()
    sells = items(lv, range="all", limit=100, min_hype="1", action="sell")
    assert sells and all(r["kind"] == "SELL" for r in sells)
    assert items(lv, range="all", min_hype="1", action="transfer_in") == []   # transfers have no amount
    # the only BEE leg is 0.25 HYPE: it matches min 0.1 and 0.2 but not 0.3
    assert {r["token"] for r in items(lv, range="all", q="bee", min_hype="0.1")} == {TOK_B}
    assert {r["token"] for r in items(lv, range="all", q="bee", min_hype="0.2")} == {TOK_B}
    assert items(lv, range="all", q="bee", min_hype="0.3") == []
    assert items(lv, range="15m", min_hype="0.1", limit=100)                 # recent window still filtered


def test_sorting_and_largest_sorts_respect_filter(amounts, make_live):  # noqa: F811
    lv = make_live()
    rows = items(lv, range="all", limit=100, min_hype="1", max_hype="50", sort="largest_buy")
    amts = [Decimal(r["amount_hype"]) for r in rows]
    assert amts == sorted(amts, reverse=True) and all(1 <= a <= 50 for a in amts)


# --- validation over HTTP ---------------------------------------------------------------------
@pytest.mark.parametrize("q", [
    {"min_hype": "-1"}, {"min_hype": "abc"}, {"max_hype": "1e5"}, {"min_hype": "10", "max_hype": "5"},
    {"min_hype": "1' OR 1=1 --"}, {"max_hype": "9" * 30}, {"min_hype": "0.0000000000000000001"},
])
def test_invalid_amounts_are_400_and_never_reach_sql(amounts, make_live, q):  # noqa: F811
    lv = make_live()
    r = get(lv, range="all", **q)
    assert r.status_code == 400 and "hype" in r.text.lower()
    assert lv.get("/activity", params={"range": "all", **q}).status_code == 400


def test_empty_values_mean_no_filter(amounts, make_live):  # noqa: F811
    lv = make_live()
    base = [r["id"] for r in items(lv, range="all", limit=100)]
    assert [r["id"] for r in items(lv, range="all", limit=100, min_hype="", max_hype="")] == base


def test_filters_are_echoed_canonically(amounts, make_live):  # noqa: F811
    j = get(make_live(), range="all", min_hype="0050.50", max_hype="100").json()
    assert j["filters"]["min_hype"] == "50.5" and j["filters"]["max_hype"] == "100"
    assert j["filters"]["amount_filter"]["applies_to"] == ["verified", "estimated"]
    j = get(make_live(), range="all").json()
    assert j["filters"]["min_hype"] is None and j["filters"]["amount_filter"] is None


# --- pagination, polling, cache ---------------------------------------------------------------
def test_keyset_pagination_is_stable_under_the_filter(amounts, make_live):  # noqa: F811
    lv = make_live()
    full = [r["id"] for r in items(lv, range="all", limit=100, min_hype="0.1")]
    seen, cursor = [], None
    for _ in range(20):
        q = {"range": "all", "limit": 3, "min_hype": "0.1"}
        if cursor:
            q.update(cursor=cursor, dir="next")
        j = lv.get(API, params=q).json()
        seen += [r["id"] for r in j["items"]]
        cursor = j["page"]["next_cursor"]
        if not cursor:
            break
    assert seen == full and len(set(seen)) == len(seen)


def test_pagination_with_amount_sort(amounts, make_live):  # noqa: F811
    lv = make_live()
    full = [r["id"] for r in items(lv, range="all", limit=100, min_hype="0.1", sort="largest_buy")]
    seen, cursor = [], None
    for _ in range(20):
        q = {"range": "all", "limit": 2, "min_hype": "0.1", "sort": "largest_buy"}
        if cursor:
            q.update(cursor=cursor, dir="next")
        j = lv.get(API, params=q).json()
        seen += [r["id"] for r in j["items"]]
        cursor = j["page"]["next_cursor"]
        if not cursor:
            break
    assert seen == full


def test_total_count_follows_the_filter(amounts, make_live):  # noqa: F811
    lv = make_live(make_cfg(activity_cache_ttl_s=0))
    t_all = lv.get(API, params={"range": "all"}).json()["total"]["count"]
    t_min = lv.get(API, params={"range": "all", "min_hype": "1"}).json()["total"]["count"]
    t_max = lv.get(API, params={"range": "all", "min_hype": "1", "max_hype": "3"}).json()["total"]["count"]
    assert t_all > t_min > t_max > 0


def test_since_id_polling_applies_the_filter(amounts, kinds, make_live):  # noqa: F811
    lv = make_live(make_cfg(activity_cache_ttl_s=0))
    head = lv.get(API, params={"range": "all"}).json()["head_id"]
    small = add(kinds, "BUY", "0xc1", hype="0.2", seconds_ago=3, block=9200)
    big = add(kinds, "BUY", "0xc2", hype="7", seconds_ago=2, block=9201)
    nul = add(kinds, "SWAP", "0xc3", hype=None, seconds_ago=1, block=9202)
    got = lv.get(API, params={"range": "all", "since_id": head, "min_hype": "5"}).json()
    assert [r["id"] for r in got["items"]] == [big]
    assert got["head_id"] == nul                       # the cursor still advances past filtered-out rows
    both = lv.get(API, params={"range": "all", "since_id": head, "min_hype": "0.1", "max_hype": "10"}).json()
    assert [r["id"] for r in both["items"]] == [big, small]


def test_cache_does_not_leak_between_different_filters(amounts, make_live):  # noqa: F811
    lv = make_live()                                     # default cache TTL on
    a = {r["id"] for r in items(lv, range="all", limit=100, min_hype="5")}
    b = {r["id"] for r in items(lv, range="all", limit=100, min_hype="0.1")}
    c = {r["id"] for r in items(lv, range="all", limit=100, min_hype="5", max_hype="50")}
    d = {r["id"] for r in items(lv, range="all", limit=100)}
    assert a < b < d and c <= a
    assert a == {r["id"] for r in items(lv, range="all", limit=100, min_hype="5")}
    ta = lv.get(API, params={"range": "all", "min_hype": "5"}).json()["total"]["count"]
    tb = lv.get(API, params={"range": "all", "min_hype": "0.1"}).json()["total"]["count"]
    assert ta == len(a) and tb == len(b)
    # equal values written differently share a key and a result
    assert {r["id"] for r in items(lv, range="all", limit=100, min_hype="5.0")} == a


# --- HTML -------------------------------------------------------------------------------------
def test_page_renders_inputs_presets_chip_and_reset(amounts, make_live):  # noqa: F811
    lv = make_live()
    html = lv.get("/activity", params={"range": "all", "min_hype": "0.5", "max_hype": "100"}).text
    assert 'name="min_hype"' in html and 'value="0.5"' in html
    assert 'name="max_hype"' in html and 'value="100"' in html
    for v in AM.PRESETS:
        assert f"min_hype={v}" in html
    assert "amount 0.5 to 100 HYPE" in html
    assert 'href="/activity">Reset' in html
    plain = lv.get("/activity", params={"range": "all"}).text
    assert "amount 0.5" not in plain and "clear amount" not in plain


def test_page_values_are_escaped(amounts, make_live):  # noqa: F811
    r = make_live().get("/activity", params={"min_hype": '"><script>alert(1)</script>'})
    assert r.status_code == 400 and "<script>alert(1)" not in r.text
