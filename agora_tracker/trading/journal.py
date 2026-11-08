"""Optional private local PAPER journal, separate from scanner/PostgreSQL/public views.

It stores hypothetical quote snapshots, never signed bytes, keys, fills or live receipts.
Sessions are still invalidated on restart. A unique intent ID prevents duplicate records.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict

from .paper import Order, Quote, TradeIntent, TradeRefused
from .private_store import PrivateSqlite


class PaperJournal(PrivateSqlite):
    def __init__(self, path: str):
        super().__init__(path)
        try:
            with self._connect() as c:
                c.execute('CREATE TABLE IF NOT EXISTS paper_orders ('
                          'intent_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, '
                          'created_at REAL NOT NULL, snapshot TEXT NOT NULL)')
                c.execute('CREATE TABLE IF NOT EXISTS paper_wallets ('
                          'user_id INTEGER PRIMARY KEY, address TEXT NOT NULL)')
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            raise TradeRefused('Private PAPER journal cannot be opened') from None

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
