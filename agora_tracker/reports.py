"""Read-only analytics over what the tracker has persisted (plus Agora's live market data).

Population rules, applied everywhere and stated in every view:
  * "Tracked wallets" = wallets the tracker followed when the trade happened: Agora's
    top-N PnL leaderboard plus the manual / Telegram watchlist. Not chain-wide.
  * "Qualified" = in the latest accepted leaderboard snapshot taken before the window ended
    with Agora-reported PnL > ui.qualified_min_pnl. When no snapshot exists the result is
    labelled "unscored" and every tracked wallet counts.
  * "Verified" HYPE = trades with confidence 'high' (HYPE leg measured on-chain).
    'estimated' amounts are reported next to it, never summed into it. Transfers are never
    purchases. Each (tx, wallet, token) leg exists once (uq_trades_leg), so nothing is
    double counted.
  * Agora-reported PnL and our realized PnL (realized_pnl, status 'measured') are separate
    numbers and are never mixed.
  * Market data (price, market cap, chain-wide volume) is Agora's; it is labelled with its
    source and time. Returns over past windows come from stored snapshots or Agora candles,
    never from a "24h change" applied to another window.

All SQL is parameterized, time-bounded and LIMITed, and runs in READ ONLY transactions with
a statement timeout.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from .config import UiConfig
from .market import AgoraMarket, TokenInfo, candles
from .timerange import TimeRange

UTC = timezone.utc
log = logging.getLogger(__name__)
WEI = Decimal(10) ** 18
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
AGORA_WINDOWS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}
MAX_TOKENS = 5000


def hype(wei: Any) -> Decimal:
    """wei (int / Decimal / None) -> HYPE as an exact Decimal."""
    if wei is None:
        return Decimal(0)
    return Decimal(wei) / WEI


def pct_change(cur: Decimal | float, prev: Decimal | float) -> float | None:
    if not prev:
        return None
    return (float(cur) - float(prev)) / float(prev)


@dataclass
class Universe:
    snapshot_id: int | None
    captured_at: datetime | None
    basis: str                                   # "leaderboard" | "unscored"
    ranks: dict[str, int] = field(default_factory=dict)
    pnl: dict[str, Decimal] = field(default_factory=dict)
    qualified: list[str] = field(default_factory=list)
    note: str = ""


class Reports:
    def __init__(self, engine: Engine, chain_id: int, ui: UiConfig, *,
                 value_unit: str = "HYPE", market: AgoraMarket | None = None,
                 collector_interval_s: float = 600.0):
        self.engine, self.chain_id, self.ui = engine, chain_id, ui
        self.unit = value_unit
        self.market = market
        self.collector_interval_s = collector_interval_s

    # --- plumbing ------------------------------------------------------------------------
    @contextmanager
    def ro(self) -> Iterator[Connection]:
        with self.engine.connect() as c:
            c.execute(sa.text("SET TRANSACTION READ ONLY"))
            c.execute(sa.text(f"SET LOCAL statement_timeout = {int(self.ui.query_timeout_ms)}"))
            try:
                yield c
            finally:
                c.rollback()

    def _all(self, c: Connection, sql: str, **p: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in c.execute(sa.text(sql), {"c": self.chain_id, **p}).mappings()]

    def _one(self, c: Connection, sql: str, **p: Any) -> dict[str, Any] | None:
        r = c.execute(sa.text(sql), {"c": self.chain_id, **p}).mappings().first()
        return dict(r) if r else None

    def _kv(self, c: Connection, key: str) -> Any:
        r = c.execute(sa.text("SELECT value FROM service_state WHERE key = :k"), {"k": key}).first()
        if r is None:
            return None
        v = r[0].get("v") if isinstance(r[0], dict) else r[0]   # Store.set_kv wraps {"v": ...}
        if isinstance(v, str):
            try:
                return json.loads(v)
            except ValueError:
                return v
        return v

    # --- metadata --------------------------------------------------------------------------
    def token_names(self, c: Connection, tokens: list[str]) -> dict[str, dict[str, Any]]:
        if not tokens:
            return {}
        rows = self._all(c, """
            SELECT a.address, m.symbol AS chain_symbol, m.decimals, p.symbol, p.name,
                   p.launchpad, p.launched_at
            FROM unnest(CAST(:t AS text[])) AS a(address)
            LEFT JOIN token_metadata m ON m.chain_id = :c AND m.address = a.address
            LEFT JOIN token_profiles p ON p.chain_id = :c AND p.address = a.address""",
            t=list(tokens))
        out = {}
        for r in rows:
            out[r["address"]] = {
                "symbol": r["symbol"] or r["chain_symbol"] or r["address"][:8],
                "name": r["name"], "decimals": r["decimals"] if r["decimals"] is not None else 18,
                "launchpad": r["launchpad"], "launched_at": r["launched_at"]}
        return out

    def universe(self, c: Connection, tr: TimeRange) -> Universe:
        snap = self._one(c, """
            SELECT id, captured_at FROM leaderboard_snapshots
            WHERE chain_id = :c AND status IN ('ok','imported') AND captured_at < :e
            ORDER BY captured_at DESC, id DESC LIMIT 1""", e=tr.end)
        note = ""
        if snap is None:
            snap = self._one(c, """
                SELECT id, captured_at FROM leaderboard_snapshots
                WHERE chain_id = :c AND status IN ('ok','imported')
                ORDER BY captured_at ASC, id ASC LIMIT 1""")
            if snap is not None:
                note = "no leaderboard snapshot before this window; using the first one after it"
        if snap is None:
            return Universe(None, None, "unscored",
                            note="no Agora leaderboard snapshot: wallets are unscored")
        rows = self._all(c, """SELECT address, rank, pnl_usd FROM leaderboard_snapshot_entries
                               WHERE snapshot_id = :s""", s=snap["id"])
        ranks = {r["address"]: r["rank"] for r in rows}
        pnl = {r["address"]: r["pnl_usd"] for r in rows if r["pnl_usd"] is not None}
        thr = Decimal(str(self.ui.qualified_min_pnl))
        qualified = sorted(a for a, v in pnl.items() if v > thr)
        return Universe(snap["id"], snap["captured_at"], "leaderboard", ranks, pnl, qualified,
                        note)

    # --- coverage ----------------------------------------------------------------------------
    def coverage(self, tr: TimeRange, now: datetime | None = None) -> dict[str, Any]:
        """How much of [start, end) the tracker actually observed, and how fresh it is."""
        now = now or datetime.now(UTC)
        with self.ro() as c:
            first = self._one(c, """
                SELECT (SELECT min(block_time) FROM trades WHERE chain_id = :c) AS first_trade,
                       (SELECT min(captured_at) FROM leaderboard_snapshots
                         WHERE chain_id = :c AND status IN ('ok','imported')) AS first_snap,
                       (SELECT block_number FROM indexer_checkpoints
                         WHERE chain_id = :c AND name = 'indexer') AS cursor,
                       (SELECT updated_at FROM indexer_checkpoints
                         WHERE chain_id = :c AND name = 'indexer') AS cursor_at,
                       (SELECT max(block_time) FROM indexed_blocks WHERE chain_id = :c)
                         AS last_block_time""") or {}
            heads = self._kv(c, "rpc_heads") or {}
            hb = self._kv(c, "heartbeat")
        head = heads.get("best") if isinstance(heads, dict) else None
        cursor = first.get("cursor")
        lag = (head - cursor) if (head is not None and cursor is not None) else None
        hb_age = int(now.timestamp()) - int(hb) if hb not in (None, "") else None
        # The cursor's own block time is not stored; when the indexer is at the head and
        # alive, it has seen everything up to its last checkpoint update.
        indexed_through = None
        if first.get("cursor_at") is not None and lag is not None and lag <= 120:
            indexed_through = first["cursor_at"]
        elif first.get("last_block_time") is not None:
            indexed_through = first["last_block_time"]
        starts = [x for x in (first.get("first_trade"), first.get("first_snap")) if x]
        tracking_since = min(starts) if starts else None
        warnings: list[str] = []
        status = "full"
        if tracking_since is None:
            status = "none"
            warnings.append("nothing recorded yet")
        else:
            if tr.end <= tracking_since:
                status = "none"
                warnings.append(f"tracking began {tracking_since:%Y-%m-%d %H:%M} UTC, "
                                "after this window")
            elif tr.start < tracking_since:
                status = "partial"
                warnings.append(f"tracking began {tracking_since:%Y-%m-%d %H:%M} UTC; "
                                "earlier activity was not observed")
            if indexed_through is not None and indexed_through < tr.end - timedelta(minutes=2):
                status = "partial" if status == "full" else status
                warnings.append(f"indexed only up to {indexed_through:%Y-%m-%d %H:%M} UTC")
        if lag is not None and lag > 120:
            warnings.append(f"indexer {lag} blocks behind the chain head")
        if hb_age is not None and hb_age > 180:
            warnings.append(f"indexer heartbeat {hb_age}s old")
        return {"status": status, "warnings": warnings, "cursor": cursor, "head": head,
                "lag": lag, "heartbeat_age_s": hb_age, "tracking_since": tracking_since,
                "indexed_through": indexed_through}

    # --- per-token flows ----------------------------------------------------------------------
    def token_flows(self, c: Connection, tr: TimeRange, qualified: list[str],
                    token: str | None = None) -> list[dict[str, Any]]:
        rows = self._all(c, f"""
            SELECT token,
              count(*) AS legs,
              count(DISTINCT wallet) AS wallets,
              count(DISTINCT wallet) FILTER (WHERE kind='BUY'  AND confidence='high') AS buyers,
              count(DISTINCT wallet) FILTER (WHERE kind='SELL' AND confidence='high') AS sellers,
              count(DISTINCT wallet) FILTER (WHERE kind='BUY'  AND confidence='high'
                                               AND wallet = ANY(:q)) AS q_buyers,
              count(DISTINCT wallet) FILTER (WHERE kind='SELL' AND confidence='high'
                                               AND wallet = ANY(:q)) AS q_sellers,
              coalesce(sum(hype_wei) FILTER (WHERE kind='BUY'  AND confidence='high'), 0)
                AS buy_wei,
              coalesce(sum(hype_wei) FILTER (WHERE kind='SELL' AND confidence='high'), 0)
                AS sell_wei,
              coalesce(sum(hype_wei) FILTER (WHERE kind='BUY'  AND confidence='high'
                                               AND wallet = ANY(:q)), 0) AS q_buy_wei,
              coalesce(sum(hype_wei) FILTER (WHERE kind='SELL' AND confidence='high'
                                               AND wallet = ANY(:q)), 0) AS q_sell_wei,
              coalesce(sum(hype_wei) FILTER (WHERE kind='BUY'  AND confidence='estimated'), 0)
                AS buy_est_wei,
              coalesce(sum(hype_wei) FILTER (WHERE kind='SELL' AND confidence='estimated'), 0)
                AS sell_est_wei,
              count(*) FILTER (WHERE kind='BUY'  AND confidence='high') AS buys,
              count(*) FILTER (WHERE kind='SELL' AND confidence='high') AS sells,
              count(*) FILTER (WHERE kind IN ('BUY','SELL') AND confidence='estimated')
                AS est_legs,
              count(*) FILTER (WHERE kind IN ('TRANSFER_IN','TRANSFER_OUT')) AS transfers,
              count(*) FILTER (WHERE confidence IN ('unknown','legacy')
                                 OR kind IN ('SWAP','MULTI')) AS other_legs,
              count(DISTINCT width_bucket(extract(epoch FROM block_time), :s_ep, :e_ep, 4))
                FILTER (WHERE kind='BUY' AND confidence='high' AND wallet = ANY(:q))
                AS q_buy_intervals,
              min(block_time) AS first_at, max(block_time) AS last_at
            FROM trades
            WHERE chain_id = :c AND block_time >= :s AND block_time < :e
              {"AND token = :tok" if token else ""}
            GROUP BY token
            ORDER BY token
            LIMIT {MAX_TOKENS}""", q=qualified, s=tr.start, e=tr.end,
            s_ep=tr.start.timestamp(), e_ep=tr.end.timestamp(), tok=token)
        for r in rows:
            for k in ("buy_wei", "sell_wei", "q_buy_wei", "q_sell_wei", "buy_est_wei",
                      "sell_est_wei"):
                r[k.replace("_wei", "")] = hype(r[k])
            r["net"] = r["buy"] - r["sell"]
            r["q_net"] = r["q_buy"] - r["q_sell"]
        return rows

    def _decorate(self, c: Connection, rows: list[dict]) -> None:
        names = self.token_names(c, [r["token"] for r in rows])
        for r in rows:
            r.update({k: v for k, v in names.get(r["token"], {}).items() if k not in r})

    # --- Market Overview ----------------------------------------------------------------------
    def _totals(self, flows: list[dict]) -> dict[str, Any]:
        t: dict[str, Any] = {k: sum((r[k] for r in flows), Decimal(0)) for k in
                             ("buy", "sell", "buy_est", "sell_est", "q_buy", "q_sell")}
        for k in ("legs", "buys", "sells", "est_legs", "transfers", "other_legs"):
            t[k] = sum(r[k] for r in flows)
        t["tokens"] = len(flows)
        t["net"] = t["buy"] - t["sell"]
        t["volume"] = t["buy"] + t["sell"]
        return t

    def market_overview(self, tr: TimeRange) -> dict[str, Any]:
        prev = tr.previous()
        with self.ro() as c:
            uni = self.universe(c, tr)
            cur = self.token_flows(c, tr, uni.qualified)
            old = self.token_flows(c, prev, uni.qualified)
            wallets = self._one(c, """
                SELECT count(DISTINCT wallet) AS wallets, count(DISTINCT tx_hash) AS txs
                FROM trades WHERE chain_id = :c AND block_time >= :s AND block_time < :e""",
                s=tr.start, e=tr.end) or {}
            prev_wallets = self._one(c, """
                SELECT count(DISTINCT wallet) AS wallets FROM trades
                WHERE chain_id = :c AND block_time >= :s AND block_time < :e""",
                s=prev.start, e=prev.end) or {}
            new_tokens = self._one(c, """
                SELECT count(DISTINCT t.token) AS n FROM trades t
                WHERE t.chain_id = :c AND t.block_time >= :s AND t.block_time < :e
                  AND NOT EXISTS (SELECT 1 FROM trades p WHERE p.chain_id = t.chain_id
                                  AND p.token = t.token AND p.block_time < :s)""",
                s=tr.start, e=tr.end) or {}
            signals = self._one(c, """
                SELECT count(*) AS n FROM signals
                WHERE chain_id = :c AND created_at >= :s AND created_at < :e""",
                s=tr.start, e=tr.end) or {}
            pads = self._all(c, """
                SELECT coalesce(p.launchpad, 'unknown') AS launchpad,
                       count(*) AS legs, count(DISTINCT t.token) AS tokens,
                       coalesce(sum(t.hype_wei) FILTER (WHERE t.confidence = 'high'
                                AND t.kind IN ('BUY','SELL')), 0) AS vol_wei
                FROM trades t LEFT JOIN token_profiles p
                  ON p.chain_id = t.chain_id AND p.address = t.token
                WHERE t.chain_id = :c AND t.block_time >= :s AND t.block_time < :e
                GROUP BY 1 ORDER BY vol_wei DESC, legs DESC LIMIT 8""", s=tr.start, e=tr.end)
            self._decorate(c, cur)
        tot, ptot = self._totals(cur), self._totals(old)
        tot["wallets"], ptot["wallets"] = wallets.get("wallets", 0), prev_wallets.get("wallets", 0)
        tot["txs"] = wallets.get("txs", 0)
        tot["new_tokens"] = new_tokens.get("n", 0)
        tot["signals"] = signals.get("n", 0)
        for p in pads:
            p["volume"] = hype(p.pop("vol_wei"))
        by_vol = sorted(cur, key=lambda r: (-(r["buy"] + r["sell"]), r["token"]))
        top3 = sum((r["buy"] + r["sell"] for r in by_vol[:3]), Decimal(0))
        concentration = float(top3 / tot["volume"]) if tot["volume"] else None
        prev_by_tok = {r["token"]: r for r in old}
        rising = []
        for r in cur:
            pv = prev_by_tok.get(r["token"])
            pb = pv["buyers"] if pv else 0
            if r["buyers"] >= 2 and r["buyers"] > pb:
                rising.append({**r, "prev_buyers": pb})
        rising.sort(key=lambda r: (-(r["buyers"] - r["prev_buyers"]), -r["buy"], r["token"]))
        selling = [r for r in cur if r["sell"] > r["buy"] and r["sellers"] >= 1]
        selling.sort(key=lambda r: (r["net"], r["token"]))
        multi_q = [r for r in cur if r["q_buyers"] >= 2]
        multi_q.sort(key=lambda r: (-r["q_buyers"], -r["q_buy"], r["token"]))
        out = {"range": tr, "previous": prev, "universe": uni, "totals": tot, "prev": ptot,
               "launchpads": pads, "top_tokens": by_vol[:5], "concentration_top3": concentration,
               "rising": rising[:5], "selling": selling[:5], "multi_qualified": multi_q[:5]}
        out["summary"] = summarize(out)
        return out

    # --- Smart Money ----------------------------------------------------------------------------
    SM_MODES = ("net", "buyers", "new", "repeat", "exits", "clusters")

    def smart_money(self, tr: TimeRange, mode: str = "net") -> dict[str, Any]:
        if mode not in self.SM_MODES:
            mode = "net"
        with self.ro() as c:
            uni = self.universe(c, tr)
            note = ""
            if mode == "clusters":
                rows = self._all(c, """
                    SELECT id, token, kind, window_start, window_end, created_at, details
                    FROM signals WHERE chain_id = :c AND created_at >= :s AND created_at < :e
                    ORDER BY created_at DESC, id DESC LIMIT 100""", s=tr.start, e=tr.end)
                for r in rows:
                    d = r["details"] or {}
                    r["n_wallets"] = len(d.get("buyers") or d.get("wallets") or [])
                self._decorate(c, rows)
                return {"mode": mode, "universe": uni, "rows": rows, "range": tr,
                        "note": "cluster signals raised by the tracker (measured buys only)"}
            pool = uni.qualified if uni.basis == "leaderboard" else self._tracked(c, tr)
            if mode == "new":
                newcomers = self._newcomers(c, tr)
                pool = sorted(set(newcomers) & set(pool)) if uni.basis == "leaderboard" \
                    else newcomers
                note = (f"{len(newcomers)} wallets entered the Agora top list during this "
                        "window (the very first snapshot is excluded)")
            flows = self.token_flows(c, tr, pool)
            if mode == "net":
                rows = [r for r in flows if r["q_buyers"] > 0]
                rows.sort(key=lambda r: (-r["q_net"], -r["q_buyers"], r["token"]))
            elif mode in ("buyers", "new"):
                rows = [r for r in flows if r["q_buyers"] > 0]
                rows.sort(key=lambda r: (-r["q_buyers"], -r["q_buy"], r["token"]))
            elif mode == "repeat":
                rows = [r for r in flows if r["q_buy_intervals"] >= 2]
                rows.sort(key=lambda r: (-r["q_buy_intervals"], -r["q_buyers"], -r["q_buy"],
                                         r["token"]))
                note = "bought by qualified wallets in at least 2 of 4 equal sub-intervals"
            else:  # exits
                rows = [r for r in flows if r["q_sellers"] > 0 and r["q_sell"] > r["q_buy"]]
                rows.sort(key=lambda r: (r["q_net"], -r["q_sellers"], r["token"]))
                note = "qualified wallets selling more than buying (verified HYPE)"
            self._decorate(c, rows)
        return {"mode": mode, "universe": uni, "rows": rows, "range": tr, "note": note}

    def _tracked(self, c: Connection, tr: TimeRange) -> list[str]:
        return [r["wallet"] for r in self._all(c, """
            SELECT DISTINCT wallet FROM trades
            WHERE chain_id = :c AND block_time >= :s AND block_time < :e LIMIT 5000""",
            s=tr.start, e=tr.end)]

    def _newcomers(self, c: Connection, tr: TimeRange) -> list[str]:
        return [r["address"] for r in self._all(c, """
            WITH firsts AS (
              SELECT e.address, min(s.captured_at) AS first_at
              FROM leaderboard_snapshot_entries e
              JOIN leaderboard_snapshots s ON s.id = e.snapshot_id
              WHERE s.chain_id = :c AND s.status IN ('ok','imported') AND s.captured_at < :e
              GROUP BY e.address)
            SELECT address FROM firsts
            WHERE first_at >= :s AND first_at > (
              SELECT min(captured_at) FROM leaderboard_snapshots
              WHERE chain_id = :c AND status IN ('ok','imported'))
            ORDER BY address LIMIT 1000""", s=tr.start, e=tr.end)]

    # --- token drill-down ----------------------------------------------------------------------
    def token_buyers(self, token: str, tr: TimeRange) -> dict[str, Any]:
        with self.ro() as c:
            uni = self.universe(c, tr)
            rows = self._all(c, """
                SELECT wallet,
                  count(*) FILTER (WHERE kind='BUY'  AND confidence='high') AS buys,
                  count(*) FILTER (WHERE kind='SELL' AND confidence='high') AS sells,
                  coalesce(sum(hype_wei) FILTER (WHERE kind='BUY'  AND confidence='high'), 0)
                    AS buy_wei,
                  coalesce(sum(hype_wei) FILTER (WHERE kind='SELL' AND confidence='high'), 0)
                    AS sell_wei,
                  coalesce(sum(hype_wei) FILTER (WHERE kind IN ('BUY','SELL')
                                                   AND confidence='estimated'), 0) AS est_wei,
                  count(*) FILTER (WHERE kind IN ('TRANSFER_IN','TRANSFER_OUT')) AS transfers,
                  min(block_time) FILTER (WHERE kind='BUY') AS first_buy,
                  max(block_time) AS last_at
                FROM trades WHERE chain_id = :c AND token = :t
                  AND block_time >= :s AND block_time < :e
                GROUP BY wallet ORDER BY buy_wei DESC, wallet LIMIT 500""",
                t=token, s=tr.start, e=tr.end)
            names = self.token_names(c, [token])
        for r in rows:
            r["buy"], r["sell"], r["est"] = hype(r["buy_wei"]), hype(r["sell_wei"]), hype(r["est_wei"])
            r["rank"] = uni.ranks.get(r["wallet"])
            r["agora_pnl"] = uni.pnl.get(r["wallet"])
            r["qualified"] = r["wallet"] in uni.qualified
        return {"token": token, "meta": names.get(token, {}), "rows": rows, "universe": uni,
                "range": tr}

    def trades(self, tr: TimeRange, *, wallet: str | None = None, token: str | None = None,
               limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Transaction-level evidence, newest first (deterministic order)."""
        cond = ""
        if wallet:
            cond += " AND t.wallet = :w"
        if token:
            cond += " AND t.token = :tok"
        with self.ro() as c:
            rows = self._all(c, f"""
                SELECT t.id, t.chain_id, t.block_number, t.block_time, t.tx_hash, t.wallet,
                       t.token, t.kind, t.confidence, t.hype_method, t.qty, t.hype_wei,
                       t.counter_token, t.counter_qty
                FROM trades t
                WHERE t.chain_id = :c AND t.block_time >= :s AND t.block_time < :e {cond}
                ORDER BY t.block_time DESC, t.block_number DESC, t.id DESC
                LIMIT :lim OFFSET :off""", s=tr.start, e=tr.end, w=wallet, tok=token,
                lim=int(limit), off=int(offset))
            names = self.token_names(c, sorted({r["token"] for r in rows}))
        for r in rows:
            m = names.get(r["token"], {})
            r["symbol"], r["decimals"] = m.get("symbol"), m.get("decimals", 18)
            r["hype"] = hype(r["hype_wei"]) if r["hype_wei"] is not None else None
            r["qty_units"] = Decimal(r["qty"]) / (Decimal(10) ** int(r["decimals"] or 18))
        return rows

    def token_detail(self, token: str, tr: TimeRange) -> dict[str, Any]:
        with self.ro() as c:
            uni = self.universe(c, tr)
            flows = self.token_flows(c, tr, uni.qualified, token=token)
            names = self.token_names(c, [token])
            seen = self._one(c, """SELECT count(*) AS n, min(block_time) AS first_at
                                   FROM trades WHERE chain_id = :c AND token = :t""", t=token)
        info = self.market.token(token) if self.market else None
        perf = self.token_performance(token, tr)
        return {"token": token, "meta": names.get(token, {}), "flow": flows[0] if flows else None,
                "universe": uni, "range": tr, "info": info, "perf": perf,
                "recent": self.trades(tr, token=token, limit=8), "seen": seen or {}}

    # --- Top Traders ------------------------------------------------------------------------------
    TT_SORTS = ("agora", "realized", "active", "consistent", "buyers", "movers")

    def top_traders(self, tr: TimeRange, sort: str = "agora", limit: int = 100
                    ) -> dict[str, Any]:
        if sort not in self.TT_SORTS and sort != "all":
            sort = "agora"
        note = ""
        with self.ro() as c:
            uni = self.universe(c, tr)
            act = {r["wallet"]: r for r in self._all(c, """
                SELECT wallet, count(*) AS legs,
                  count(*) FILTER (WHERE kind='BUY'  AND confidence='high') AS buys,
                  count(*) FILTER (WHERE kind='SELL' AND confidence='high') AS sells,
                  count(DISTINCT token) FILTER (WHERE kind='BUY' AND confidence='high')
                    AS tokens_bought,
                  coalesce(sum(hype_wei) FILTER (WHERE kind='BUY' AND confidence='high'), 0)
                    AS buy_wei,
                  max(block_time) AS last_at
                FROM trades WHERE chain_id = :c AND block_time >= :s AND block_time < :e
                GROUP BY wallet LIMIT 5000""", s=tr.start, e=tr.end)}
            real = {r["wallet"]: r for r in self._all(c, """
                SELECT r.wallet,
                  count(*) FILTER (WHERE r.status = 'measured') AS n_measured,
                  count(*) FILTER (WHERE r.status = 'measured' AND r.pnl_wei > 0) AS wins,
                  count(*) FILTER (WHERE r.status <> 'measured') AS n_other,
                  coalesce(sum(r.pnl_wei) FILTER (WHERE r.status = 'measured'), 0) AS pnl_wei,
                  coalesce(sum(r.pnl_wei) FILTER (WHERE r.status = 'estimated'), 0)
                    AS pnl_est_wei
                FROM realized_pnl r JOIN trades t ON t.id = r.trade_id
                WHERE r.chain_id = :c AND t.block_time >= :s AND t.block_time < :e
                GROUP BY r.wallet LIMIT 5000""", s=tr.start, e=tr.end)}
            movers: dict[str, dict] = {}
            if sort == "movers":
                start_snap = self._one(c, """
                    SELECT id, captured_at FROM leaderboard_snapshots
                    WHERE chain_id = :c AND status IN ('ok','imported') AND captured_at <= :s
                    ORDER BY captured_at DESC, id DESC LIMIT 1""", s=tr.start) or self._one(c, """
                    SELECT id, captured_at FROM leaderboard_snapshots
                    WHERE chain_id = :c AND status IN ('ok','imported') AND captured_at >= :s
                      AND captured_at < :e ORDER BY captured_at, id LIMIT 1""",
                    s=tr.start, e=tr.end)
                if start_snap and uni.snapshot_id and start_snap["id"] != uni.snapshot_id:
                    old = {r["address"]: r["rank"] for r in self._all(c, """
                        SELECT address, rank FROM leaderboard_snapshot_entries
                        WHERE snapshot_id = :s""", s=start_snap["id"])}
                    for a, rk in uni.ranks.items():
                        movers[a] = {"old_rank": old.get(a), "new_rank": rk,
                                     "delta": (old[a] - rk) if a in old else None}
                    note = (f"rank change from the snapshot of "
                            f"{start_snap['captured_at']:%Y-%m-%d %H:%M} UTC")
                else:
                    note = "needs two different leaderboard snapshots inside or around the window"
        addrs = set(uni.ranks) | set(act) | set(real)
        rows = []
        for a in addrs:
            r_act, r_real = act.get(a, {}), real.get(a, {})
            rows.append({
                "wallet": a, "rank": uni.ranks.get(a), "agora_pnl": uni.pnl.get(a),
                "legs": r_act.get("legs", 0), "buys": r_act.get("buys", 0),
                "sells": r_act.get("sells", 0), "tokens_bought": r_act.get("tokens_bought", 0),
                "buy": hype(r_act.get("buy_wei")), "last_at": r_act.get("last_at"),
                "realized": hype(r_real.get("pnl_wei")),
                "realized_est": hype(r_real.get("pnl_est_wei")),
                "n_measured": r_real.get("n_measured", 0), "wins": r_real.get("wins", 0),
                "n_other": r_real.get("n_other", 0), **movers.get(a, {})})
        big = Decimal("1e40")
        if sort == "agora":
            rows = [r for r in rows if r["rank"] is not None]
            rows.sort(key=lambda r: (r["rank"], r["wallet"]))
            note = note or "Agora-reported PnL (their accounting, all-time)"
        elif sort == "realized":
            rows = [r for r in rows if r["n_measured"]]
            rows.sort(key=lambda r: (-r["realized"], r["wallet"]))
            note = "realized PnL we measured on-chain inside this window only"
        elif sort == "active":
            rows = [r for r in rows if r["legs"]]
            rows.sort(key=lambda r: (-r["legs"], r["wallet"]))
        elif sort == "consistent":
            rows = [r for r in rows if r["n_measured"] >= 3]
            rows.sort(key=lambda r: (-(r["wins"] / r["n_measured"]), -r["realized"], r["wallet"]))
            note = "win rate of measured closing trades in this window, min 3 per wallet"
        elif sort == "buyers":
            rows = [r for r in rows if r["tokens_bought"]]
            rows.sort(key=lambda r: (-r["tokens_bought"], -r["buy"], r["wallet"]))
        elif sort == "all":   # exports: everyone with a rank or activity, rank first
            rows.sort(key=lambda r: (r["rank"] if r["rank"] is not None else 10**9,
                                     -r["legs"], r["wallet"]))
        else:
            rows = [r for r in rows if r.get("delta") is not None or
                    (r.get("new_rank") is not None and r.get("old_rank") is None and movers)]
            rows.sort(key=lambda r: (-(r["delta"] if r.get("delta") is not None else big),
                                     r["wallet"]))
        return {"sort": sort, "rows": rows[:limit], "universe": uni, "range": tr, "note": note}

    # --- wallet ----------------------------------------------------------------------------------
    def wallet_detail(self, wallet: str, tr: TimeRange) -> dict[str, Any]:
        with self.ro() as c:
            uni = self.universe(c, tr)
            watch = self._all(c, """
                SELECT source, label FROM wallet_watchlist
                WHERE chain_id = :c AND address = :w AND removed_at IS NULL ORDER BY source""",
                w=wallet)
            act = self._one(c, """
                SELECT count(*) AS legs,
                  count(*) FILTER (WHERE kind='BUY'  AND confidence='high') AS buys,
                  count(*) FILTER (WHERE kind='SELL' AND confidence='high') AS sells,
                  count(*) FILTER (WHERE kind IN ('BUY','SELL') AND confidence='estimated')
                    AS est_legs,
                  count(*) FILTER (WHERE kind IN ('TRANSFER_IN','TRANSFER_OUT')) AS transfers,
                  coalesce(sum(hype_wei) FILTER (WHERE kind='BUY'  AND confidence='high'), 0)
                    AS buy_wei,
                  coalesce(sum(hype_wei) FILTER (WHERE kind='SELL' AND confidence='high'), 0)
                    AS sell_wei,
                  count(DISTINCT token) AS tokens
                FROM trades WHERE chain_id = :c AND wallet = :w
                  AND block_time >= :s AND block_time < :e""", w=wallet, s=tr.start, e=tr.end)
            last = self._one(c, """SELECT max(block_time) AS last_at, min(block_time) AS first_at,
                                          count(*) AS n FROM trades
                                   WHERE chain_id = :c AND wallet = :w""", w=wallet) or {}
            realized = {r["status"]: r for r in self._all(c, """
                SELECT r.status, count(*) AS n, coalesce(sum(r.pnl_wei), 0) AS pnl_wei
                FROM realized_pnl r JOIN trades t ON t.id = r.trade_id
                WHERE r.chain_id = :c AND r.wallet = :w
                  AND t.block_time >= :s AND t.block_time < :e
                GROUP BY r.status""", w=wallet, s=tr.start, e=tr.end)}
            tokens = self._all(c, """
                SELECT token, count(*) AS legs,
                  coalesce(sum(hype_wei) FILTER (WHERE kind='BUY'  AND confidence='high'), 0)
                    AS buy_wei,
                  coalesce(sum(hype_wei) FILTER (WHERE kind='SELL' AND confidence='high'), 0)
                    AS sell_wei
                FROM trades WHERE chain_id = :c AND wallet = :w
                  AND block_time >= :s AND block_time < :e
                GROUP BY token
                ORDER BY coalesce(sum(hype_wei) FILTER (WHERE kind IN ('BUY','SELL')
                                                         AND confidence='high'), 0) DESC,
                         count(*) DESC, token LIMIT 8""",
                w=wallet, s=tr.start, e=tr.end)
            positions = self._all(c, """
                SELECT token, qty, cost_wei, basis, opened_at, realized_wei, realized_est_wei
                FROM wallet_positions WHERE chain_id = :c AND wallet = :w AND qty > 0
                ORDER BY cost_wei DESC, token LIMIT 10""", w=wallet)
            names = self.token_names(c, sorted({r["token"] for r in tokens + positions}))
        for r in tokens:
            r.update(names.get(r["token"], {}))
            r["buy"], r["sell"] = hype(r["buy_wei"]), hype(r["sell_wei"])
        for r in positions:
            m = names.get(r["token"], {})
            r.update(m)
            r["cost"] = hype(r["cost_wei"])
            r["qty_units"] = Decimal(r["qty"]) / (Decimal(10) ** int(m.get("decimals", 18)))
        act = act or {}
        return {"wallet": wallet, "range": tr, "universe": uni, "watch": watch,
                "rank": uni.ranks.get(wallet), "agora_pnl": uni.pnl.get(wallet),
                "qualified": wallet in uni.qualified, "activity": act,
                "buy": hype(act.get("buy_wei")), "sell": hype(act.get("sell_wei")),
                "last": last, "tokens": tokens, "positions": positions,
                "realized": {k: {"n": v["n"], "pnl": hype(v["pnl_wei"])}
                             for k, v in realized.items()},
                "recent": self.trades(tr, wallet=wallet, limit=6)}

    # --- search ----------------------------------------------------------------------------------
    def search(self, q: str) -> dict[str, Any]:
        q = q.strip()
        if ADDR_RE.match(q):
            a = q.lower()
            with self.ro() as c:
                is_tok = self._one(c, """
                    SELECT (EXISTS (SELECT 1 FROM token_metadata WHERE chain_id = :c AND address = :a)
                         OR EXISTS (SELECT 1 FROM token_profiles WHERE chain_id = :c AND address = :a)
                         OR EXISTS (SELECT 1 FROM trades WHERE chain_id = :c AND token = :a LIMIT 1))
                         AS tok,
                           (EXISTS (SELECT 1 FROM wallets WHERE chain_id = :c AND address = :a)
                         OR EXISTS (SELECT 1 FROM trades WHERE chain_id = :c AND wallet = :a LIMIT 1))
                         AS wal""", a=a) or {}
            if is_tok.get("tok"):
                return {"kind": "token", "address": a}
            if is_tok.get("wal"):
                return {"kind": "wallet", "address": a}
            info = self.market.token(a) if self.market else None
            if info is not None and info.symbol:
                return {"kind": "token", "address": a}
            return {"kind": "wallet", "address": a, "unknown": True}
        sym = re.sub(r"[^A-Za-z0-9/._-]", "", q.lstrip("$"))[:20]
        if not sym:
            return {"kind": "none"}
        with self.ro() as c:
            rows = self._all(c, """
                SELECT address, symbol, name, launchpad, launched_at FROM (
                  SELECT p.address, p.symbol, p.name, p.launchpad, p.launched_at
                  FROM token_profiles p WHERE p.chain_id = :c AND lower(p.symbol) = lower(:s)
                  UNION
                  SELECT m.address, m.symbol, NULL, NULL, NULL
                  FROM token_metadata m WHERE m.chain_id = :c AND lower(m.symbol) = lower(:s)
                    AND NOT EXISTS (SELECT 1 FROM token_profiles p
                                    WHERE p.chain_id = m.chain_id AND p.address = m.address)
                ) x ORDER BY launched_at NULLS LAST, address LIMIT 10""", s=sym)
            if rows:
                act = {r["token"]: r["n"] for r in self._all(c, """
                    SELECT token, count(*) AS n FROM trades
                    WHERE chain_id = :c AND token = ANY(:t) GROUP BY token""",
                    t=[r["address"] for r in rows])}
                for r in rows:
                    r["tracked_legs"] = act.get(r["address"], 0)
                rows.sort(key=lambda r: (-r["tracked_legs"], r["address"]))
        if len(rows) == 1:
            return {"kind": "token", "address": rows[0]["address"]}
        if rows:
            return {"kind": "tokens", "symbol": sym, "rows": rows}
        return {"kind": "none", "symbol": sym}

    # --- health ----------------------------------------------------------------------------------
    def _outbox_counts(self, c: Connection) -> dict[str, int]:
        """Alert queue size per status. The public dashboard overrides this to read the
        message-free `outbox_status` view instead of the raw table."""
        return {x["status"]: x["n"] for x in self._all(c, """
            SELECT status, count(*) AS n FROM notification_outbox GROUP BY status""")}

    def health(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        t0 = time.monotonic()
        try:
            with self.ro() as c:
                c.execute(sa.text("SELECT 1"))
                db_ms = (time.monotonic() - t0) * 1000
                r = self._one(c, """
                    SELECT (SELECT count(*) FROM trades WHERE chain_id = :c) AS trades,
                      (SELECT count(*) FROM wallet_watchlist
                        WHERE chain_id = :c AND removed_at IS NULL) AS watch,
                      (SELECT count(DISTINCT address) FROM wallet_watchlist
                        WHERE chain_id = :c AND removed_at IS NULL) AS wallets,
                      (SELECT max(captured_at) FROM leaderboard_snapshots
                        WHERE chain_id = :c AND status = 'ok') AS lb_ok,
                      (SELECT max(captured_at) FROM leaderboard_snapshots
                        WHERE chain_id = :c) AS lb_any,
                      (SELECT max(captured_at) FROM token_market_snapshots
                        WHERE chain_id = :c) AS market_at,
                      (SELECT count(DISTINCT token) FROM token_market_snapshots
                        WHERE chain_id = :c AND captured_at > now() - interval '1 day')
                        AS market_tokens,
                      (SELECT min(captured_at) FROM token_market_snapshots
                        WHERE chain_id = :c) AS market_since,
                      (SELECT max(block_time) FROM trades WHERE chain_id = :c) AS last_trade
                    """) or {}
                outbox = self._outbox_counts(c)
                heads = self._kv(c, "rpc_heads") or {}
                ui_hb = self._kv(c, "ui_heartbeat")
                coll = self._kv(c, "collector_last") or {}
                disk = self._kv(c, "disk") or {}
                dbsize = c.execute(sa.text("SELECT pg_database_size(current_database())")).scalar()
                ok = True
        except Exception as e:  # noqa: BLE001 - the health page must render anyway
            log.warning("health query failed: %s", e)
            return {"db_ok": False, "db_error": type(e).__name__}
        cov = self.coverage(TimeRange(now - timedelta(hours=1), now, "1h"), now)
        warnings = list(cov["warnings"])
        if r.get("lb_ok") is None or now - r["lb_ok"] > timedelta(minutes=30):
            warnings.append("no successful leaderboard refresh in 30 min")
        if r.get("market_at") is None:
            warnings.append("no market snapshots yet")
        elif now - r["market_at"] > timedelta(seconds=self.collector_interval_s * 3):
            warnings.append("market snapshots are stale")
        if disk.get("total") and disk["free"] / disk["total"] < self.ui.disk_warn_pct:
            warnings.append(f"VPS disk {disk['free'] / 1e9:.1f} GB free "
                            f"({disk['free'] / disk['total']:.0%})")
        if outbox.get("dead"):
            warnings.append(f"{outbox['dead']} alerts dead-lettered")
        return {"db_ok": ok, "db_ms": db_ms, **r, "outbox": outbox, "heads": heads,
                "ui_heartbeat_age_s": (int(now.timestamp()) - int(ui_hb)) if ui_hb else None,
                "collector": coll, "coverage": cov, "warnings": warnings, "disk": disk,
                "db_bytes": dbsize}

    # --- Gainers / Losers (P2) -------------------------------------------------------------------
    def _filters(self, info: TokenInfo, vol: float | None, now: float) -> str | None:
        if vol is not None and vol < self.ui.min_volume_hype:
            return "volume"
        if info.mcap_hype is not None and info.mcap_hype < self.ui.min_mcap_hype:
            return "mcap"
        if info.created_at and now - info.created_at < self.ui.min_age_h * 3600:
            return "age"
        return None

    def performance(self, tr: TimeRange, direction: str = "up", metric: str = "price",
                    now: datetime | None = None) -> dict[str, Any]:
        res = self._performance(tr, direction, metric, now)
        res["min_volume"] = self.ui.min_volume_hype
        if metric != "flow" and res["rows"]:
            with self.ro() as c:
                uni = self.universe(c, tr)
                flows = {r["token"]: r for r in self.token_flows(c, tr, uni.qualified)}
            for r in res["rows"]:
                f = flows.get(r["token"])
                r["q_buyers"] = f["q_buyers"] if f else 0
                r["q_sellers"] = f["q_sellers"] if f else 0
        return res

    def _performance(self, tr: TimeRange, direction: str, metric: str,
                     now: datetime | None) -> dict[str, Any]:
        """Token price performance / volume acceleration / tracked flow for a window.

        Price returns come from, in order of preference:
          1. Agora's own change for that exact rolling window (1h/6h/24h/7d ending now);
          2. our stored snapshots, when they cover both ends of the window;
          3. Agora candles (windows starting within the last 7 days);
        otherwise the result says why it is unavailable."""
        now = now or datetime.now(UTC)
        sign = 1 if direction == "up" else -1
        res: dict[str, Any] = {"range": tr, "direction": direction, "metric": metric,
                               "rows": [], "excluded": {}, "source": None, "note": ""}
        if metric == "flow":
            with self.ro() as c:
                uni = self.universe(c, tr)
                flows = self.token_flows(c, tr, uni.qualified)
                rows = [r for r in flows if (r["q_net"] * sign) > 0]
                rows.sort(key=lambda r: (-(r["q_net"] * sign), r["token"]))
                self._decorate(c, rows)
            res.update(rows=rows, source="tracker", universe=uni,
                       note="verified net HYPE flow of qualified tracked wallets")
            return res
        live_window = tr.preset if (tr.preset in AGORA_WINDOWS and
                                    abs((now - tr.end).total_seconds()) < 120) else None
        if live_window and self.market is not None:
            toks, meta = self.market.top_tokens(200, window=live_window)
            if toks:
                return self._perf_live(res, toks, meta, live_window, sign, metric, now)
        if metric == "volume":
            res["note"] = ("volume acceleration needs Agora's rolling windows "
                           "(1h, 6h, 24h, 7d ending now)")
            return res
        snap = self._perf_snapshots(tr, sign)
        if snap["rows"]:
            res.update(snap)
            return res
        if self.market is not None and (now - tr.start) <= timedelta(days=7):
            cand = self._perf_candles(tr, sign, now)
            if cand["rows"] or cand.get("checked"):
                res.update(cand)
                return res
        res["note"] = snap.get("note") or "no price history covers this window yet"
        return res

    def _perf_live(self, res: dict, toks: list[TokenInfo], meta: dict, w: str, sign: int,
                   metric: str, now: datetime) -> dict:
        vol_k = {"1h": "vol_1h_hype", "6h": "vol_6h_hype", "24h": "vol_24h_hype",
                 "7d": "vol_7d_hype"}[w]
        ch_k = {"1h": "change_1h", "6h": "change_6h", "24h": "change_24h", "7d": "change_7d"}[w]
        # activity acceleration: this window's volume vs the average rate of a longer one
        base = {"1h": ("vol_24h_hype", 24), "6h": ("vol_24h_hype", 4),
                "24h": ("vol_7d_hype", 7), "7d": (None, None)}[w]
        rows: list[dict[str, Any]] = []
        excluded: dict[str, int] = {}
        ts = now.timestamp()
        for t in toks:
            vol = getattr(t, vol_k)
            why = self._filters(t, vol, ts)
            if why:
                excluded[why] = excluded.get(why, 0) + 1
                continue
            ch = getattr(t, ch_k)
            accel = None
            if base[0] and getattr(t, base[0]):
                accel = (vol or 0) / (getattr(t, base[0]) / base[1])
            rows.append({"token": t.address, "symbol": t.symbol, "name": t.name,
                         "launchpad": t.launchpad, "ret": ch, "volume": vol,
                         "mcap": t.mcap_hype, "price_end": t.price_hype,
                         "price_start": (t.price_hype / (1 + ch)) if (ch is not None and
                                                                     t.price_hype and ch > -1)
                         else None, "accel": accel, "created_at": t.created_at})
        if metric == "volume":
            if base[0] is None:
                res["note"] = "volume acceleration is not defined for 7d (no longer window)"
                return res
            rows = [r for r in rows if r["accel"] is not None and (r["accel"] - 1) * sign > 0]
            rows.sort(key=lambda r: (-(r["accel"] - 1) * sign, r["token"]))
            note = (f"{w} volume vs its average rate over the "
                    f"{'24h' if base[0] == 'vol_24h_hype' else '7d'} window")
        else:
            rows = [r for r in rows if r["ret"] is not None and r["ret"] * sign > 0]
            rows.sort(key=lambda r: (-(r["ret"] * sign), r["token"]))
            note = f"Agora's own {w} price change (last trade vs {w} ago)"
        synced = (meta.get("synced") or {})
        res.update(rows=rows, excluded=excluded, source="agora_live", note=note,
                   population=f"chain-wide: top 200 tokens by {w} volume on Agora "
                              f"(of {(meta.get('totals') or {}).get('tokens', '?')})",
                   source_at=datetime.fromtimestamp(synced["at"] / 1000, UTC)
                   if synced.get("at") else None)
        return res

    def _perf_snapshots(self, tr: TimeRange, sign: int) -> dict:
        tol = timedelta(seconds=max(self.collector_interval_s * 1.5, tr.seconds * 0.05))
        with self.ro() as c:
            span = self._one(c, """SELECT min(captured_at) AS a, max(captured_at) AS b
                                   FROM token_market_snapshots WHERE chain_id = :c""") or {}
            rows = self._all(c, """
                WITH w AS (
                  SELECT token, captured_at, price_hype, mcap_hype, vol_24h_hype,
                    row_number() OVER (PARTITION BY token ORDER BY captured_at) AS rn_a,
                    row_number() OVER (PARTITION BY token ORDER BY captured_at DESC) AS rn_d,
                    count(*) OVER (PARTITION BY token) AS n
                  FROM token_market_snapshots
                  WHERE chain_id = :c AND captured_at >= :s AND captured_at < :e
                    AND price_hype > 0)
                SELECT a.token, a.captured_at AS t0, a.price_hype AS p0,
                       d.captured_at AS t1, d.price_hype AS p1, d.mcap_hype, d.vol_24h_hype, a.n
                FROM w a JOIN w d ON d.token = a.token AND d.rn_d = 1
                WHERE a.rn_a = 1 AND a.n >= :min_obs
                  AND a.captured_at <= :s_tol AND d.captured_at >= :e_tol
                ORDER BY a.token LIMIT 5000""", s=tr.start, e=tr.end,
                min_obs=self.ui.min_observations, s_tol=tr.start + tol, e_tol=tr.end - tol)
            names = self.token_names(c, [r["token"] for r in rows])
        out: list[dict[str, Any]] = []
        excluded: dict[str, int] = {}
        for r in rows:
            p0, p1 = float(r["p0"]), float(r["p1"])
            mcap = float(r["mcap_hype"]) if r["mcap_hype"] is not None else None
            vol = float(r["vol_24h_hype"]) if r["vol_24h_hype"] is not None else None
            if vol is not None and vol < self.ui.min_volume_hype:
                excluded["volume"] = excluded.get("volume", 0) + 1
                continue
            if mcap is not None and mcap < self.ui.min_mcap_hype:
                excluded["mcap"] = excluded.get("mcap", 0) + 1
                continue
            ret = p1 / p0 - 1
            if ret * sign <= 0:
                continue
            m = names.get(r["token"], {})
            out.append({"token": r["token"], "symbol": m.get("symbol"), "name": m.get("name"),
                        "launchpad": m.get("launchpad"), "ret": ret, "price_start": p0,
                        "price_end": p1, "mcap": mcap, "volume": vol, "n_obs": r["n"],
                        "t0": r["t0"], "t1": r["t1"]})
        out.sort(key=lambda r: (-(r["ret"] * sign), r["token"]))
        note = "first vs last stored snapshot inside the window"
        if not rows:
            if span.get("a") is None:
                note = "no market snapshots stored yet"
            elif span["a"] > tr.start + tol:
                note = f"snapshots start {span['a']:%Y-%m-%d %H:%M} UTC, after this window began"
            else:
                note = "snapshots don't cover both ends of this window"
        return {"rows": out, "excluded": excluded, "source": "snapshots", "note": note,
                "population": "tokens in stored snapshots (Agora top by volume + tracked)",
                "volume_basis": "Agora 24h volume at the last snapshot"}

    def _perf_candles(self, tr: TimeRange, sign: int, now: datetime) -> dict:
        assert self.market is not None
        age = (now - tr.start).total_seconds()
        rng = "1h" if age <= 3500 else ("24h" if age <= 86000 else "7d")
        cands, _ = self.market.top_tokens(self.ui.candle_fallback_tokens, window="7d")
        s, e = tr.start.timestamp(), tr.end.timestamp()
        out: list[dict[str, Any]] = []
        excluded: dict[str, int] = {}
        checked = 0
        for t in cands:
            cs = candles(self.market.token_detail(t.address, rng))
            if len(cs) < 2:
                continue
            checked += 1
            step = (cs[-1][0] - cs[0][0]) / max(1, len(cs) - 1)
            if t.created_at and t.created_at > s:
                excluded["launched_in_window"] = excluded.get("launched_in_window", 0) + 1
                continue
            start_c = [x for x in cs if x[0] <= s < x[0] + step] or \
                      [x for x in cs if s <= x[0] < s + step]
            end_c = [x for x in cs if x[0] < e]
            if not start_c or not end_c or end_c[-1][0] < s:
                continue
            p0, p1 = start_c[0][1], end_c[-1][4]
            vol = sum(x[5] for x in cs if s <= x[0] < e)
            if vol < self.ui.min_volume_hype:
                excluded["volume"] = excluded.get("volume", 0) + 1
                continue
            if t.mcap_hype is not None and t.mcap_hype < self.ui.min_mcap_hype:
                excluded["mcap"] = excluded.get("mcap", 0) + 1
                continue
            if p0 <= 0:
                continue
            ret = p1 / p0 - 1
            if ret * sign <= 0:
                continue
            out.append({"token": t.address, "symbol": t.symbol, "name": t.name,
                        "launchpad": t.launchpad, "ret": ret, "price_start": p0,
                        "price_end": p1, "volume": vol, "mcap": t.mcap_hype,
                        "resolution_s": int(step)})
        out.sort(key=lambda r: (-(r["ret"] * sign), r["token"]))
        return {"rows": out, "excluded": excluded, "source": "agora_candles", "checked": checked,
                "note": f"Agora {rng} candles (~{cs_step_minutes(rng):g}-min): open at the "
                        "window start, close before its end",
                "population": f"top {self.ui.candle_fallback_tokens} tokens by 7d volume "
                              "(chain-wide, Agora)",
                "volume_basis": "sum of candle volume inside the window (bucket precision)"}

    def token_performance(self, token: str, tr: TimeRange, now: datetime | None = None
                          ) -> dict[str, Any] | None:
        """Price change of one token over [start, end), from snapshots or candles."""
        now = now or datetime.now(UTC)
        with self.ro() as c:
            r = self._one(c, """
                SELECT (SELECT price_hype FROM token_market_snapshots
                         WHERE chain_id = :c AND token = :t AND captured_at >= :s
                           AND captured_at < :e AND price_hype > 0
                         ORDER BY captured_at LIMIT 1) AS p0,
                       (SELECT captured_at FROM token_market_snapshots
                         WHERE chain_id = :c AND token = :t AND captured_at >= :s
                           AND captured_at < :e AND price_hype > 0
                         ORDER BY captured_at LIMIT 1) AS t0,
                       (SELECT price_hype FROM token_market_snapshots
                         WHERE chain_id = :c AND token = :t AND captured_at >= :s
                           AND captured_at < :e AND price_hype > 0
                         ORDER BY captured_at DESC LIMIT 1) AS p1,
                       (SELECT captured_at FROM token_market_snapshots
                         WHERE chain_id = :c AND token = :t AND captured_at >= :s
                           AND captured_at < :e AND price_hype > 0
                         ORDER BY captured_at DESC LIMIT 1) AS t1""",
                t=token, s=tr.start, e=tr.end) or {}
        tol = timedelta(seconds=max(self.collector_interval_s * 1.5, tr.seconds * 0.05))
        if r.get("p0") and r.get("p1") and r["t0"] <= tr.start + tol and r["t1"] >= tr.end - tol:
            p0, p1 = float(r["p0"]), float(r["p1"])
            return {"ret": p1 / p0 - 1, "p0": p0, "p1": p1, "source": "snapshots"}
        if self.market is None or (now - tr.start) > timedelta(days=7):
            return None
        age = (now - tr.start).total_seconds()
        rng = "1h" if age <= 3500 else ("24h" if age <= 86000 else "7d")
        cs = candles(self.market.token_detail(token, rng))
        if len(cs) < 2:
            return None
        s, e = tr.start.timestamp(), tr.end.timestamp()
        step = (cs[-1][0] - cs[0][0]) / max(1, len(cs) - 1)
        start_c = [x for x in cs if x[0] <= s < x[0] + step]
        end_c = [x for x in cs if x[0] < e]
        if not start_c or not end_c or start_c[0][1] <= 0:
            return None
        p0, p1 = start_c[0][1], end_c[-1][4]
        return {"ret": p1 / p0 - 1, "p0": p0, "p1": p1, "source": f"Agora {rng} candles"}

    # --- Trending (P2) ---------------------------------------------------------------------------
    TR_SORTS = ("score", "smart", "buying", "new", "growth")

    def _mcap_text(self, mcap_hype: float) -> str:
        """Market cap as shown inside report text. The Telegram reports speak HYPE; the public
        dashboard overrides this to speak USD."""
        return f"{mcap_hype:.0f} HYPE"

    def trending(self, tr: TimeRange, sort: str = "score", now: datetime | None = None
                 ) -> dict[str, Any]:
        """Explainable trending score (0-100) from tracked-wallet evidence plus Agora's live
        market stats. Component weights are fixed and listed in docs/TELEGRAM_UI.md."""
        now = now or datetime.now(UTC)
        if sort not in self.TR_SORTS:
            sort = "score"
        live = tr.preset is not None and abs((now - tr.end).total_seconds()) < 120
        w = {"15m": "1h", "1h": "1h", "6h": "6h", "24h": "24h", "7d": "7d",
             "30d": "7d"}.get(tr.preset or "", "24h")
        with self.ro() as c:
            uni = self.universe(c, tr)
            flows = {r["token"]: r for r in self.token_flows(c, tr, uni.qualified)}
            conc = {r["token"]: r for r in self._all(c, """
                SELECT token, max(v) AS top_wei, sum(v) AS all_wei FROM (
                  SELECT token, wallet, sum(hype_wei) AS v FROM trades
                  WHERE chain_id = :c AND kind = 'BUY' AND confidence = 'high'
                    AND block_time >= :s AND block_time < :e GROUP BY token, wallet) x
                GROUP BY token LIMIT 5000""", s=tr.start, e=tr.end)}
            names = self.token_names(c, list(flows))
        market: dict[str, TokenInfo] = {}
        meta: dict = {}
        if live and self.market is not None:
            toks, meta = self.market.top_tokens(200, window="24h")
            market = {t.address: t for t in toks}
        rows = []
        for addr in set(flows) | set(market):
            f, m = flows.get(addr), market.get(addr)
            comp: dict[str, float] = {}
            why: list[str] = []
            flags: list[str] = []
            qb = f["q_buyers"] if f else 0
            qnet = float(f["q_net"]) if f else 0.0
            comp["smart_buyers"] = min(qb, 5) / 5 * 30
            if qb:
                why.append(f"{qb} qualified tracked wallet{'s' if qb > 1 else ''} bought")
            comp["smart_net"] = min(max(qnet, 0) / 20, 1) * 15
            if f and (f["q_buy"] or f["q_sell"]):
                why.append(f"verified net {'buying' if qnet >= 0 else 'selling'}: "
                           f"{qnet:+.2f} HYPE")
            cc = conc.get(addr)
            if cc and cc["all_wei"] and qb >= 1:
                share = float(cc["top_wei"] / cc["all_wei"])
                if share > 0.8 and (f and f["buyers"] > 1):
                    flags.append(f"one wallet = {share:.0%} of tracked buying")
            accel = None
            if m is not None:
                vol = getattr(m, f"vol_{w}_hype")
                base: tuple[float | None, int] = {"1h": (m.vol_24h_hype, 24),
                                                  "6h": (m.vol_24h_hype, 4),
                                                  "24h": (m.vol_7d_hype, 7),
                                                  "7d": (None, 1)}[w]
                accel = (vol or 0) / (base[0] / base[1]) if base[0] else None
                comp["acceleration"] = (min(max(accel - 1, 0) / 2, 1) * 15) if accel else 0.0
                if accel and accel > 1.2:
                    why.append(f"{w} volume {accel:.1f}x its average rate")
                bf = (m.buys_24h / m.trades_24h) if (m.trades_24h and m.buys_24h is not None) \
                    else None
                comp["buy_pressure"] = max(0.0, (bf - 0.5) * 2) * 10 if bf else 0.0
                if bf and bf > 0.6:
                    why.append(f"{bf:.0%} of 24h trades are buys")
                comp["frequency"] = min(math.log10((m.trades_24h or 0) + 1) / 3, 1) * 10
                age_h = (now.timestamp() - m.created_at) / 3600 if m.created_at else None
                comp["recency"] = max(0.0, 1 - age_h / 72) * 10 if age_h is not None else 0.0
                if age_h is not None and age_h < 72:
                    why.append(f"launched {age_h:.0f}h ago")
                ch = getattr(m, f"change_{w}")
                comp["momentum"] = min(max(ch or 0, 0), 1) * 10
                if ch is not None:
                    why.append(f"price {ch:+.0%} ({w})")
                if m.mcap_hype is not None and m.mcap_hype < self.ui.min_mcap_hype:
                    flags.append(f"thin market (MC {self._mcap_text(m.mcap_hype)})")
                if m.trades_24h and (m.vol_24h_hype or 0) / m.trades_24h < 0.05:
                    flags.append("many tiny trades (bots / wash risk)")
                    comp["wash_penalty"] = -10.0
            score = max(0.0, min(100.0, sum(comp.values())))
            if score <= 0 and not qb:
                continue
            nm = names.get(addr, {})
            rows.append({"token": addr, "symbol": (m.symbol if m else None) or nm.get("symbol"),
                         "name": (m.name if m else None) or nm.get("name"),
                         "launchpad": (m.launchpad if m else None) or nm.get("launchpad"),
                         "score": round(score), "components": comp, "why": why, "flags": flags,
                         "q_buyers": qb, "q_net": qnet,
                         "buy": float(f["buy"]) if f else 0.0,
                         "mcap": m.mcap_hype if m else None,
                         "created_at": m.created_at if m else None, "accel": accel})
        key = {"score": lambda r: (-r["score"], r["token"]),
               "smart": lambda r: (-r["q_buyers"], -r["score"], r["token"]),
               "buying": lambda r: (-r["buy"], -r["score"], r["token"]),
               "new": lambda r: (-(r["created_at"] or 0), r["token"]),
               "growth": lambda r: (-(r["accel"] or 0), -r["score"], r["token"])}[sort]
        rows.sort(key=key)
        note = (f"market components: Agora live {w} window" if live and market else
                "historical window: Agora's live market stats don't apply, so only "
                "tracked-wallet evidence is scored (max 45)")
        return {"rows": rows, "sort": sort, "range": tr, "universe": uni, "note": note,
                "window": w, "live": bool(live and market), "meta": meta}


def cs_step_minutes(rng: str) -> float:
    return {"1h": 1, "24h": 15, "7d": 84}.get(rng, 15)


def summarize(o: dict[str, Any]) -> list[str]:
    """Deterministic plain-language summary of a market overview. Every number is computed."""
    t, p = o["totals"], o["prev"]
    out: list[str] = []
    if not t["legs"]:
        return ["No tracked-wallet activity was recorded in this window."]
    ch = pct_change(t["buy"], p["buy"])
    if ch is None:
        out.append(f"Tracked wallets bought {t['buy']:.2f} HYPE (verified); there was no "
                   "verified buying in the previous period to compare with.")
    else:
        word = "increased" if ch >= 0 else "decreased"
        out.append(f"Verified buying by tracked wallets {word} {abs(ch):.0%} versus the "
                   f"previous period ({p['buy']:.2f} → {t['buy']:.2f} HYPE).")
    net = t["net"]
    out.append(f"They were net {'buyers' if net >= 0 else 'sellers'} of "
               f"{abs(net):.2f} HYPE across {t['tokens']} tokens.")
    mq = o["multi_qualified"]
    if mq:
        names = ", ".join(r.get("symbol") or r["token"][:8] for r in mq[:3])
        out.append(f"{len(mq)} token{'s' if len(mq) > 1 else ''} attracted 2+ qualified "
                   f"wallets ({names}).")
    prev_tokens = {r["token"] for r in o.get("selling", [])}
    if prev_tokens:
        out.append(f"{len(prev_tokens)} token{'s' if len(prev_tokens) > 1 else ''} saw net "
                   "selling by tracked wallets.")
    if o.get("concentration_top3") is not None and t["tokens"] > 3:
        out.append(f"The top 3 tokens took {o['concentration_top3']:.0%} of verified volume.")
    est = t["buy_est"] + t["sell_est"]
    if est:
        out.append(f"A further {est:.2f} HYPE was estimated (not measured) and is excluded "
                   "from the totals above.")
    return out
