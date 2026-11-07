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
        parent = self.path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode):
            raise TradeRefused('Paper journal directory cannot be a symlink')
        self._directory_identity = (parent.st_dev, parent.st_ino)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise TradeRefused('Paper journal directory must be owned by this user with mode 0700')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            self._identity = (st.st_dev, st.st_ino)
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
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            raise TradeRefused('Private PAPER journal cannot be opened') from None

    def _check_private_path(self) -> None:
        try:
            directory = self.path.parent.lstat()
            file = self.path.lstat()
            if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) & 0o077
                or (directory.st_dev, directory.st_ino) != self._directory_identity
                or not stat.S_ISREG(file.st_mode) or file.st_uid != os.getuid()
                or stat.S_IMODE(file.st_mode) & 0o077 or file.st_nlink != 1
                or (file.st_dev, file.st_ino) != self._identity):
                raise TradeRefused('Private paper journal path or permissions changed')
        except OSError:
            raise TradeRefused('Private paper journal unavailable') from None

    @contextmanager
    def _connect(self):
        self._check_private_path()
        conn = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=2)
        try:
            self._check_private_path()
            conn.execute('PRAGMA trusted_schema=OFF')
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
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            raise TradeRefused('Paper journal unavailable or duplicate order; nothing submitted') from None

    def recent(self, uid: int) -> list[dict]:
        try:
            with self._connect() as c:
                rows = c.execute('SELECT intent_id, created_at, snapshot FROM paper_orders '
                                 'WHERE user_id=? ORDER BY created_at DESC LIMIT 10', (uid,)).fetchall()
            records = []
            for row in rows:
                snapshot = json.loads(row[2])
                if (not isinstance(snapshot, dict) or snapshot.get('mode') != 'PAPER'
                    or snapshot.get('status') not in ('hypothetical_only', 'hypothetical_requires_approval')
                    or snapshot.get('intent', {}).get('user_id') != uid
                    or snapshot.get('intent', {}).get('id') != row[0]
                    or snapshot.get('quote', {}).get('intent_id') != row[0]):
                    raise ValueError('Invalid private record binding')
                records.append({'intent_id':row[0], 'created_at':row[1], 'mode':'PAPER',
                    'status':snapshot['status'], 'snapshot':snapshot})
            return records
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            raise TradeRefused('Paper journal unavailable') from None

    def set_wallet(self, uid: int, wallet: str) -> None:
        try:
            with self._connect() as c:
                c.execute('INSERT INTO paper_wallets VALUES (?, ?) ON CONFLICT(user_id) '
                          'DO UPDATE SET address=excluded.address', (uid, wallet))
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            raise TradeRefused('Private watch-wallet storage unavailable') from None

    def wallet(self, uid: int) -> str | None:
        try:
            with self._connect() as c:
                row = c.execute('SELECT address FROM paper_wallets WHERE user_id=?', (uid,)).fetchone()
            return row[0] if row else None
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            raise TradeRefused('Private watch-wallet storage unavailable') from None
