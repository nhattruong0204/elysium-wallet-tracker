"""One-time import of a v0.1 / v0.2 SQLite database into PostgreSQL.

Guarantees
  * The source file is opened read-only and a byte-for-byte backup copy is written next to
    it first (``<file>.bak-YYYYmmddTHHMMSSZ``) — also in dry-run mode.
  * Everything is imported in ONE PostgreSQL transaction. Validation runs inside that
    transaction; on any mismatch, or with --dry-run, it is rolled back and nothing changes.
  * Validation is row-by-row and exact: every source row is looked up in PostgreSQL and its
    quantities compared as integers (no float, no rounding). Aggregate sums are cross-checked
    too.
  * Alerts already delivered keep status 'sent' and are never re-sent; pending ones stay
    pending; dead stay dead.
  * Re-running is safe: rows already present are counted as such, not duplicated.
  * Rows the v0.1 bug T2 stored twice for the same (tx, wallet, token) leg are reported;
    the first one (by insertion order) is kept, which is what positions were built from.

Rollback: the import commits atomically, so a failed import leaves PostgreSQL untouched.
After a successful import, see docs/OPERATIONS.md ("Rolling back the PostgreSQL migration").
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import schema as t
from .config import Config
from .db import INDEXER_CHECKPOINT, Store, utc
from .migrate import current_revision, head_revision

log = logging.getLogger(__name__)


class ImportError_(RuntimeError):
    pass


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _usd(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.000001"))


def backup_sqlite(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)  # consistent even if the WAL has uncheckpointed pages
        dst.close()
    finally:
        src.close()
    for suffix in ("-wal", "-shm"):  # nothing to copy after backup(); keep the dir tidy
        Path(f"{dest}{suffix}").unlink(missing_ok=True)
    return dest


def read_source(path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = _tables(con)
        required = {"kv", "traders", "events", "positions", "tokens", "contracts", "outbox"}
        missing = required - tables
        if missing:
            raise ImportError_(f"{path} is not a tracker database (missing {sorted(missing)})")
        ev_cols, pos_cols, ob_cols = _cols(con, "events"), _cols(con, "positions"), \
            _cols(con, "outbox")
        src: dict[str, Any] = {
            "version": "v0.2" if "confidence" in ev_cols else "v0.1",
            "kv": {r["k"]: r["v"] for r in con.execute("SELECT k, v FROM kv")},
            "traders": [dict(r) for r in con.execute("SELECT * FROM traders ORDER BY address")],
            "events": [dict(r) for r in con.execute("SELECT rowid AS _rowid, * FROM events "
                                                    "ORDER BY rowid")],
            "positions": [dict(r) for r in con.execute("SELECT * FROM positions")],
            "tokens": [dict(r) for r in con.execute("SELECT * FROM tokens")],
            "contracts": [dict(r) for r in con.execute("SELECT * FROM contracts")],
            "outbox": [dict(r) for r in con.execute("SELECT * FROM outbox ORDER BY id")],
            "cluster_alerts": [dict(r) for r in con.execute("SELECT * FROM cluster_alerts")]
            if "cluster_alerts" in tables else [],
        }
        src["has"] = {"confidence": "confidence" in ev_cols,
                      "hype_method": "hype_method" in ev_cols,
                      "basis": "basis" in pos_cols,
                      "realized_est": "realized_est_wei" in pos_cols,
                      "outbox_status": "status" in ob_cols,
                      "last_error": "last_error" in ob_cols}
        return src
    finally:
        con.close()


def _outbox_status(r: dict, has_status: bool) -> str:
    # A recorded delivery always wins: never let an import make a delivered alert resend,
    # whatever a status column (possibly defaulted to 'pending') says.
    if r.get("sent_at") is not None:
        return "sent"
    if has_status and r.get("status") in ("pending", "dead"):
        return r["status"]
    return "dead" if (r.get("attempts") or 0) >= 10 else "pending"  # v0.1 gave up at 10


def _event_confidence(r: dict, has: dict) -> tuple[str, str]:
    conf = r.get("confidence") if has["confidence"] else None
    if conf not in ("high", "estimated", "unknown", "legacy"):
        conf = "legacy"
    if conf == "legacy" and r.get("approx"):
        conf = "estimated"  # v0.1 already knew this one was approximate
    method = r.get("hype_method") if has["hype_method"] else None
    return conf, method or "legacy"


def import_into(conn: sa.Connection, store: Store, src: dict[str, Any]) -> dict[str, Any]:
    """Insert everything (idempotently) on `conn`. Caller owns the transaction."""
    chain = store.chain_id
    rep: dict[str, Any] = {"tables": {}, "duplicate_legs": [], "warnings": []}

    def existing(table: sa.Table, col: sa.Column, keys: list) -> set:
        if not keys:
            return set()
        extra = [table.c.chain_id == chain] if "chain_id" in table.c else []
        return {r[0] for r in conn.execute(sa.select(col).where(col.in_(keys), *extra))}

    def note(name: str, source: int, present: int) -> None:
        rep["tables"][name] = {"source_rows": source, "already_present": present,
                               "inserted": source - present}

    # tokens
    toks = [{"chain_id": chain, "address": r["address"].lower(), "symbol": r["symbol"] or "",
             "decimals": int(r["decimals"]), "source": "import"} for r in src["tokens"]]
    pre = existing(t.token_metadata, t.token_metadata.c.address, [x["address"] for x in toks])
    if toks:
        conn.execute(pg_insert(t.token_metadata).values(toks).on_conflict_do_nothing())
    note("tokens", len(toks), len(pre))

    # wallets: contract cache + every trader ever seen + every event wallet
    contract = {r["address"].lower(): bool(r["is_contract"]) for r in src["contracts"]}
    addrs = set(contract) | {r["address"].lower() for r in src["traders"]} | \
        {r["wallet"].lower() for r in src["events"]}
    pre = existing(t.wallets, t.wallets.c.address, sorted(addrs))
    if addrs:
        conn.execute(pg_insert(t.wallets).values(
            [{"chain_id": chain, "address": a, "is_contract": contract.get(a)}
             for a in sorted(addrs)]).on_conflict_do_nothing())
    note("wallets", len(addrs), len(pre))

    # leaderboard: one 'imported' snapshot of the last known top set + watchlist history
    top = [r for r in src["traders"] if r.get("in_top")]
    snap_key = f"sqlite-import:{len(top)}:{max((r['updated_at'] for r in top), default=0)}"
    already = conn.execute(sa.select(t.leaderboard_snapshots.c.id).where(
        t.leaderboard_snapshots.c.chain_id == chain,
        t.leaderboard_snapshots.c.source == "sqlite-import",
        t.leaderboard_snapshots.c.request_meta["key"].astext == snap_key)).scalar()
    if top and already is None:
        sid = conn.execute(sa.insert(t.leaderboard_snapshots).values(
            chain_id=chain, source="sqlite-import", status="imported", complete=True,
            row_count=len(top), accepted_count=len(top),
            captured_at=utc(max(r["updated_at"] for r in top)),
            request_meta={"key": snap_key, "note": "last top set from SQLite"})
            .returning(t.leaderboard_snapshots.c.id)).scalar_one()
        conn.execute(sa.insert(t.leaderboard_snapshot_entries), [
            {"snapshot_id": sid, "address": r["address"].lower(), "rank": r["rank"] or 0,
             "pnl_usd": _usd(r["pnl"]), "worth_usd": _usd(r["worth"]),
             "volume_usd": _usd(r["volume"]), "trades": r["trades"]} for r in top])
        w = t.wallet_watchlist
        current = {r[0] for r in conn.execute(sa.select(w.c.address).where(
            w.c.chain_id == chain, w.c.source == "leaderboard", w.c.removed_at.is_(None)))}
        rows = []
        for r in src["traders"]:
            a = r["address"].lower()
            if r.get("in_top") and a not in current:
                rows.append({"chain_id": chain, "address": a, "source": "leaderboard",
                             "status": "active", "removed_at": None,
                             "reason": "imported from SQLite"})
            elif not r.get("in_top"):
                rows.append({"chain_id": chain, "address": a, "source": "import",
                             "status": "inactive", "removed_at": utc(r["updated_at"]),
                             "reason": "previously in top (imported from SQLite)"})
        if rows:
            conn.execute(sa.insert(w), rows)
    note("leaderboard_top", len(top), len(top) if already is not None else 0)

    # transactions (stubs) + trades
    seen_leg: dict[tuple, str] = {}
    trades_rows = []
    for r in src["events"]:
        leg = (r["tx"].lower(), r["wallet"].lower(), r["token"].lower())
        if leg in seen_leg:
            rep["duplicate_legs"].append({"kept": seen_leg[leg], "skipped": r["key"],
                                          "kind": r["kind"]})
            continue
        seen_leg[leg] = r["key"]
        conf, method = _event_confidence(r, src["has"])
        trades_rows.append({
            "chain_id": chain, "tx_hash": leg[0], "wallet": leg[1], "token": leg[2],
            "kind": r["kind"], "confidence": conf, "hype_method": method,
            "qty": int(r["qty"]), "hype_wei": None if r["hype_wei"] is None else int(r["hype_wei"]),
            "counter_token": (r.get("counter_token") or None) and r["counter_token"].lower(),
            "counter_qty": None if r.get("counter_qty") is None else int(r["counter_qty"]),
            "block_number": r["block"], "block_time": utc(r["ts"]), "legacy_key": r["key"]})
    pre = existing(t.trades, t.trades.c.legacy_key, [x["legacy_key"] for x in trades_rows])
    if trades_rows:
        stubs = {(x["tx_hash"], x["block_number"]) for x in trades_rows}
        conn.execute(pg_insert(t.transactions).values(
            [{"chain_id": chain, "tx_hash": h, "block_number": b} for h, b in sorted(stubs)])
            .on_conflict_do_nothing())
        for i in range(0, len(trades_rows), 1000):
            conn.execute(pg_insert(t.trades).values(trades_rows[i:i + 1000])
                         .on_conflict_do_nothing())
    note("trades", len(trades_rows), len(pre))
    if rep["duplicate_legs"]:
        rep["warnings"].append(f"{len(rep['duplicate_legs'])} duplicate legs from the v0.1 "
                               "replay bug collapsed (first kept); see duplicate_legs")

    # positions
    has = src["has"]
    pos_rows = [{"chain_id": chain, "wallet": r["wallet"].lower(), "token": r["token"].lower(),
                 "qty": int(r["qty"]), "cost_wei": int(r["cost_wei"]),
                 "realized_wei": int(r["realized_wei"]),
                 "realized_est_wei": int(r["realized_est_wei"]) if has["realized_est"] else 0,
                 "basis": r["basis"] if has["basis"] else "legacy",
                 "opened_at": utc(r["opened_at"])} for r in src["positions"]]
    p = t.wallet_positions
    pre_pos = {(r[0], r[1]) for r in conn.execute(sa.select(p.c.wallet, p.c.token).where(
        p.c.chain_id == chain))}
    if pos_rows:
        conn.execute(pg_insert(p).values(pos_rows).on_conflict_do_nothing())
    note("positions", len(pos_rows),
         sum(1 for x in pos_rows if (x["wallet"], x["token"]) in pre_pos))

    # cluster alert history -> signals
    sig_rows = [{"chain_id": chain, "kind": "cluster_buy_legacy", "token": r["token"].lower(),
                 "dedup_key": f"legacy-cluster:{r['token'].lower()}:{r['ts']}",
                 "window_end": utc(r["ts"]), "details": {"imported": True}}
                for r in src["cluster_alerts"]]
    pre = existing(t.signals, t.signals.c.dedup_key, [x["dedup_key"] for x in sig_rows])
    if sig_rows:
        conn.execute(pg_insert(t.signals).values(sig_rows).on_conflict_do_nothing())
    note("signals", len(sig_rows), len(pre))

    # outbox
    ob_rows = [{"dedup_key": r["key"], "text": r["text"],
                "status": _outbox_status(r, has["outbox_status"]),
                "attempts": r.get("attempts") or 0, "created_at": utc(r["created_at"]),
                "sent_at": utc(r.get("sent_at")),
                "last_error": r.get("last_error") if has["last_error"] else None}
               for r in src["outbox"]]
    pre = existing(t.notification_outbox, t.notification_outbox.c.dedup_key,
                   [x["dedup_key"] for x in ob_rows])
    if ob_rows:
        conn.execute(pg_insert(t.notification_outbox).values(ob_rows).on_conflict_do_nothing())
    note("outbox", len(ob_rows), len(pre))

    # checkpoint
    cur = src["kv"].get("cursor")
    target = conn.execute(sa.select(t.indexer_checkpoints.c.block_number).where(
        t.indexer_checkpoints.c.chain_id == chain,
        t.indexer_checkpoints.c.name == INDEXER_CHECKPOINT)).scalar()
    if cur is not None:
        if target is None:
            conn.execute(sa.insert(t.indexer_checkpoints).values(
                chain_id=chain, name=INDEXER_CHECKPOINT, block_number=int(cur)))
        elif target != int(cur):
            rep["warnings"].append(f"checkpoint not changed: PostgreSQL already at {target}, "
                                   f"SQLite at {cur}")
    rep["checkpoint"] = {"sqlite": None if cur is None else int(cur),
                         "postgres_before": target}
    return rep


def validate(conn: sa.Connection, store: Store, src: dict[str, Any],
             rep: dict[str, Any]) -> list[str]:
    """Row-by-row exact comparison of every source row against PostgreSQL."""
    chain = store.chain_id
    errors: list[str] = []
    skipped = {d["skipped"] for d in rep["duplicate_legs"]}

    tr = t.trades
    all_trades = list(conn.execute(sa.select(tr).where(tr.c.chain_id == chain)).mappings())
    got = {r["legacy_key"]: r for r in all_trades if r["legacy_key"]}
    by_leg = {(r["tx_hash"], r["wallet"], r["token"]): r for r in all_trades}
    sums_src: Counter = Counter()
    for r in src["events"]:
        if r["key"] in skipped:
            continue
        g = got.get(r["key"])
        if g is None:
            live = by_leg.get((r["tx"].lower(), r["wallet"].lower(), r["token"].lower()))
            if live is None:
                errors.append(f"trade {r['key']} missing")
            else:  # already indexed live by the PostgreSQL tracker: keep that row
                rep.setdefault("present_from_live_indexing", []).append(r["key"])
            continue
        sums_src[("qty", r["kind"])] += int(r["qty"])
        sums_src[("hype", r["kind"])] += int(r["hype_wei"] or 0)
        for col, want in (("kind", r["kind"]), ("qty", int(r["qty"])),
                          ("hype_wei", None if r["hype_wei"] is None else int(r["hype_wei"])),
                          ("block_number", r["block"]), ("wallet", r["wallet"].lower()),
                          ("token", r["token"].lower())):
            have = g[col]
            have = int(have) if isinstance(have, Decimal) else have
            if have != want:
                errors.append(f"trade {r['key']}: {col} {have!r} != {want!r}")
    sums_pg: Counter = Counter()
    for kind, q, h in conn.execute(sa.select(
            tr.c.kind, sa.func.sum(tr.c.qty), sa.func.coalesce(sa.func.sum(tr.c.hype_wei), 0))
            .where(tr.c.chain_id == chain, tr.c.legacy_key.isnot(None)).group_by(tr.c.kind)):
        sums_pg[("qty", kind)], sums_pg[("hype", kind)] = int(q), int(h)
    for k in set(sums_src) | set(sums_pg):
        if sums_src[k] != sums_pg[k] and not rep["tables"]["trades"]["already_present"] \
                and not rep.get("present_from_live_indexing"):
            errors.append(f"aggregate {k}: sqlite {sums_src[k]} != postgres {sums_pg[k]}")
    rep["sums"] = {f"{a}:{b}": str(v) for (a, b), v in sorted(sums_src.items())}

    p = t.wallet_positions
    pos = {(r["wallet"], r["token"]): r for r in conn.execute(
        sa.select(p).where(p.c.chain_id == chain)).mappings()}
    for r in src["positions"]:
        g = pos.get((r["wallet"].lower(), r["token"].lower()))
        if g is None:
            errors.append(f"position {r['wallet']}/{r['token']} missing")
            continue
        for col in ("qty", "cost_wei", "realized_wei"):
            if int(g[col]) != int(r[col]):
                errors.append(f"position {r['wallet']}/{r['token']}: {col} "
                              f"{int(g[col])} != {int(r[col])}")

    m = t.token_metadata
    toks = {r.address: (r.symbol, r.decimals) for r in conn.execute(
        sa.select(m.c.address, m.c.symbol, m.c.decimals).where(m.c.chain_id == chain))}
    for r in src["tokens"]:
        if toks.get(r["address"].lower()) != (r["symbol"] or "", int(r["decimals"])):
            errors.append(f"token {r['address']} differs")

    o = t.notification_outbox
    ob = {r["dedup_key"]: r for r in conn.execute(sa.select(o)).mappings()}
    for r in src["outbox"]:
        g = ob.get(r["key"])
        if g is None:
            errors.append(f"alert {r['key']} missing")
        elif _outbox_status(r, src["has"]["outbox_status"]) == "sent" and g["status"] != "sent":
            errors.append(f"alert {r['key']} was delivered but is {g['status']} (would resend)")
        elif g["text"] != r["text"]:
            errors.append(f"alert {r['key']} text differs")

    cur = src["kv"].get("cursor")
    if cur is not None and rep["checkpoint"]["postgres_before"] is None:
        have = store_cursor(conn, chain)
        if have != int(cur):
            errors.append(f"checkpoint {have} != {cur}")
    return errors


def store_cursor(conn: sa.Connection, chain: int) -> int | None:
    c = t.indexer_checkpoints
    return conn.execute(sa.select(c.c.block_number).where(
        c.c.chain_id == chain, c.c.name == INDEXER_CHECKPOINT)).scalar()


def run_import(path_str: str, cfg: Config, *, dry_run: bool = False,
               report_path: str | None = None, store: Store | None = None) -> int:
    path = Path(path_str)
    if not path.exists():
        log.error("no such file: %s", path)
        return 2
    store = store or Store(chain_id=cfg.chain.chain_id)
    rev, head = current_revision(store.engine), head_revision()
    if rev != head:
        log.error("database schema at %s, expected %s: run --migrate first", rev, head)
        return 2

    backup = backup_sqlite(path)
    log.info("backup of %s written to %s", path, backup)
    started = time.time()
    src = read_source(path)
    report: dict[str, Any] = {"source": str(path), "source_version": src["version"],
                              "backup": str(backup), "dry_run": dry_run,
                              "chain_id": store.chain_id}
    with store.engine.connect() as conn:
        trans = conn.begin()
        try:
            report.update(import_into(conn, store, src))
            errors = validate(conn, store, src, report)
        except Exception:
            trans.rollback()
            raise
        report["errors"] = errors
        report["result"] = "failed" if errors else ("dry-run ok" if dry_run else "ok")
        if errors or dry_run:
            trans.rollback()
        else:
            trans.commit()
    report["seconds"] = round(time.time() - started, 2)

    _print_report(report)
    if report_path:
        Path(report_path).write_text(json.dumps(report, indent=2, default=str))
    return 1 if report["errors"] else 0


def _print_report(rep: dict[str, Any]) -> None:
    lines = [f"SQLite import ({rep['source_version']}) -> PostgreSQL chain {rep['chain_id']}: "
             f"{rep['result'].upper()}" + (" (rolled back)" if rep["dry_run"] else ""),
             f"  backup: {rep['backup']}",
             f"  {'table':<16}{'source':>8}{'present':>9}{'inserted':>10}"]
    for name, c in rep.get("tables", {}).items():
        lines.append(f"  {name:<16}{c['source_rows']:>8}{c['already_present']:>9}"
                     f"{c['inserted']:>10}")
    cp = rep.get("checkpoint", {})
    lines.append(f"  checkpoint: sqlite={cp.get('sqlite')} postgres_before="
                 f"{cp.get('postgres_before')}")
    for w in rep.get("warnings", []):
        lines.append(f"  WARNING: {w}")
    by: defaultdict[str, int] = defaultdict(int)
    for e in rep.get("errors", []):
        by[e.split(" ")[0]] += 1
    for e in rep.get("errors", [])[:20]:
        lines.append(f"  ERROR: {e}")
    if len(rep.get("errors", [])) > 20:
        lines.append(f"  ... {len(rep['errors']) - 20} more errors ({dict(by)})")
    print("\n".join(lines))
