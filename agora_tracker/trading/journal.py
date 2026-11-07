"""Optional private local PAPER journal, separate from scanner/PostgreSQL/public views.

It stores hypothetical quote snapshots, never signed bytes, keys, fills or live receipts.
Sessions are still invalidated on restart. A unique intent ID prevents duplicate records.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .paper import Order, Quote, TradeIntent, TradeRefused


class PaperJournal:
    def __init__(self, path: str):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise TradeRefused('Paper journal directory must be owned by this user with mode 0700')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
                raise TradeRefused('Paper journal must be a private regular file with mode 0600')
        finally:
            os.close(fd)
        try:
            with self._connect() as c:
                c.execute('CREATE TABLE IF NOT EXISTS paper_orders ('
                          'intent_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, '
                          'created_at REAL NOT NULL, snapshot TEXT NOT NULL)')
                c.execute('CREATE TABLE IF NOT EXISTS paper_wallets ('
                          'user_id INTEGER PRIMARY KEY, address TEXT NOT NULL)')
        except sqlite3.Error:
            raise TradeRefused('Private PAPER journal cannot be opened') from None

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=2)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save(self, order: Order, intent: TradeIntent, quote: Quote) -> None:
        try:
            with self._connect() as c:
                c.execute('INSERT INTO paper_orders VALUES (?, ?, ?, ?)',
                    (order.intent_id, order.user_id, time.time(), json.dumps({
                        'mode': order.mode, 'status': order.status,
                        'intent': asdict(intent), 'quote': asdict(quote)})))
        except sqlite3.Error:
            raise TradeRefused('Paper journal unavailable or duplicate order; nothing submitted') from None

    def recent(self, uid: int) -> list[dict]:
        try:
            with self._connect() as c:
                rows = c.execute('SELECT intent_id, created_at, snapshot FROM paper_orders '
                                 'WHERE user_id=? ORDER BY created_at DESC LIMIT 10', (uid,)).fetchall()
            return [{'intent_id': r[0], 'created_at': r[1], 'mode': 'PAPER',
                     'status': json.loads(r[2])['status'], 'snapshot': json.loads(r[2])} for r in rows]
        except sqlite3.Error:
            raise TradeRefused('Paper journal unavailable') from None

    def set_wallet(self, uid: int, wallet: str) -> None:
        try:
            with self._connect() as c:
                c.execute('INSERT INTO paper_wallets VALUES (?, ?) ON CONFLICT(user_id) '
                          'DO UPDATE SET address=excluded.address', (uid, wallet))
        except sqlite3.Error:
            raise TradeRefused('Private watch-wallet storage unavailable') from None

    def wallet(self, uid: int) -> str | None:
        try:
            with self._connect() as c:
                row = c.execute('SELECT address FROM paper_wallets WHERE user_id=?', (uid,)).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            raise TradeRefused('Private watch-wallet storage unavailable') from None
