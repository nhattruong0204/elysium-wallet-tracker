"""PostgreSQL storage (SQLAlchemy Core + psycopg 3).

Transaction model
  * `Store.tx()` yields one connection inside one transaction. Everything derived from a
    block range (evidence, trades, positions, PnL, signals, alerts, checkpoint) is written
    through that connection, so it all commits or all rolls back. The checkpoint can never
    move past data that was not committed.
  * Methods taking a `c` argument must be called inside `tx()`. Methods without it run in
    their own short transaction (caches, heartbeat, snapshots).
Idempotency
  * trades are unique per (chain_id, tx_hash, wallet, token); evidence and alerts are
    upserted with ON CONFLICT DO NOTHING, so replaying any range is a no-op.
Concurrency
  * One active indexer per chain, enforced with a PostgreSQL session advisory lock.
  * The notifier claims outbox rows with FOR UPDATE SKIP LOCKED, so several notifier
    processes never send the same alert concurrently.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from . import schema as t
from .dburl import redact_db_url, resolve_database_url

log = logging.getLogger(__name__)

BASIS_KNOWN, BASIS_ESTIMATED, BASIS_PARTIAL, BASIS_LEGACY = (
    "known", "estimated", "partial_unknown", "legacy")
INDEXER_CHECKPOINT = "indexer"
_LOCK_CLASS = 0x454C5953  # "ELYS"


def utc(ts: int | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)


def epoch(dt: datetime | None) -> int | None:
    return None if dt is None else int(dt.timestamp())


def _int(v: Decimal | int | None) -> int | None:
    if v is None:
        return None
    if isinstance(v, Decimal) and v != v.to_integral_value():
        raise ValueError(f"non-integer on-chain quantity {v}")
    return int(v)


@dataclass
class Position:
    qty: int = 0
    cost_wei: int = 0          # cost of the part of `qty` whose cost is known/estimated
    realized_wei: int = 0      # PnL from sells with measured proceeds and known basis
    opened_at: int | None = None
    basis: str = BASIS_KNOWN   # known | estimated | partial_unknown | legacy
    realized_est_wei: int = 0  # PnL that relied on an estimate somewhere


def make_engine(url: str | None = None, *, pool_size: int = 5) -> Engine:
    url = resolve_database_url(url)
    eng = sa.create_engine(
        url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True,
        pool_recycle=1800, pool_timeout=30,
        connect_args={"application_name": "elysium-wallet-tracker", "connect_timeout": 10})
    log.debug("database engine for %s", redact_db_url(url))
    return eng


class Store:
    def __init__(self, url_or_engine: str | Engine | None = None, *, chain_id: int,
                 chain_name: str = "Elysium"):
        self.engine = (url_or_engine if isinstance(url_or_engine, Engine)
                       else make_engine(url_or_engine))
        self.chain_id = chain_id
        self._lock_conn: Connection | None = None
        with self.engine.begin() as c:
            c.execute(pg_insert(t.chains).values(chain_id=chain_id, name=chain_name)
                      .on_conflict_do_nothing())

    def dispose(self) -> None:
        self.release_indexer_lock()
        self.engine.dispose()

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        with self.engine.begin() as c:
            yield c

    # --- coordination -----------------------------------------------------------------
    def acquire_indexer_lock(self) -> bool:
        """Session-level advisory lock held on a dedicated connection for the process
        lifetime. Returns False if another indexer for this chain holds it."""
        if self._lock_conn is not None:
            return True
        conn = self.engine.connect()
        got = conn.execute(sa.text("SELECT pg_try_advisory_lock(:a, :b)"),
                           {"a": _LOCK_CLASS, "b": self.chain_id % 2**31}).scalar()
        conn.commit()
        if got:
            self._lock_conn = conn
            return True
        conn.close()
        return False

    def indexer_lock_alive(self) -> bool:
        if self._lock_conn is None:
            return False
        try:
            self._lock_conn.execute(sa.text("SELECT 1"))
            self._lock_conn.commit()
            return True
        except sa.exc.DBAPIError:
            # Connection lost => PostgreSQL released the lock; another indexer may own it.
            self._lock_conn = None
            return False

    def release_indexer_lock(self) -> None:
        if self._lock_conn is not None:
            try:
                self._lock_conn.execute(sa.text("SELECT pg_advisory_unlock(:a, :b)"),
                                        {"a": _LOCK_CLASS, "b": self.chain_id % 2**31})
                self._lock_conn.commit()
                self._lock_conn.close()
            except sa.exc.DBAPIError:
                pass
            self._lock_conn = None

    # --- service state (heartbeat etc.) -----------------------------------------------
    def get_kv(self, k: str) -> str | None:
        with self.engine.connect() as c:
            v = c.execute(sa.select(t.service_state.c.value)
                          .where(t.service_state.c.key == k)).scalar()
        return None if v is None else str(v.get("v"))

    def set_kv(self, k: str, v: str) -> None:
        with self.engine.begin() as c:
            stmt = pg_insert(t.service_state).values(key=k, value={"v": v})
            c.execute(stmt.on_conflict_do_update(
                index_elements=["key"], set_={"value": stmt.excluded.value,
                                              "updated_at": sa.func.now()}))

    # --- checkpoint -------------------------------------------------------------------
    def get_cursor(self, name: str = INDEXER_CHECKPOINT) -> int | None:
        with self.engine.connect() as c:
            return c.execute(sa.select(t.indexer_checkpoints.c.block_number).where(
                t.indexer_checkpoints.c.chain_id == self.chain_id,
                t.indexer_checkpoints.c.name == name)).scalar()

    def set_cursor(self, c: Connection | None, block: int, block_hash: str | None = None,
                   name: str = INDEXER_CHECKPOINT) -> None:
        stmt = pg_insert(t.indexer_checkpoints).values(
            chain_id=self.chain_id, name=name, block_number=block, block_hash=block_hash)
        stmt = stmt.on_conflict_do_update(
            index_elements=["chain_id", "name"],
            set_={"block_number": stmt.excluded.block_number,
                  "block_hash": stmt.excluded.block_hash, "updated_at": sa.func.now()})
        if c is None:
            with self.engine.begin() as cc:
                cc.execute(stmt)
        else:
            c.execute(stmt)

    def record_blocks(self, c: Connection, block_times: dict[int, int],
                      hashes: dict[int, tuple[str | None, str | None]] | None = None) -> None:
        hashes = hashes or {}
        rows = [{"chain_id": self.chain_id, "block_number": b, "block_time": utc(ts),
                 "block_hash": hashes.get(b, (None, None))[0],
                 "parent_hash": hashes.get(b, (None, None))[1]}
                for b, ts in block_times.items()]
        if rows:
            c.execute(pg_insert(t.indexed_blocks).values(rows).on_conflict_do_nothing())

    # --- wallets & leaderboard --------------------------------------------------------
    def _ensure_wallets(self, c: Connection, addrs: Iterable[str]) -> None:
        rows = [{"chain_id": self.chain_id, "address": a} for a in sorted(set(addrs))]
        if rows:
            c.execute(pg_insert(t.wallets).values(rows).on_conflict_do_nothing())

    def get_is_contract(self, address: str) -> bool | None:
        with self.engine.connect() as c:
            return c.execute(sa.select(t.wallets.c.is_contract).where(
                t.wallets.c.chain_id == self.chain_id, t.wallets.c.address == address)).scalar()

    def put_is_contract(self, address: str, value: bool) -> None:
        with self.engine.begin() as c:
            stmt = pg_insert(t.wallets).values(chain_id=self.chain_id, address=address,
                                               is_contract=value)
            c.execute(stmt.on_conflict_do_update(index_elements=["chain_id", "address"],
                                                 set_={"is_contract": value}))

    def _latest_snapshot_id(self, c: Connection) -> int | None:
        s = t.leaderboard_snapshots
        return c.execute(sa.select(s.c.id).where(
            s.c.chain_id == self.chain_id, s.c.status.in_(("ok", "imported")))
            .order_by(s.c.captured_at.desc(), s.c.id.desc()).limit(1)).scalar()

    def top_traders(self) -> dict[str, dict[str, Any]]:
        """Entries of the latest accepted snapshot: {address: {rank, pnl, ...}}."""
        e = t.leaderboard_snapshot_entries
        with self.engine.connect() as c:
            sid = self._latest_snapshot_id(c)
            if sid is None:
                return {}
            rows = c.execute(sa.select(e).where(e.c.snapshot_id == sid)).mappings().all()
        return {r["address"]: {"rank": r["rank"], "in_top": 1,
                               "pnl": None if r["pnl_usd"] is None else float(r["pnl_usd"])}
                for r in rows}

    def trader(self, address: str) -> dict[str, Any] | None:
        return self.top_traders().get(address)

    def record_snapshot(self, traders: list, *, source: str, status: str, complete: bool,
                        row_count: int, request_meta: dict | None = None,
                        raw_payload: Any = None) -> int:
        """Persist a leaderboard snapshot (never overwrites history). For status 'ok' the
        leaderboard watchlist is moved to this snapshot's set; history rows are kept."""
        with self.engine.begin() as c:
            sid = c.execute(sa.insert(t.leaderboard_snapshots).values(
                chain_id=self.chain_id, source=source, status=status, complete=complete,
                row_count=row_count, accepted_count=len(traders),
                request_meta=request_meta or {}, raw_payload=raw_payload)
                .returning(t.leaderboard_snapshots.c.id)).scalar_one()
            if traders:
                c.execute(sa.insert(t.leaderboard_snapshot_entries), [
                    {"snapshot_id": sid, "address": tr.address, "rank": tr.rank,
                     "pnl_usd": _usd(tr.pnl), "worth_usd": _usd(tr.worth),
                     "volume_usd": _usd(tr.volume), "trades": tr.trades,
                     "extra": _jsonable(getattr(tr, "raw", None))}
                    for tr in traders])
            if status == "ok":
                self._sync_watchlist(c, {tr.address for tr in traders}, "leaderboard")
                if complete and traders:   # only a complete list is evidence of membership
                    from . import tracking
                    tracking.enroll_snapshot(c, self.chain_id, sid,
                                             {tr.address: tr.rank for tr in traders})
        return sid

    def replace_top(self, traders: list) -> None:
        """Back-compat helper: record an accepted snapshot."""
        self.record_snapshot(traders, source="agora", status="ok", complete=True,
                             row_count=len(traders))

    def _sync_watchlist(self, c: Connection, addrs: set[str], source: str,
                        labels: dict[str, str] | None = None) -> None:
        w = t.wallet_watchlist
        self._ensure_wallets(c, addrs)
        current = {r[0] for r in c.execute(sa.select(w.c.address).where(
            w.c.chain_id == self.chain_id, w.c.source == source, w.c.removed_at.is_(None)))}
        gone = current - addrs
        if gone:
            c.execute(sa.update(w).where(
                w.c.chain_id == self.chain_id, w.c.source == source,
                w.c.removed_at.is_(None), w.c.address.in_(gone))
                .values(removed_at=sa.func.now(), status="inactive", reason="left source"))
        new = addrs - current
        if new:
            c.execute(sa.insert(w), [{"chain_id": self.chain_id, "address": a, "source": source,
                                     "label": (labels or {}).get(a)} for a in sorted(new)])

    def sync_manual_watchlist(self, labels: dict[str, str]) -> None:
        with self.engine.begin() as c:
            self._sync_watchlist(c, set(labels), "manual", labels)

    # Wallets added from the Telegram bot (source 'telegram'). They are not touched by
    # sync_manual_watchlist (which only manages source 'manual' from watchlist.txt).
    def watch_add(self, address: str, *, source: str = "telegram", reason: str | None = None,
                  label: str | None = None) -> bool:
        w = t.wallet_watchlist
        with self.engine.begin() as c:
            self._ensure_wallets(c, {address})
            exists = c.execute(sa.select(w.c.id).where(
                w.c.chain_id == self.chain_id, w.c.address == address, w.c.source == source,
                w.c.removed_at.is_(None))).first()
            if exists:
                return False
            c.execute(sa.insert(w).values(chain_id=self.chain_id, address=address,
                                          source=source, label=label, reason=reason))
        return True

    def watch_remove(self, address: str, *, source: str = "telegram") -> bool:
        w = t.wallet_watchlist
        with self.engine.begin() as c:
            n = c.execute(sa.update(w).where(
                w.c.chain_id == self.chain_id, w.c.address == address, w.c.source == source,
                w.c.removed_at.is_(None)).values(removed_at=sa.func.now(), status="inactive",
                                                 reason="removed via Telegram")).rowcount
        return bool(n)

    def watchlist_addresses(self, source: str) -> set[str]:
        w = t.wallet_watchlist
        with self.engine.connect() as c:
            return {r[0] for r in c.execute(sa.select(w.c.address).where(
                w.c.chain_id == self.chain_id, w.c.source == source,
                w.c.removed_at.is_(None)))}

    # --- cumulative tracking (see tracking.py) -----------------------------------------
    def enrolled_addresses(self) -> set[str]:
        from . import tracking
        with self.engine.connect() as c:
            return tracking.enrolled_addresses(c, self.chain_id)

    def ensure_enrollment(self) -> dict[str, int] | None:
        """Rebuild the registry from stored snapshots when it is behind (idempotent)."""
        from . import tracking
        with self.engine.begin() as c:
            return tracking.ensure_enrollment(c, self.chain_id)

    def sync_exclusions(self, enabled: bool = True) -> tuple[int, int]:
        from . import tracking
        with self.engine.begin() as c:
            return tracking.sync_exclusions(c, self.chain_id, enabled)

    def sync_coverage(self, tracked: set[str], cursor: int) -> tuple[int, int]:
        from . import tracking
        with self.engine.begin() as c:
            return tracking.sync_coverage(c, self.chain_id, tracked, cursor)

    def derive_legacy_coverage(self) -> int:
        from . import tracking
        with self.engine.begin() as c:
            return tracking.derive_legacy_coverage(c, self.chain_id)

    def tracking_report(self, scanned: set[str] | None = None) -> dict[str, Any]:
        from . import tracking
        with self.engine.connect() as c:
            return tracking.tracking_report(c, self.chain_id, scanned)

    # --- token metadata ---------------------------------------------------------------
    def get_token(self, address: str) -> tuple[str, int] | None:
        m = t.token_metadata
        with self.engine.connect() as c:
            r = c.execute(sa.select(m.c.symbol, m.c.decimals).where(
                m.c.chain_id == self.chain_id, m.c.address == address)).first()
        return (r[0], r[1]) if r else None

    def put_token(self, address: str, symbol: str, decimals: int, source: str = "rpc") -> None:
        with self.engine.begin() as c:
            stmt = pg_insert(t.token_metadata).values(chain_id=self.chain_id, address=address,
                                                      symbol=symbol, decimals=decimals,
                                                      source=source)
            c.execute(stmt.on_conflict_do_update(
                index_elements=["chain_id", "address"],
                set_={"symbol": symbol, "decimals": decimals, "source": source,
                      "fetched_at": sa.func.now()}))

    # --- evidence ---------------------------------------------------------------------
    def insert_evidence(self, c: Connection, transactions: list[dict],
                        transfers: list[dict]) -> None:
        if transactions:
            # Multi-row INSERT takes its column list from the FIRST row; give every row the
            # full key set so stub rows (hash + block only) never drop other rows' columns.
            cols = sorted({k for tx in transactions for k in tx})
            c.execute(pg_insert(t.transactions).values(
                [{"chain_id": self.chain_id, **{k: tx.get(k) for k in cols}}
                 for tx in transactions]).on_conflict_do_nothing())
        if transfers:
            c.execute(pg_insert(t.token_transfers).values(
                [{"chain_id": self.chain_id, **tr} for tr in transfers])
                .on_conflict_do_nothing())

    def _ensure_tx_stub(self, c: Connection, tx_hash: str, block: int) -> None:
        c.execute(pg_insert(t.transactions).values(chain_id=self.chain_id, tx_hash=tx_hash,
                                                   block_number=block)
                  .on_conflict_do_nothing())

    # --- trades & positions -----------------------------------------------------------
    def insert_trade(self, c: Connection, ev, block_ts: int | None,
                     legacy_key: str | None = None) -> int | None:
        """Insert once per (tx, wallet, token) leg. Returns the new id, or None if this leg
        was already recorded (replay), whatever kind it was classified as then."""
        self._ensure_tx_stub(c, ev.tx, ev.block)
        stmt = pg_insert(t.trades).values(
            chain_id=self.chain_id, tx_hash=ev.tx, wallet=ev.wallet, token=ev.token,
            kind=ev.kind, confidence=ev.confidence, hype_method=ev.hype_method, qty=ev.qty,
            hype_wei=ev.hype_wei, counter_token=ev.counter_token, counter_qty=ev.counter_qty,
            block_number=ev.block, block_time=utc(block_ts), legacy_key=legacy_key,
        ).on_conflict_do_nothing().returning(t.trades.c.id)
        return c.execute(stmt).scalar()

    def get_position(self, c: Connection, wallet: str, token: str) -> Position:
        p = t.wallet_positions
        r = c.execute(sa.select(p).where(p.c.chain_id == self.chain_id, p.c.wallet == wallet,
                                         p.c.token == token).with_for_update()).mappings().first()
        if not r:
            return Position()
        return Position(_int(r["qty"]) or 0, _int(r["cost_wei"]) or 0,
                        _int(r["realized_wei"]) or 0, epoch(r["opened_at"]), r["basis"],
                        _int(r["realized_est_wei"]) or 0)

    def put_position(self, c: Connection, wallet: str, token: str, pos: Position) -> None:
        vals = {"qty": pos.qty, "cost_wei": pos.cost_wei, "realized_wei": pos.realized_wei,
                "realized_est_wei": pos.realized_est_wei, "basis": pos.basis,
                "opened_at": utc(pos.opened_at), "updated_at": sa.func.now()}
        stmt = pg_insert(t.wallet_positions).values(chain_id=self.chain_id, wallet=wallet,
                                                    token=token, **vals)
        c.execute(stmt.on_conflict_do_update(index_elements=["chain_id", "wallet", "token"],
                                             set_=vals))

    def record_realized(self, c: Connection, trade_id: int, wallet: str, token: str, *,
                        qty_sold: int, proceeds_wei: int, cost_basis_wei: int | None,
                        pnl_wei: int | None, status: str) -> None:
        c.execute(pg_insert(t.realized_pnl).values(
            chain_id=self.chain_id, trade_id=trade_id, wallet=wallet, token=token,
            qty_sold=qty_sold, proceeds_wei=proceeds_wei, cost_basis_wei=cost_basis_wei,
            pnl_wei=pnl_wei, status=status).on_conflict_do_nothing())

    # --- signals ----------------------------------------------------------------------
    def recent_buyers(self, c: Connection, token: str, since_ts: int,
                      min_hype_wei: int = 0) -> list[tuple[str, int]]:
        """(wallet, first qualifying trade id) for distinct wallets with a measured
        (high-confidence) BUY of at least min_hype_wei since since_ts."""
        tr = t.trades
        rows = c.execute(sa.select(tr.c.wallet, sa.func.min(tr.c.id)).where(
            tr.c.chain_id == self.chain_id, tr.c.token == token, tr.c.kind == "BUY",
            tr.c.confidence == "high", tr.c.block_time >= utc(since_ts),
            tr.c.hype_wei >= min_hype_wei)
            .group_by(tr.c.wallet).order_by(sa.func.min(tr.c.id))).all()
        return [(r[0], r[1]) for r in rows]

    def record_signal(self, c: Connection, *, kind: str, token: str, dedup_key: str,
                      window_start: int, window_end: int, details: dict,
                      trade_ids: list[int], cooldown_s: int) -> int | None:
        """Insert a signal unless one of the same kind/token exists within the cooldown.
        Returns the signal id, or None when suppressed / duplicate."""
        s = t.signals
        recent = c.execute(sa.select(s.c.id).where(
            s.c.chain_id == self.chain_id, s.c.kind == kind, s.c.token == token,
            s.c.window_end > utc(window_end - cooldown_s)).limit(1)).scalar()
        if recent is not None:
            return None
        sid = c.execute(pg_insert(s).values(
            chain_id=self.chain_id, kind=kind, token=token, dedup_key=dedup_key,
            window_start=utc(window_start), window_end=utc(window_end), details=details)
            .on_conflict_do_nothing().returning(s.c.id)).scalar()
        if sid is not None and trade_ids:
            c.execute(pg_insert(t.signal_trades).values(
                [{"signal_id": sid, "trade_id": tid} for tid in trade_ids])
                .on_conflict_do_nothing())
        return sid

    # --- outbox -----------------------------------------------------------------------
    def enqueue(self, c: Connection, key: str, text: str, severity: str = "info",
                payload: dict | None = None) -> None:
        c.execute(pg_insert(t.notification_outbox).values(
            dedup_key=key, text=text, severity=severity, payload=payload)
            .on_conflict_do_nothing())

    def enqueue_now(self, key: str, text: str, severity: str = "info",
                    payload: dict | None = None) -> None:
        with self.engine.begin() as c:
            self.enqueue(c, key, text, severity, payload)

    def pending_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        o = t.notification_outbox
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(
                sa.select(o).where(o.c.status == "pending").order_by(o.c.id).limit(limit))
                .mappings()]

    def claim_due_alert(self, c: Connection) -> dict[str, Any] | None:
        """Lock the oldest due alert for the duration of the caller's transaction.
        SKIP LOCKED lets concurrent notifiers work without sending the same row."""
        o = t.notification_outbox
        r = c.execute(sa.select(o.c.id, o.c.text, o.c.payload, o.c.attempts).where(
            o.c.status == "pending", o.c.next_attempt_at <= sa.func.now())
            .order_by(o.c.id).limit(1).with_for_update(skip_locked=True)).mappings().first()
        return dict(r) if r else None

    def mark_sent(self, c: Connection, alert_id: int) -> None:
        o = t.notification_outbox
        c.execute(sa.update(o).where(o.c.id == alert_id)
                  .values(status="sent", sent_at=sa.func.now(), attempts=o.c.attempts + 1))
        c.execute(sa.insert(t.notification_deliveries).values(outbox_id=alert_id, ok=True))

    def mark_failed(self, c: Connection, alert_id: int, attempts_before: int, error: str,
                    max_attempts: int, permanent: bool = False) -> str:
        """Exponential backoff (10s, 20s, 40s ... capped at 1h); dead-letter after
        max_attempts or on a permanent error. Dead rows stay for inspection."""
        o = t.notification_outbox
        attempts = attempts_before + 1
        status = "dead" if permanent or attempts >= max_attempts else "pending"
        delay = min(10 * 2 ** (attempts - 1), 3600)
        c.execute(sa.update(o).where(o.c.id == alert_id).values(
            attempts=attempts, status=status, last_error=error[:500],
            next_attempt_at=sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, 0, delay)))
        c.execute(sa.insert(t.notification_deliveries).values(
            outbox_id=alert_id, ok=False, error=error[:500]))
        return status

    def outbox_counts(self) -> dict[str, int]:
        o = t.notification_outbox
        with self.engine.connect() as c:
            return {r[0]: r[1] for r in c.execute(
                sa.select(o.c.status, sa.func.count()).group_by(o.c.status))}

    # --- launch intelligence ------------------------------------------------------------
    def recent_buyer_blocks(self, c: Connection, token: str, since_ts: int,
                            min_hype_wei: int = 0) -> list[tuple[str, int, int, int]]:
        """(wallet, first trade id, block, hype_wei) per distinct wallet with a measured BUY
        since since_ts, ordered by that first trade. The block/amount are of that first trade."""
        tr = t.trades
        rows = c.execute(
            sa.select(tr.c.wallet, tr.c.id, tr.c.block_number, tr.c.hype_wei)
            .where(tr.c.chain_id == self.chain_id, tr.c.token == token, tr.c.kind == "BUY",
                   tr.c.confidence == "high", tr.c.block_time >= utc(since_ts),
                   tr.c.hype_wei >= min_hype_wei)
            .order_by(tr.c.id)).all()
        seen: dict[str, tuple[str, int, int, int]] = {}
        for w, tid, blk, wei in rows:
            seen.setdefault(w, (w, tid, int(blk), _int(wei) or 0))
        return list(seen.values())

    def bundle_wallets(self, *, lookback_days: int, min_wallets: int, min_hits: int) -> set[str]:
        """Wallets that repeatedly buy a token in the same block as other tracked wallets."""
        tr = t.trades
        since = datetime.now(timezone.utc).timestamp() - lookback_days * 86400
        with self.engine.connect() as c:
            rows = c.execute(
                sa.select(tr.c.block_number, tr.c.token, tr.c.wallet).where(
                    tr.c.chain_id == self.chain_id, tr.c.kind == "BUY",
                    tr.c.confidence == "high", tr.c.block_time >= utc(int(since)))).all()
        from .launchintel import find_bundle_wallets
        return find_bundle_wallets(((int(b), tok, w) for b, tok, w in rows),
                                   min_wallets=min_wallets, min_hits=min_hits)

    def record_paper_trade(self, c: Connection, *, token: str, signal_id: int | None,
                           opened_at: int, block: int | None, entry_price_hype: Decimal | None,
                           n_independent: int, n_raw: int, details: dict) -> bool:
        """One paper trade per token; True when this call created it."""
        pt = t.paper_trades
        got = c.execute(pg_insert(pt).values(
            chain_id=self.chain_id, token=token, signal_id=signal_id, opened_at=utc(opened_at),
            block_number=block, entry_price_hype=entry_price_hype, n_independent=n_independent,
            n_raw=n_raw, details=details)
            .on_conflict_do_nothing(constraint="uq_paper_trades_token").returning(pt.c.id)
        ).scalar()
        return got is not None

    def paper_trades(self, since_days: int | None = None) -> list[dict[str, Any]]:
        """Paper trades with the market snapshots recorded after each opened."""
        pt, ms = t.paper_trades, t.token_market_snapshots
        q = sa.select(pt).where(pt.c.chain_id == self.chain_id).order_by(pt.c.opened_at)
        if since_days:
            q = q.where(pt.c.opened_at >= sa.func.now() - sa.text(f"interval '{int(since_days)} days'"))
        out = []
        with self.engine.connect() as c:
            for r in c.execute(q).mappings():
                snaps = c.execute(
                    sa.select(ms.c.captured_at, ms.c.price_hype).where(
                        ms.c.chain_id == self.chain_id, ms.c.token == r["token"],
                        ms.c.captured_at >= r["opened_at"], ms.c.price_hype.isnot(None))
                    .order_by(ms.c.captured_at)).all()
                d = dict(r)
                d["snapshots"] = [(a.timestamp(), float(p)) for a, p in snaps]
                d["opened_ts"] = r["opened_at"].timestamp()
                d["entry"] = float(r["entry_price_hype"]) if r["entry_price_hype"] else None
                out.append(d)
        return out

    def launch_venue_add(self, key: str, kind: str, details: dict) -> bool:
        lv = t.launch_venues
        with self.engine.begin() as c:
            got = c.execute(pg_insert(lv).values(
                chain_id=self.chain_id, key=key, kind=kind, details=details)
                .on_conflict_do_nothing(constraint="uq_launch_venues_key")
                .returning(lv.c.id)).scalar()
        return got is not None

    def ascend_upsert(self, rows: list[dict]) -> int:
        """Insert Ascend launches (idempotent: a replayed range changes nothing). Returns new rows."""
        al = t.ascend_launches
        n = 0
        with self.engine.begin() as c:
            for r in rows:
                ts_ = r.get("launched_ts")
                stmt = pg_insert(al).values(
                    chain_id=self.chain_id, token=r["token"], pool_id=r["pool_id"],
                    quote=r["quote"], token_is0=r["token_is0"], fee_pips=r["fee_pips"],
                    builder_fee_pips=r["builder_fee_pips"], supply_seeded=r["supply_seeded"],
                    dev_buy_tokens=r["dev_buy_tokens"], creator=r["creator"],
                    escrow=r.get("escrow"), recipient=r.get("recipient"),
                    block_number=r["block_number"], launched_at=utc(ts_) if ts_ else None,
                    tx_hash=r["tx_hash"])
                got = c.execute(stmt.on_conflict_do_update(
                    index_elements=[al.c.chain_id, al.c.token],
                    set_={"escrow": sa.func.coalesce(al.c.escrow, stmt.excluded.escrow),
                          "recipient": sa.func.coalesce(al.c.recipient, stmt.excluded.recipient),
                          "launched_at": sa.func.coalesce(al.c.launched_at,
                                                          stmt.excluded.launched_at)})
                    .returning(sa.literal_column("xmax = 0"))).scalar()
                n += 1 if got else 0
        return n

    def ascend_launch(self, token: str) -> dict[str, Any] | None:
        al = t.ascend_launches
        with self.engine.connect() as c:
            r = c.execute(sa.select(al).where(al.c.chain_id == self.chain_id,
                                              al.c.token == token.lower())).mappings().first()
        return dict(r) if r else None

    def launch_factories(self) -> list[str]:
        """DEX factories that created pools we have seen (for the due-diligence pool lookup)."""
        lv = t.launch_venues
        with self.engine.connect() as c:
            rows = c.execute(sa.select(sa.distinct(lv.c.details["factory"].astext)).where(
                lv.c.chain_id == self.chain_id, lv.c.kind == "pool")).all()
        return [r[0] for r in rows if r[0]]

    def launch_pending_pools(self, limit: int = 200) -> list[dict[str, Any]]:
        lv = t.launch_venues
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(
                sa.select(lv.c.key, lv.c.details).where(
                    lv.c.chain_id == self.chain_id, lv.c.kind == "pool",
                    lv.c.alerted_at.is_(None)).order_by(lv.c.id).limit(limit)).mappings()]

    def launch_venue_done(self, key: str, extra: dict, payload: dict | None = None) -> None:
        """Mark a venue handled and, in the same transaction, queue its alert (if any)."""
        lv = t.launch_venues
        with self.engine.begin() as c:
            c.execute(sa.update(lv).where(lv.c.chain_id == self.chain_id, lv.c.key == key)
                      .values(alerted_at=sa.func.now(),
                              details=lv.c.details.op("||")(sa.cast(extra, t.JSONB))))
            if payload:
                self.enqueue(c, f"launch:{key}", f"New launch {payload.get('symbol') or ''}",
                             severity="high", payload=payload)

    def register_launchpads(self, c: Connection, found: dict[str, list[dict]]
                            ) -> list[tuple[str, list[dict]]]:
        """Record launchpad names Agora reports. Returns the ones never seen before.

        The very first call only seeds the table (everything already indexed is not 'new')."""
        lv = t.launch_venues
        have = {r[0] for r in c.execute(sa.select(lv.c.key).where(
            lv.c.chain_id == self.chain_id, lv.c.kind == "launchpad"))}
        seeding = not have
        if seeding:   # everything Agora already lists (not just this round's top list) is known
            for (name,) in c.execute(sa.select(sa.distinct(t.token_profiles.c.launchpad)).where(
                    t.token_profiles.c.chain_id == self.chain_id,
                    t.token_profiles.c.launchpad.isnot(None))):
                found.setdefault(name, [])
        new = []
        for name, toks in found.items():
            if not name:
                continue
            key = f"launchpad:{name}"
            if key in have:
                continue
            got = c.execute(pg_insert(lv).values(
                chain_id=self.chain_id, key=key, kind="launchpad",
                alerted_at=sa.func.now() if seeding else None,
                details={"tokens": toks[:5], "seeded": seeding})
                .on_conflict_do_nothing(constraint="uq_launch_venues_key")
                .returning(lv.c.id)).scalar()
            if got is not None and not seeding:
                new.append((name, toks[:5]))
        return new

    def token_profile_age_s(self, token: str) -> float | None:
        p = t.token_profiles
        with self.engine.connect() as c:
            ts_ = c.execute(sa.select(p.c.launched_at).where(
                p.c.chain_id == self.chain_id, p.c.address == token)).scalar()
        return None if ts_ is None else time.time() - ts_.timestamp()

    # --- status -----------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        tr, p = t.trades, t.wallet_positions
        with self.engine.connect() as c:
            by_conf = {r[0]: r[1] for r in c.execute(
                sa.select(tr.c.confidence, sa.func.count()).where(
                    tr.c.chain_id == self.chain_id).group_by(tr.c.confidence))}
            open_pos = c.execute(sa.select(sa.func.count()).select_from(p).where(
                p.c.chain_id == self.chain_id, p.c.qty > 0)).scalar()
            snap = c.execute(sa.select(t.leaderboard_snapshots.c.captured_at,
                                       t.leaderboard_snapshots.c.status)
                             .where(t.leaderboard_snapshots.c.chain_id == self.chain_id)
                             .order_by(t.leaderboard_snapshots.c.id.desc()).limit(1)).first()
            watch = {r[0]: r[1] for r in c.execute(
                sa.select(t.wallet_watchlist.c.source, sa.func.count()).where(
                    t.wallet_watchlist.c.chain_id == self.chain_id,
                    t.wallet_watchlist.c.removed_at.is_(None))
                .group_by(t.wallet_watchlist.c.source))}
        hb = self.get_kv("heartbeat")
        return {
            "chain_id": self.chain_id,
            "cursor": self.get_cursor(),
            "heartbeat_age_s": int(time.time()) - int(hb) if hb else None,
            "watchlist": watch,
            "last_leaderboard": {"at": snap[0].isoformat(), "status": snap[1]} if snap else None,
            "trades_by_confidence": by_conf,
            "open_positions": open_pos,
            "outbox": self.outbox_counts(),
        }


def _usd(v: float | None) -> Decimal | None:
    if v is None:
        return None
    # via str() so 0.1 stays 0.1 instead of its binary float expansion
    return Decimal(str(v)).quantize(Decimal("0.000001"))


def _jsonable(obj: Any) -> Any:
    if obj is None:
        return None
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return None
