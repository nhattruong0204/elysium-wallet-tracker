"""Reports for the public dashboard: the Telegram analytics, pointed at `public_api`.

`Reports` queries unqualified table names. The dashboard role's search_path is
`public_api`, so the very same SQL runs against the filtered views: there is exactly one
implementation of every metric, and the privacy boundary is the database's, not the
templates'. This subclass only adds the aggregate shapes the website needs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from ..market import TokenInfo
from ..reports import Reports
from ..timerange import TimeRange

UTC = timezone.utc


class StoredMarket:
    """Market stats from the collector's stored snapshots, shaped like AgoraMarket.

    Only the methods Reports calls on the dashboard path exist. Nothing here touches the
    network. Data older than `max_age_s` is treated as unavailable, so a stalled collector
    shows up as "no market data" instead of silently old numbers.
    """

    def __init__(self, engine: Engine, chain_id: int, max_age_s: int = 1800):
        self.engine, self.chain_id, self.max_age_s = engine, chain_id, max_age_s

    _SQL = """
        SELECT s.token, s.captured_at, s.source_synced_at, s.source_head, s.price_hype,
               s.mcap_hype, s.vol_1h_hype, s.vol_24h_hype, s.change_1h, s.change_24h,
               s.buys_24h, s.trades_24h, s.last_trade_at,
               p.symbol, p.name, p.launchpad, p.version, p.launched_at
        FROM token_market_snapshots s
        LEFT JOIN token_profiles p ON p.chain_id = s.chain_id AND p.address = s.token
        WHERE s.chain_id = :c AND {where}"""

    @staticmethod
    def _info(r: dict[str, Any]) -> TokenInfo:
        def f(v: Any) -> float | None:
            return None if v is None else float(v)

        def ts(v: datetime | None) -> int | None:
            return int(v.timestamp()) if v else None

        return TokenInfo(
            address=r["token"], symbol=r["symbol"], name=r["name"], launchpad=r["launchpad"],
            price_hype=f(r["price_hype"]), mcap_hype=f(r["mcap_hype"]),
            vol_1h_hype=f(r["vol_1h_hype"]), vol_24h_hype=f(r["vol_24h_hype"]),
            change_1h=f(r["change_1h"]), change_24h=f(r["change_24h"]),
            last_trade_at=ts(r["last_trade_at"]), version=r["version"],
            buys_24h=r["buys_24h"], trades_24h=r["trades_24h"], created_at=ts(r["launched_at"]))

    def _rows(self, where: str, **p: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(x) for x in c.execute(
                sa.text(self._SQL.format(where=where)), {"c": self.chain_id, **p}).mappings()]

    def top_tokens(self, n: int, *, window: str = "24h", sort: str = "volume"
                   ) -> tuple[list[TokenInfo], dict]:
        with self.engine.connect() as c:
            newest = c.execute(sa.text(
                "SELECT max(captured_at) FROM token_market_snapshots WHERE chain_id = :c"),
                {"c": self.chain_id}).scalar()
        if newest is None or (datetime.now(UTC) - newest).total_seconds() > self.max_age_s:
            return [], {}
        rows = self._rows("s.captured_at = :t ORDER BY s.vol_24h_hype DESC NULLS LAST, "
                          "s.token LIMIT :n", t=newest, n=int(n))
        synced = rows[0]["source_synced_at"] if rows else None
        meta = {"synced": {"at": int((synced or newest).timestamp() * 1000),
                           "head": rows[0]["source_head"] if rows else None},
                "totals": {"tokens": len(rows)}, "captured_at": newest}
        return [self._info(r) for r in rows], meta

    def token(self, address: str) -> TokenInfo | None:
        rows = self._rows("s.token = :a ORDER BY s.captured_at DESC LIMIT 1", a=address)
        return self._info(rows[0]) if rows else None

    def token_detail(self, address: str, range_: str = "24h") -> dict | None:
        return None        # candles come from Agora live; the website never calls it


class PublicReports(Reports):
    def _outbox_counts(self, c: Connection) -> dict[str, int]:
        return {x["status"]: x["n"] for x in self._all(c, "SELECT status, n FROM outbox_status")}

    # --- Projects / tokens table --------------------------------------------------------------
    def token_table(self, tr: TimeRange) -> dict[str, Any]:
        """Every token tracked wallets touched in the window, with flows and the latest
        stored market snapshot. Filtering, sorting and paging happen on this list."""
        with self.ro() as c:
            uni = self.universe(c, tr)
            flows = self.token_flows(c, tr, uni.qualified)
            self._decorate(c, flows)
            toks = [r["token"] for r in flows]
            ever = {r["token"]: r for r in self._all(c, """
                SELECT token, min(block_time) AS first_ever, max(block_time) AS last_ever
                FROM trades WHERE chain_id = :c AND token = ANY(:t) GROUP BY token""", t=toks)}
            snap = {r["token"]: r for r in self._all(c, """
                SELECT DISTINCT ON (token) token, captured_at, price_hype, mcap_hype,
                       change_1h, change_24h
                FROM token_market_snapshots
                WHERE chain_id = :c AND token = ANY(:t) AND captured_at >= :since
                ORDER BY token, captured_at DESC""", t=toks,
                since=datetime.now(UTC) - timedelta(days=2))}
        for r in flows:
            e, s = ever.get(r["token"], {}), snap.get(r["token"])
            r["first_ever"], r["last_ever"] = e.get("first_ever"), e.get("last_ever")
            r["new_in_window"] = bool(e.get("first_ever") and e["first_ever"] >= tr.start)
            r["market"] = None if not s else {
                "at": s["captured_at"],
                "price_hype": s["price_hype"], "mcap_hype": s["mcap_hype"],
                "change_1h": s["change_1h"], "change_24h": s["change_24h"]}
        return {"rows": flows, "universe": uni, "range": tr}

    def snapshot_series(self, token: str, tr: TimeRange, limit: int = 600
                        ) -> list[dict[str, Any]]:
        with self.ro() as c:
            return self._all(c, """
                SELECT captured_at, price_hype, mcap_hype FROM token_market_snapshots
                WHERE chain_id = :c AND token = :t AND captured_at >= :s AND captured_at < :e
                ORDER BY captured_at LIMIT :n""", t=token, s=tr.start, e=tr.end, n=int(limit))

    def snapshot_history(self, limit: int = 12) -> list[dict[str, Any]]:
        with self.ro() as c:
            return self._all(c, """
                SELECT id, captured_at, status, complete, row_count, accepted_count
                FROM leaderboard_snapshots WHERE chain_id = :c
                ORDER BY captured_at DESC, id DESC LIMIT :n""", n=int(limit))

    def is_public_wallet(self, wallet: str) -> bool:
        with self.ro() as c:
            r = self._one(c, """
                SELECT EXISTS (SELECT 1 FROM wallets WHERE chain_id = :c AND address = :a)
                    OR EXISTS (SELECT 1 FROM trades WHERE chain_id = :c AND wallet = :a)
                    OR EXISTS (SELECT 1 FROM leaderboard_snapshot_entries WHERE address = :a)
                    AS ok""", a=wallet)
        return bool(r and r["ok"])

    def first_trade(self, token: str) -> dict[str, Any]:
        with self.ro() as c:
            return self._one(c, """SELECT count(*) AS n, min(block_time) AS first_at,
                                          max(block_time) AS last_at
                                   FROM trades WHERE chain_id = :c AND token = :t""",
                             t=token) or {}


def hype_str(v: Decimal | float | None, places: int = 2) -> str:
    return "–" if v is None else f"{float(v):,.{places}f}"
