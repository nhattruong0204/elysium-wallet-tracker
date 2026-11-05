"""Reports for the public dashboard: the Telegram analytics, pointed at `public_api`.

`Reports` queries unqualified table names. The dashboard role's search_path is
`public_api`, so the very same SQL runs against the filtered views: there is exactly one
implementation of every metric, and the privacy boundary is the database's, not the
templates'. This subclass only adds the aggregate shapes the website needs.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from ..market import TokenInfo
from ..reports import Reports, hype
from ..timerange import TimeRange
from .activity import (
    KIND_TO_ACTION,
    TOTAL_CAP,
    ActivityParams,
    dt_to_micros,
    filter_sql,
    order_and_cursor,
)
from .consensus import MAX_CARDS, RANGE_CHOICES, ConsensusParams, build_cards
from .usd import FX_LATERAL, FxPolicy, format_usd, to_usd

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


class _BasePublicReports(Reports):
    # Latest stored snapshot per token with the HYPE/USD rate closest in time to it. The rate
    # lookup runs once per returned row (a few hundred at most), never per stored snapshot.
    _LATEST_USD = """
        SELECT d.token, d.captured_at, d.price_hype, d.mcap_hype, d.change_1h, d.change_24h,
               fx.observed_at AS fx_at, fx.hype_usd AS fx_rate
        FROM (SELECT DISTINCT ON (token) token, captured_at, price_hype, mcap_hype,
                     change_1h, change_24h
              FROM token_market_snapshots
              WHERE chain_id = :c AND token = ANY(:t) AND captured_at >= :since
              ORDER BY token, captured_at DESC) d""" + FX_LATERAL.format(ts="d.captured_at")

    fx_policy = FxPolicy()

    def usd(self, row: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Public USD contract for one snapshot row that carries fx_at / fx_rate."""
        return to_usd(row.get("mcap_hype"), row.get("fx_rate"), row.get("fx_at"),
                      row.get("captured_at"), self.fx_policy, now)

    def latest_usd(self, tokens: list[str], now: datetime | None = None,
                   lookback: timedelta = timedelta(days=2)) -> dict[str, dict[str, Any]]:
        """token -> USD contract of its newest stored snapshot (tokens without one are absent)."""
        now = now or datetime.now(UTC)
        if not tokens:
            return {}
        with self.ro() as c:
            rows = self._all(c, self._LATEST_USD, t=list(tokens), since=now - lookback)
        return {r["token"]: {**self.usd(r, now), "price_hype": r["price_hype"]} for r in rows}

    def _mcap_text(self, mcap_hype: float) -> str:
        with self.ro() as c:
            r = self._one(c, "SELECT hype_usd, observed_at FROM hype_usd_rates "
                             "ORDER BY observed_at DESC LIMIT 1")
        now = datetime.now(UTC)
        rate = r["hype_usd"] if r else None
        rate_at = r["observed_at"] if r else None
        u = to_usd(mcap_hype, rate, rate_at, now, self.fx_policy, now)
        return format_usd(u["market_cap_usd"]) if u["market_cap_usd"] is not None \
            else "USD value unavailable"

    def latest_fx(self) -> dict[str, Any] | None:
        with self.ro() as c:
            return self._one(c, "SELECT hype_usd, observed_at, source FROM hype_usd_rates "
                                "ORDER BY observed_at DESC LIMIT 1")

    def _outbox_counts(self, c: Connection) -> dict[str, int]:
        return {x["status"]: x["n"] for x in self._all(c, "SELECT status, n FROM outbox_status")}

    # --- Projects / tokens table --------------------------------------------------------------
    def token_table(self, tr: TimeRange) -> dict[str, Any]:
        """Every token tracked wallets touched in the window, with flows and the latest
        stored market snapshot. Filtering, sorting and paging happen on this list."""
        now = datetime.now(UTC)
        with self.ro() as c:
            uni = self.universe(c, tr)
            flows = self.token_flows(c, tr, uni.qualified)
            self._decorate(c, flows)
            toks = [r["token"] for r in flows]
            ever = {r["token"]: r for r in self._all(c, """
                SELECT token, min(block_time) AS first_ever, max(block_time) AS last_ever
                FROM trades WHERE chain_id = :c AND token = ANY(:t) GROUP BY token""", t=toks)}
            snap = {r["token"]: r for r in self._all(c, self._LATEST_USD, t=toks,
                                                     since=datetime.now(UTC) - timedelta(days=2))}
        for r in flows:
            e, s = ever.get(r["token"], {}), snap.get(r["token"])
            r["first_ever"], r["last_ever"] = e.get("first_ever"), e.get("last_ever")
            r["new_in_window"] = bool(e.get("first_ever") and e["first_ever"] >= tr.start)
            r["market"] = None if not s else {
                "at": s["captured_at"],
                "price_hype": s["price_hype"], "mcap_hype": s["mcap_hype"],
                "change_1h": s["change_1h"], "change_24h": s["change_24h"],
                "usd": self.usd(s, now)}
        return {"rows": flows, "universe": uni, "range": tr}

    def snapshot_series(self, token: str, tr: TimeRange, limit: int = 600
                        ) -> list[dict[str, Any]]:
        """Stored snapshots in the window, each with the USD value at its own time."""
        now = datetime.now(UTC)
        with self.ro() as c:
            rows = self._all(c, """
                SELECT d.captured_at, d.price_hype, d.mcap_hype,
                       fx.observed_at AS fx_at, fx.hype_usd AS fx_rate
                FROM (SELECT captured_at, price_hype, mcap_hype FROM token_market_snapshots
                      WHERE chain_id = :c AND token = :t AND captured_at >= :s
                        AND captured_at < :e
                      ORDER BY captured_at LIMIT :n) d""" + FX_LATERAL.format(ts="d.captured_at")
                + " ORDER BY d.captured_at", t=token, s=tr.start, e=tr.end, n=int(limit))
        for r in rows:
            u = self.usd(r, now)
            r["mcap_usd"], r["usd_status"] = u["market_cap_usd"], u["market_cap_status"]
        return rows

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


# --- Activity monitor -----------------------------------------------------------------------------
# Market cap of a row is taken at the row's own block time, never today's value:
#   snapshot     the token's stored Agora snapshot nearest to the block (within MCAP_NEAR_S)
#   trade_price  no snapshot that close: the leg's own executed price x the supply implied by
#                the token's latest snapshot (mcap / price). Only verified buys and sells.
# Both are converted with the HYPE/USD rate observed closest to the block time.
MCAP_NEAR_S = 1800

_ACT_MARKET = """
    SELECT x.id, near.captured_at AS snap_at, near.mcap_hype AS snap_mcap,
           fx.observed_at AS fx_at, fx.hype_usd AS fx_rate
    FROM unnest(CAST(:ids AS bigint[]), CAST(:toks AS text[]), CAST(:ts AS timestamptz[]))
         AS x(id, token, ts)
    LEFT JOIN LATERAL (
        SELECT c.captured_at, c.mcap_hype FROM (
            (SELECT s.captured_at, s.mcap_hype FROM token_market_snapshots s
              WHERE s.chain_id = :c AND s.token = x.token AND s.mcap_hype IS NOT NULL
                AND s.captured_at <= x.ts AND s.captured_at > x.ts - make_interval(secs => :near)
              ORDER BY s.captured_at DESC LIMIT 1)
            UNION ALL
            (SELECT s.captured_at, s.mcap_hype FROM token_market_snapshots s
              WHERE s.chain_id = :c AND s.token = x.token AND s.mcap_hype IS NOT NULL
                AND s.captured_at > x.ts AND s.captured_at < x.ts + make_interval(secs => :near)
              ORDER BY s.captured_at ASC LIMIT 1)
        ) c ORDER BY abs(extract(epoch FROM c.captured_at - x.ts)) LIMIT 1
    ) near ON true""" + FX_LATERAL.format(ts="x.ts")

# supply implied by each token's latest snapshot with both a price and a market cap
_ACT_SUPPLY = """
    SELECT DISTINCT ON (token) token, mcap_hype / price_hype AS supply
    FROM token_market_snapshots
    WHERE chain_id = :c AND token = ANY(:t) AND price_hype > 0 AND mcap_hype > 0
    ORDER BY token, captured_at DESC"""


def launchpad_label(raw: str | None) -> str | None:
    if not raw:
        return None
    from ..cards import LAUNCHPAD_NAMES
    return LAUNCHPAD_NAMES.get(raw.lower(), raw[:1].upper() + raw[1:])


def _activity_row(r: dict[str, Any], names: dict[str, dict[str, Any]]) -> dict[str, Any]:
    m = names.get(r["token"], {})
    dec = m.get("decimals", 18)
    conf = r["confidence"]
    return {
        "id": r["id"], "block_time": r["block_time"], "block_number": r["block_number"],
        "tx_hash": r["tx_hash"], "wallet": r["wallet"], "token": r["token"],
        "symbol": m.get("symbol"), "kind": r["kind"], "action": KIND_TO_ACTION.get(r["kind"], "other"),
        "confidence": conf,
        "basis": "verified" if conf == "high" else "estimated" if conf == "estimated" else "unknown",
        "amount_hype": None if r["hype_wei"] is None else hype(r["hype_wei"]),
        "hype_method": r["hype_method"],
        "_wei": None if r["hype_wei"] is None else int(r["hype_wei"]),
        "qty_units": Decimal(r["qty"]) / (Decimal(10) ** int(dec if dec is not None else 18)),
        "launchpad": m.get("launchpad"), "launchpad_label": launchpad_label(m.get("launchpad")),
    }


class PublicReports(_BasePublicReports):
    """The dashboard's reports: the shared analytics plus the public Activity queries."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self._names: dict[str, tuple[float, dict[str, Any]]] = {}
        self._names_lock = threading.Lock()
        self.hidden_tokens: tuple[str, ...] = ()       # alerts.mute_tokens

    _ACT_COLS = ("t.id, t.block_number, t.block_time, t.tx_hash, t.wallet, t.token, t.kind, "
                 "t.confidence, t.hype_method, t.qty, t.hype_wei")

    def activity_head(self, now: datetime) -> dict[str, Any]:
        """Cheap freshness facts: newest leg id and time, indexer cursor, heartbeat, retention.

        LIMIT-1 index probes, not max()/min() over the view: an aggregate has to read every
        public trade (240 ms at 1M rows), a probe reads a handful of index entries."""
        with self.ro() as c:
            newest = self._one(c, """SELECT id, block_time FROM trades WHERE chain_id = :c
                                     ORDER BY id DESC LIMIT 1""") or {}
            latest = self._one(c, """SELECT block_time FROM trades WHERE chain_id = :c
                                     ORDER BY block_time DESC, id DESC LIMIT 1""") or {}
            first = self._one(c, """SELECT block_time FROM trades WHERE chain_id = :c
                                    ORDER BY block_time ASC, id ASC LIMIT 1""") or {}
            r = self._one(c, """
                SELECT (SELECT block_number FROM indexer_checkpoints
                         WHERE chain_id = :c AND name = 'indexer') AS cursor,
                       (SELECT updated_at FROM indexer_checkpoints
                         WHERE chain_id = :c AND name = 'indexer') AS cursor_at""") or {}
            heads = self._kv(c, "rpc_heads") or {}
            hb = self._kv(c, "heartbeat")
        r["head_id"] = newest.get("id")
        r["last_trade_at"] = latest.get("block_time")
        r["first_trade_at"] = first.get("block_time")
        best = heads.get("best") if isinstance(heads, dict) else None
        cur = r.get("cursor")
        r["chain_head"] = best
        r["lag_blocks"] = (best - cur) if (best is not None and cur is not None) else None
        try:
            r["heartbeat_age_s"] = max(0, int(now.timestamp()) - int(hb)) if hb not in (None, "") else None
        except (TypeError, ValueError):
            r["heartbeat_age_s"] = None
        return r

    def _activity_names(self, c: Connection, tokens: list[str]) -> dict[str, dict[str, Any]]:
        """Symbols for the tokens on a page. They almost never change, and resolving them
        through the public views costs a full pass over the public trades, so they are cached
        in-process (bounded, 10 min). Only tokens that appeared in public rows get cached."""
        now = time.monotonic()
        with self._names_lock:
            hit = {t: e for t in tokens if (e := self._names.get(t)) and e[0] > now}
        missing = [t for t in tokens if t not in hit]
        out = {t: e[1] for t, e in hit.items()}
        if missing:
            got = self.token_names(c, missing)
            with self._names_lock:
                if len(self._names) > 4000:
                    self._names = {k: e for k, e in self._names.items() if e[0] > now}
                    if len(self._names) > 4000:
                        self._names.clear()
                for t, info in got.items():
                    self._names[t] = (now + 600, info)
            out.update(got)
        return out

    def _activity_market(self, c: Connection, items: list[dict[str, Any]]) -> None:
        """Adds market_cap_usd / _source / _text to each item, valued at its block time."""
        if not items:
            return
        rows = {r["id"]: r for r in self._all(
            c, _ACT_MARKET, ids=[i["id"] for i in items], toks=[i["token"] for i in items],
            ts=[i["block_time"] for i in items], near=MCAP_NEAR_S)}
        supply = {r["token"]: r["supply"] for r in self._all(
            c, _ACT_SUPPLY, t=sorted({i["token"] for i in items}))}
        for it in items:
            r = rows.get(it["id"]) or {}
            cap, src, at = r.get("snap_mcap"), "snapshot", r.get("snap_at")
            if cap is None and it["basis"] == "verified" and it["kind"] in ("BUY", "SELL") \
                    and it["_wei"] and it["qty_units"] and supply.get(it["token"]):
                price = Decimal(it["_wei"]) / Decimal(10**18) / it["qty_units"]
                cap, src, at = price * Decimal(supply[it["token"]]), "trade_price", it["block_time"]
            u = to_usd(cap, r.get("fx_rate"), r.get("fx_at"), at, self.fx_policy) \
                if cap is not None else None
            usd = u["market_cap_usd"] if u else None
            it["market_cap_usd"] = usd
            it["market_cap_source"] = src if usd is not None else None
            it["market_cap_text"] = format_usd(usd) if usd is not None else None

    # --- consensus buys ----------------------------------------------------------------------
    _CONS_LEGS = """
        SELECT t.token, t.wallet, t.kind, t.hype_wei, t.block_time, t.block_number, t.tx_hash
        FROM trades t
        WHERE t.chain_id = :c AND t.kind IN ('BUY', 'SELL') AND t.confidence = 'high'
          AND t.hype_wei IS NOT NULL AND t.hype_wei > 0
          AND t.block_time >= :lo AND t.block_time < :hi
          AND NOT (t.token = ANY(CAST(:hidden AS text[])))
          AND t.token IN (
            SELECT b.token FROM trades b
            WHERE b.chain_id = :c AND b.kind = 'BUY' AND b.confidence = 'high'
              AND b.block_time >= :lo AND b.block_time < :hi
            GROUP BY b.token HAVING count(DISTINCT b.wallet) >= :n)
        ORDER BY t.token, t.block_time, t.id"""

    # wallets buying a token in the same block as >= 2 other public wallets, >= 5 times
    _CONS_BUNDLES = """
        SELECT b.wallet FROM trades b
        JOIN (SELECT token, block_number FROM trades
              WHERE chain_id = :c AND kind = 'BUY' AND confidence = 'high' AND block_time >= :lo
              GROUP BY token, block_number HAVING count(DISTINCT wallet) >= 3) g
          ON g.token = b.token AND g.block_number = b.block_number
        WHERE b.chain_id = :c AND b.kind = 'BUY' AND b.confidence = 'high' AND b.block_time >= :lo
        GROUP BY b.wallet HAVING count(DISTINCT (b.token, b.block_number)) >= 5"""

    _CONS_RANKS = """
        SELECT e.address, e.rank FROM leaderboard_snapshot_entries e
        WHERE e.snapshot_id = (SELECT id FROM leaderboard_snapshots
                               WHERE status IN ('ok', 'imported') ORDER BY captured_at DESC LIMIT 1)
          AND e.address = ANY(:a)"""

    def consensus_bundles(self, now: datetime) -> set[str]:
        with self.ro() as c:
            return {r["wallet"] for r in self._all(c, self._CONS_BUNDLES,
                                                   lo=now - timedelta(days=14))}

    def consensus(self, p: ConsensusParams, now: datetime, bundles: set[str]) -> dict[str, Any]:
        since = now - RANGE_CHOICES[p.range]
        lo = since - timedelta(minutes=p.window_min)
        n_pre = 2 if p.independent else p.min_traders     # a cheap pre-filter, never stricter
        with self.ro() as c:
            rows = self._all(c, self._CONS_LEGS, lo=lo, hi=now, n=min(n_pre, p.min_traders),
                             hidden=list(self.hidden_tokens))
            cards = build_cards(rows, p, since, bundles)
            names = self._activity_names(c, sorted({k["token"] for k in cards}))
            for k in cards:
                m = names.get(k["token"], {})
                k["symbol"] = m.get("symbol")
                k["launchpad"] = m.get("launchpad")
                k["launchpad_label"] = launchpad_label(m.get("launchpad"))
            pads = sorted({k["launchpad"] for k in cards if k["launchpad"]} | (
                {p.launchpad} if p.launchpad else set()))
            if p.launchpad:
                cards = [k for k in cards if (k["launchpad"] or "") == p.launchpad]
            total = len(cards)
            cards = cards[:MAX_CARDS]
            # market cap when the threshold was reached, and the HYPE rate of that moment
            if cards:
                mk = {r["id"]: r for r in self._all(
                    c, _ACT_MARKET, ids=list(range(len(cards))), toks=[k["token"] for k in cards],
                    ts=[k["trigger_at"] for k in cards], near=MCAP_NEAR_S)}
                wallets = sorted({m["wallet"] for k in cards for m in k["members"]})
                ranks = {r["address"]: r["rank"] for r in self._all(c, self._CONS_RANKS,
                                                                    a=wallets)}
            else:
                mk, ranks = {}, {}
        now_usd = self.latest_usd(sorted({k["token"] for k in cards}), now)
        for i, k in enumerate(cards):
            r = mk.get(i) or {}
            rate, rate_at = r.get("fx_rate"), r.get("fx_at")
            at = to_usd(r.get("snap_mcap"), rate, rate_at, r.get("snap_at"), self.fx_policy) \
                if r.get("snap_mcap") is not None else None
            k["mcap_at_usd"] = at["market_cap_usd"] if at else None
            k["mcap_now"] = now_usd.get(k["token"])
            nowv = (k["mcap_now"] or {}).get("market_cap_usd")
            k["change_x"] = (float(nowv) / float(k["mcap_at_usd"])) \
                if nowv and k["mcap_at_usd"] else None
            fx = float(rate) if rate else None
            k["net_usd"] = k["net_hype"] * fx if fx else None
            for m in k["members"]:
                m["rank"] = ranks.get(m["wallet"])
                m["net_usd"] = m["net_hype"] * fx if fx else None
        return {"cards": cards, "total": total, "since": since, "launchpads": pads,
                "legs": len(rows)}

    def activity_page(self, p: ActivityParams) -> dict[str, Any]:
        """One keyset page. Fetches size+1 rows to learn whether more exist in that direction."""
        where, prm = filter_sql(p, hidden=self.hidden_tokens)
        order, cur_sql, cprm = order_and_cursor(p)
        if cur_sql:
            where += " AND " + cur_sql
            prm.update(cprm)
        with self.ro() as c:
            rows = self._all(c, f"""
                SELECT {self._ACT_COLS} FROM trades t WHERE {where}
                ORDER BY {order} LIMIT :lim""", lim=p.size + 1, **prm)
            more = len(rows) > p.size
            rows = rows[:p.size]
            if p.direction == "prev":
                rows.reverse()
            names = self._activity_names(c, sorted({r["token"] for r in rows}))
            items = [_activity_row(r, names) for r in rows]
            self._activity_market(c, items)
        return {"items": items, "more": more}

    def activity_since(self, p: ActivityParams, limit: int) -> dict[str, Any]:
        """Rows inserted after `since_id` that match the filters, newest first."""
        where, prm = filter_sql(p, hidden=self.hidden_tokens)
        with self.ro() as c:
            rows = self._all(c, f"""
                SELECT {self._ACT_COLS} FROM trades t WHERE {where} AND t.id > :since
                ORDER BY t.id DESC LIMIT :lim""", since=p.since_id, lim=limit + 1, **prm)
            overflow = len(rows) > limit
            rows = rows[:limit]
            names = self._activity_names(c, sorted({r["token"] for r in rows}))
            items = [_activity_row(r, names) for r in rows]
            self._activity_market(c, items)
        items.sort(key=lambda i: (i["block_time"], i["id"]), reverse=True)
        return {"items": items, "overflow": overflow}

    def activity_total(self, p: ActivityParams) -> dict[str, Any]:
        where, prm = filter_sql(p, hidden=self.hidden_tokens)
        with self.ro() as c:
            n = c.execute(sa.text(f"SELECT count(*) FROM (SELECT 1 FROM trades t WHERE {where} "
                                   "LIMIT :cap) x"), {"c": self.chain_id, "cap": TOTAL_CAP + 1,
                                                      **prm}).scalar()
        total = int(n or 0)
        return {"count": min(total, TOTAL_CAP), "capped": total > TOTAL_CAP}


def sort_key(sort: str, item: dict[str, Any]) -> Any:
    """The cursor key of an item for the given sort."""
    if sort in ("newest", "oldest"):
        return dt_to_micros(item["block_time"])
    return str(item["_wei"])                 # exact wei, as an integer string


