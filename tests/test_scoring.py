# ruff: noqa: F811
"""Explainable wallet scores: the maths, missing-is-not-zero, immutable versions, and above all
point-in-time correctness (nothing newer than `as_of` can change a score)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from conftest import CHAIN, role_url
from test_dashboard_db import NOW, PRIV, PUB1, PUB2, TOK_PUB, ago, seeded  # noqa: F401
from test_web import make_cfg, make_live, web  # noqa: F401

from agora_tracker import scoring as S
from agora_tracker.classify import TradeEvent
from agora_tracker.config import ScoringConfig
from agora_tracker.leaderboard import Trader

UTC = timezone.utc
E18 = 10 ** 18
AS_OF = NOW + timedelta(hours=1)


def cfg(**kw) -> ScoringConfig:
    kw.setdefault("version", "t1")
    kw.setdefault("min_closes", 3)
    kw.setdefault("min_snapshots", 2)
    return ScoringConfig(**kw)


# --- the maths -------------------------------------------------------------------------------
def test_wilson_lower_bound_penalises_small_samples():
    big = S.wilson_lower(80, 100, 1.645)
    small = S.wilson_lower(4, 5, 1.645)
    assert 0.72 < big < 0.80 and small < big and 0 <= small <= 1
    assert S.wilson_lower(0, 5, 1.645) == 0.0
    assert S.wilson_lower(5, 5, 1.645) < 1.0
    with pytest.raises(ValueError):
        S.wilson_lower(0, 0, 1.645)


def ev(**kw) -> S.Evidence:
    return S.Evidence(**kw)


def test_components_missing_evidence_is_unavailable_not_zero():
    c = cfg()
    comps = S.components(ev(), AS_OF, c)
    assert all(not x["available"] and x["value"] is None for x in comps.values())
    assert "needs 3" in comps["win_rate_lb"]["missing"]
    assert comps["recency"]["missing"] == "no trade recorded before as_of"
    full = S.components(ev(closes=10, wins=7, pnl_wei=Decimal(E18), cost_wei=Decimal(4 * E18),
                           last_trade_at=AS_OF - timedelta(days=7), snapshots=20, present=10,
                           strength_sum=7.0), AS_OF, c)
    assert all(x["available"] for x in full.values())
    assert full["recency"]["value"] == pytest.approx(0.5)               # exactly one half-life
    assert full["list_persistence"]["value"] == pytest.approx(0.5)
    assert full["rank_strength"]["value"] == pytest.approx(0.7)
    assert full["return_on_cost"]["value"] == pytest.approx((0.25 + 1) / 2)   # +25% on cost


def test_return_on_cost_is_clamped_and_needs_cost():
    c = cfg()
    huge = S.components(ev(closes=5, wins=5, pnl_wei=Decimal(50 * E18), cost_wei=Decimal(E18)),
                        AS_OF, c)
    assert huge["return_on_cost"]["value"] == 1.0
    ruin = S.components(ev(closes=5, wins=0, pnl_wei=Decimal(-E18), cost_wei=Decimal(E18)),
                        AS_OF, c)
    assert ruin["return_on_cost"]["value"] == 0.0
    nocost = S.components(ev(closes=5, wins=5, pnl_wei=Decimal(E18), cost_wei=Decimal(0)), AS_OF, c)
    assert not nocost["return_on_cost"]["available"]


def test_combine_renormalises_over_available_weight():
    c = cfg(limited_cap=100.0)          # isolate the arithmetic from the cap (tested separately)
    comps = S.components(ev(closes=10, wins=8, pnl_wei=Decimal(E18), cost_wei=Decimal(E18),
                            last_trade_at=AS_OF, snapshots=0, present=0), AS_OF, c)
    score, share, quality, reason = S.combine(comps, c)
    # persistence and rank are missing: 0.15 + 0.20 of 1.0 -> 65% of the weight is present
    assert share == Decimal("0.6500") and quality == "limited" and reason
    avail = {k: v for k, v in comps.items() if v["available"]}
    expect = sum(v["weight"] * v["value"] for v in avail.values()) / 0.65 * 100
    assert float(score) == pytest.approx(expect, abs=0.01)
    assert sum(v["contribution"] for v in avail.values()) == pytest.approx(float(score), abs=0.02)
    assert all(v["contribution"] is None for k, v in comps.items() if k not in avail)


def test_below_minimum_weight_there_is_no_score():
    c = cfg()
    comps = S.components(ev(last_trade_at=AS_OF - timedelta(days=1)), AS_OF, c)   # only recency
    score, share, quality, reason = S.combine(comps, c)
    assert score is None and quality == "insufficient" and share == Decimal("0.1500")
    assert "needed" in reason


def test_sufficient_needs_both_measured_components_and_weight():
    c = cfg()
    full = S.components(ev(closes=10, wins=6, pnl_wei=Decimal(E18), cost_wei=Decimal(5 * E18),
                           last_trade_at=AS_OF, snapshots=10, present=10, strength_sum=9.0),
                        AS_OF, c)
    score, share, quality, reason = S.combine(full, c)
    assert quality == "sufficient" and share == Decimal("1.0000") and reason is None
    assert 0 <= score <= 100
    for name in ("win_rate_lb", "return_on_cost"):
        broken = copy.deepcopy(full)
        broken[name].update(available=False, value=None)
        assert S.combine(broken, c)[2] == "limited"


def test_weights_must_name_every_component():
    with pytest.raises(S.ScoringConfigError):
        S.validate(cfg(weights={"recency": 1.0}))
    with pytest.raises(S.ScoringConfigError):
        S.validate(cfg(weights={k: 0.0 for k in S.COMPONENTS}))
    with pytest.raises(S.ScoringConfigError):
        S.validate(cfg(weights={**cfg().weights, "recency": -1.0}))


def test_hash_ignores_scheduling_but_not_the_model():
    base = cfg()
    assert S.config_hash(base.model()) == S.config_hash(cfg(interval_s=5, enabled=False).model())
    assert S.config_hash(base.model()) != S.config_hash(cfg(wilson_z=1.0).model())
    assert S.config_hash(base.model()) != S.config_hash(
        cfg(weights={**base.weights, "recency": 0.5}).model())


# --- database -------------------------------------------------------------------------------
def snap(store, traders, hours_ago):
    sid = store.record_snapshot(traders, source="agora", status="ok", complete=True,
                                row_count=len(traders))
    with store.tx() as c:
        c.execute(sa.text("UPDATE leaderboard_snapshots SET captured_at = :t WHERE id = :i"),
                  {"t": NOW - timedelta(hours=hours_ago), "i": sid})
    return sid


def close(store, wallet, tx, pnl_wei, cost_wei, ts, block):
    with store.tx() as c:
        tid = store.insert_trade(c, TradeEvent("SELL", tx.ljust(66, "0"), block, wallet, TOK_PUB,
                                               10 * E18, cost_wei + pnl_wei, confidence="high",
                                               hype_method="balance_diff"), ts)
        store.record_realized(c, tid, wallet, TOK_PUB, qty_sold=10 * E18,
                              proceeds_wei=cost_wei + pnl_wei, cost_basis_wei=cost_wei,
                              pnl_wei=pnl_wei, status="measured")
    return tid


@pytest.fixture
def scored(seeded):
    with seeded.tx() as conn:        # both wallets were on the list for a while before the tests
        conn.execute(sa.text("UPDATE wallet_enrollment SET first_top_at = :t"),
                     {"t": NOW - timedelta(days=10)})
    for i, h in enumerate((18, 12, 6)):
        snap(seeded, [Trader(address=PUB1, rank=1 + i % 2, pnl=500.0),
                      Trader(address=PUB2, rank=3, pnl=300.0)] if i != 1 else
             [Trader(address=PUB1, rank=2, pnl=510.0)], h)
    # PUB1: four measured closes inside the lookback, three wins
    for i, (pnl, mins) in enumerate(((E18, 500), (E18 // 2, 400), (E18, 300), (-E18 // 4, 200))):
        close(seeded, PUB1, f"0xc{i}", pnl, 2 * E18, ago(minutes=mins), 3000 + i)
    return seeded


def rows_at(store, c, as_of):
    with store.tx() as conn:
        return {r["address"]: r for r in S.compute(conn, CHAIN, as_of, c)}


def test_compute_scores_known_wallets_with_explainable_components(scored):
    c = cfg()
    r = rows_at(scored, c, AS_OF)
    assert set(r) == {PUB1, PUB2} and PRIV not in r
    a = r[PUB1]
    assert a["quality"] == "sufficient" and a["score"] is not None and 0 <= a["score"] <= 100
    comp = a["components"]
    assert comp["win_rate_lb"]["inputs"] == {"closes": 5, "wins": 4} or comp["win_rate_lb"]["inputs"]["closes"] >= 4
    assert comp["return_on_cost"]["available"] and comp["recency"]["available"]
    assert comp["list_persistence"]["inputs"]["lists"] == 4         # seeded + three added
    # PUB2 has no measured close: PnL components unavailable, not zero
    b = r[PUB2]
    assert not b["components"]["win_rate_lb"]["available"]
    assert b["components"]["win_rate_lb"]["value"] is None
    assert b["quality"] in ("limited", "insufficient")


def test_limited_scores_are_capped_below_measured_evidence():
    c = cfg()
    comps = S.components(ev(last_trade_at=AS_OF, snapshots=10, present=10, strength_sum=10.0),
                         AS_OF, c)       # perfect list position and recency, no trades measured
    score, _, quality, reason = S.combine(comps, c)
    assert quality == "limited" and score == Decimal("50.00") and "capped at 50" in reason
    low = cfg(limited_cap=100.0)
    assert S.combine(S.components(ev(last_trade_at=AS_OF, snapshots=10, present=10,
                                     strength_sum=10.0), AS_OF, low), low)[0] > 50


def test_no_look_ahead_future_rows_cannot_change_a_past_score(scored):
    c = cfg()
    before = rows_at(scored, c, AS_OF)
    # everything below happens AFTER as_of: trades, a measured loss, a new list, a new wallet
    future = AS_OF + timedelta(hours=2)
    for i in range(4):
        close(scored, PUB1, f"0xd{i}", -E18, E18, int(future.timestamp()) + i, 9000 + i)
    with scored.tx() as conn:
        conn.execute(sa.text("UPDATE leaderboard_snapshots SET captured_at = captured_at"))
    sid = scored.record_snapshot([Trader(address=PUB2, rank=1, pnl=999.0),
                                  Trader(address="0x" + "a7" * 20, rank=2, pnl=1.0)],
                                 source="agora", status="ok", complete=True, row_count=2)
    with scored.tx() as conn:
        conn.execute(sa.text("UPDATE leaderboard_snapshots SET captured_at = :t WHERE id = :i"),
                     {"t": future, "i": sid})
        conn.execute(sa.text("UPDATE wallet_enrollment SET first_top_at = :t WHERE address = :a"),
                     {"t": future, "a": "0x" + "a7" * 20})
    after = rows_at(scored, c, AS_OF)
    assert set(after) == set(before) == {PUB1, PUB2}                 # the new wallet is not scored
    for w in before:
        for k in ("score", "weight_available", "quality", "reason", "components"):
            assert after[w][k] == before[w][k], (w, k)
    # and a later as_of really does see the new information
    later = rows_at(scored, c, future + timedelta(hours=1))
    assert later[PUB1]["score"] != before[PUB1]["score"]


def test_data_older_than_the_lookback_is_ignored(scored):
    c = cfg(lookback_days=1)
    with scored.tx() as conn:
        conn.execute(sa.text("UPDATE trades SET block_time = block_time - interval '10 days' "
                             "WHERE wallet = :w"), {"w": PUB1})
    r = rows_at(scored, c, AS_OF)[PUB1]
    assert not r["components"]["win_rate_lb"]["available"]
    assert r["components"]["recency"]["available"]     # recency looks at the last trade ever


def test_versions_are_immutable(scored):
    with scored.tx() as conn:
        h = S.ensure_version(conn, cfg())
    with scored.tx() as conn:
        assert S.ensure_version(conn, cfg()) == h        # same config: fine
    with pytest.raises(S.ScoringConfigError):
        with scored.tx() as conn:
            S.ensure_version(conn, cfg(wilson_z=2.0))
    with scored.tx() as conn:
        assert S.ensure_version(conn, cfg(wilson_z=2.0, version="t2"))


def test_run_once_is_idempotent_and_prunes_hourly_rows(scored):
    c = cfg()
    hour = S.floor_hour(AS_OF)
    first = S.run_once(scored, c, now=hour + timedelta(minutes=5))
    assert first["wallets"] == 2 and first["inserted"] == 2
    again = S.run_once(scored, c, now=hour + timedelta(minutes=40))      # same hour
    assert again["inserted"] == 0
    with scored.tx() as conn:
        n = conn.execute(sa.text("SELECT count(*) FROM wallet_scores")).scalar()
    assert n == 2
    # a row hours old that is not the 00:00 row is thinned; the midnight row is kept
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
    with scored.tx() as conn:
        for t_ in (midnight, midnight + timedelta(hours=5)):
            conn.execute(sa.text("""INSERT INTO wallet_scores (chain_id, address, version, as_of,
                score, weight_available, quality, components) VALUES (:c, :a, 't1', :t, 50, 1,
                'sufficient', '{}')"""), {"c": CHAIN, "a": PUB1, "t": t_})
    out = S.run_once(scored, c, now=datetime.now(UTC))
    assert out["pruned"] == 1
    with scored.tx() as conn:
        kept = {r[0] for r in conn.execute(sa.text(
            "SELECT as_of FROM wallet_scores WHERE address = :a"), {"a": PUB1})}
    assert midnight in kept and midnight + timedelta(hours=5) not in kept


def test_backfill_replays_with_the_live_code_path(scored):
    c = cfg()
    out = S.backfill(scored, c, days=2, now=AS_OF)
    assert out["points"] >= 2 and out["inserted"] >= 1
    again = S.backfill(scored, c, days=2, now=AS_OF)
    assert again["inserted"] == 0
    with scored.tx() as conn:
        pts = conn.execute(sa.text("SELECT as_of FROM wallet_scores WHERE address = :a "
                                   "ORDER BY as_of"), {"a": PUB1}).scalars().all()
    assert all(p.hour == 0 and p.minute == 0 for p in pts)


def test_score_and_quality_are_constrained(scored):
    with pytest.raises(sa.exc.DBAPIError):
        with scored.tx() as conn:
            S.ensure_version(conn, cfg())
            conn.execute(sa.text("""INSERT INTO wallet_scores (chain_id, address, version, as_of,
                score, weight_available, quality, components) VALUES (:c, :a, 't1', now(), 101, 1,
                'sufficient', '{}')"""), {"c": CHAIN, "a": PUB1})
    with pytest.raises(sa.exc.DBAPIError):
        with scored.tx() as conn:
            S.ensure_version(conn, cfg())
            conn.execute(sa.text("""INSERT INTO wallet_scores (chain_id, address, version, as_of,
                score, weight_available, quality, components) VALUES (:c, :a, 't1', now(), 50, 1,
                'great', '{}')"""), {"c": CHAIN, "a": PUB1})


# --- exposure --------------------------------------------------------------------------------
def test_dashboard_role_sees_scores_of_public_wallets_only(scored):
    c = cfg()
    S.run_once(scored, c, now=AS_OF)
    with scored.tx() as conn:
        conn.execute(sa.text("""INSERT INTO wallet_scores (chain_id, address, version, as_of,
            score, weight_available, quality, components) VALUES (:c, :a, 't1', now(), 50, 1,
            'sufficient', '{}')"""), {"c": CHAIN, "a": PRIV})
    eng = sa.create_engine(role_url())
    with eng.connect() as conn:
        seen = {r[0] for r in conn.execute(sa.text("SELECT address FROM public_api.wallet_scores"))}
        vers = conn.execute(sa.text("SELECT version FROM public_api.score_versions")).scalars().all()
        with pytest.raises(sa.exc.DBAPIError):
            conn.execute(sa.text("SELECT 1 FROM public.wallet_scores LIMIT 1"))
    eng.dispose()
    assert seen == {PUB1, PUB2} and PRIV not in seen and vers == ["t1"]


def live_cfg(**kw):
    c = make_cfg()
    c.scoring = cfg(**kw)
    return c


def test_leaderboard_shows_score_filters_and_sorts_with_unknown_last(scored, make_live):
    S.run_once(scored, cfg(), now=AS_OF)
    lv = make_live(live_cfg())
    j = lv.get("/api/v1/leaderboard", params={"window": "all", "sort": "score", "limit": 100}).json()
    ws = {w["wallet"]: w for w in j["page"]["items"]}
    assert ws[PUB1]["score"] is not None and ws[PUB1]["score_quality"] == "sufficient"
    assert j["score_version"] == "t1"
    items = j["page"]["items"]
    known = [w["score"] for w in items if w["score"] is not None]
    assert known == sorted(known, key=Decimal, reverse=True)          # highest score first
    assert all(Decimal(w["score"]) <= 50 for w in items if w["score_quality"] == "limited")
    none_after = [w["score"] is None for w in items]
    assert none_after == sorted(none_after)                  # all unknown scores at the end
    asc = lv.get("/api/v1/leaderboard", params={"window": "all", "sort": "score", "dir": "asc"}).json()
    flags = [w["score"] is None for w in asc["page"]["items"]]
    assert flags == sorted(flags)
    hi = lv.get("/api/v1/leaderboard", params={"window": "all", "min_score": "1"}).json()
    assert PUB1 in {w["wallet"] for w in hi["page"]["items"]}
    top = lv.get("/api/v1/leaderboard", params={"window": "all", "min_score": "100"}).json()
    assert top["page"]["total"] == 0
    for bad in ("101", "-1", "abc", "5.123", "1e1"):
        assert lv.get("/api/v1/leaderboard", params={"min_score": bad}).status_code == 400
    page = lv.get("/leaderboard", params={"window": "all"}).text
    assert "Score" in page and "t1" in page


def test_profile_explains_the_score_and_has_no_invented_confidence(scored, make_live):
    S.run_once(scored, cfg(), now=AS_OF)
    S.backfill(scored, cfg(), days=2, now=AS_OF)
    lv = make_live(live_cfg())
    j = lv.get(f"/api/v1/leaderboard/{PUB1}/score").json()
    sc = j["score"]
    assert sc["quality"] == "sufficient" and set(sc["components"]) == set(S.COMPONENTS)
    assert all({"available", "value", "weight", "inputs"} <= set(c) for c in sc["components"].values())
    assert j["score_version"] == "t1" and j["score_versions"][0]["config"]["weights"]
    assert len(j["score_history"]) >= 1
    text = json.dumps(j).lower()
    assert "confidence" not in text and "probability" not in text and "%" not in text
    html = lv.get(f"/leaderboard/{PUB1}").text
    assert "Contribution (points)" in html and "win_rate_lb" in html
    nob = lv.get(f"/api/v1/leaderboard/{PUB2}/score").json()
    assert nob["score"] is None or nob["score"]["quality"] != "sufficient"


def test_csv_has_score_columns(scored, make_live):
    S.run_once(scored, cfg(), now=AS_OF)
    r = make_live(live_cfg()).get("/api/v1/leaderboard.csv", params={"window": "all", "q": PUB1})
    head, row = r.text.strip().split("\n")[:2]
    cols = head.split(",")
    vals = dict(zip(cols, row.split(","), strict=False))
    assert vals["score_version"] == "t1" and vals["score_quality"] == "sufficient"
    assert Decimal(vals["score"]) > 0


def test_later_tracking_exclusion_does_not_rewrite_historical_score(scored):
    before = rows_at(scored, cfg(), AS_OF)
    with scored.tx() as conn:
        conn.execute(sa.text("UPDATE wallet_enrollment SET status = 'excluded', "
                             "excluded_reason = 'contract' WHERE address = :w"), {"w": PUB1})
    assert rows_at(scored, cfg(), AS_OF) == before
