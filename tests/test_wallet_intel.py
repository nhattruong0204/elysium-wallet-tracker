# ruff: noqa: F811
"""Wallet Intelligence: population, filters, sorting, NULL semantics, data quality, exports and
the privacy boundary. Everything runs through the real ASGI app as the `dashboard_ro` role."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from conftest import CHAIN, role_url
from test_dashboard_db import (  # noqa: F401
    NOW,
    PRIV,
    PRIVATE_LABEL,
    PUB1,
    PUB2,
    TOK_PUB,
    ago,
    seeded,
)
from test_web import PRIVATE_MARKERS, make_cfg, make_live, web  # noqa: F401

from agora_tracker import schema as t
from agora_tracker.classify import TradeEvent
from agora_tracker.leaderboard import Trader
from agora_tracker.web import intel as I
from agora_tracker.web.params import ParamError

UTC = timezone.utc
E18 = 10 ** 18
PUB3 = "0x" + "a3" * 20
PUB4 = "0x" + "a4" * 20
API = "/api/v1/leaderboard"


@pytest.fixture
def board(seeded):
    """PUB1 stays in the list, PUB2 leaves it (former), PUB3 enters with an unknown PnL, PUB4
    appears and leaves again (so it has two list events). PRIV is only on a watchlist."""
    seeded.record_snapshot([Trader(address=PUB1, rank=2, pnl=520.0),
                            Trader(address=PUB3, rank=1, pnl=None),
                            Trader(address=PUB4, rank=3, pnl=-5.0)],
                           source="agora", status="ok", complete=True, row_count=3)
    seeded.record_snapshot([Trader(address=PUB1, rank=1, pnl=540.0),
                            Trader(address=PUB3, rank=2, pnl=None)],
                           source="agora", status="ok", complete=True, row_count=2)
    return seeded


def rows(web, **q):
    r = web.get(API, params={"window": "all", "limit": 100, **q})
    assert r.status_code == 200, r.text
    return r.json()["page"]["items"], r.json()


def wallets(web, **q):
    return [w["wallet"] for w in rows(web, **q)[0]]


# --- parameters -------------------------------------------------------------------------------
@pytest.mark.parametrize("q", [
    {"window": "1y"}, {"membership": "x"}, {"quality": "x"}, {"sort": "nope"}, {"dir": "up"},
    {"min_trades": "-1"}, {"min_trades": "1.5"}, {"min_rank": "0"}, {"max_rank": "abc"},
    {"min_rank": "9", "max_rank": "3"}, {"min_pnl": "1e3"}, {"min_pnl": "x'--"},
    {"q": "zz"}, {"q": "0x" + "g" * 10}, {"limit": "0"}, {"limit": "101"}, {"page": "0"},
    {"active": "maybe"}, {"min_trades": "٣"},
])
def test_invalid_parameters_are_rejected(board, make_live, q):
    lv = make_live()
    assert lv.get(API, params=q).status_code == 400
    assert lv.get("/leaderboard", params=q).status_code == 400


def test_parse_defaults_and_ranges():
    p = I.parse_intel({})
    assert (p.window, p.membership, p.sort, p.desc, p.size) == ("7d", "all", "rank", False, 50)
    p = I.parse_intel({"sort": "pnl", "min_pnl": "-12.5", "min_rank": "3", "max_rank": "3"})
    assert p.desc is True and p.min_pnl == Decimal("-12.5") and p.min_rank == p.max_rank == 3
    assert I.parse_intel({"sort": "pnl", "dir": "asc"}).desc is False
    with pytest.raises(ParamError):
        I.parse_intel({"min_pnl": "1" * 20})


# --- population and statuses --------------------------------------------------------------------
def test_population_is_everyone_ever_on_the_list_and_nobody_else(board, make_live):
    lv = make_live()
    ws = {w["wallet"]: w for w in rows(lv)[0]}
    assert set(ws) == {PUB1, PUB2, PUB3, PUB4}
    assert PRIV not in ws
    assert ws[PUB1]["status"] == "current" and ws[PUB1]["rank"] == 1
    assert ws[PUB3]["status"] == "current" and ws[PUB3]["rank"] == 2
    assert ws[PUB2]["status"] == "former" and ws[PUB2]["rank"] is None
    assert ws[PUB4]["status"] == "former" and ws[PUB4]["best_rank"] == 3
    assert ws[PUB1]["appearances"] == 3 and ws[PUB1]["best_rank"] == 1
    assert ws[PUB2]["appearances"] == 1 and ws[PUB2]["last_rank"] == 2


def test_kpis_count_the_whole_tracked_set_not_the_page(board, make_live):
    _, j = rows(make_live(), limit=1, membership="former")
    k = j["kpis"]
    assert k["tracked"] == 4 and k["current"] == 2 and k["former"] == 2
    assert j["filtered"] == 2 and j["page"]["total"] == 2 and len(j["page"]["items"]) == 1


def test_unknown_values_are_null_never_zero(board, make_live):
    ws = {w["wallet"]: w for w in rows(make_live())[0]}
    assert ws[PUB3]["agora_pnl"] is None                  # Agora gave no PnL
    assert ws[PUB2]["measured_pnl"] is None               # no measured closing trade
    assert ws[PUB2]["win_rate"] is None
    assert ws[PUB1]["measured_pnl"] == "0.5" and ws[PUB1]["measured_closes"] == 1
    assert ws[PUB1]["win_rate"] == "1"
    assert ws[PUB3]["last_trade_at"] is None and ws[PUB3]["legs"] == 0


def test_amounts_are_verified_only_and_exact(board, make_live):
    ws = {w["wallet"]: w for w in rows(make_live())[0]}
    assert ws[PUB1]["buy_hype"] == "2" and ws[PUB1]["sell_hype"] == "1.5"
    assert ws[PUB1]["buys"] == 1 and ws[PUB1]["sells"] == 1 and ws[PUB1]["tokens"] == 1


def test_estimated_legs_are_counted_apart(board, make_live):
    with board.tx() as c:
        board.insert_trade(c, TradeEvent("BUY", "0xe9".ljust(66, "0"), 5000, PUB2, TOK_PUB,
                                         10 * E18, 9 * E18, confidence="estimated",
                                         hype_method="estimate"), ago(minutes=20))
    ws = {w["wallet"]: w for w in rows(make_live(make_cfg()))[0]}
    assert ws[PUB2]["estimated_legs"] == 1
    assert ws[PUB2]["buy_hype"] == "1"                    # the 9 HYPE estimate is not in it


# --- filters ------------------------------------------------------------------------------------
def test_membership_filter(board, make_live):
    lv = make_live()
    assert set(wallets(lv, membership="current")) == {PUB1, PUB3}
    assert set(wallets(lv, membership="former")) == {PUB2, PUB4}


def test_active_and_min_trades(board, make_live):
    lv = make_live()
    assert set(wallets(lv, active="1")) == {PUB1, PUB2}
    assert set(wallets(lv, min_trades="2")) == {PUB1}
    assert set(wallets(lv, window="24h", min_trades="3")) == set()


def test_rank_bounds_apply_to_current_rank_only(board, make_live):
    lv = make_live()
    assert wallets(lv, min_rank="1", max_rank="1") == [PUB1]
    assert wallets(lv, max_rank="2") == [PUB1, PUB3]
    assert set(wallets(lv, min_rank="1")) == {PUB1, PUB3}      # former members have no rank


def test_min_pnl_excludes_unknown_and_includes_negative_floor(board, make_live):
    lv = make_live()
    # PnL is the latest KNOWN Agora figure, so a former member keeps its last value (PUB2: 300)
    assert set(wallets(lv, min_pnl="0")) == {PUB1, PUB2}       # PUB3 unknown, PUB4 is -5
    assert PUB3 not in wallets(lv, min_pnl="-1000")            # unknown stays out, never read as 0
    assert set(wallets(lv, min_pnl="-1000")) == {PUB1, PUB2, PUB4}
    assert set(wallets(lv, min_pnl="-5")) == {PUB1, PUB2, PUB4}
    assert set(wallets(lv, min_pnl="-4.999999999999999999")) == {PUB1, PUB2}


def test_search_by_address_fragment(board, make_live):
    lv = make_live()
    assert wallets(lv, q=PUB2) == [PUB2]
    assert wallets(lv, q="a3a3a3") == [PUB3]
    assert wallets(lv, q="0xa4") == [PUB4]
    assert wallets(lv, q=PRIV) == []                           # a private address finds nothing


def test_filters_compose(board, make_live):
    lv = make_live()
    assert wallets(lv, membership="former", active="1") == [PUB2]
    assert wallets(lv, membership="current", min_trades="1", min_pnl="500") == [PUB1]


# --- sorting and pagination ----------------------------------------------------------------------
def test_sort_puts_unknown_last_in_both_directions(board, make_live):
    lv = make_live()
    for d in ("asc", "desc"):
        got = wallets(lv, sort="pnl", dir=d)
        assert got[-1] in (PUB2, PUB3, PUB4) and PUB1 in got[:1] if d == "desc" else True
        known = [w for w in rows(lv, sort="pnl", dir=d)[0] if w["agora_pnl"] is not None]
        unknown = [w for w in rows(lv, sort="pnl", dir=d)[0] if w["agora_pnl"] is None]
        allw = rows(lv, sort="pnl", dir=d)[0]
        assert allw[: len(known)] == known and allw[len(known):] == unknown


def test_default_sorts_and_stability(board, make_live):
    lv = make_live()
    assert wallets(lv, sort="rank")[:2] == [PUB1, PUB3]
    assert wallets(lv, sort="appearances")[0] == PUB1
    assert wallets(lv, sort="legs")[0] == PUB1
    assert wallets(lv, sort="rank") == wallets(lv, sort="rank")


def test_pagination_is_complete_and_disjoint(board, make_live):
    lv = make_live()
    seen = []
    first = lv.get(API, params={"window": "all", "limit": 2, "page": 1}).json()
    assert first["page"]["pages"] == 2
    for page in range(1, first["page"]["pages"] + 1):
        j = lv.get(API, params={"window": "all", "limit": 2, "page": page}).json()
        seen += [w["wallet"] for w in j["page"]["items"]]
    assert sorted(seen) == sorted([PUB1, PUB2, PUB3, PUB4]) and len(set(seen)) == 4


# --- data quality ----------------------------------------------------------------------------------
def test_classify_quality_matrix():
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    c = I.classify_quality
    assert c("excluded", 1, 0, t0, t0, True) == "excluded"
    assert c("active", 0, 0, None, t0, False) == "unscanned"
    assert c("active", 2, 1, t0, t0, True) == "gaps"
    assert c("active", 1, 0, t0 + timedelta(hours=3), t0, True) == "partial"
    assert c("active", 1, 0, t0, t0, True) == "complete"
    assert c("active", 1, 0, t0 - timedelta(days=1), t0, True) == "complete"
    assert c("active", 1, 0, t0, t0, False) == "partial"       # derived coverage is a claim only
    assert c("active", 1, 0, None, t0, True) == "partial"


def test_quality_from_real_coverage_rows(board, make_live):
    with board.tx() as c:
        c.execute(sa.text("INSERT INTO indexed_blocks (chain_id, block_number, block_time) VALUES "
                          "(:c, 10, :t1), (:c, 500, :t2)"),
                  {"c": CHAIN, "t1": NOW - timedelta(days=3), "t2": NOW - timedelta(hours=1)})
        c.execute(sa.insert(t.wallet_scan_coverage), [
            {"chain_id": CHAIN, "address": PUB1, "from_block": 10, "to_block": None, "basis": "live"},
            {"chain_id": CHAIN, "address": PUB2, "from_block": 10, "to_block": 100, "basis": "live"},
            {"chain_id": CHAIN, "address": PUB2, "from_block": 300, "to_block": None, "basis": "live"},
            {"chain_id": CHAIN, "address": PUB3, "from_block": 500, "to_block": None, "basis": "derived"}])
    lv = make_live()
    q = {w["wallet"]: w for w in rows(lv)[0]}
    assert q[PUB1]["quality"] == "complete" and q[PUB1]["tracking_basis"] == "live"
    assert q[PUB2]["quality"] == "gaps" and q[PUB2]["scan_gaps"] == 1
    assert q[PUB3]["quality"] == "partial" and q[PUB3]["tracking_basis"] == "derived"
    assert q[PUB4]["quality"] == "unscanned" and q[PUB4]["tracking_basis"] == "none"
    assert wallets(lv, quality="gaps") == [PUB2]
    assert wallets(lv, quality="unscanned") == [PUB4]


def test_private_coverage_is_invisible_to_the_dashboard_role(board):
    with board.tx() as c:
        c.execute(sa.insert(t.wallet_scan_coverage), [
            {"chain_id": CHAIN, "address": PRIV, "from_block": 1, "to_block": None, "basis": "live"},
            {"chain_id": CHAIN, "address": PUB1, "from_block": 1, "to_block": None, "basis": "live"}])
    eng = sa.create_engine(role_url())
    with eng.connect() as c:
        got = {r[0] for r in c.execute(sa.text("SELECT address FROM public_api.wallet_scan_coverage"))}
        denied = False
        try:
            c.execute(sa.text("SELECT 1 FROM public.wallet_scan_coverage LIMIT 1"))
        except sa.exc.DBAPIError:
            denied = True
    eng.dispose()
    assert got == {PUB1} and denied


# --- wallet profile ------------------------------------------------------------------------------------
def test_profile_contents(board, make_live):
    lv = make_live()
    j = lv.get(f"{API}/{PUB1}").json()
    assert j["summary"]["wallet"] == PUB1 and j["summary"]["status"] == "current"
    assert j["performance"]["all"]["trades"] == 2 and j["performance"]["all"]["measured_closes"] == 1
    assert j["performance"]["all"]["measured_pnl_hype"] == "0.5"
    assert j["performance"]["24h"]["trades"] == 2
    assert [e["kind"] for e in j["membership_events"]] == ["ENTERED"]
    assert len(j["rank_history"]) >= 1 and j["rank_history"][-1]["rank"] == 1
    assert j["tokens"][0]["token"] == TOK_PUB and j["tokens"][0]["symbol"] == "PUB"
    assert j["relationships"] == []
    f = lv.get(f"{API}/{PUB2}").json()
    assert [e["kind"] for e in f["membership_events"]] == ["ENTERED", "EXITED"]
    assert f["performance"]["all"]["measured_pnl_hype"] is None


def test_profile_sections_and_step(board, make_live):
    lv = make_live()
    assert set(lv.get(f"{API}/{PUB1}/performance").json()) >= {"summary", "performance"}
    rh = lv.get(f"{API}/{PUB1}/rank-history", params={"step": "snapshot"}).json()
    assert rh["rank_history_points"] == 3 and len(rh["rank_history"]) == 3
    assert [r["rank"] for r in rh["rank_history"]] == [1, 2, 1]
    hourly = lv.get(f"{API}/{PUB1}/rank-history", params={"step": "hour"}).json()
    assert len(hourly["rank_history"]) <= 3
    assert lv.get(f"{API}/{PUB1}/rank-history", params={"step": "week"}).status_code == 400
    assert lv.get(f"{API}/{PUB1}/relationships").status_code == 200


def test_unknown_private_and_malformed_wallets(board, make_live):
    lv = make_live()
    for a in (PRIV, "0x" + "c7" * 20):
        assert lv.get(f"{API}/{a}").status_code == 404
        assert lv.get(f"/leaderboard/{a}").status_code == 404
    assert lv.get(f"{API}/nonsense").status_code == 404
    assert lv.get("/leaderboard/0x123").status_code == 404


def test_relationships_show_tracked_pairs_only(board, make_live):
    with board.tx() as c:
        tx = "0xf1".ljust(66, "0")
        board.insert_trade(c, TradeEvent("TRANSFER_OUT", tx, 7001, PUB1, TOK_PUB, 5 * E18, None,
                                         confidence="unknown", hype_method="none"), ago(minutes=9))
        board.insert_trade(c, TradeEvent("TRANSFER_IN", tx, 7001, PUB2, TOK_PUB, 5 * E18, None,
                                         confidence="unknown", hype_method="none"), ago(minutes=9))
        tx2 = "0xf2".ljust(66, "0")                      # into a private wallet: invisible
        board.insert_trade(c, TradeEvent("TRANSFER_OUT", tx2, 7002, PUB1, TOK_PUB, 5 * E18, None,
                                         confidence="unknown", hype_method="none"), ago(minutes=8))
        board.insert_trade(c, TradeEvent("TRANSFER_IN", tx2, 7002, PRIV, TOK_PUB, 5 * E18, None,
                                         confidence="unknown", hype_method="none"), ago(minutes=8))
    lv = make_live()
    j = lv.get(f"{API}/{PUB1}/relationships").json()
    assert [(r["src"], r["dst"], r["n"]) for r in j["relationships"]] == [(PUB1, PUB2, 1)]
    assert PRIV not in str(j)
    assert "does not say who controls" in j["relationships_note"]


# --- exports ------------------------------------------------------------------------------------
def test_csv_export_matches_filters_and_has_stable_columns(board, make_live):
    lv = make_live()
    r = lv.get(f"{API}.csv", params={"window": "all", "membership": "former"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "private, no-store"
    got = list(csv.DictReader(io.StringIO(r.text)))
    assert tuple(got[0].keys()) == I.CSV_COLUMNS
    assert {g["wallet"] for g in got} == {PUB2, PUB4}
    assert all(g["status"] == "former" and g["rank"] == "" for g in got)    # empty, not 0
    pub1 = next(csv.DictReader(io.StringIO(lv.get(f"{API}.csv", params={"window": "all", "q": PUB1}).text)))
    assert pub1["measured_pnl_hype"] == "0.5" and pub1["bought_hype"] == "2"


@pytest.mark.parametrize("raw,expect", [
    ("=HYPERLINK(\"x\")", "'=HYPERLINK(\"x\")"), ("+1+1", "'+1+1"), ("-2", "'-2"), ("@SUM(A1)", "'@SUM(A1)"),
    ("\tcmd", "'\tcmd"), ("\rcmd", "'\rcmd"), ("0xabc", "0xabc"), ("plain", "plain"),
])
def test_csv_cell_neutralises_formulas(raw, expect):
    assert I.csv_cell(raw) == expect


def test_csv_numbers_stay_numbers():
    assert I.csv_cell(Decimal("-5.25")) == "-5.25" and I.csv_cell(-3) == -3
    assert I.csv_cell(None) == "" and I.csv_cell(True) == "true"
    assert I.csv_cell(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2026-01-02T03:04:05Z"


def test_rank_history_csv(board, make_live):
    r = make_live().get(f"{API}/{PUB1}/rank-history.csv", params={"step": "snapshot"})
    rows_ = list(csv.reader(io.StringIO(r.text)))
    assert rows_[0] == ["captured_at", "rank", "agora_pnl_hype", "agora_worth_hype"]
    assert [x[1] for x in rows_[1:]] == ["1", "2", "1"]


# --- pages and privacy ------------------------------------------------------------------------------------
def test_pages_render_with_filters_and_links(board, make_live):
    lv = make_live()
    html = lv.get("/leaderboard", params={"window": "all", "membership": "former"}).text
    assert "Wallet intelligence" in html and f"/leaderboard/{PUB2}" in html
    assert 'name="min_pnl"' in html and 'href="/leaderboard">Reset' in html
    assert "never combined" in html
    page = lv.get(f"/leaderboard/{PUB1}").text
    assert "Rank history" in page and "Relationships (evidence only)" in page
    assert lv.get("/leaderboard", params={"window": "all", "min_pnl": "9999"}).text.count("No wallet matches") == 1


def test_nothing_private_anywhere_in_the_new_surface(board, make_live):
    lv = make_live()
    paths = ["/leaderboard", "/leaderboard?window=all", "/leaderboard?q=" + PRIV[:10],
             f"{API}?window=all&limit=100", f"{API}.csv?window=all", "/api/v1/meta"]
    for a in (PUB1, PUB2, PUB3, PUB4):
        paths += [f"/leaderboard/{a}", f"{API}/{a}", f"{API}/{a}/rank-history.csv"]
    for p in paths:
        r = lv.get(p)
        assert r.status_code == 200, p
        for m in PRIVATE_MARKERS + (PRIVATE_LABEL, PRIV):
            assert m not in r.text, f"{m} leaked on {p}"


def test_role_cannot_write_and_views_are_the_only_source(board):
    eng = sa.create_engine(role_url())
    with eng.connect() as c:
        with pytest.raises(sa.exc.DBAPIError):
            c.execute(sa.text("DELETE FROM public_api.wallet_enrollment"))
    eng.dispose()
