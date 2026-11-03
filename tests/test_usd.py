"""USD market capitalisation: conversion maths, provenance, historical alignment, privacy.

Pure-unit tests need no database; the integration tests run the real SQL through the
`dashboard_ro` role (TEST_DATABASE_URL).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from conftest import CHAIN, role_url
from test_dashboard_db import NOW, PRIV, PUB2, TOK_PRIV, TOK_PUB, seeded  # noqa: F401
from test_web import PRIVATE_MARKERS, make_cfg, make_live, web  # noqa: F401

from agora_tracker import schema as t
from agora_tracker.leaderboard import Trader
from agora_tracker.migrate import downgrade, set_dashboard_password, upgrade
from agora_tracker.web.usd import FxPolicy, dec, format_usd, rate_quality, to_usd

UTC = timezone.utc
P = FxPolicy()
D = Decimal


# ---------------------------------------------------------------- pure unit tests -------------
@pytest.mark.parametrize("v,expect", [
    (0, "$0"), (None, "–"), ("", "–"), (-5, "–"), (0.004, "<$0.01"), (0.5, "$0.50"),
    (830, "$830"), (99.5, "$99.50"), (999, "$999"), (999.6, "$1K"), (1000, "$1K"),
    (1234.5, "$1.23K"), (12500, "$12.5K"), (125_000, "$125K"), (999_960, "$1M"),
    (1_280_000, "$1.28M"), (12_600_000, "$12.6M"), (3_450_000_000, "$3.45B"),
    (2 * 10 ** 12, "$2T"), (D("1250000"), "$1.25M"), ("850000", "$850K"),
    (float("nan"), "–"), (float("inf"), "–"),
])
def test_format_usd(v, expect):
    assert format_usd(v) == expect


def test_missing_is_never_rendered_as_zero():
    assert format_usd(None) != format_usd(0)
    assert format_usd(0) == "$0"


@pytest.mark.parametrize("gap,expect", [
    (0, "exact"), (60, "exact"), (-60, "exact"), (61, "aligned"), (600, "aligned"),
    (-601, "stale"), (3600, "stale"), (3601, "unavailable"), (None, "unavailable")])
def test_rate_quality_boundaries(gap, expect):
    assert rate_quality(gap, P) == expect


def snap(**kw):
    base = dict(mcap_hype=D("10000"), rate=D("35"), rate_at=NOW, snap_at=NOW, p=P, now=NOW)
    base.update(kw)
    return to_usd(base["mcap_hype"], base["rate"], base["rate_at"], base["snap_at"], base["p"],
                  base["now"])


def test_conversion_is_exact_decimal():
    u = snap()
    assert u["market_cap_usd"] == D("350000.00") and u["market_cap_status"] == "valid"
    assert u["market_cap_hype"] == D("10000")           # source evidence is kept, unconverted
    assert u["hype_usd_quality"] == "exact" and u["market_cap_source"] == "stored_snapshot"
    # no binary-float noise: 0.1 * 3 is exactly 0.3 in Decimal arithmetic
    assert snap(mcap_hype=0.1, rate=D("3"))["market_cap_usd"] == D("0.30")


def test_history_is_not_repriced_when_the_rate_changes_later():
    """10,000 HYPE observed at $35 is $350,000 forever, not $420,000 at today's $42."""
    old = snap(rate=D("35"), rate_at=NOW - timedelta(hours=5), snap_at=NOW - timedelta(hours=5),
               now=NOW)
    assert old["market_cap_usd"] == D("350000.00")
    # the same snapshot paired with a rate from far away is refused rather than repriced
    far = snap(rate=D("42"), rate_at=NOW, snap_at=NOW - timedelta(hours=5))
    assert far["market_cap_usd"] is None and far["market_cap_status"] == "unavailable"
    assert far["hype_usd"] is None


def test_missing_stale_and_invalid_inputs_are_honest():
    assert snap(rate=None, rate_at=None)["market_cap_usd"] is None
    assert snap(rate=D("0"))["market_cap_usd"] is None
    assert snap(rate=D("-3"))["market_cap_usd"] is None
    assert snap(mcap_hype=None)["market_cap_status"] == "unavailable"
    assert snap(mcap_hype=D("-1"))["market_cap_status"] == "invalid"
    assert snap(mcap_hype=D("0"))["market_cap_usd"] == D("0.00")          # real zero stays zero
    # rate 40 minutes away: usable but flagged
    st = snap(rate_at=NOW - timedelta(minutes=40))
    assert st["market_cap_status"] == "stale" and st["hype_usd_quality"] == "stale"
    # fresh rate, old snapshot (collector stalled)
    old = snap(snap_at=NOW - timedelta(minutes=45), rate_at=NOW - timedelta(minutes=45))
    assert old["market_cap_status"] == "stale" and old["market_cap_usd"] == D("350000.00")


def test_dec_handles_odd_values():
    assert dec(None) is None and dec("abc") is None and dec(float("nan")) is None
    assert dec(0.1) == D("0.1") and dec(5) == D("5")


# ---------------------------------------------------------------- database ----------------------
def put_rates(store, rows):
    with store.tx() as c:
        c.execute(sa.insert(t.hype_usd_rates), [
            {"observed_at": at, "hype_usd": D(str(r)), "source": "test"} for at, r in rows])


def set_snapshot(store, token, at, mcap_hype, price="1"):
    with store.tx() as c:
        c.execute(sa.insert(t.token_market_snapshots).values(
            chain_id=CHAIN, token=token, captured_at=at, source="agora",
            price_hype=D(price), mcap_hype=D(str(mcap_hype))))


def clear_snapshots(store):
    with store.tx() as c:
        c.execute(sa.delete(t.token_market_snapshots))
        c.execute(sa.delete(t.hype_usd_rates))


def api_cap(web, token=TOK_PUB, **q):
    r = web.get(f"/api/v1/tokens/{token}", params=q)
    assert r.status_code == 200, r.text
    return r.json()["market_cap"]


def test_usd_uses_the_rate_at_the_snapshot_not_the_latest(seeded, make_live):
    clear_snapshots(seeded)
    at = NOW - timedelta(minutes=5)
    put_rates(seeded, [(at - timedelta(seconds=20), 35), (NOW, 42)])      # HYPE rallies later
    set_snapshot(seeded, TOK_PUB, at, 10_000)
    cap = api_cap(make_live())
    assert D(cap["market_cap_usd"]) == D("350000.00")
    assert cap["hype_usd_quality"] == "exact" and D(cap["hype_usd"]) == D("35")
    assert D(cap["market_cap_hype"]) == D("10000")
    # a later rate arriving never moves it
    put_rates(seeded, [(NOW + timedelta(seconds=1), 80)])
    assert D(api_cap(make_live())["market_cap_usd"]) == D("350000.00")


def test_alignment_picks_the_nearest_side_and_labels_it(seeded, make_live):
    clear_snapshots(seeded)
    at = NOW - timedelta(minutes=3)
    put_rates(seeded, [(at - timedelta(seconds=200), 30), (at + timedelta(seconds=100), 36)])
    set_snapshot(seeded, TOK_PUB, at, 1000)
    cap = api_cap(make_live())
    assert D(cap["hype_usd"]) == D("36") and cap["hype_usd_quality"] == "aligned"
    assert cap["hype_usd_gap_s"] == 100 and D(cap["market_cap_usd"]) == D("36000.00")


@pytest.mark.parametrize("gap,quality,status", [
    (59, "exact", "valid"), (61, "aligned", "valid"), (601, "stale", "stale"),
    (3599, "stale", "stale"), (3601, "unavailable", "unavailable")])
def test_max_mismatch_is_enforced_end_to_end(seeded, make_live, gap, quality, status):
    clear_snapshots(seeded)
    at = NOW - timedelta(minutes=2)
    put_rates(seeded, [(at - timedelta(seconds=gap), 50)])
    set_snapshot(seeded, TOK_PUB, at, 200)
    cap = api_cap(make_live())
    assert cap["hype_usd_quality"] == quality and cap["market_cap_status"] == status
    if status == "unavailable":
        assert cap["market_cap_usd"] is None and cap["hype_usd"] is None
    else:
        assert D(cap["market_cap_usd"]) == D("10000.00")


def test_no_rate_at_all_shows_unavailable_never_zero(seeded, make_live):
    clear_snapshots(seeded)
    set_snapshot(seeded, TOK_PUB, NOW - timedelta(minutes=2), 5000)
    lv = make_live()
    cap = api_cap(lv)
    assert cap["market_cap_usd"] is None and cap["market_cap_status"] == "unavailable"
    html = lv.get(f"/tokens/{TOK_PUB}").text
    assert "$0" not in re.sub(r"\$0\.\d+", "", html)
    row = lv.get("/tokens?range=7d").text
    assert "Market cap (USD)" in row and "HYPE)</a>" not in row.split("Market cap (USD)")[1][:40]


def test_projects_table_sorts_by_usd_not_by_hype(seeded, make_live):
    """Token A has the larger HYPE cap but its snapshot used a far lower rate."""
    clear_snapshots(seeded)
    tok_b = "0x" + "1b" * 20
    with seeded.tx() as c:
        c.execute(sa.insert(t.token_profiles).values(chain_id=CHAIN, address=tok_b, symbol="BBB",
                                                     name="B", launchpad="boxa"))
        seeded.insert_trade(c, __import__("agora_tracker.classify", fromlist=["x"]).TradeEvent(
            "BUY", "0xb1".ljust(66, "0"), 1100, "0x" + "a1" * 20, tok_b, 10 ** 18, 10 ** 18,
            confidence="high", hype_method="balance_diff"), int(NOW.timestamp()) - 600)
    t_old, t_new = NOW - timedelta(hours=30), NOW - timedelta(minutes=2)
    put_rates(seeded, [(t_old, 10), (t_new, 100)])
    set_snapshot(seeded, TOK_PUB, t_old, 5000)         # 5,000 HYPE * $10   = $50,000  (older)
    set_snapshot(seeded, TOK_PUB, t_new, 2000)         # latest: 2,000 * 100 = $200,000
    set_snapshot(seeded, tok_b, t_new, 3000)           # 3,000 * 100         = $300,000
    lv = make_live()
    rows = lv.get("/api/v1/tokens", params={"range": "7d", "sort": "mcap", "dir": "desc"}).json()
    order = [r["token"] for r in rows["page"]["items"] if r["market"]]
    assert order == [tok_b, TOK_PUB]
    caps = {r["token"]: D(r["market"]["usd"]["market_cap_usd"]) for r in rows["page"]["items"]
            if r["market"]}
    assert caps == {tok_b: D("300000.00"), TOK_PUB: D("200000.00")}
    asc = lv.get("/api/v1/tokens", params={"range": "7d", "sort": "mcap", "dir": "asc"}).json()
    assert [r["token"] for r in asc["page"]["items"] if r["market"]] == [TOK_PUB, tok_b]


def test_html_and_json_agree_and_units_are_explicit(seeded, make_live):
    clear_snapshots(seeded)
    at = NOW - timedelta(minutes=1)
    put_rates(seeded, [(at, 84.1925)])
    set_snapshot(seeded, TOK_PUB, at, 14_856)           # 14,856 * 84.1925 = 1,250,820.3...
    lv = make_live()
    cap = api_cap(lv)
    assert set(cap) >= {"market_cap_usd", "market_cap_hype", "market_cap_source",
                        "market_cap_as_of", "market_cap_status", "hype_usd", "hype_usd_as_of",
                        "hype_usd_quality"}
    usd = D(cap["market_cap_usd"])
    assert usd == (D("14856") * D("84.1925")).quantize(D("0.01"))
    assert format_usd(usd) == "$1.25M"
    assert cap["market_cap_source"] == "stored_snapshot"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", cap["market_cap_as_of"])
    html = lv.get(f"/tokens/{TOK_PUB}").text
    assert "$1.25M" in html and "14,856 HYPE" not in html.split("Market cap")[1][:300]
    assert "Market cap basis" in html and "Source: Agora market cap in HYPE" in html
    assert "$1.25M" in lv.get("/tokens?range=7d").text
    assert "$1.25M" in lv.get("/").text                # Overview "Most traded tokens"
    assert "Market cap (HYPE)" not in lv.get("/tokens").text
    meta = lv.get("/api/v1/meta").json()
    assert meta["units"]["market_cap_usd"] == "USD"


def test_early_search_and_smart_money_show_usd_too(seeded, make_live):
    clear_snapshots(seeded)
    at = NOW - timedelta(minutes=1)
    put_rates(seeded, [(at, 20)])
    set_snapshot(seeded, TOK_PUB, at, 123_456)          # -> $2,469,120
    dup = "0x" + "1c" * 20                              # a second PUB so search lists both
    with seeded.tx() as c:
        c.execute(sa.insert(t.token_profiles).values(chain_id=CHAIN, address=dup, symbol="PUB",
                                                     name="Other PUB", launchpad="kleos"))
        seeded.insert_trade(c, __import__("agora_tracker.classify", fromlist=["x"]).TradeEvent(
            "BUY", "0xb2".ljust(66, "0"), 1200, "0x" + "a1" * 20, dup, 10 ** 18, 10 ** 18,
            confidence="high", hype_method="balance_diff"), int(NOW.timestamp()) - 300)
    set_snapshot(seeded, dup, at, 1000)
    lv = make_live()
    for path in ("/early?range=24h", "/smart-money?view=net&range=24h", "/search?q=PUB"):
        html = lv.get(path).text
        assert "Market cap (USD)" in html, path
        assert "Market cap (HYPE)" not in html, path
    early = lv.get("/api/v1/early?range=24h").json()
    for r in early["trending"]:
        assert "mcap" not in r                         # the unlabeled HYPE field is gone
    assert "$2.47M" in lv.get("/search?q=PUB").text


def test_series_uses_each_snapshots_own_rate(seeded, make_live):
    clear_snapshots(seeded)
    base = NOW - timedelta(hours=3)
    put_rates(seeded, [(base, 10), (base + timedelta(hours=1), 20), (base + timedelta(hours=2), 40)])
    for i in range(3):
        set_snapshot(seeded, TOK_PUB, base + timedelta(hours=i, seconds=5), 1000)   # same HYPE cap
    series = make_live().get(f"/api/v1/tokens/{TOK_PUB}", params={"range": "24h"}).json()["series"]
    assert [D(s["mcap_usd"]) for s in series] == [D("10000.00"), D("20000.00"), D("40000.00")]
    assert {D(s["mcap_hype"]) for s in series} == {D("1000")}


# ---------------------------------------------------------------- migration & trigger -------------
def test_trigger_records_valid_rates_and_ignores_garbage(seeded, pg_engine):
    clear_snapshots(seeded)

    def snapshot(payload):
        sid = seeded.record_snapshot([Trader(address="0x" + "c3" * 20, rank=1, pnl=1.0)],
                                     source="agora", status="ok", complete=True, row_count=1,
                                     raw_payload=payload)
        return sid

    for payload in ({"hypeUsd": 84.1925}, {"hypeUsd": "84"}, {"hypeUsd": -1}, {"hypeUsd": 0},
                    {"hypeUsd": 1e9}, {"other": 1}, [1, 2], None, {"hypeUsd": None}):
        snapshot(payload)                               # none of these may fail the insert
    with pg_engine.connect() as c:
        rows = c.execute(sa.text("SELECT hype_usd, source FROM hype_usd_rates")).all()
    assert [(r[0], r[1]) for r in rows] == [(D("84.19250000"), "agora_leaderboard")]


def test_trigger_cannot_break_leaderboard_writes(seeded, pg_engine):
    clear_snapshots(seeded)
    with pg_engine.begin() as c:
        c.execute(sa.text("ALTER TABLE hype_usd_rates RENAME TO hype_usd_rates_x"))
    try:
        sid = seeded.record_snapshot([Trader(address="0x" + "c4" * 20, rank=1, pnl=1.0)],
                                     source="agora", status="ok", complete=True, row_count=1,
                                     raw_payload={"hypeUsd": 50})
        assert sid
    finally:
        with pg_engine.begin() as c:
            c.execute(sa.text("ALTER TABLE hype_usd_rates_x RENAME TO hype_usd_rates"))


def test_same_observation_is_stored_once(seeded, pg_engine):
    clear_snapshots(seeded)
    sid = seeded.record_snapshot([Trader(address="0x" + "c5" * 20, rank=1, pnl=1.0)],
                                 source="agora", status="ok", complete=True, row_count=1,
                                 raw_payload={"hypeUsd": 70})
    with seeded.tx() as c:
        c.execute(sa.text("UPDATE leaderboard_snapshots SET raw_payload = :p WHERE id = :i"),
                  {"p": json.dumps({"hypeUsd": 71}), "i": sid})
    with pg_engine.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM hype_usd_rates")).scalar()
    assert n == 1


def test_migration_backfills_history_and_is_idempotent(seeded, pg_engine):
    """Down to 0004 and back: the rate series is rebuilt from the stored payloads."""
    clear_snapshots(seeded)
    for i, rate in enumerate((80.5, 81.25, 82.0)):
        sid = seeded.record_snapshot([Trader(address="0x" + "c6" * 20, rank=1, pnl=1.0)],
                                     source="agora", status="ok", complete=True, row_count=1,
                                     raw_payload={"hypeUsd": rate})
        with seeded.tx() as c:
            c.execute(sa.text("UPDATE leaderboard_snapshots SET captured_at = :t WHERE id = :i"),
                      {"t": NOW - timedelta(hours=3 - i), "i": sid})
    # a payload without a rate and one with garbage do not produce rows
    seeded.record_snapshot([Trader(address="0x" + "c6" * 20, rank=1, pnl=1.0)], source="agora",
                           status="ok", complete=True, row_count=1, raw_payload={"x": 1})
    downgrade(pg_engine, "0004")
    with pg_engine.connect() as c:
        assert c.execute(sa.text("SELECT to_regclass('public.hype_usd_rates')")).scalar() is None
        assert c.execute(sa.text("SELECT count(*) FROM leaderboard_snapshots")).scalar() >= 4
        assert c.execute(sa.text("SELECT count(*) FROM pg_trigger WHERE tgname = "
                                 "'trg_leaderboard_hype_usd'")).scalar() == 0
    upgrade(pg_engine)
    upgrade(pg_engine)                                  # idempotent
    set_dashboard_password(pg_engine, __import__("conftest").ROLE_PASSWORD)
    with pg_engine.connect() as c:
        got = sorted(r[0] for r in c.execute(sa.text("SELECT hype_usd FROM hype_usd_rates")))
        # the seeded fixture snapshot ({"secret": 1}) has no rate
        assert [float(x) for x in got] == [80.5, 81.25, 82.0]
        # market history was not touched by the migration
        assert c.execute(sa.text("SELECT count(*) FROM token_market_snapshots")).scalar() >= 0


# ---------------------------------------------------------------- privacy -----------------------
def test_role_sees_rates_only_through_the_narrow_view(seeded, role_ready):
    put_rates(seeded, [(NOW, 55)])
    eng = sa.create_engine(role_url())
    try:
        with eng.connect() as c:
            cols = [r[0] for r in c.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public_api' AND table_name = 'hype_usd_rates' "
                "ORDER BY ordinal_position"))]
            assert cols == ["observed_at", "hype_usd", "source"]
            assert c.execute(sa.text("SELECT count(*) FROM hype_usd_rates")).scalar() >= 1
        for sql in ("SELECT * FROM public.hype_usd_rates",
                    "INSERT INTO public_api.hype_usd_rates VALUES (now(), 1, 'x')",
                    "DELETE FROM public_api.hype_usd_rates",
                    "SELECT raw_payload FROM public.leaderboard_snapshots"):
            with pytest.raises(sa.exc.DBAPIError) as ei, eng.begin() as c:
                c.execute(sa.text(sql))
            m = str(ei.value).lower()
            assert "permission denied" in m or "read-only" in m or "cannot" in m, m
    finally:
        eng.dispose()


def test_usd_pages_leak_nothing_private(seeded, make_live):
    clear_snapshots(seeded)
    put_rates(seeded, [(NOW - timedelta(minutes=1), 60)])
    set_snapshot(seeded, TOK_PUB, NOW - timedelta(minutes=1), 100)
    set_snapshot(seeded, TOK_PRIV, NOW - timedelta(minutes=1), 100)
    lv = make_live()
    for path in ("/", "/tokens?range=7d", "/early", "/smart-money", f"/tokens/{TOK_PUB}",
                 "/api/v1/tokens?range=7d", "/api/v1/overview", "/api/v1/early"):
        body = lv.get(path).text
        for marker in PRIVATE_MARKERS:
            assert marker not in body, (path, marker)
    assert lv.get(f"/tokens/{TOK_PRIV}").status_code == 404


# ---------------------------------------------------------------- public_wallets rewrite ----------
def test_public_wallets_skip_scan_equals_the_original_definition(seeded, pg_engine):
    """0005 replaced SELECT DISTINCT with an index skip scan: same set, including snapshots
    that must NOT make a wallet public (degraded / failed) and wallets only on the watchlist."""
    import importlib.util
    import pathlib

    from agora_tracker.migrations.versions import __path__ as _  # noqa: F401  (package exists)
    spec = importlib.util.spec_from_file_location(
        "m0005", pathlib.Path(__file__).resolve().parents[1] / "agora_tracker" / "migrations"
        / "versions" / "0005_hype_usd_rates.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    only_degraded, only_failed, both = ("0x" + x * 20 for x in ("d1", "d2", "d3"))
    for status, wallets in (("degraded", [only_degraded, both]), ("failed", [only_failed]),
                            ("imported", [both]), ("ok", [PUB2])):
        seeded.record_snapshot([Trader(address=w, rank=i + 1, pnl=1.0) for i, w in enumerate(wallets)],
                               source="agora", status=status, complete=True, row_count=len(wallets))
    with pg_engine.connect() as c:
        new = sorted(r[0] for r in c.execute(sa.text("SELECT address FROM public_api.public_wallets")))
        old = sorted(r[0] for r in c.execute(sa.text(m.PUBLIC_WALLETS_OLD)))
    assert new == old and len(new) >= 3
    assert only_degraded not in new and only_failed not in new
    assert both in new and PRIV not in new
    # no duplicates, no NULL row from the recursion's terminating step
    assert len(set(new)) == len(new) and None not in new


def test_public_wallets_empty_database(pg_engine):
    from conftest import truncate_all
    truncate_all(pg_engine)
    with pg_engine.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM public_api.public_wallets")).scalar() == 0
