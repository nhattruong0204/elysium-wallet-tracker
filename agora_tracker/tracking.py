"""Cumulative Top-100 tracking: registry, membership events, scan coverage, reconciliation.

Three facts that used to be one, and are not the same thing:

  leaderboard membership   Is the wallet in Agora's top list right now? (`in_top`, events)
  tracking eligibility     May the scanner follow it? `wallet_enrollment.status`: every wallet
                           with leaderboard evidence stays 'active' forever, unless an explicit
                           policy excludes it (contracts), which is recorded with a reason.
  scan coverage            In which blocks was it actually scanned? `wallet_scan_coverage`.
                           A wallet enrolled today was not scanned yesterday: that gap is
                           stored, never papered over.

Evidence rule: only an *accepted* list counts, i.e. status 'ok' or 'imported' AND `complete`
(the collector marks a response with fewer than `top_n` rows incomplete; a response that shrank
by half is 'degraded'; failures are 'failed'). Incidental senders, recipients or pools never
become enrolled: the only source is `leaderboard_snapshot_entries`.

All writes are idempotent. The incremental path (one accepted snapshot at a time) and the full
recompute (`backfill_enrollment`) are defined to give identical rows; a test compares them.
"""

from __future__ import annotations

import bisect
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from . import schema as t

log = logging.getLogger(__name__)

ACCEPTED = "s.status IN ('ok','imported') AND s.complete"
LEGACY_SAMPLE_ABOVE = 200_000       # indexed_blocks rows; above this, sample instead of loading all


# --- registry -------------------------------------------------------------------------
def latest_accepted(c: Connection, chain_id: int) -> tuple[int, datetime] | None:
    row = c.execute(sa.text(
        f"SELECT s.id, s.captured_at FROM leaderboard_snapshots s WHERE s.chain_id = :c "
        f"AND {ACCEPTED} ORDER BY s.captured_at DESC, s.id DESC LIMIT 1"), {"c": chain_id}).first()
    return None if row is None else (row[0], row[1])


def backfill_enrollment(c: Connection, chain_id: int) -> dict[str, int]:
    """Rebuild events and the evidence columns of `wallet_enrollment` from every accepted
    snapshot. Idempotent: running it twice changes nothing. `status`, `excluded_reason` and
    `enrolled_at` are never touched, so a policy decision survives a rebuild."""
    p = {"c": chain_id}
    c.execute(sa.text(f"""
        INSERT INTO wallets (chain_id, address)
        SELECT DISTINCT :c, e.address
        FROM leaderboard_snapshot_entries e JOIN leaderboard_snapshots s ON s.id = e.snapshot_id
        WHERE s.chain_id = :c AND {ACCEPTED}
        ON CONFLICT DO NOTHING"""), p)
    # ENTERED / EXITED against the previous accepted list (the first list: everyone entered)
    ev = c.execute(sa.text(f"""
        WITH ord AS (
            SELECT s.id, s.captured_at, lag(s.id) OVER (ORDER BY s.captured_at, s.id) AS prev
            FROM leaderboard_snapshots s WHERE s.chain_id = :c AND {ACCEPTED}),
        entered AS (
            SELECT o.id AS sid, e.address, e.rank, o.captured_at AS at, 'ENTERED' AS kind
            FROM ord o JOIN leaderboard_snapshot_entries e ON e.snapshot_id = o.id
            WHERE NOT EXISTS (SELECT 1 FROM leaderboard_snapshot_entries q
                              WHERE q.snapshot_id = o.prev AND q.address = e.address)),
        exited AS (
            SELECT o.id AS sid, q.address, q.rank, o.captured_at AS at, 'EXITED' AS kind
            FROM ord o JOIN leaderboard_snapshot_entries q ON q.snapshot_id = o.prev
            WHERE o.prev IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM leaderboard_snapshot_entries e
                              WHERE e.snapshot_id = o.id AND e.address = q.address))
        INSERT INTO leaderboard_membership_events (chain_id, address, snapshot_id, kind, rank, at)
        SELECT :c, address, sid, kind, rank, at FROM entered
        UNION ALL SELECT :c, address, sid, kind, rank, at FROM exited
        ON CONFLICT ON CONSTRAINT uq_membership_event DO NOTHING"""), p).rowcount
    n = c.execute(sa.text(f"""
        WITH acc AS (SELECT s.id, s.captured_at FROM leaderboard_snapshots s
                     WHERE s.chain_id = :c AND {ACCEPTED}),
        ent AS (SELECT e.address, e.rank, a.id AS sid, a.captured_at AS ts
                FROM leaderboard_snapshot_entries e JOIN acc a ON a.id = e.snapshot_id),
        fst AS (SELECT DISTINCT ON (address) address, sid, ts FROM ent
                  ORDER BY address, ts, sid),
        lst AS (SELECT DISTINCT ON (address) address, sid, ts, rank FROM ent
                 ORDER BY address, ts DESC, sid DESC),
        agg AS (SELECT address, min(rank) AS best, count(*) AS n FROM ent GROUP BY address),
        cnt AS (SELECT address,
                       count(*) FILTER (WHERE kind = 'ENTERED') AS entries,
                       count(*) FILTER (WHERE kind = 'EXITED') AS exits
                FROM leaderboard_membership_events WHERE chain_id = :c GROUP BY address),
        newest AS (SELECT id FROM acc ORDER BY captured_at DESC, id DESC LIMIT 1)
        INSERT INTO wallet_enrollment (chain_id, address, first_top_at, last_top_at,
            first_snapshot_id, last_snapshot_id, best_rank, last_rank, appearances,
            entries, exits, in_top)
        SELECT :c, f.address, f.ts, l.ts, f.sid, l.sid, a.best, l.rank, a.n,
               coalesce(k.entries, 0), coalesce(k.exits, 0), l.sid = (SELECT id FROM newest)
        FROM fst f JOIN lst l USING (address) JOIN agg a USING (address)
        LEFT JOIN cnt k USING (address)
        ON CONFLICT (chain_id, address) DO UPDATE SET
            first_top_at = EXCLUDED.first_top_at, last_top_at = EXCLUDED.last_top_at,
            first_snapshot_id = EXCLUDED.first_snapshot_id,
            last_snapshot_id = EXCLUDED.last_snapshot_id, best_rank = EXCLUDED.best_rank,
            last_rank = EXCLUDED.last_rank, appearances = EXCLUDED.appearances,
            entries = EXCLUDED.entries, exits = EXCLUDED.exits, in_top = EXCLUDED.in_top"""),
        p).rowcount
    return {"events_added": ev or 0, "wallets_upserted": n or 0}


def ensure_enrollment(c: Connection, chain_id: int) -> dict[str, int] | None:
    """Run the full rebuild only when the registry is behind the newest accepted snapshot
    (first start after the migration, or after a restore). Cheap no-op otherwise."""
    newest = latest_accepted(c, chain_id)
    if newest is None:
        return None
    have = c.execute(sa.text(
        "SELECT max(last_snapshot_id) FROM wallet_enrollment WHERE chain_id = :c"),
        {"c": chain_id}).scalar()
    if have == newest[0]:
        return None
    return backfill_enrollment(c, chain_id)


def enroll_snapshot(c: Connection, chain_id: int, sid: int, ranks: dict[str, int]) -> None:
    """Incremental path, called inside the transaction that stored accepted snapshot `sid`.
    Falls back to the full rebuild when the registry is behind the previous snapshot."""
    s = t.leaderboard_snapshots
    captured = c.execute(sa.select(s.c.captured_at).where(s.c.id == sid)).scalar_one()
    prev = c.execute(sa.text(
        f"SELECT s.id FROM leaderboard_snapshots s WHERE s.chain_id = :c AND {ACCEPTED} "
        "AND s.id <> :sid AND (s.captured_at, s.id) < (:ts, :sid) "
        "ORDER BY s.captured_at DESC, s.id DESC LIMIT 1"),
        {"c": chain_id, "sid": sid, "ts": captured}).scalar()
    if prev is not None:
        have = c.execute(sa.text(
            "SELECT max(last_snapshot_id) FROM wallet_enrollment WHERE chain_id = :c"),
            {"c": chain_id}).scalar()
        if have != prev:
            backfill_enrollment(c, chain_id)   # includes this snapshot: it is already stored
            return
    prev_ranks: dict[str, int] = {}
    if prev is not None:
        prev_ranks = {r[0]: r[1] for r in c.execute(sa.text(
            "SELECT address, rank FROM leaderboard_snapshot_entries WHERE snapshot_id = :p"),
            {"p": prev})}
    entered = sorted(set(ranks) - set(prev_ranks))
    exited = sorted(set(prev_ranks) - set(ranks))
    ev = t.leaderboard_membership_events
    rows = ([{"chain_id": chain_id, "address": a, "snapshot_id": sid, "kind": "ENTERED",
              "rank": ranks[a], "at": captured} for a in entered] +
            [{"chain_id": chain_id, "address": a, "snapshot_id": sid, "kind": "EXITED",
              "rank": prev_ranks[a], "at": captured} for a in exited])
    if rows:
        c.execute(pg_insert(ev).values(rows).on_conflict_do_nothing(constraint="uq_membership_event"))
    if ranks:
        e = t.wallet_enrollment
        was_out = set(entered)
        stmt = pg_insert(e).values([{
            "chain_id": chain_id, "address": a, "first_top_at": captured,
            "last_top_at": captured, "first_snapshot_id": sid, "last_snapshot_id": sid,
            "best_rank": r, "last_rank": r, "appearances": 1,
            "entries": 1 if a in was_out else 0, "exits": 0, "in_top": True}
            for a, r in sorted(ranks.items())])
        x = stmt.excluded
        c.execute(stmt.on_conflict_do_update(
            index_elements=["chain_id", "address"],
            set_={"last_top_at": x.last_top_at, "last_snapshot_id": x.last_snapshot_id,
                  "best_rank": sa.func.least(e.c.best_rank, x.best_rank),
                  "last_rank": x.last_rank, "appearances": e.c.appearances + 1,
                  "entries": e.c.entries + x.entries, "in_top": True},
            where=e.c.last_snapshot_id < x.last_snapshot_id))
    # everyone who was in the list and is not in this snapshot has left it
    c.execute(sa.text(
        "UPDATE wallet_enrollment SET in_top = false, exits = exits + 1 "
        "WHERE chain_id = :c AND in_top AND last_snapshot_id < :sid"),
        {"c": chain_id, "sid": sid})


def enrolled_addresses(c: Connection, chain_id: int) -> set[str]:
    e = t.wallet_enrollment
    return {r[0] for r in c.execute(sa.select(e.c.address).where(
        e.c.chain_id == chain_id, e.c.status == "active"))}


def sync_exclusions(c: Connection, chain_id: int, enabled: bool = True) -> tuple[int, int]:
    """Policy: contracts (pools, routers, vaults) are not wallets. They stay in the registry,
    marked 'excluded' with the reason, so the report counts them and nothing is silently
    dropped. Driven by the cached `wallets.is_contract` flag (set only from a definitive
    eth_getCode answer); a wallet that is no longer a contract, or the policy being switched
    off, reinstates it. Returns (excluded, reinstated)."""
    p = {"c": chain_id}
    ex = 0
    if enabled:
        ex = c.execute(sa.text(
            "UPDATE wallet_enrollment e SET status = 'excluded', excluded_reason = 'contract' "
            "FROM wallets w WHERE w.chain_id = e.chain_id AND w.address = e.address "
            "AND e.chain_id = :c AND e.status = 'active' AND w.is_contract IS TRUE"), p).rowcount
    back = c.execute(sa.text(
        "UPDATE wallet_enrollment e SET status = 'active', excluded_reason = NULL "
        "FROM wallets w WHERE w.chain_id = e.chain_id AND w.address = e.address "
        "AND e.chain_id = :c AND e.status = 'excluded' AND e.excluded_reason = 'contract' "
        + ("AND w.is_contract IS NOT TRUE" if enabled else "")), p).rowcount
    return ex, back


# --- scan coverage --------------------------------------------------------------------
def sync_coverage(c: Connection, chain_id: int, tracked: set[str], cursor: int) -> tuple[int, int]:
    """Open an interval at cursor+1 for wallets that join the scanned set and close it at
    `cursor` for wallets that leave it. Call it right before scanning with the same set."""
    cov = t.wallet_scan_coverage
    open_ = {r[0] for r in c.execute(sa.select(cov.c.address).where(
        cov.c.chain_id == chain_id, cov.c.to_block.is_(None)))}
    added, removed = sorted(tracked - open_), sorted(open_ - tracked)
    if added:
        c.execute(pg_insert(t.wallets).values(
            [{"chain_id": chain_id, "address": a} for a in added]).on_conflict_do_nothing())
        c.execute(sa.insert(cov), [{"chain_id": chain_id, "address": a,
                                    "from_block": cursor + 1, "basis": "live"} for a in added])
    if removed:
        c.execute(sa.update(cov).where(
            cov.c.chain_id == chain_id, cov.c.to_block.is_(None), cov.c.address.in_(removed))
            .values(to_block=cursor, closed_at=sa.func.now()))
    return len(added), len(removed)


def derive_legacy_coverage(c: Connection, chain_id: int) -> int:
    """One-time reconstruction for wallets scanned before coverage was recorded: every
    watchlist row (any source) is an interval [added_at, removed_at or open], mapped to the
    nearest indexed blocks. Approximate by nature (basis 'derived'); skipped when coverage
    already exists. Returns the number of intervals written."""
    cov = t.wallet_scan_coverage
    if c.execute(sa.select(sa.func.count()).select_from(cov).where(
            cov.c.chain_id == chain_id)).scalar():
        return 0
    n_blocks = c.execute(sa.text(
        "SELECT count(*) FROM indexed_blocks WHERE chain_id = :c AND block_time IS NOT NULL"),
        {"c": chain_id}).scalar() or 0
    if n_blocks == 0:
        return 0
    step = max(1, n_blocks // LEGACY_SAMPLE_ABOVE) if n_blocks > LEGACY_SAMPLE_ABOVE else 1
    anchors = c.execute(sa.text(
        "SELECT block_number, block_time FROM (SELECT block_number, block_time, "
        "row_number() OVER (ORDER BY block_number) AS rn FROM indexed_blocks "
        "WHERE chain_id = :c AND block_time IS NOT NULL) x WHERE (rn - 1) % :k = 0 "
        "ORDER BY block_number"), {"c": chain_id, "k": step}).all()
    blocks = [r[0] for r in anchors]
    times = [r[1] for r in anchors]
    head = c.execute(sa.text("SELECT block_number FROM indexer_checkpoints WHERE chain_id = :c "
                             "AND name = 'indexer'"), {"c": chain_id}).scalar()

    def at_or_after(ts: datetime) -> int:
        i = bisect.bisect_left(times, ts)
        return blocks[min(i, len(blocks) - 1)]

    def at_or_before(ts: datetime) -> int:
        i = bisect.bisect_right(times, ts) - 1
        return blocks[max(i, 0)]

    per: dict[str, list[tuple[int, int | None]]] = defaultdict(list)
    for addr, added, removed in c.execute(sa.text(
            "SELECT address, added_at, removed_at FROM wallet_watchlist WHERE chain_id = :c "
            "AND status <> 'excluded'"), {"c": chain_id}):
        lo = at_or_after(added)
        hi = None if removed is None else at_or_before(removed)
        if hi is not None and hi < lo:
            continue      # tracked between two indexed blocks: nothing observable
        per[addr].append((lo, hi))
    rows = []
    for addr, ivs in per.items():
        ivs.sort(key=lambda x: x[0])
        merged: list[list[int | None]] = []
        for lo, hi in ivs:
            if merged and (merged[-1][1] is None or lo <= merged[-1][1] + 1):
                if merged[-1][1] is not None:
                    merged[-1][1] = None if hi is None else max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        for lo, hi in merged:
            if hi is None and head is not None and lo > head:
                lo = head
            rows.append({"chain_id": chain_id, "address": addr, "from_block": lo,
                         "to_block": hi, "basis": "derived"})
    if rows:
        c.execute(sa.insert(cov), rows)
    return len(rows)


def coverage_gaps(intervals: list[tuple[int, int | None]]) -> tuple[int, int]:
    """(gap count, gap blocks) between consecutive intervals of one wallet. Blocks before the
    first interval are unknown, not a gap."""
    ivs = sorted(intervals, key=lambda x: x[0])
    n = blocks = 0
    for (_, hi), (lo, _) in zip(ivs, ivs[1:], strict=False):
        if hi is not None and lo > hi + 1:
            n += 1
            blocks += lo - hi - 1
    return n, blocks


def tracking_report(c: Connection, chain_id: int, scanned: set[str] | None = None,
                    manual: set[str] | None = None) -> dict[str, Any]:
    """Reconciliation counts only (no addresses). `scanned` is the set the scanner follows
    right now (None: use the open coverage intervals); `manual` the active manual/Telegram
    wallets (None: read the watchlist)."""
    p = {"c": chain_id}
    one = lambda q: c.execute(sa.text(q), p).scalar() or 0  # noqa: E731
    if manual is None:
        manual = {r[0] for r in c.execute(sa.text(
            "SELECT DISTINCT address FROM wallet_watchlist WHERE chain_id = :c "
            "AND removed_at IS NULL AND source IN ('manual','telegram','import')"), p)}
    enrolled = {r[0]: r[1] for r in c.execute(sa.text(
        "SELECT address, status FROM wallet_enrollment WHERE chain_id = :c"), p)}
    if scanned is None:
        scanned = {r[0] for r in c.execute(sa.text(
            "SELECT address FROM wallet_scan_coverage WHERE chain_id = :c AND to_block IS NULL"),
            p)}
    ivs: dict[str, list[tuple[int, int | None]]] = defaultdict(list)
    for a, lo, hi in c.execute(sa.text(
            "SELECT address, from_block, to_block FROM wallet_scan_coverage WHERE chain_id = :c"),
            p):
        ivs[a].append((lo, hi))
    first_top = {r[0]: r[1] for r in c.execute(sa.text(
        "SELECT address, first_top_at FROM wallet_enrollment WHERE chain_id = :c"), p)}
    # history incomplete: never scanned, scanned with gaps, or first scanned after their
    # first appearance in the list (the older trades are not in the database)
    first_scan = {a: min(lo for lo, _ in v) for a, v in ivs.items()}
    block_at = {r[0]: r[1] for r in c.execute(sa.text(
        "SELECT address, (SELECT min(block_number) FROM indexed_blocks b WHERE b.chain_id = :c "
        "AND b.block_time >= e.first_top_at) FROM wallet_enrollment e WHERE e.chain_id = :c"), p)}
    incomplete = 0
    for a, st in enrolled.items():
        if st != "active":
            continue
        if a not in ivs:
            incomplete += 1
            continue
        gaps, _ = coverage_gaps(ivs[a])
        fb = block_at.get(a)
        if gaps or (fb is not None and first_scan[a] > fb):
            incomplete += 1
    active = {a for a, st in enrolled.items() if st == "active"}
    return {
        "snapshots_accepted": one(f"SELECT count(*) FROM leaderboard_snapshots s WHERE "
                                  f"s.chain_id = :c AND {ACCEPTED}"),
        "snapshots_not_accepted": one(f"SELECT count(*) FROM leaderboard_snapshots s WHERE "
                                      f"s.chain_id = :c AND NOT ({ACCEPTED})"),
        "ever_in_top": len(enrolled),
        "current_top": one("SELECT count(*) FROM wallet_enrollment WHERE chain_id = :c AND in_top"),
        "former_top": one("SELECT count(*) FROM wallet_enrollment WHERE chain_id = :c "
                          "AND NOT in_top"),
        "manual_tracked": len(manual),
        "manual_only": len(manual - set(enrolled)),
        "scanned_now": len(scanned),
        "enrolled_not_scanned_now": len(active - scanned),
        "excluded_by_policy": sum(1 for s in enrolled.values() if s == "excluded"),
        "excluded_reasons": {r[0]: r[1] for r in c.execute(sa.text(
            "SELECT excluded_reason, count(*) FROM wallet_enrollment WHERE chain_id = :c "
            "AND status = 'excluded' GROUP BY 1"), p)},
        "history_incomplete": incomplete,
        "re_entered": one("SELECT count(*) FROM wallet_enrollment WHERE chain_id = :c "
                          "AND entries > 1"),
        "first_top_range": [str(min(first_top.values())) if first_top else None,
                            str(max(first_top.values())) if first_top else None],
    }
