"""Private SQLite auth state. BEGIN IMMEDIATE serializes replay/nonce operations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..private_store import PrivateSqlite
from .telegram import AuthRefused, RateLimited


@dataclass(frozen=True)
class Session:
    token: str  # SHA-256 only, never the bearer cookie
    csrf: str  # SHA-256 only
    uid: int
    bot: int
    intent: str
    origin: str
    created: int
    expires: int
    wallet: str | None
    generation: int
    realm: str


@dataclass(frozen=True)
class Challenge:
    nonce: str
    session: str
    generation: int
    wallet: str
    issued: int
    expires: int
    binding: str
    message: str
    used: int


class AuthStore(PrivateSqlite):
    def __init__(self, path: str):
        super().__init__(path)
        with self._connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS auth_launches (
                    fingerprint TEXT PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token TEXT PRIMARY KEY, csrf TEXT NOT NULL, uid INTEGER NOT NULL,
                    bot INTEGER NOT NULL, intent TEXT NOT NULL, origin TEXT NOT NULL,
                    created INTEGER NOT NULL, expires INTEGER NOT NULL, wallet TEXT,
                    generation INTEGER NOT NULL DEFAULT 0, realm TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_challenges (
                    nonce TEXT PRIMARY KEY, session TEXT NOT NULL, generation INTEGER NOT NULL,
                    wallet TEXT NOT NULL, issued INTEGER NOT NULL, expires INTEGER NOT NULL,
                    binding TEXT NOT NULL, message TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(session) REFERENCES auth_sessions(token)
                        ON DELETE CASCADE ON UPDATE CASCADE);
                CREATE TABLE IF NOT EXISTS auth_limits (
                    key TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL);
            """)

    @staticmethod
    def _begin(c):
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _clean(c, now):
        c.execute("DELETE FROM auth_sessions WHERE expires<=?", (now,))
        c.execute("DELETE FROM auth_challenges WHERE expires<=?", (now,))
        c.execute("DELETE FROM auth_launches WHERE expires<=?", (now,))
        c.execute("DELETE FROM auth_limits WHERE window<?", (now // 60 - 1,))

    def limit(self, key: str, maximum: int, now: int):
        with self._connect() as c:
            self._begin(c)
            self._clean(c, now)
            row = c.execute("SELECT window, count FROM auth_limits WHERE key=?", (key,)).fetchone()
            count = row["count"] if row and row["window"] == now // 60 else 0
            if (
                count >= maximum
                or c.execute("SELECT count(*) FROM auth_limits").fetchone()[0] >= 2048
            ):
                raise RateLimited("Too many authentication attempts; reopen later")
            c.execute(
                "INSERT INTO auth_limits VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                "SET window=excluded.window, count=excluded.count",
                (key, now // 60, count + 1),
            )

    def create(self, session: Session, launch: str, launch_expiry: int, previous: str | None):
        try:
            with self._connect() as c:
                self._begin(c)
                self._clean(c, session.created)
                if c.execute("SELECT count(*) FROM auth_sessions").fetchone()[0] >= 256:
                    raise AuthRefused("Authentication unavailable")
                c.execute("INSERT INTO auth_launches VALUES (?, ?)", (launch, launch_expiry))
                if previous:
                    c.execute("DELETE FROM auth_sessions WHERE token=?", (previous,))
                c.execute(
                    "INSERT INTO auth_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(vars(session).values()),
                )
        except sqlite3.IntegrityError:
            raise AuthRefused("Launch already used; reopen from Telegram") from None

    @staticmethod
    def _session(c, token: str, now: int) -> Session:
        row = c.execute("SELECT * FROM auth_sessions WHERE token=?", (token,)).fetchone()
        if not row or not row["created"] <= now < row["expires"]:
            raise AuthRefused("Authentication required")
        return Session(**dict(row))

    def get(self, token: str, now: int) -> Session:
        with self._connect() as c:
            c.row_factory = sqlite3.Row
            return self._session(c, token, now)

    def revoke(self, token: str, now: int, *, logout=False) -> Session:
        with self._connect() as c:
            self._begin(c)
            s = self._session(c, token, now)
            c.execute("DELETE FROM auth_challenges WHERE session=?", (token,))
            if logout:
                c.execute("DELETE FROM auth_sessions WHERE token=?", (token,))
            else:
                c.execute(
                    "UPDATE auth_sessions SET wallet=NULL, generation=generation+1 WHERE token=?",
                    (token,),
                )
                s = self._session(c, token, now)
            return s

    def challenge(self, challenge: Challenge, now: int):
        with self._connect() as c:
            self._begin(c)
            s = self._session(c, challenge.session, now)
            if s.generation != challenge.generation or not now < challenge.expires <= s.expires:
                raise AuthRefused("Session changed; reconnect")
            c.execute(
                "INSERT INTO auth_challenges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(vars(challenge).values()),
            )

    def consume(self, token: str, nonce: str, wallet: str, now: int) -> Challenge:
        with self._connect() as c:
            self._begin(c)
            s = self._session(c, token, now)
            row = c.execute(
                "SELECT * FROM auth_challenges WHERE nonce=? AND session=?", (nonce, token)
            ).fetchone()
            if (
                not row
                or row["used"]
                or row["generation"] != s.generation
                or row["wallet"] != wallet
                or not row["issued"] <= now < row["expires"]
            ):
                raise AuthRefused("Ownership proof refused")
            c.execute("UPDATE auth_challenges SET used=1 WHERE nonce=? AND used=0", (nonce,))
            return Challenge(**dict(row))

    def complete(self, old: Session, q: Challenge, token: str, csrf: str, now: int):
        with self._connect() as c:
            self._begin(c)
            s = self._session(c, old.token, now)
            consumed = c.execute(
                "SELECT 1 FROM auth_challenges WHERE nonce=? AND session=? AND used=1 AND generation=?",
                (q.nonce, old.token, q.generation),
            ).fetchone()
            if not consumed or s.generation != q.generation or not q.issued <= now < q.expires:
                raise AuthRefused("Session changed; reconnect")
            # Rotate BOTH secrets when elevating from Telegram-only to ownership proof.
            c.execute(
                "UPDATE auth_sessions SET token=?, csrf=?, wallet=?, generation=generation+1 "
                "WHERE token=?",
                (token, csrf, q.wallet, old.token),
            )
            c.execute("DELETE FROM auth_challenges WHERE session=?", (token,))
