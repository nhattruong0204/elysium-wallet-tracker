"""Wallet Intelligence: a research view over every wallet that has ever been on the Top 100.

Read model only. Everything comes from `public_api` views (so a private manual / Telegram wallet
cannot appear), is computed from stored rows, and carries its own data-quality label:

* `status`      current (on the latest accepted list) | former (was, no longer is)
* `quality`     how complete our trade history for the wallet is (see `classify_quality`)
* amounts       Agora-reported PnL / worth / volume are Agora's accounting; "measured" figures are
                what we recomputed from on-chain trades, only for sells whose cost basis we know.
                They are shown side by side and never added together.

No score, no verdict: ranking and filtering by facts only. Scoring lives in its own, versioned
module (phase 4).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .params import ADDR_RE, ParamError

UTC = timezone.utc
E18 = Decimal(10) ** 18

WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30),
           "all": None}
MEMBERSHIPS = ("all", "current", "former")
QUALITIES = ("all", "complete", "partial", "gaps", "unscanned", "excluded")
SORTS = {                       # key -> (label, default descending)
    "rank": ("Rank", False), "pnl": ("Agora PnL", True), "worth": ("Worth", True),
    "volume": ("Agora volume", True), "legs": ("Trades in window", True),
    "buy": ("Bought (HYPE)", True), "realized": ("Measured PnL", True),
    "last_trade": ("Last trade", True), "appearances": ("Snapshot appearances", True),
    "best_rank": ("Best rank", False), "last_top": ("Last on list", True),
    "first_top": ("First on list", False),
}
PAGE_SIZES = (25, 50, 100)
MAX_ROWS = 20_000
_NUM = re.compile(r"^-?[0-9]{1,12}(?:\.[0-9]{1,18})?$")


# --- parameters -----------------------------------------------------------------------------
@dataclass(frozen=True)
class IntelParams:
    window: str = "7d"
    membership: str = "all"
    quality: str = "all"
    active: bool = False                    # traded inside the window
    min_trades: int = 0
    min_rank: int | None = None             # best (closest to 1) acceptable rank, current rank
    max_rank: int | None = None
    min_pnl: Decimal | None = None          # Agora-reported PnL, HYPE
    q: str = ""                             # address fragment
    sort: str = "rank"
    desc: bool = False
    page: int = 1
    size: int = 50

    def key(self) -> tuple:
        return (self.window,)               # the base table depends only on the window


def _int(raw: str | None, name: str, lo: int, hi: int) -> int | None:
    if raw is None or raw.strip() == "":
        return None
    t = raw.strip()
    if not (t.isascii() and t.isdigit()):
        raise ParamError(f"{name} must be a whole number")
    v = int(t)
    if not lo <= v <= hi:
        raise ParamError(f"{name} must be between {lo} and {hi}")
    return v


def parse_intel(q: Mapping[str, str], *, default_size: int = 50) -> IntelParams:
    window = (q.get("window") or "7d").strip().lower()
    if window not in WINDOWS:
        raise ParamError(f"window must be one of {', '.join(WINDOWS)}")
    membership = (q.get("membership") or "all").strip().lower()
    if membership not in MEMBERSHIPS:
        raise ParamError(f"membership must be one of {', '.join(MEMBERSHIPS)}")
    quality = (q.get("quality") or "all").strip().lower()
    if quality not in QUALITIES:
        raise ParamError(f"quality must be one of {', '.join(QUALITIES)}")
    active_raw = (q.get("active") or "").strip().lower()
    if active_raw not in ("", "0", "1", "true", "false"):
        raise ParamError("active must be 0 or 1")
    sort = (q.get("sort") or "rank").strip().lower()
    if sort not in SORTS:
        raise ParamError(f"sort must be one of {', '.join(SORTS)}")
    d = (q.get("dir") or "").strip().lower()
    if d not in ("", "asc", "desc"):
        raise ParamError("dir must be asc or desc")
    desc = SORTS[sort][1] if not d else d == "desc"
    min_rank, max_rank = _int(q.get("min_rank"), "min_rank", 1, 100_000), \
        _int(q.get("max_rank"), "max_rank", 1, 100_000)
    if min_rank is not None and max_rank is not None and min_rank > max_rank:
        raise ParamError("min_rank must not be greater than max_rank")
    min_pnl = None
    raw_pnl = (q.get("min_pnl") or "").strip()
    if raw_pnl:
        if not (raw_pnl.isascii() and _NUM.match(raw_pnl)):
            raise ParamError("min_pnl must be a plain decimal number (HYPE)")
        try:
            min_pnl = Decimal(raw_pnl)
        except InvalidOperation:   # pragma: no cover - the regex already guarantees validity
            raise ParamError("min_pnl must be a plain decimal number (HYPE)") from None
    text = (q.get("q") or "").strip().lower()[:44]
    if text and not re.fullmatch(r"(0x)?[0-9a-f]{1,40}", text):
        raise ParamError("q must be an address or the start of one (hex)")
    size = _int(q.get("limit"), "limit", 1, 100) or default_size
    page = _int(q.get("page"), "page", 1, 10_000) or 1
    return IntelParams(window=window, membership=membership, quality=quality,
                       active=active_raw in ("1", "true"),
                       min_trades=_int(q.get("min_trades"), "min_trades", 0, 1_000_000) or 0,
                       min_rank=min_rank, max_rank=max_rank, min_pnl=min_pnl, q=text,
                       sort=sort, desc=desc, page=page, size=size)


# --- classification --------------------------------------------------------------------------
def classify_quality(status: str, intervals: int, gaps: int, first_scanned_at: datetime | None,
                     first_top_at: datetime | None, has_live: bool,
                     tolerance: timedelta = timedelta(minutes=15)) -> str:
    """How much of the wallet's activity we can claim to have seen.

    excluded   the wallet is on the list but the tracker is told not to follow it (contract)
    unscanned  no scan interval is recorded yet
    gaps       scanned, but with holes between intervals (trades inside a hole are not recorded)
    partial    first scanned after the wallet first appeared on the list: earlier trades are
               not in the database (this is true of every wallet that entered the list before
               cumulative tracking existed, and is shown, not hidden)
    complete   scanned since the wallet first appeared, with no gaps
    """
    if status == "excluded":
        return "excluded"
    if intervals == 0:
        return "unscanned"
    if gaps:
        return "gaps"
    if first_scanned_at is None or first_top_at is None or first_scanned_at > first_top_at + tolerance:
        return "partial"
    # derived-only coverage (reconstructed from watchlist timestamps) is a claim, not a scan
    return "complete" if has_live else "partial"


QUALITY_NOTES = {
    "complete": "Scanned without gaps since the wallet first appeared on the list.",
    "partial": "Trades before tracking began (or before the first scan) are not in the database.",
    "gaps": "Scanned with holes between intervals: trades inside a hole are not recorded.",
    "unscanned": "No scan interval recorded yet.",
    "excluded": "On the list but not followed (contract address).",
}


# --- queries ---------------------------------------------------------------------------------
BASE_SQL = """
WITH snaps AS (
  SELECT id, captured_at FROM leaderboard_snapshots
  WHERE chain_id = :c AND status IN ('ok', 'imported') AND complete),
latest AS (SELECT id, captured_at FROM snaps ORDER BY captured_at DESC, id DESC LIMIT 1),
last_entry AS (
  SELECT DISTINCT ON (e.address) e.address, e.snapshot_id, e.rank, e.pnl_usd AS pnl,
         e.worth_usd AS worth, e.volume_usd AS volume, e.trades AS agora_trades
  FROM leaderboard_snapshot_entries e JOIN snaps s ON s.id = e.snapshot_id
  ORDER BY e.address, s.captured_at DESC, s.id DESC),
act AS (
  SELECT wallet, count(*) AS legs,
    count(*) FILTER (WHERE kind = 'BUY') AS buys, count(*) FILTER (WHERE kind = 'SELL') AS sells,
    count(*) FILTER (WHERE kind IN ('TRANSFER_IN', 'TRANSFER_OUT')) AS transfers,
    count(DISTINCT token) AS tokens,
    count(*) FILTER (WHERE confidence = 'estimated') AS est_legs,
    coalesce(sum(hype_wei) FILTER (WHERE kind = 'BUY' AND confidence = 'high'), 0) AS buy_wei,
    coalesce(sum(hype_wei) FILTER (WHERE kind = 'SELL' AND confidence = 'high'), 0) AS sell_wei
  FROM trades WHERE chain_id = :c AND block_time >= :since GROUP BY wallet),
act_all AS (
  SELECT wallet, count(*) AS legs_all, min(block_time) AS first_trade_at,
         max(block_time) AS last_trade_at
  FROM trades WHERE chain_id = :c GROUP BY wallet),
real AS (
  SELECT r.wallet, count(*) FILTER (WHERE r.status = 'measured') AS n_measured,
    count(*) FILTER (WHERE r.status = 'measured' AND r.pnl_wei > 0) AS wins,
    coalesce(sum(r.pnl_wei) FILTER (WHERE r.status = 'measured'), 0) AS pnl_wei
  FROM realized_pnl r JOIN trades t ON t.id = r.trade_id
  WHERE r.chain_id = :c AND t.block_time >= :since GROUP BY r.wallet),
cov AS (
  SELECT address, count(*) AS intervals, min(from_block) AS first_block,
         bool_or(basis = 'live') AS has_live,
         sum(CASE WHEN prev_hi IS NOT NULL AND from_block > prev_hi + 1 THEN 1 ELSE 0 END) AS gaps
  FROM (SELECT address, from_block, basis,
               max(coalesce(to_block, 9223372036854775807)) OVER (
                 PARTITION BY address ORDER BY from_block
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_hi
        FROM wallet_scan_coverage WHERE chain_id = :c) x
  GROUP BY address)
SELECT e.address AS wallet, e.status AS enrollment, e.in_top, e.first_top_at, e.last_top_at,
  e.best_rank, e.last_rank, e.appearances, e.entries, e.exits,
  l.rank AS current_rank, le.rank AS last_known_rank, le.pnl, le.worth, le.volume,
  le.agora_trades,
  coalesce(a.legs, 0) AS legs, coalesce(a.buys, 0) AS buys, coalesce(a.sells, 0) AS sells,
  coalesce(a.transfers, 0) AS transfers, coalesce(a.tokens, 0) AS tokens,
  coalesce(a.est_legs, 0) AS est_legs, coalesce(a.buy_wei, 0) AS buy_wei,
  coalesce(a.sell_wei, 0) AS sell_wei,
  coalesce(aa.legs_all, 0) AS legs_all, aa.first_trade_at, aa.last_trade_at,
  coalesce(r.n_measured, 0) AS n_measured, coalesce(r.wins, 0) AS wins,
  coalesce(r.pnl_wei, 0) AS realized_wei,
  coalesce(cv.intervals, 0) AS intervals, cv.first_block, coalesce(cv.has_live, false) AS has_live,
  coalesce(cv.gaps, 0) AS gaps,
  (SELECT b.block_time FROM indexed_blocks b WHERE b.chain_id = :c
     AND b.block_number >= cv.first_block ORDER BY b.block_number LIMIT 1) AS first_scanned_at
FROM wallet_enrollment e
LEFT JOIN last_entry le ON le.address = e.address
LEFT JOIN leaderboard_snapshot_entries l ON l.address = e.address AND l.snapshot_id = (SELECT id FROM latest)
LEFT JOIN act a ON a.wallet = e.address
LEFT JOIN act_all aa ON aa.wallet = e.address
LEFT JOIN real r ON r.wallet = e.address
LEFT JOIN cov cv ON cv.address = e.address
WHERE e.chain_id = :c
ORDER BY e.address
LIMIT :cap"""


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(v)


def shape(r: Mapping[str, Any]) -> dict[str, Any]:
    """One database row -> the public wallet record (no private fields exist in the source)."""
    buy, sell = Decimal(r["buy_wei"]) / E18, Decimal(r["sell_wei"]) / E18
    n_meas = int(r["n_measured"])
    quality = classify_quality(r["enrollment"], int(r["intervals"]), int(r["gaps"]),
                               r["first_scanned_at"], r["first_top_at"], bool(r["has_live"]))
    current = bool(r["in_top"]) and r["current_rank"] is not None
    legs = int(r["legs"])
    return {
        "wallet": r["wallet"], "status": "current" if current else "former",
        "rank": r["current_rank"] if current else None,
        "last_rank": r["last_rank"], "best_rank": r["best_rank"],
        "appearances": r["appearances"], "entries": r["entries"], "exits": r["exits"],
        "re_entered": r["entries"] > 1, "first_top_at": r["first_top_at"],
        "last_top_at": r["last_top_at"],
        "agora_pnl": _dec(r["pnl"]), "agora_worth": _dec(r["worth"]),
        "agora_volume": _dec(r["volume"]), "agora_trades": r["agora_trades"],
        "legs": legs, "buys": int(r["buys"]), "sells": int(r["sells"]),
        "transfers": int(r["transfers"]), "tokens": int(r["tokens"]),
        "estimated_legs": int(r["est_legs"]),
        "estimated_share": (Decimal(int(r["est_legs"])) / legs) if legs else None,
        "buy_hype": buy, "sell_hype": sell, "legs_all": int(r["legs_all"]),
        "first_trade_at": r["first_trade_at"], "last_trade_at": r["last_trade_at"],
        "measured_closes": n_meas, "wins": int(r["wins"]),
        "win_rate": (Decimal(int(r["wins"])) / n_meas) if n_meas else None,
        "measured_pnl": (Decimal(r["realized_wei"]) / E18) if n_meas else None,
        "quality": quality, "quality_note": QUALITY_NOTES[quality],
        "tracking_basis": ("none" if not r["intervals"] else
                           "live" if r["has_live"] else "derived"),
        "scan_gaps": int(r["gaps"]), "tracked_since": r["first_scanned_at"],
        "enrollment": r["enrollment"],
    }


def matches(w: Mapping[str, Any], p: IntelParams) -> bool:
    if p.membership != "all" and w["status"] != p.membership:
        return False
    if p.quality != "all" and w["quality"] != p.quality:
        return False
    if p.active and w["legs"] <= 0:
        return False
    if w["legs"] < p.min_trades:
        return False
    # rank bounds apply to the CURRENT rank: a former member has none and is excluded by them
    if p.min_rank is not None and (w["rank"] is None or w["rank"] < p.min_rank):
        return False
    if p.max_rank is not None and (w["rank"] is None or w["rank"] > p.max_rank):
        return False
    # NULL PnL is unknown, never zero: it cannot satisfy a PnL floor
    if p.min_pnl is not None and (w["agora_pnl"] is None or w["agora_pnl"] < p.min_pnl):
        return False
    if p.q:
        frag = p.q if p.q.startswith("0x") else "0x" + p.q
        if not w["wallet"].startswith(frag) and p.q not in w["wallet"]:
            return False
    return True


_SORT_VALUE = {
    "rank": lambda w: w["rank"], "pnl": lambda w: w["agora_pnl"], "worth": lambda w: w["agora_worth"],
    "volume": lambda w: w["agora_volume"], "legs": lambda w: w["legs"],
    "buy": lambda w: w["buy_hype"], "realized": lambda w: w["measured_pnl"],
    "last_trade": lambda w: w["last_trade_at"], "appearances": lambda w: w["appearances"],
    "best_rank": lambda w: w["best_rank"], "last_top": lambda w: w["last_top_at"],
    "first_top": lambda w: w["first_top_at"],
}


def order(rows: list[dict[str, Any]], p: IntelParams) -> list[dict[str, Any]]:
    """Stable order; rows with no value for the sort key always go last (never as 0)."""
    key = _SORT_VALUE[p.sort]
    have = [w for w in rows if key(w) is not None]
    none = [w for w in rows if key(w) is None]
    have.sort(key=lambda w: w["wallet"])                       # tie-break: address ascending
    have.sort(key=key, reverse=p.desc)
    none.sort(key=lambda w: w["wallet"])
    return have + none


def kpis(rows: list[dict[str, Any]], window: str) -> dict[str, Any]:
    """Counts over the whole tracked set (not the filtered page)."""
    def n(f: Any) -> int:
        return sum(1 for w in rows if f(w))
    pnls = [w["agora_pnl"] for w in rows if w["agora_pnl"] is not None and w["status"] == "current"]
    return {
        "window": window, "tracked": len(rows), "current": n(lambda w: w["status"] == "current"),
        "former": n(lambda w: w["status"] == "former"),
        "re_entered": n(lambda w: w["re_entered"]),
        "active_in_window": n(lambda w: w["legs"] > 0),
        "complete_history": n(lambda w: w["quality"] == "complete"),
        "partial_history": n(lambda w: w["quality"] in ("partial", "gaps", "unscanned")),
        "trades_in_window": sum(w["legs"] for w in rows),
        "bought_hype": sum((w["buy_hype"] for w in rows), Decimal(0)),
        "sold_hype": sum((w["sell_hype"] for w in rows), Decimal(0)),
        "median_current_pnl": (sorted(pnls)[len(pnls) // 2] if pnls else None),
    }


# --- CSV (formula-injection safe) --------------------------------------------------------------
CSV_COLUMNS = (
    "wallet", "status", "rank", "best_rank", "appearances", "entries", "first_top_at",
    "last_top_at", "agora_pnl_hype", "agora_worth_hype", "agora_volume_hype", "window",
    "trades_in_window", "buys", "sells", "tokens", "bought_hype", "sold_hype",
    "estimated_legs", "measured_closes", "measured_win_rate", "measured_pnl_hype",
    "last_trade_at", "data_quality", "tracking_basis", "tracked_since", "scan_gaps")
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(v: Any) -> Any:
    """Text that a spreadsheet could read as a formula is prefixed with an apostrophe. Numbers
    (including negative PnL) are written as numbers, never as text."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return v
    s = str(v)
    return "'" + s if s.startswith(_FORMULA) else s


def to_csv(rows: list[dict[str, Any]], window: str) -> str:
    out = io.StringIO()
    wr = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    wr.writerow(CSV_COLUMNS)
    for w in rows:
        wr.writerow([csv_cell(x) for x in (
            w["wallet"], w["status"], w["rank"], w["best_rank"], w["appearances"], w["entries"],
            w["first_top_at"], w["last_top_at"], w["agora_pnl"], w["agora_worth"],
            w["agora_volume"], window, w["legs"], w["buys"], w["sells"], w["tokens"],
            w["buy_hype"], w["sell_hype"], w["estimated_legs"], w["measured_closes"],
            w["win_rate"], w["measured_pnl"], w["last_trade_at"], w["quality"],
            w["tracking_basis"], w["tracked_since"], w["scan_gaps"])])
    return out.getvalue()


# --- the reader --------------------------------------------------------------------------------
class WalletIntel:
    """Runs the queries. `rep` is a PublicReports (read-only connection as dashboard_ro)."""

    def __init__(self, rep: Any) -> None:
        self.rep = rep

    def table(self, window: str, now: datetime) -> dict[str, Any]:
        delta = WINDOWS[window]
        since = (now - delta) if delta else datetime(2000, 1, 1, tzinfo=UTC)
        with self.rep.ro() as c:
            rows = self.rep._all(c, BASE_SQL, since=since, cap=MAX_ROWS + 1)
            snap = self.rep._one(c, """
                SELECT id, captured_at, row_count FROM leaderboard_snapshots
                WHERE chain_id = :c AND status IN ('ok', 'imported') AND complete
                ORDER BY captured_at DESC, id DESC LIMIT 1""")
        truncated = len(rows) > MAX_ROWS
        wallets = [shape(r) for r in rows[:MAX_ROWS]]
        return {"wallets": wallets, "truncated": truncated, "window": window, "since": since,
                "snapshot": snap, "kpis": kpis(wallets, window)}

    def profile(self, wallet: str, now: datetime, base: dict[str, Any] | None, *,
                step: str = "hour") -> dict[str, Any]:
        """Everything the research page shows for one wallet, from public views only."""
        if not ADDR_RE.match(wallet):
            raise ParamError("not a valid address")
        with self.rep.ro() as c:
            enrolled = self.rep._one(c, """
                SELECT 1 AS ok FROM wallet_enrollment WHERE chain_id = :c AND address = :w""",
                                     w=wallet)
            if not enrolled:
                return {}
            windows = {}
            for name in ("24h", "7d", "30d", "all"):
                delta = WINDOWS[name]
                since = (now - delta) if delta else datetime(2000, 1, 1, tzinfo=UTC)
                windows[name] = self.rep._one(c, """
                    SELECT count(*) AS legs,
                      count(*) FILTER (WHERE kind = 'BUY') AS buys,
                      count(*) FILTER (WHERE kind = 'SELL') AS sells,
                      count(*) FILTER (WHERE kind IN ('TRANSFER_IN', 'TRANSFER_OUT')) AS transfers,
                      count(DISTINCT token) AS tokens,
                      count(*) FILTER (WHERE confidence = 'estimated') AS est_legs,
                      coalesce(sum(hype_wei) FILTER (WHERE kind = 'BUY' AND confidence = 'high'), 0) AS buy_wei,
                      coalesce(sum(hype_wei) FILTER (WHERE kind = 'SELL' AND confidence = 'high'), 0) AS sell_wei
                    FROM trades WHERE chain_id = :c AND wallet = :w AND block_time >= :s""",
                                             w=wallet, s=since) or {}
                windows[name].update(self.rep._one(c, """
                    SELECT count(*) FILTER (WHERE r.status = 'measured') AS n_measured,
                      count(*) FILTER (WHERE r.status = 'measured' AND r.pnl_wei > 0) AS wins,
                      coalesce(sum(r.pnl_wei) FILTER (WHERE r.status = 'measured'), 0) AS pnl_wei
                    FROM realized_pnl r JOIN trades t ON t.id = r.trade_id
                    WHERE r.chain_id = :c AND r.wallet = :w AND t.block_time >= :s""",
                                                    w=wallet, s=since) or {})
            hist = self.rep._all(c, """
                SELECT s.captured_at, e.rank, e.pnl_usd AS pnl, e.worth_usd AS worth
                FROM leaderboard_snapshot_entries e JOIN leaderboard_snapshots s ON s.id = e.snapshot_id
                WHERE s.chain_id = :c AND s.status IN ('ok', 'imported') AND s.complete
                  AND e.address = :w ORDER BY s.captured_at, s.id""", w=wallet)
            events = self.rep._all(c, """
                SELECT kind, rank, at, snapshot_id FROM leaderboard_membership_events
                WHERE chain_id = :c AND address = :w ORDER BY at, snapshot_id, kind LIMIT 500""", w=wallet)
            cover = self.rep._all(c, """
                SELECT from_block, to_block, basis FROM wallet_scan_coverage
                WHERE chain_id = :c AND address = :w ORDER BY from_block""", w=wallet)
            tokens = self.rep._all(c, """
                SELECT t.token, count(*) AS legs,
                  count(*) FILTER (WHERE t.kind = 'BUY') AS buys,
                  count(*) FILTER (WHERE t.kind = 'SELL') AS sells,
                  coalesce(sum(t.hype_wei) FILTER (WHERE t.kind = 'BUY' AND t.confidence = 'high'), 0) AS buy_wei,
                  coalesce(sum(t.hype_wei) FILTER (WHERE t.kind = 'SELL' AND t.confidence = 'high'), 0) AS sell_wei,
                  min(t.block_time) AS first_at, max(t.block_time) AS last_at
                FROM trades t WHERE t.chain_id = :c AND t.wallet = :w
                GROUP BY t.token ORDER BY count(*) DESC, t.token LIMIT 25""", w=wallet)
            names = self.rep.token_names(c, [t["token"] for t in tokens])
            related = self.rep._all(c, """
                SELECT o.wallet AS src, i.wallet AS dst, o.token, count(*) AS n,
                       min(o.block_time) AS first_at, max(o.block_time) AS last_at
                FROM trades o JOIN trades i
                  ON i.chain_id = o.chain_id AND i.tx_hash = o.tx_hash AND i.token = o.token
                 AND i.kind = 'TRANSFER_IN' AND i.wallet <> o.wallet
                WHERE o.chain_id = :c AND o.kind = 'TRANSFER_OUT' AND (o.wallet = :w OR i.wallet = :w)
                GROUP BY o.wallet, i.wallet, o.token ORDER BY max(o.block_time) DESC LIMIT 100""",
                                   w=wallet)
        for t in tokens:
            t.update(names.get(t["token"], {}))
            t["buy_hype"], t["sell_hype"] = Decimal(t.pop("buy_wei")) / E18, Decimal(t.pop("sell_wei")) / E18
        perf = {}
        for name, r in windows.items():
            n = int(r["n_measured"])
            perf[name] = {
                "trades": int(r["legs"]), "buys": int(r["buys"]), "sells": int(r["sells"]),
                "transfers": int(r["transfers"]), "tokens": int(r["tokens"]),
                "estimated_legs": int(r["est_legs"]),
                "bought_hype": Decimal(r["buy_wei"]) / E18, "sold_hype": Decimal(r["sell_wei"]) / E18,
                "measured_closes": n, "wins": int(r["wins"]),
                "win_rate": (Decimal(int(r["wins"])) / n) if n else None,
                "measured_pnl_hype": (Decimal(r["pnl_wei"]) / E18) if n else None}
        return {"wallet": wallet, "summary": base, "performance": perf,
                "rank_history": downsample(hist, step), "rank_history_step": step,
                "rank_history_points": len(hist), "membership_events": events,
                "coverage": cover, "tokens": tokens,
                "relationships": [{**r, "evidence": "same-transaction token transfer between two "
                                   "tracked wallets"} for r in related],
                "relationships_note": "Evidence only: one transaction moved a token from one "
                                      "tracked wallet to another. It does not say who controls "
                                      "either wallet."}


def downsample(points: list[dict[str, Any]], step: str) -> list[dict[str, Any]]:
    """Last observation per bucket. `step`: snapshot (all points, capped) | hour | day."""
    if step == "snapshot":
        return points[-2000:]
    fmt = "%Y-%m-%dT%H" if step == "hour" else "%Y-%m-%d"
    out: dict[str, dict[str, Any]] = {}
    for p in points:
        out[p["captured_at"].astimezone(UTC).strftime(fmt)] = p
    return list(out.values())
