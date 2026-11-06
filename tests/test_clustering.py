"""Evidence-based clusters: the evidence rules, the strength matrix, deterministic lifecycle
(create / extend / reduce / merge / split / invalidate), point-in-time membership, immutable
versions, retention that keeps history, and the privacy boundary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from conftest import CHAIN, role_url

from agora_tracker import clustering as K
from agora_tracker.classify import TradeEvent
from agora_tracker.config import ClusteringConfig
from agora_tracker.leaderboard import Trader

UTC = timezone.utc
E18 = 10 ** 18
D0 = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=20)
W = {i: "0x" + f"a{i}" * 20 for i in range(1, 8)}
PRIVATE = "0x" + "b9" * 20


def tok(i: int) -> str:
    return "0x" + f"{i:040x}"


def cfg(**kw) -> ClusteringConfig:
    kw.setdefault("version", "t1")
    kw.setdefault("lookback_days", 4)
    return ClusteringConfig(**kw)


def at(days: float) -> datetime:
    return D0 + timedelta(days=days)


# --- pure rules -------------------------------------------------------------------------------
def pair(sb=0, tr=0, co=0) -> K.Pair:
    p = K.Pair()
    for i in range(sb):
        p.same_block[tok(i)].add(100 + i)
    for i in range(tr):
        p.transfers.add((f"0x{i:064x}", tok(i)))
    for i in range(co):
        p.co_entry[tok(50 + i)] = 10
    return p


@pytest.mark.parametrize("sb,tr,co,expect", [
    (0, 0, 0, None), (0, 0, 2, None), (0, 0, 3, "weak"), (0, 0, 4, "weak"), (0, 0, 5, "moderate"),
    (1, 0, 0, "weak"), (1, 0, 4, "weak"), (2, 0, 0, "moderate"), (3, 0, 0, "strong"),
    (2, 1, 0, "strong"), (2, 0, 2, "strong"), (0, 1, 0, "weak"), (0, 2, 0, "moderate"),
    (0, 3, 0, "strong"), (0, 1, 2, "moderate"), (1, 1, 0, "weak"), (1, 0, 5, "moderate"),
])
def test_edge_strength_matrix(sb, tr, co, expect):
    assert K.classify_edge(pair(sb, tr, co), cfg()) == expect


def test_components_use_only_strong_and_moderate_edges():
    edges = {(W[1], W[2]): "strong", (W[2], W[3]): "weak", (W[4], W[5]): "moderate",
             (W[5], W[6]): "weak"}
    comps, big = K.build_components(edges, cfg())
    assert comps == [frozenset({W[1], W[2]}), frozenset({W[4], W[5]})] and big == []


def test_components_are_transitive_sorted_and_report_oversized():
    edges = {(W[1], W[2]): "moderate", (W[2], W[3]): "strong", (W[5], W[6]): "strong",
             (W[6], W[7]): "strong"}
    comps, big = K.build_components(edges, cfg(max_cluster_size=2))
    assert sorted(len(b) for b in big) == [3, 3] and comps == []
    comps, big = K.build_components(edges, cfg())
    assert comps == [frozenset({W[1], W[2], W[3]}), frozenset({W[5], W[6], W[7]})] and not big


def test_lifecycle_plan_is_deterministic_and_covers_every_case():
    old = {1: frozenset({W[1], W[2], W[3]}), 2: frozenset({W[5], W[6]}), 3: frozenset({W[7]})}
    plan = K.plan_lifecycle(old, [frozenset({W[1], W[2]}), frozenset({W[3], W[5], W[6]})])
    assert plan.continues == {1: frozenset({W[1], W[2]}), 2: frozenset({W[3], W[5], W[6]})}
    assert plan.invalidates == [3] and plan.creates == [] and plan.merges == {}
    # merge: both old clusters are the best match of one component; the larger overlap survives
    plan = K.plan_lifecycle({1: frozenset({W[1], W[2], W[3]}), 2: frozenset({W[5], W[6]})},
                            [frozenset({W[1], W[2], W[3], W[5], W[6]})])
    assert plan.continues == {1: frozenset({W[1], W[2], W[3], W[5], W[6]})}
    assert plan.merges == {1: [2]} and plan.creates == [] and plan.invalidates == []
    # split: the part that no old cluster continues is a new cluster recorded as a split
    plan = K.plan_lifecycle({1: frozenset({W[1], W[2], W[3], W[4]})},
                            [frozenset({W[1], W[2], W[3]}), frozenset({W[4], W[5]})])
    assert plan.continues == {1: frozenset({W[1], W[2], W[3]})}
    assert plan.creates == [(frozenset({W[4], W[5]}), 1)]
    # ties are broken by the smallest address, independent of input order
    a = K.plan_lifecycle({1: frozenset({W[1], W[2], W[3], W[4]})},
                         [frozenset({W[3], W[4]}), frozenset({W[1], W[2]})])
    b = K.plan_lifecycle({1: frozenset({W[1], W[2], W[3], W[4]})},
                         [frozenset({W[1], W[2]}), frozenset({W[3], W[4]})])
    assert a.continues == b.continues == {1: frozenset({W[1], W[2]})}


def test_config_validation_and_hash():
    with pytest.raises(K.ClusteringConfigError):
        K.validate(cfg(same_block_crowd_cap=1))
    with pytest.raises(K.ClusteringConfigError):
        K.validate(cfg(min_cluster_size=1))
    with pytest.raises(K.ClusteringConfigError):
        K.validate(cfg(max_cluster_size=1))
    assert K.config_hash(cfg().model()) == K.config_hash(cfg(interval_s=60.0, enabled=False).model())
    assert K.config_hash(cfg().model()) != K.config_hash(cfg(strong_same_block=4).model())
    assert K.floor_interval(datetime(2026, 1, 1, 7, 59, tzinfo=UTC), 21600) == \
        datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


# --- database ---------------------------------------------------------------------------------
_seq = {"n": 0, "block": 10_000}


def _tx() -> str:
    _seq["n"] += 1
    return f"0x{_seq['n']:064x}"


def buy(store, wallet, token, block, days):
    with store.tx() as c:
        store.insert_trade(c, TradeEvent("BUY", _tx(), block, wallet, token, 10 * E18, E18,
                                         confidence="high", hype_method="balance_diff"),
                           int(at(days).timestamp()))


def same_block(store, wallets, token, block, days):
    for w in wallets:
        buy(store, w, token, block, days)


def transfer(store, src, dst, token, block, days):
    tx = _tx()
    with store.tx() as c:
        store.insert_trade(c, TradeEvent("TRANSFER_OUT", tx, block, src, token, E18, None,
                                         confidence="unknown"), int(at(days).timestamp()))
        store.insert_trade(c, TradeEvent("TRANSFER_IN", tx, block, dst, token, E18, None,
                                         confidence="unknown"), int(at(days).timestamp()))


@pytest.fixture
def world(store, role_ready):
    store.record_snapshot([Trader(address=W[i], rank=i, pnl=100.0) for i in range(1, 8)],
                          source="agora", status="ok", complete=True, row_count=7)
    store.sync_manual_watchlist({PRIVATE: "private-friend"})
    with store.tx() as c:
        c.execute(sa.text("UPDATE wallet_enrollment SET first_top_at = :t"), {"t": at(-30)})
    return store


def evaluate(store, c, as_of):
    with store.tx() as conn:
        return K.evaluate(conn, CHAIN, as_of, c)


def test_same_block_buys_of_two_tokens_make_a_moderate_pair(world):
    same_block(world, [W[1], W[2]], tok(1), 20_001, 1.0)
    same_block(world, [W[1], W[2]], tok(2), 20_002, 1.1)
    r = evaluate(world, cfg(), at(2))
    assert r["strengths"] == {(W[1], W[2]): "moderate"}
    assert r["components"] == [frozenset({W[1], W[2]})]
    ev = r["pairs"][(W[1], W[2])].evidence()
    assert ev["same_block_buy"]["tokens"] == 2 and ev["direct_transfer"]["transfers"] == 0
    assert ev["same_block_buy"]["examples"][0] == {"token": tok(1), "block": 20_001}
    assert ev["first_at"] and ev["last_at"]


def test_one_shared_token_is_only_a_weak_pair_and_forms_no_cluster(world):
    same_block(world, [W[1], W[2]], tok(1), 20_001, 1.0)
    r = evaluate(world, cfg(), at(2))
    assert r["strengths"] == {(W[1], W[2]): "weak"} and r["components"] == []


def test_a_crowd_in_one_block_is_not_pair_evidence(world):
    same_block(world, [W[1], W[2], W[3], W[4]], tok(1), 20_001, 1.0)
    same_block(world, [W[1], W[2], W[3], W[4]], tok(2), 20_002, 1.1)
    assert len(evaluate(world, cfg(same_block_crowd_cap=4), at(2))["strengths"]) == 6
    r = evaluate(world, cfg(same_block_crowd_cap=3), at(2))
    assert r["strengths"] == {} and r["stats"]["crowds_skipped"] == 2


def test_same_transaction_transfers_between_wallets_are_evidence(world):
    for i in range(3):
        transfer(world, W[1], W[2] if i % 2 else W[2], tok(i), 20_100 + i, 1.0 + i / 10)
    transfer(world, W[3], PRIVATE, tok(9), 20_200, 1.0)          # private wallet: no evidence
    r = evaluate(world, cfg(), at(2))
    assert r["strengths"] == {(W[1], W[2]): "strong"}
    assert r["pairs"][(W[1], W[2])].evidence()["direct_transfer"]["tokens"] == 3
    assert PRIVATE not in {a for k in r["pairs"] for a in k}


def test_co_entry_needs_a_selective_token_and_different_blocks(world):
    # five tokens, each first-bought by W1 then W2 forty seconds later, in different blocks
    for i in range(5):
        buy(world, W[1], tok(i), 21_000 + 2 * i, 1.0 + i / 100)
        with world.tx() as c:
            world.insert_trade(c, TradeEvent("BUY", _tx(), 21_001 + 2 * i, W[2], tok(i), E18, E18),
                               int((at(1.0 + i / 100) + timedelta(seconds=40)).timestamp()))
    r = evaluate(world, cfg(), at(2))
    assert r["strengths"] == {(W[1], W[2]): "moderate"}
    assert r["pairs"][(W[1], W[2])].evidence()["co_entry"]["tokens"] == 5
    # the same pattern on a token that many tracked wallets entered is a hot token, not evidence
    r = evaluate(world, cfg(co_entry_max_wallets=1), at(2))
    assert r["strengths"] == {} and r["stats"]["hot_tokens_skipped"] == 5
    # a slower follower is not a co-entry
    assert evaluate(world, cfg(co_entry_window_s=10), at(2))["strengths"] == {}


def test_a_followers_later_buy_is_not_a_first_entry(world):
    for i in range(5):
        buy(world, W[2], tok(i), 19_000 + i, 0.2)                # W2 already held the token
        buy(world, W[1], tok(i), 21_000 + 2 * i, 1.0 + i / 100)
        with world.tx() as c:
            world.insert_trade(c, TradeEvent("BUY", _tx(), 21_001 + 2 * i, W[2], tok(i), E18, E18),
                               int((at(1.0 + i / 100) + timedelta(seconds=40)).timestamp()))
    assert evaluate(world, cfg(lookback_days=10), at(2))["strengths"] == {}


def test_no_look_ahead_future_trades_cannot_change_an_earlier_evaluation(world):
    same_block(world, [W[1], W[2]], tok(1), 20_001, 1.0)
    same_block(world, [W[1], W[2]], tok(2), 20_002, 1.1)
    before = evaluate(world, cfg(), at(2))
    # everything below is AFTER as_of: more evidence, a new pair, even the same pair again
    same_block(world, [W[1], W[2]], tok(3), 20_003, 2.5)
    same_block(world, [W[3], W[4]], tok(4), 20_004, 2.6)
    same_block(world, [W[3], W[4]], tok(5), 20_005, 2.7)
    after = evaluate(world, cfg(), at(2))
    assert after["strengths"] == before["strengths"] == {(W[1], W[2]): "moderate"}
    assert after["pairs"][(W[1], W[2])].evidence() == before["pairs"][(W[1], W[2])].evidence()
    later = evaluate(world, cfg(), at(3))
    assert later["strengths"][(W[1], W[2])] == "strong"           # three tokens now
    assert later["strengths"][(W[3], W[4])] == "moderate"


def test_evidence_older_than_the_lookback_is_ignored(world):
    same_block(world, [W[1], W[2]], tok(1), 20_001, 1.0)
    same_block(world, [W[1], W[2]], tok(2), 20_002, 1.1)
    assert evaluate(world, cfg(lookback_days=4), at(2))["strengths"]
    assert not evaluate(world, cfg(lookback_days=4), at(8))["strengths"]


def events(store, cid=None):
    with store.tx() as c:
        rows = c.execute(sa.text("SELECT cluster_id, kind, detail FROM cluster_events "
                                 "ORDER BY id")).mappings().all()
    return [dict(r) for r in rows if cid is None or r["cluster_id"] == cid]


def test_full_lifecycle_create_extend_merge_split_invalidate(world):
    c = cfg()

    def run(day):
        out = K.run_once(world, c, as_of=at(day))
        assert not out.get("skipped"), out
        return out

    # day 1: two separate pairs
    same_block(world, [W[1], W[2]], tok(1), 20_001, 1.0)
    same_block(world, [W[1], W[2]], tok(2), 20_002, 1.05)
    same_block(world, [W[5], W[6]], tok(3), 20_003, 1.0)
    same_block(world, [W[5], W[6]], tok(4), 20_004, 1.05)
    out = run(1.5)
    assert out["clusters"] == 2 and out["changes"] == {"created": 2}
    with world.tx() as conn:
        ids = {r[0]: r[1] for r in conn.execute(sa.text(
            "SELECT m.address, m.cluster_id FROM wallet_cluster_members m"))}
    c1, c2 = ids[W[1]], ids[W[5]]
    assert ids[W[2]] == c1 and ids[W[6]] == c2 and c1 != c2

    # day 2: W3 joins the first cluster (three same-block tokens with W2)
    for i, blk in enumerate((20_011, 20_012, 20_013)):
        same_block(world, [W[2], W[3]], tok(10 + i), blk, 2.0 + i / 100)
    out = run(2.5)
    assert out["changes"] == {"extended": 1} and out["clusters"] == 2

    # day 3: a strong transfer link joins the two clusters; the larger one survives
    for i in range(3):
        transfer(world, W[3], W[5], tok(20 + i), 20_021 + i, 3.0 + i / 100)
    out = run(3.5)
    assert out["changes"].get("merged") == 1 and out["clusters"] == 1
    with world.tx() as conn:
        row = conn.execute(sa.text("SELECT status, merged_into FROM wallet_clusters WHERE id = :i"),
                           {"i": c2}).one()
    assert row == ("merged", c1)
    kinds = [e["kind"] for e in events(world)]
    assert "MERGED_INTO" in kinds and "ABSORBED" in kinds

    # day 7.5: only the original two pairs are still being refreshed -> the big cluster shrinks
    # to one pair and the other pair is recorded as a split from it
    for i, blk in ((1, 20_031), (2, 20_032)):
        same_block(world, [W[1], W[2]], tok(30 + i), blk, 7.0 + i / 100)
        same_block(world, [W[5], W[6]], tok(40 + i), blk + 100, 7.0 + i / 100)
    out = run(7.5)
    assert out["clusters"] == 2 and out["changes"].get("split") == 1
    with world.tx() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, status, split_from FROM wallet_clusters ORDER BY id")).all()
    split = [r for r in rows if r[2] == c1]
    assert len(split) == 1 and split[0][1] == "active"
    assert {e["kind"] for e in events(world, c1)} >= {"CREATED", "EXTENDED", "ABSORBED", "REDUCED"}

    # day 12: nothing inside the lookback any more -> everything is invalidated, nothing deleted
    out = run(12)
    assert out["clusters"] == 0 and out["changes"] == {"invalidated": 2}
    with world.tx() as conn:
        status = dict(conn.execute(sa.text("SELECT status, count(*) FROM wallet_clusters "
                                           "GROUP BY status")).all())
        open_members = conn.execute(sa.text(
            "SELECT count(*) FROM wallet_cluster_members WHERE valid_to IS NULL")).scalar()
    assert status == {"merged": 1, "invalidated": 2} and open_members == 0

    # point-in-time membership never changes for an earlier moment
    with world.tx() as conn:
        v = c.version
        assert K.membership_at(conn, CHAIN, v, [W[1], W[2], W[3], W[5]], at(1.6)) == \
            {W[1]: c1, W[2]: c1, W[5]: c2}
        assert K.membership_at(conn, CHAIN, v, [W[3]], at(2.6)) == {W[3]: c1}
        assert K.membership_at(conn, CHAIN, v, [W[5]], at(3.6)) == {W[5]: c1}   # after the merge
        assert K.membership_at(conn, CHAIN, v, [W[1], W[2], W[3]], at(7.6)) == {W[1]: c1, W[2]: c1}
        assert K.membership_at(conn, CHAIN, v, [W[1]], at(12.1)) == {}
        assert K.membership_at(conn, CHAIN, v, [W[1]], at(1.4)) == {}            # before the run
        assert K.membership_at(conn, CHAIN, v, [], at(1.6)) == {}


def test_runs_are_idempotent_ordered_and_versions_immutable(world):
    c = cfg()
    same_block(world, [W[1], W[2]], tok(1), 20_001, 1.0)
    same_block(world, [W[1], W[2]], tok(2), 20_002, 1.1)
    first = K.run_once(world, c, as_of=at(2))
    assert first["clusters"] == 1 and first["run_id"]
    again = K.run_once(world, c, as_of=at(2))
    assert again["skipped"] is True
    assert K.run_once(world, c, as_of=at(1))["skipped"] is True        # never out of order
    with world.tx() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM cluster_runs")).scalar() == 1
        assert conn.execute(sa.text("SELECT count(*) FROM wallet_clusters")).scalar() == 1
    with pytest.raises(K.ClusteringConfigError):
        K.run_once(world, cfg(strong_same_block=4), as_of=at(3))
    assert K.run_once(world, cfg(strong_same_block=4, version="t2"), as_of=at(3))["clusters"] == 1


def test_unchanged_membership_writes_no_events_and_stores_no_weak_edges(world):
    c = cfg()
    same_block(world, [W[1], W[2]], tok(1), 20_001, 1.0)
    same_block(world, [W[1], W[2]], tok(2), 20_002, 1.1)
    same_block(world, [W[3], W[4]], tok(3), 20_003, 1.0)                 # a weak pair only
    K.run_once(world, c, as_of=at(2), now=at(2))
    n = len(events(world))
    out = K.run_once(world, c, as_of=at(2.25), now=at(2.25))
    assert out["changes"] == {} and len(events(world)) == n
    with world.tx() as conn:
        s = dict(conn.execute(sa.text("SELECT strength, count(*) FROM wallet_edges "
                                      "GROUP BY strength")).all())
        runs = conn.execute(sa.text("SELECT edges_n FROM cluster_runs ORDER BY id")).scalars().all()
    assert s == {"moderate": 2} and runs == [2, 2]                      # edges_n counts weak too
    K.run_once(world, cfg(store_weak_edges=True, version="t3"), as_of=at(2.5), now=at(2.5))
    with world.tx() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM wallet_edges e JOIN cluster_runs r "
                                    "ON r.id = e.run_id WHERE r.version = 't3' "
                                    "AND e.strength = 'weak'")).scalar() == 1


def test_backfill_replays_forward_and_retention_keeps_history(world):
    c = cfg()
    for i in range(3):
        same_block(world, [W[1], W[2]], tok(i), 20_001 + i, 11.0 + i / 10)
    out = K.backfill(world, c, 4, now=at(13))
    assert out == {"points": 4, "run": 4, "skipped": 0, "version": "t1"}      # days 9..12
    assert K.backfill(world, c, 4, now=at(13))["run"] == 0                    # forward only
    K.run_once(world, c, as_of=at(13.25), now=at(13.25))                      # a 6-hourly run
    with world.tx() as conn:
        by_run = dict(conn.execute(sa.text(
            "SELECT r.as_of, count(e.id) FROM cluster_runs r LEFT JOIN wallet_edges e "
            "ON e.run_id = r.id GROUP BY r.as_of")).all())
    assert by_run[at(13.25)] == 1 and by_run[at(12)] == 1 and by_run[at(9)] == 0
    with world.tx() as conn:      # a week later only the 00:00 UTC runs keep their edges
        n = K.prune(conn, c.version, at(21), keep_all_days=7, keep_daily_days=90)
        left = conn.execute(sa.text("SELECT r.as_of FROM wallet_edges e JOIN cluster_runs r "
                                    "ON r.id = e.run_id")).scalars().all()
    assert n == 1 and left == [at(12)]
    with world.tx() as conn:      # far enough later, even the daily rows go; history stays
        assert K.prune(conn, c.version, at(200), keep_all_days=7, keep_daily_days=90) == 1
        assert conn.execute(sa.text("SELECT count(*) FROM wallet_edges")).scalar() == 0
        assert conn.execute(sa.text("SELECT count(*) FROM cluster_runs")).scalar() == 5
        assert conn.execute(sa.text("SELECT count(*) FROM wallet_clusters")).scalar() == 1
        assert conn.execute(sa.text("SELECT count(*) FROM cluster_events")).scalar() >= 1


def test_private_wallets_never_enter_clusters(world):
    same_block(world, [W[1], PRIVATE], tok(1), 20_001, 1.0)
    same_block(world, [W[1], PRIVATE], tok(2), 20_002, 1.1)
    same_block(world, [W[1], PRIVATE], tok(3), 20_003, 1.2)
    r = evaluate(world, cfg(), at(2))
    assert r["strengths"] == {} and r["stats"]["wallets"] == 7


def test_dashboard_role_sees_only_clusters_made_entirely_of_public_wallets(world):
    c = cfg()
    same_block(world, [W[1], W[2]], tok(1), 20_001, 1.0)
    same_block(world, [W[1], W[2]], tok(2), 20_002, 1.1)
    K.run_once(world, c, as_of=at(2))
    # a cluster that (hypothetically) contains a private wallet must be invisible, with its
    # members, its events and its edges
    with world.tx() as conn:
        run_id = conn.execute(sa.text("SELECT id FROM cluster_runs")).scalar()
        hid = conn.execute(sa.text(
            "INSERT INTO wallet_clusters (chain_id, version, created_run_id, created_at) "
            "VALUES (:c, :v, :r, now()) RETURNING id"),
            {"c": CHAIN, "v": c.version, "r": run_id}).scalar()
        conn.execute(sa.text(
            "INSERT INTO wallet_cluster_members (cluster_id, chain_id, address, valid_from, "
            "from_run_id) VALUES (:i, :c, :a, now(), :r), (:i, :c, :b, now(), :r)"),
            {"i": hid, "c": CHAIN, "a": W[7], "b": PRIVATE, "r": run_id})
        conn.execute(sa.text("INSERT INTO cluster_events (cluster_id, run_id, at, kind, detail) "
                             "VALUES (:i, :r, now(), 'CREATED', CAST(:d AS jsonb))"),
                     {"i": hid, "r": run_id, "d": json.dumps({"members": [PRIVATE]})})
        conn.execute(sa.text(
            "INSERT INTO wallet_edges (run_id, chain_id, wallet_a, wallet_b, strength, evidence) "
            "VALUES (:r, :c, :a, :b, 'strong', '{}')"),
            {"r": run_id, "c": CHAIN, "a": W[7], "b": PRIVATE})
    eng = sa.create_engine(role_url())
    with eng.connect() as conn:
        clusters = conn.execute(sa.text("SELECT id FROM public_api.wallet_clusters")).scalars().all()
        members = conn.execute(sa.text(
            "SELECT address FROM public_api.wallet_cluster_members")).scalars().all()
        ev = conn.execute(sa.text("SELECT cluster_id FROM public_api.cluster_events")).scalars().all()
        edges = conn.execute(sa.text("SELECT wallet_b FROM public_api.wallet_edges")).scalars().all()
        for table in ("wallet_clusters", "wallet_cluster_members", "wallet_edges", "cluster_events"):
            with pytest.raises(sa.exc.ProgrammingError):
                with conn.begin_nested():
                    conn.execute(sa.text(f"SELECT 1 FROM public.{table}"))
    eng.dispose()
    assert hid not in clusters and len(clusters) == 1
    assert sorted(members) == [W[1], W[2]] and PRIVATE not in members
    assert hid not in ev and PRIVATE not in edges
