"""SQLite state: cursor, leaderboard snapshot, trade events, positions, token cache, alert outbox.

Big integers (wei, raw token amounts) are stored as TEXT to avoid 64-bit overflow.
Event and alert keys are UNIQUE, so replaying a block range is a no-op (idempotent).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS traders (
    address TEXT PRIMARY KEY, rank INTEGER, pnl REAL, worth REAL, volume REAL,
    trades INTEGER, in_top INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    key TEXT PRIMARY KEY, block INTEGER NOT NULL, ts INTEGER, tx TEXT NOT NULL,
    wallet TEXT NOT NULL, kind TEXT NOT NULL, token TEXT NOT NULL, qty TEXT NOT NULL,
    hype_wei TEXT, counter_token TEXT, counter_qty TEXT, approx INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_events_token_ts ON events(token, kind, ts);
CREATE INDEX IF NOT EXISTS ix_events_wallet ON events(wallet, block);
CREATE TABLE IF NOT EXISTS positions (
    wallet TEXT NOT NULL, token TEXT NOT NULL, qty TEXT NOT NULL, cost_wei TEXT NOT NULL,
    realized_wei TEXT NOT NULL, opened_at INTEGER, PRIMARY KEY (wallet, token));
CREATE TABLE IF NOT EXISTS tokens (address TEXT PRIMARY KEY, symbol TEXT, decimals INTEGER);
CREATE TABLE IF NOT EXISTS contracts (address TEXT PRIMARY KEY, is_contract INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cluster_alerts (token TEXT PRIMARY KEY, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, text TEXT NOT NULL,
    created_at INTEGER NOT NULL, sent_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0);
"""


# Columns added after v0.1. Existing databases are upgraded in place on startup; the
# backfilled values say plainly that old rows were produced by the v0.1 logic.
_UPGRADES = [
    ("events", "confidence", "TEXT NOT NULL DEFAULT 'legacy'"),
    ("events", "hype_method", "TEXT NOT NULL DEFAULT 'legacy'"),
    ("positions", "basis", "TEXT NOT NULL DEFAULT 'legacy'"),
    ("positions", "realized_est_wei", "TEXT NOT NULL DEFAULT '0'"),
    ("outbox", "status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("outbox", "next_attempt_at", "INTEGER NOT NULL DEFAULT 0"),
    ("outbox", "last_error", "TEXT"),
]

BASIS_KNOWN, BASIS_ESTIMATED, BASIS_PARTIAL, BASIS_LEGACY = (
    "known", "estimated", "partial_unknown", "legacy")


@dataclass
class Position:
    qty: int = 0
    cost_wei: int = 0          # cost of the part of `qty` whose cost is known/estimated
    realized_wei: int = 0      # PnL from sells with measured proceeds and known basis
    opened_at: int | None = None
    basis: str = BASIS_KNOWN   # known | estimated | partial_unknown | legacy
    realized_est_wei: int = 0  # PnL that relied on an estimate somewhere


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._upgrade()

    def _upgrade(self) -> None:
        for table, col, decl in _UPGRADES:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                if (table, col) == ("events", "confidence"):
                    self.conn.execute("UPDATE events SET confidence='estimated' WHERE approx=1")
                if (table, col) == ("outbox", "status"):
                    self.conn.execute("UPDATE outbox SET status='sent' WHERE sent_at IS NOT NULL")
                    self.conn.execute("UPDATE outbox SET status='dead' "
                                      "WHERE sent_at IS NULL AND attempts >= 10")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_events_leg ON events(tx, wallet, token)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_outbox_due "
                          "ON outbox(status, next_attempt_at)")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # --- kv -----------------------------------------------------------------
    def get_kv(self, k: str) -> str | None:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def set_kv(self, k: str, v: str) -> None:
        self.conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                          (k, v))

    # --- traders ------------------------------------------------------------
    def top_traders(self) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM traders WHERE in_top=1").fetchall()
        return {r["address"]: r for r in rows}

    def trader(self, address: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM traders WHERE address=?", (address,)).fetchone()

    def replace_top(self, traders: list) -> None:
        now = int(time.time())
        with self.tx() as c:
            c.execute("UPDATE traders SET in_top=0")
            for t in traders:
                c.execute(
                    """INSERT INTO traders(address,rank,pnl,worth,volume,trades,in_top,updated_at)
                       VALUES(?,?,?,?,?,?,1,?)
                       ON CONFLICT(address) DO UPDATE SET rank=excluded.rank, pnl=excluded.pnl,
                       worth=excluded.worth, volume=excluded.volume, trades=excluded.trades,
                       in_top=1, updated_at=excluded.updated_at""",
                    (t.address, t.rank, t.pnl, t.worth, t.volume, t.trades, now))

    # --- events / positions -------------------------------------------------
    @staticmethod
    def insert_event(c: sqlite3.Connection, ev, block_ts: int | None) -> bool:
        """Insert once per (tx, wallet, token) leg. Returns False if the leg already exists,
        whatever kind it was classified as before (v0.1 keys also contained the kind)."""
        if c.execute("SELECT 1 FROM events WHERE tx=? AND wallet=? AND token=? LIMIT 1",
                     (ev.tx, ev.wallet, ev.token)).fetchone():
            return False
        cur = c.execute(
            """INSERT OR IGNORE INTO events(key,block,ts,tx,wallet,kind,token,qty,hype_wei,
               counter_token,counter_qty,approx,confidence,hype_method)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ev.key, ev.block, block_ts, ev.tx, ev.wallet, ev.kind, ev.token, str(ev.qty),
             None if ev.hype_wei is None else str(ev.hype_wei), ev.counter_token,
             None if ev.counter_qty is None else str(ev.counter_qty), int(ev.approx),
             ev.confidence, ev.hype_method))
        return cur.rowcount == 1

    @staticmethod
    def get_position(c: sqlite3.Connection, wallet: str, token: str) -> Position:
        r = c.execute("SELECT * FROM positions WHERE wallet=? AND token=?", (wallet, token)).fetchone()
        if not r:
            return Position()
        return Position(int(r["qty"]), int(r["cost_wei"]), int(r["realized_wei"]), r["opened_at"],
                        r["basis"], int(r["realized_est_wei"]))

    @staticmethod
    def put_position(c: sqlite3.Connection, wallet: str, token: str, p: Position) -> None:
        c.execute(
            """INSERT INTO positions(wallet,token,qty,cost_wei,realized_wei,opened_at,basis,
               realized_est_wei) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(wallet,token) DO UPDATE SET
               qty=excluded.qty, cost_wei=excluded.cost_wei, realized_wei=excluded.realized_wei,
               opened_at=excluded.opened_at, basis=excluded.basis,
               realized_est_wei=excluded.realized_est_wei""",
            (wallet, token, str(p.qty), str(p.cost_wei), str(p.realized_wei), p.opened_at,
             p.basis, str(p.realized_est_wei)))

    @staticmethod
    def recent_buyers(c: sqlite3.Connection, token: str, since_ts: int,
                      min_hype_wei: int = 0) -> list[str]:
        """Distinct wallets with a measured (high-confidence) BUY of at least min_hype_wei."""
        rows = c.execute(
            "SELECT wallet, hype_wei FROM events WHERE token=? AND kind='BUY' AND ts>=? "
            "AND confidence='high' ORDER BY ts, block", (token, since_ts)).fetchall()
        out: list[str] = []
        for r in rows:
            if r["hype_wei"] is not None and int(r["hype_wei"]) >= min_hype_wei \
                    and r["wallet"] not in out:
                out.append(r["wallet"])
        return out

    @staticmethod
    def cluster_due(c: sqlite3.Connection, token: str, now: int, window: int) -> bool:
        r = c.execute("SELECT ts FROM cluster_alerts WHERE token=?", (token,)).fetchone()
        if r and now - r["ts"] < window:
            return False
        c.execute("INSERT INTO cluster_alerts(token,ts) VALUES(?,?) ON CONFLICT(token) "
                  "DO UPDATE SET ts=excluded.ts", (token, now))
        return True

    # --- caches -------------------------------------------------------------
    def get_token(self, address: str) -> tuple[str, int] | None:
        r = self.conn.execute("SELECT symbol,decimals FROM tokens WHERE address=?", (address,)).fetchone()
        return (r["symbol"], r["decimals"]) if r else None

    def put_token(self, address: str, symbol: str, decimals: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO tokens(address,symbol,decimals) VALUES(?,?,?)",
                          (address, symbol, decimals))

    def get_is_contract(self, address: str) -> bool | None:
        r = self.conn.execute("SELECT is_contract FROM contracts WHERE address=?", (address,)).fetchone()
        return bool(r["is_contract"]) if r else None

    def put_is_contract(self, address: str, value: bool) -> None:
        self.conn.execute("INSERT OR REPLACE INTO contracts(address,is_contract) VALUES(?,?)",
                          (address, int(value)))

    # --- outbox -------------------------------------------------------------
    @staticmethod
    def enqueue(c: sqlite3.Connection, key: str, text: str) -> None:
        c.execute("INSERT OR IGNORE INTO outbox(key,text,created_at) VALUES(?,?,?)",
                  (key, text, int(time.time())))

    def enqueue_now(self, key: str, text: str) -> None:
        self.enqueue(self.conn, key, text)

    def pending_alerts(self, limit: int = 20, now: int | None = None) -> list[sqlite3.Row]:
        now = int(time.time()) if now is None else now
        return self.conn.execute(
            "SELECT * FROM outbox WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT ?",
            (now, limit)).fetchall()

    def outbox_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")}

    def mark_sent(self, alert_id: int) -> None:
        self.conn.execute("UPDATE outbox SET sent_at=?, status='sent' WHERE id=?",
                          (int(time.time()), alert_id))

    def mark_failed(self, alert_id: int, error: str, max_attempts: int,
                    permanent: bool = False, now: int | None = None) -> str:
        """Exponential backoff (10s, 20s, 40s ... capped at 1h); dead-letter after
        max_attempts or on a permanent error. Dead rows stay for inspection."""
        now = int(time.time()) if now is None else now
        row = self.conn.execute("SELECT attempts FROM outbox WHERE id=?", (alert_id,)).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        status = "dead" if permanent or attempts >= max_attempts else "pending"
        delay = min(10 * 2 ** (attempts - 1), 3600)
        self.conn.execute(
            "UPDATE outbox SET attempts=?, status=?, next_attempt_at=?, last_error=? WHERE id=?",
            (attempts, status, now + delay, error[:500], alert_id))
        return status
