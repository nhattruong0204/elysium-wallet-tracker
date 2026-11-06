"""Cumulative Top-100 tracking: every wallet that was ever in an accepted, complete list stays
scanned; membership, eligibility and scan coverage are recorded as different facts."""

from __future__ import annotations

import json
import logging
from collections import defaultdict

import pytest
import requests
import sqlalchemy as sa
from conftest import role_url
from test_tracker import E18, FEE, POOL, W1, FakeChain

from agora_tracker import rpc as rpc_mod
from agora_tracker import tracking
from agora_tracker.__main__ import Tracker
from agora_tracker.config import AlertConfig, ChainConfig, Config, LeaderboardConfig, TelegramConfig
from agora_tracker.leaderboard import Trader

CHAIN = 99901


def addr(i: int) -> str:
    return "0x" + f"{i:040x}"


A, B, C, D, E_, F = (addr(i) for i in range(1, 7))
MANUAL = addr(900)
TG = addr(901)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(rpc_mod.time, "sleep", lambda s: None)


class LB:
    """Scripted leaderboard endpoint."""
    last_payload = None
    last_meta: dict = {}

    def __init__(self):
        self.rows: list[Trader] = []
        self.error: Exception | None = None

    def set(self, addrs):
        self.rows = [Trader(address=a, rank=i + 1, pnl=1000.0 - i) for i, a in enumerate(addrs)]

    def fetch(self):
        if self.error:
            raise self.error
        return self.rows


class CountingChain(FakeChain):
    """FakeChain with the cost counters RpcClient keeps (see RpcClient.metrics)."""

    def __init__(self):
        super().__init__()
        self.metrics: dict[str, float] = defaultdict(float)

    def call(self, method, params):
        self.metrics["http"] += 1
        self.metrics["calls"] += 1
        self.metrics["m:" + method] += 1
        return super().call(method, params)

    def batch(self, calls):
        self.metrics["http"] += 1
        return super().batch(calls)


def make_tracker(store, monkeypatch, *, top_n=4, cumulative=True) -> tuple[Tracker, LB]:
    cfg = Config(chain=ChainConfig(rpc_urls=["http://fake"], max_block_range=100),
                 leaderboard=LeaderboardConfig(enabled=False, top_n=top_n),
                 alerts=AlertConfig(), telegram=TelegramConfig())
    cfg.exclude_contracts = False
    cfg.tracking.cumulative = cumulative
    monkeypatch.setattr("agora_tracker.__main__.Store", lambda **kw: store)
    t = Tracker(cfg, dry_run=True)
    chain = CountingChain()
    t.rpc = t.follower.rpc = t.follower.tokens.rpc = chain
    t.chain = chain
    lb = LB()
    t.leaderboard = lb
    return t, lb


def rows(store, sql, **p):
    with store.engine.connect() as c:
        return [dict(r) for r in c.execute(sa.text(sql), p).mappings()]


def enrollment(store):
    return {r["address"]: r for r in rows(store, "SELECT * FROM wallet_enrollment")}


def events(store, a):
    return [r["kind"] for r in rows(
        store, "SELECT kind FROM leaderboard_membership_events WHERE address = :a ORDER BY id",
        a=a)]


def snap(store, addrs, status="ok", complete=True):
    ts = [Trader(address=a, rank=i + 1, pnl=100.0 - i) for i, a in enumerate(addrs)]
    return store.record_snapshot(ts, source="agora", status=status, complete=complete,
                                 row_count=len(ts))


# --- membership vs eligibility ---------------------------------------------------------
def test_wallet_leaving_the_top_stays_scanned_and_is_marked_former(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    lb.set([A, B, C, D])
    t.refresh_leaderboard()
    lb.set([A, B, D, E_])           # C leaves, E enters
    t.refresh_leaderboard()
    assert t.tracked == {A, B, C, D, E_}
    en = enrollment(store)
    assert en[C]["in_top"] is False and en[C]["exits"] == 1 and en[C]["status"] == "active"
    assert en[E_]["in_top"] is True and en[E_]["entries"] == 1
    assert set(store.top_traders()) == {A, B, D, E_}      # "current top" is still separate
    assert store.enrolled_addresses() == {A, B, C, D, E_}


def test_reentry_is_preserved_in_events_and_counters(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    for lst in ([A, B, C, D], [A, B, D, E_], [A, B, C, D], [A, B, C, D]):
        lb.set(lst)
        t.refresh_leaderboard()
    en = enrollment(store)
    assert events(store, C) == ["ENTERED", "EXITED", "ENTERED"]
    assert (en[C]["entries"], en[C]["exits"], en[C]["appearances"], en[C]["in_top"]) == \
        (2, 1, 3, True)
    assert (en[E_]["entries"], en[E_]["exits"], en[E_]["in_top"]) == (1, 1, False)
    assert (en[A]["appearances"], en[A]["best_rank"], en[A]["entries"], en[A]["exits"]) == \
        (4, 1, 1, 0)
    assert t.tracked == {A, B, C, D, E_}                   # nobody was ever removed


def test_best_rank_first_and_last_seen(store):
    s1 = snap(store, [A, B, C, D])
    snap(store, [B, A, C, D])
    s3 = snap(store, [C, B, D, A])
    en = enrollment(store)
    assert en[A]["best_rank"] == 1 and en[A]["last_rank"] == 4
    assert en[C]["best_rank"] == 1 and en[C]["last_rank"] == 1
    assert (en[A]["first_snapshot_id"], en[A]["last_snapshot_id"]) == (s1, s3)
    assert en[A]["first_top_at"] <= en[A]["last_top_at"]


def test_new_top_wallet_is_enrolled_automatically(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    lb.set([A, B, C, D])
    t.refresh_leaderboard()
    assert F not in t.tracked
    lb.set([F, A, B, C])
    t.refresh_leaderboard()
    assert F in t.tracked and enrollment(store)[F]["first_snapshot_id"] == enrollment(store)[F]["last_snapshot_id"]


# --- bad snapshots must not corrupt the registry -----------------------------------------
def test_degraded_and_failed_snapshots_change_nothing(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    lb.set([A, B, C, D])
    t.refresh_leaderboard()
    before = enrollment(store)
    lb.set([E_])                     # less than half: recorded as degraded
    t.refresh_leaderboard()
    lb.error = requests.ConnectionError("boom")
    t.refresh_leaderboard()          # failed
    after = enrollment(store)
    assert set(after) == {A, B, C, D}                       # the stray wallet is not enrolled
    assert {k: (v["in_top"], v["appearances"], v["exits"]) for k, v in after.items()} == \
        {k: (v["in_top"], v["appearances"], v["exits"]) for k, v in before.items()}
    assert {A, B, C, D} <= t.tracked
    statuses = [r["status"] for r in rows(store, "SELECT status FROM leaderboard_snapshots ORDER BY id")]
    assert statuses == ["ok", "degraded", "failed"]


def test_partial_response_is_evidence_of_nothing(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch, top_n=4)
    lb.set([A, B, C, D])
    t.refresh_leaderboard()
    lb.set([A, B, F])                # 3 of 4 rows: status ok but not complete
    t.refresh_leaderboard()
    snaps = rows(store, "SELECT status, complete FROM leaderboard_snapshots ORDER BY id")
    assert snaps == [{"status": "ok", "complete": True}, {"status": "ok", "complete": False}]
    en = enrollment(store)
    assert F not in en                                      # not enrolled from a partial list
    assert all(v["in_top"] for v in en.values()) and en[C]["exits"] == 0   # nobody "left"
    assert {A, B, C, D} <= t.tracked


def test_scan_set_never_shrinks_when_the_registry_read_fails(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    lb.set([A, B, C, D])
    t.refresh_leaderboard()
    lb.set([A, B, D, E_])
    t.refresh_leaderboard()
    assert t.tracked == {A, B, C, D, E_}
    monkeypatch.setattr(store, "enrolled_addresses", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    lb.error = requests.ConnectionError("down")
    t.refresh_leaderboard()
    assert t.tracked == {A, B, C, D, E_}


# --- separation from manual / Telegram wallets -------------------------------------------
def test_manual_and_telegram_wallets_are_tracked_but_never_enrolled(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    t.watchlist = {MANUAL: "mine"}
    store.sync_manual_watchlist(t.watchlist)
    store.watch_add(TG)
    lb.set([A, B, C, D])
    t.refresh_leaderboard()
    assert {MANUAL, TG} <= t.tracked
    assert MANUAL not in enrollment(store) and TG not in enrollment(store)
    rep = store.tracking_report(scanned=t.tracked)
    assert rep["manual_tracked"] == 2 and rep["manual_only"] == 2 and rep["ever_in_top"] == 4
    store.watch_remove(TG)                                   # removal is honoured for manual...
    t.refresh_leaderboard()
    assert TG not in t.tracked
    lb.set([B, C, D, E_])                                    # ...but not for former top wallets
    t.refresh_leaderboard()
    assert A in t.tracked


def test_restart_rebuilds_the_same_universe(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    for lst in ([A, B, C, D], [B, C, D, E_]):
        lb.set(lst)
        t.refresh_leaderboard()
    t2, lb2 = make_tracker(store, monkeypatch)
    lb2.error = requests.ConnectionError("endpoint down at start-up")
    t2.prepare_registry()
    t2.refresh_leaderboard()
    assert t2.tracked == {A, B, C, D, E_}


def test_cumulative_can_be_switched_off_without_touching_the_database(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch, cumulative=False)
    for lst in ([A, B, C, D], [B, C, D, E_]):
        lb.set(lst)
        t.refresh_leaderboard()
    assert t.tracked == {B, C, D, E_}                        # old behaviour
    assert set(enrollment(store)) == {A, B, C, D, E_}        # evidence is still recorded


# --- registry maintenance ---------------------------------------------------------------
def _norm(en):
    return {a: {k: v for k, v in r.items() if k != "enrolled_at"} for a, r in en.items()}


def test_incremental_registry_equals_full_rebuild(store):
    lists = [[A, B, C, D], [A, B, D, E_], [B, A, E_, F], [A, C, D, E_], [F, A, B, C],
             [F, A, B, C], [D, E_, F, A]]
    for lst in lists:
        snap(store, lst)
    snap(store, [A], status="degraded", complete=False)        # ignored by both
    snap(store, [B, C, D], status="ok", complete=False)        # ignored by both
    inc = _norm(enrollment(store))
    inc_ev = rows(store, "SELECT address, snapshot_id, kind, rank FROM leaderboard_membership_events ORDER BY 1,2,3")
    with store.engine.begin() as c:
        c.execute(sa.text("TRUNCATE leaderboard_membership_events, wallet_enrollment"))
    with store.engine.begin() as c:
        out = tracking.backfill_enrollment(c, CHAIN)
    assert out["wallets_upserted"] == len(inc)
    assert _norm(enrollment(store)) == inc
    assert rows(store, "SELECT address, snapshot_id, kind, rank FROM leaderboard_membership_events ORDER BY 1,2,3") == inc_ev


def test_backfill_is_idempotent_and_keeps_policy_decisions(store):
    for lst in ([A, B, C, D], [A, B, D, E_]):
        snap(store, lst)
    store.put_is_contract(C, True)
    assert store.sync_exclusions(True) == (1, 0)
    with store.engine.begin() as c:
        tracking.backfill_enrollment(c, CHAIN)
        tracking.backfill_enrollment(c, CHAIN)
    en = enrollment(store)
    assert en[C]["status"] == "excluded" and en[C]["excluded_reason"] == "contract"
    n_events = rows(store, "SELECT count(*) AS n FROM leaderboard_membership_events")[0]["n"]
    with store.engine.begin() as c:
        tracking.backfill_enrollment(c, CHAIN)
    assert rows(store, "SELECT count(*) AS n FROM leaderboard_membership_events")[0]["n"] == n_events


def test_ensure_enrollment_rebuilds_a_registry_that_is_behind(store):
    for lst in ([A, B, C, D], [A, B, D, E_]):
        snap(store, lst)
    with store.engine.begin() as c:
        c.execute(sa.text("TRUNCATE leaderboard_membership_events, wallet_enrollment"))
    assert store.ensure_enrollment() is not None
    assert set(enrollment(store)) == {A, B, C, D, E_}
    assert store.ensure_enrollment() is None                   # up to date: no-op
    snap(store, [F, A, B, C])                                  # incremental continues cleanly
    assert enrollment(store)[F]["in_top"] and not enrollment(store)[E_]["in_top"]


def test_a_stale_registry_is_repaired_by_the_next_snapshot(store):
    snap(store, [A, B, C, D])
    with store.engine.begin() as c:
        c.execute(sa.text("TRUNCATE leaderboard_membership_events, wallet_enrollment"))
    snap(store, [A, B, D, E_])                                 # registry empty: full rebuild
    en = enrollment(store)
    assert set(en) == {A, B, C, D, E_} and en[C]["exits"] == 1 and en[C]["in_top"] is False


def test_contract_exclusion_is_recorded_and_reversible(store):
    snap(store, [A, B, C, D])
    store.put_is_contract(B, True)
    assert store.sync_exclusions(True) == (1, 0)
    assert store.enrolled_addresses() == {A, C, D}
    rep = store.tracking_report(scanned=set())
    assert rep["excluded_by_policy"] == 1 and rep["excluded_reasons"] == {"contract": 1}
    assert rep["ever_in_top"] == 4                              # still counted, never deleted
    store.put_is_contract(B, False)
    assert store.sync_exclusions(True) == (0, 1)
    assert store.enrolled_addresses() == {A, B, C, D}


# --- scan coverage ----------------------------------------------------------------------
def test_coverage_opens_at_the_cursor_and_records_gaps(store):
    assert store.sync_coverage({A, B}, 100) == (2, 0)
    assert store.sync_coverage({A, B}, 150) == (0, 0)           # same set: nothing to do
    assert store.sync_coverage({A}, 200) == (0, 1)              # B leaves at 200
    assert store.sync_coverage({A, B}, 300) == (1, 0)           # B returns: gap 201..300
    iv = rows(store, "SELECT from_block, to_block, basis FROM wallet_scan_coverage "
                     "WHERE address = :a ORDER BY from_block", a=B)
    assert iv == [{"from_block": 101, "to_block": 200, "basis": "live"},
                  {"from_block": 301, "to_block": None, "basis": "live"}]
    assert tracking.coverage_gaps([(101, 200), (301, None)]) == (1, 100)
    assert tracking.coverage_gaps([(101, None)]) == (0, 0)


def test_history_before_first_scan_is_flagged_incomplete(store):
    snap(store, [A, B, C, D])
    with store.engine.begin() as c:       # blocks every minute around the snapshot time
        c.execute(sa.text(
            "INSERT INTO indexed_blocks (chain_id, block_number, block_time) "
            "SELECT :c, g, now() - interval '2 hours' + g * interval '1 minute' "
            "FROM generate_series(1, 400) g"), {"c": CHAIN})
    store.sync_coverage({A}, 300)         # A first scanned at 301 (after its first appearance)
    store.sync_coverage({A, B}, 350)
    rep = store.tracking_report()
    assert rep["ever_in_top"] == 4 and rep["scanned_now"] == 2
    assert rep["enrolled_not_scanned_now"] == 2                 # C, D: enrolled, no coverage yet
    assert rep["history_incomplete"] == 4                       # nobody was scanned from the start


def test_legacy_coverage_is_derived_once_and_marked_approximate(store):
    snap(store, [A, B])
    with store.engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO indexed_blocks (chain_id, block_number, block_time) "
            "SELECT :c, g, now() - interval '100 minutes' + g * interval '1 minute' "
            "FROM generate_series(1, 100) g"), {"c": CHAIN})
        c.execute(sa.text("UPDATE wallet_watchlist SET added_at = now() - interval '80 minutes'"))
        c.execute(sa.text("UPDATE wallet_watchlist SET removed_at = now() - interval '40 minutes' "
                          "WHERE address = :a"), {"a": B})
        c.execute(sa.text("UPDATE wallet_watchlist SET status='inactive' WHERE address = :a"),
                  {"a": B})
        c.execute(sa.text("INSERT INTO indexer_checkpoints (chain_id, name, block_number) "
                          "VALUES (:c, 'indexer', 100)"), {"c": CHAIN})
    assert store.derive_legacy_coverage() == 2
    assert store.derive_legacy_coverage() == 0                  # once
    iv = {r["address"]: r for r in rows(
        store, "SELECT address, from_block, to_block, basis FROM wallet_scan_coverage")}
    assert iv[A]["to_block"] is None and iv[B]["to_block"] is not None
    assert {r["basis"] for r in iv.values()} == {"derived"}
    assert 15 <= iv[A]["from_block"] <= 25 and 55 <= iv[B]["to_block"] <= 65


def test_tracker_records_coverage_and_scan_cost(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    t.chain.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 1000 * E18)],
                   bal={W1: (10 * E18, 9 * E18 - FEE)})
    lb.set([W1, B, C, D])
    t.refresh_leaderboard()
    t.store.set_cursor(None, 5)
    t.scan_step()
    cov = rows(store, "SELECT address, from_block, to_block FROM wallet_scan_coverage ORDER BY 1")
    assert len(cov) == 4 and all(r["from_block"] == 6 and r["to_block"] is None for r in cov)
    stats = json.loads(store.get_kv("scan_stats"))
    assert stats["get_logs"] >= 2 and stats["http_requests"] >= 2
    assert stats["wallets_leaderboard"] == 4 and stats["lag_blocks"] == 0
    assert stats["over_capacity"] is False and stats["blocks"] == 35
    # a former wallet that is still scanned keeps one open interval; nothing duplicates
    lb.set([W1, B, C, F])
    t.refresh_leaderboard()
    t.chain.head = 60
    t.scan_step()
    assert len(rows(store, "SELECT 1 FROM wallet_scan_coverage WHERE to_block IS NULL")) == 5
    assert rows(store, "SELECT from_block FROM wallet_scan_coverage WHERE address = :a", a=F) \
        == [{"from_block": 41}]
    assert tracking.coverage_gaps([(r["from_block"], r["to_block"]) for r in rows(
        store, "SELECT from_block, to_block FROM wallet_scan_coverage WHERE address = :a", a=D)]) \
        == (0, 0)


def test_replaying_a_range_with_the_larger_set_does_not_duplicate_trades(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    t.chain.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 1000 * E18)],
                   bal={W1: (10 * E18, 9 * E18 - FEE)})
    lb.set([W1, B, C, D])
    t.refresh_leaderboard()
    lb.set([B, C, D, E_])                                       # W1 leaves, stays tracked
    t.refresh_leaderboard()
    assert W1 in t.tracked
    for _ in range(2):
        t.store.set_cursor(None, 5)
        t.scan_step()
    assert rows(store, "SELECT count(*) AS n FROM trades")[0]["n"] == 1


def test_capacity_warning_is_logged_once_and_never_drops_wallets(store, monkeypatch, caplog):
    t, lb = make_tracker(store, monkeypatch)
    t.cfg.tracking.capacity_warn = 3
    caplog.set_level(logging.WARNING)
    for lst in ([A, B, C, D], [B, C, D, E_]):
        lb.set(lst)
        t.refresh_leaderboard()
    assert sum("above the configured capacity" in r.message for r in caplog.records) == 1
    assert len(t.tracked) == 5


# --- public / private boundary ---------------------------------------------------------
@pytest.fixture
def dash(pg_engine, store):
    eng = sa.create_engine(role_url())
    yield eng
    eng.dispose()


def q(eng, sql, **p):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(sa.text(sql), p).mappings()]


def test_public_views_show_leaderboard_wallets_only(store, dash):
    store.sync_manual_watchlist({MANUAL: "private"})
    store.watch_add(TG)
    store.sync_coverage({A, MANUAL, TG}, 10)
    snap(store, [A, B, C, D])
    snap(store, [A, B, D, E_])
    pub = {r["address"]: r for r in q(dash, "SELECT * FROM public_api.wallet_enrollment")}
    assert set(pub) == {A, B, C, D, E_}
    assert MANUAL not in pub and TG not in pub
    assert pub[C]["in_top"] is False and pub[C]["exits"] == 1
    ev = q(dash, "SELECT DISTINCT address FROM public_api.leaderboard_membership_events")
    assert {r["address"] for r in ev} == {A, B, C, D, E_}
    assert "excluded_reason" not in pub[A]


def test_role_cannot_read_raw_registry_or_coverage(store, dash):
    snap(store, [A, B, C, D])
    store.sync_coverage({A, MANUAL}, 10)       # a private wallet has coverage rows
    for table in ("wallet_enrollment", "leaderboard_membership_events", "wallet_scan_coverage"):
        with pytest.raises(sa.exc.DBAPIError):
            q(dash, f"SELECT * FROM public.{table}")
    # migration 0009 exposes coverage through a view restricted to public wallets: it works, and
    # the private (manual) wallet's intervals are not in it
    seen = {r["address"] for r in q(dash, "SELECT address FROM public_api.wallet_scan_coverage")}
    assert A in seen and MANUAL not in seen


def test_scan_stats_is_visible_but_private_state_is_not(store, dash):
    store.set_kv("scan_stats", json.dumps({"get_logs": 2}))
    store.set_kv("tg_offset", "123")
    keys = {r["key"] for r in q(dash, "SELECT key FROM public_api.service_state")}
    assert "scan_stats" in keys and "tg_offset" not in keys
