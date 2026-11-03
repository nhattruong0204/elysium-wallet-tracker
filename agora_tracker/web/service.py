"""Page builders: one function per dashboard section, shared by the HTML pages and the JSON API.

Each builder returns plain data (dicts / lists / Decimals / datetimes) computed by
`PublicReports`, i.e. by the same SQL the Telegram menu uses, restricted by the database to
public wallets and tokens. Results are cached per (section, window, parameters).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..config import Config
from ..timerange import TimeRange
from .cache import Hit, TTLCache
from .params import ListParams, paginate, range_dict, range_key
from .reports import PublicReports

UTC = timezone.utc

TOKEN_SORTS = ("last", "first", "activity", "buyers", "sellers", "net", "buy", "sell",
               "mcap", "symbol")
SM_VIEWS = ("net", "buyers", "new", "repeat", "exits", "clusters",
            "traders-agora", "traders-realized", "traders-active", "traders-consistent",
            "traders-movers")
EARLY_SORTS = ("score", "smart", "buying")

SM_LABELS = {
    "net": "Net buying", "buyers": "Most buyers", "new": "New wallets", "repeat": "Repeated accumulation",
    "exits": "Exits", "clusters": "Cluster activity", "traders-agora": "Leaders (Agora PnL)",
    "traders-realized": "Leaders (measured PnL)", "traders-active": "Most active",
    "traders-consistent": "Most consistent", "traders-movers": "Rank movers"}

PROVIDERS = (("thekleos", "Kleos"), ("boxa", "BOXA"), ("elyspump", "ElysPump"))


class NotFound(LookupError):
    pass


class Busy(RuntimeError):
    """Too many page builds are already running; the caller should retry shortly."""


class Dashboard:
    def __init__(self, reports: PublicReports, cfg: Config, cache: TTLCache | None = None):
        self.rep, self.cfg, self.w = reports, cfg, cfg.web
        self.cache = cache or TTLCache(self.w.cache_ttl_s, self.w.cache_max_entries,
                                       self.w.cache_stale_s)
        self._sem = threading.BoundedSemaphore(self.w.max_concurrent_builds)

    # --- plumbing ----------------------------------------------------------------------------
    def _cached(self, key: tuple, fn: Callable[[], Any]) -> Hit:
        def guarded() -> Any:
            if not self._sem.acquire(timeout=8):
                raise Busy("too many page builds in flight")
            try:
                return fn()
            finally:
                self._sem.release()
        return self.cache.get(key, guarded)

    @staticmethod
    def _meta(hit: Hit, tr: TimeRange | None, now: datetime) -> dict[str, Any]:
        return {"generated_at": now - timedelta(seconds=hit.age_s), "age_s": round(hit.age_s),
                "stale": hit.stale, "range": range_dict(tr) if tr else None}

    # --- overview ------------------------------------------------------------------------------
    def overview(self, tr: TimeRange, now: datetime) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            o = self.rep.market_overview(tr)
            return {
                "totals": o["totals"], "prev": o["prev"], "summary": o["summary"],
                "universe": o["universe"], "launchpads": o["launchpads"],
                "top_tokens": o["top_tokens"], "rising": o["rising"], "selling": o["selling"],
                "multi_qualified": o["multi_qualified"],
                "concentration_top3": o["concentration_top3"],
                "coverage": self.rep.coverage(tr, now),
                "health": public_health(self.rep.health(now), self.w),
                "recent": self.rep.trades(tr, limit=15),
            }
        hit = self._cached(("overview", range_key(tr)), build)
        return {**hit.value, "meta": self._meta(hit, tr, now)}

    # --- tokens ------------------------------------------------------------------------------------
    def _token_table(self, tr: TimeRange) -> Hit:
        return self._cached(("token_table", range_key(tr)), lambda: self.rep.token_table(tr))

    def tokens(self, tr: TimeRange, lp: ListParams, now: datetime) -> dict[str, Any]:
        hit = self._token_table(tr)
        tbl = hit.value
        rows = list(tbl["rows"])
        if lp.q:
            needle = lp.q.lower()
            rows = [r for r in rows if needle in r["token"] or needle in (r.get("symbol") or "").lower()
                    or needle in (r.get("name") or "").lower()]
        keyf = {
            "last": lambda r: r["last_at"], "first": lambda r: r["first_ever"],
            "activity": lambda r: r["legs"], "buyers": lambda r: r["q_buyers"],
            "sellers": lambda r: r["q_sellers"], "net": lambda r: r["q_net"],
            "buy": lambda r: r["buy"], "sell": lambda r: r["sell"],
            "symbol": lambda r: (r.get("symbol") or "").lower(),
            "mcap": lambda r: (r["market"] or {}).get("mcap_hype"),
        }[lp.sort]
        have = [r for r in rows if keyf(r) is not None]
        missing = [r for r in rows if keyf(r) is None]          # None sorts last either way
        have.sort(key=lambda r: (keyf(r), r["token"]), reverse=lp.desc)
        page = paginate(have + missing, lp.page, self.w.page_size)
        return {"page": page, "universe": tbl["universe"], "query": lp.q, "sort": lp.sort,
                "desc": lp.desc, "totals": {"tokens": len(tbl["rows"]), "matching": len(rows)},
                "meta": self._meta(hit, tr, now)}

    def token(self, addr: str, tr: TimeRange, page: int, now: datetime) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            seen = self.rep.first_trade(addr)
            if not seen.get("n"):
                raise NotFound("no public tracked-wallet activity for this token")
            d = self.rep.token_detail(addr, tr)
            series = self.rep.snapshot_series(addr, tr)
            return {"detail": d, "buyers": self.rep.token_buyers(addr, tr)["rows"],
                    "series": downsample(series, 120), "seen": seen}
        hit = self._cached(("token", addr, range_key(tr)), build)
        trades = self._cached(("token_trades", addr, range_key(tr), page), lambda: self._trades(
            tr, page, token=addr))
        v = hit.value
        return {"token": addr, "meta_token": v["detail"]["meta"], "flow": v["detail"]["flow"],
                "info": v["detail"]["info"], "perf": v["detail"]["perf"],
                "universe": v["detail"]["universe"], "buyers": v["buyers"], "series": v["series"],
                "seen": v["seen"], "trades": trades.value, "meta": self._meta(hit, tr, now)}

    def _trades(self, tr: TimeRange, page: int, **kw: Any) -> dict[str, Any]:
        size = self.w.page_size
        rows = self.rep.trades(tr, limit=size + 1, offset=(page - 1) * size, **kw)
        return {"items": rows[:size], "page": page, "has_next": len(rows) > size, "size": size}

    # --- smart money ------------------------------------------------------------------------------------
    def smart_money(self, tr: TimeRange, view: str, page: int, now: datetime) -> dict[str, Any]:
        if view not in SM_VIEWS:
            view = "net"

        def build() -> dict[str, Any]:
            if view.startswith("traders-"):
                d = self.rep.top_traders(tr, view.split("-", 1)[1], limit=200)
                return {"kind": "wallets", "rows": d["rows"], "universe": d["universe"],
                        "note": d["note"]}
            d = self.rep.smart_money(tr, view)
            return {"kind": "clusters" if view == "clusters" else "tokens", "rows": d["rows"],
                    "universe": d["universe"], "note": d["note"]}
        hit = self._cached(("smart", view, range_key(tr)), build)
        v = hit.value
        return {"view": view, "views": SM_LABELS, "kind": v["kind"], "universe": v["universe"],
                "note": v["note"], "page": paginate(v["rows"], page, self.w.page_size),
                "meta": self._meta(hit, tr, now)}

    def wallet(self, addr: str, tr: TimeRange, page: int, now: datetime) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            if not self.rep.is_public_wallet(addr):
                raise NotFound("this wallet is not on the public leaderboard")
            return self.rep.wallet_detail(addr, tr)
        hit = self._cached(("wallet", addr, range_key(tr)), build)
        trades = self._cached(("wallet_trades", addr, range_key(tr), page),
                              lambda: self._trades(tr, page, wallet=addr))
        d = dict(hit.value)
        d.pop("recent", None)
        return {**d, "trades": trades.value, "meta": self._meta(hit, tr, now)}

    # --- early plays ------------------------------------------------------------------------------------
    def early(self, tr: TimeRange, sort: str, now: datetime) -> dict[str, Any]:
        if sort not in EARLY_SORTS:
            sort = "score"

        tbl = self._token_table(tr).value            # shared, cached pieces: fetched
        ov = self.overview(tr, now)                  # before (not inside) this page's build

        def build() -> dict[str, Any]:
            t = self.rep.trending(tr, sort, now=now)
            new = [r for r in tbl["rows"] if r["new_in_window"]]
            new.sort(key=lambda r: (-r["q_buyers"], -r["buyers"], r["first_ever"], r["token"]))
            clusters = self.rep.smart_money(tr, "clusters")["rows"][:15]
            return {"trending": t["rows"][:30], "note": t["note"], "live": t["live"],
                    "universe": t["universe"], "new": new[:30], "rising": ov["rising"],
                    "multi_qualified": ov["multi_qualified"], "clusters": clusters,
                    "window": t["window"]}
        hit = self._cached(("early", sort, range_key(tr)), build)
        return {"sort": sort, **hit.value, "meta": self._meta(hit, tr, now)}

    # --- scanner health ----------------------------------------------------------------------------------
    def health(self, now: datetime) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            h = self.rep.health(now)
            snaps = self.rep.snapshot_history(12) if h.get("db_ok") else []
            return {"health": public_health(h, self.w), "snapshots": snaps}
        hit = self._cached(("health",), build)
        return {**hit.value, "meta": self._meta(hit, None, now)}

    # --- search ---------------------------------------------------------------------------------------------
    def search(self, q: str) -> dict[str, Any]:
        if not q.strip():
            return {"kind": "none"}
        return self._cached(("search", q.strip().lower()), lambda: self.rep.search(q)).value


# --- helpers -------------------------------------------------------------------------------------------------
def provider_label(host: str | None, n: int) -> str:
    h = (host or "").lower()
    for needle, label in PROVIDERS:
        if needle in h:
            return label
    return f"node {n}"


def public_health(h: dict[str, Any], w: Any) -> dict[str, Any]:
    """Public-safe scanner health: states and ages, no hostnames, no URLs, no sizes of the
    host, no queue contents. `status` is ok / degraded / down with the reasons spelled out."""
    if not h.get("db_ok"):
        return {"db_ok": False, "status": "down", "reasons": ["database unreachable"],
                "checks": []}
    cov, now = h["coverage"], datetime.now(UTC)
    heads = h.get("heads") or {}
    nodes = [{"label": provider_label(e.get("host"), i + 1), "head": e.get("head"),
              "lagging": bool(e.get("lagging")),
              "behind": (heads["best"] - e["head"]) if (heads.get("best") is not None
                                                        and e.get("head") is not None) else None}
             for i, e in enumerate(heads.get("endpoints") or [])]
    coll = h.get("collector") or {}
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    status = "ok"

    def add(name: str, state: str, detail: str) -> None:
        nonlocal status
        checks.append({"name": name, "state": state, "detail": detail})
        if state == "down":
            status = "down"
            reasons.append(f"{name}: {detail}")
        elif state == "warn" and status == "ok":
            status = "degraded"
            reasons.append(f"{name}: {detail}")

    age = cov.get("heartbeat_age_s")
    add("Indexer heartbeat", "unknown" if age is None else "down" if age > 300
        else "warn" if age > 120 else "ok",
        "no heartbeat recorded" if age is None else f"{age}s ago")
    lag = cov.get("lag")
    add("Block lag", "unknown" if lag is None else "warn" if lag > 120 else "ok",
        "chain head unknown" if lag is None else f"{lag} blocks behind the best node")
    lb = h.get("lb_ok")
    lb_age = (now - lb).total_seconds() if lb else None
    add("Leaderboard refresh", "down" if lb_age is None else "warn" if lb_age > 1800 else "ok",
        "never succeeded" if lb_age is None else f"last accepted snapshot {int(lb_age // 60)} min ago")
    m_at = h.get("market_at")
    m_age = (now - m_at).total_seconds() if m_at else None
    add("Market snapshots", "warn" if m_age is None or m_age > w.market_max_age_s else "ok",
        "none stored yet" if m_age is None else f"newest {int(m_age // 60)} min ago, "
        f"{h.get('market_tokens') or 0} tokens in 24h")
    ob = h.get("outbox") or {}
    add("Alert queue", "warn" if ob.get("dead") else "ok",
        f"{ob.get('pending', 0)} pending, {ob.get('sent', 0)} sent, {ob.get('dead', 0)} failed")
    ui_age = h.get("ui_heartbeat_age_s")
    add("Telegram menu", "unknown" if ui_age is None else "warn" if ui_age > 180 else "ok",
        "no heartbeat recorded" if ui_age is None else f"heartbeat {ui_age}s ago")
    if coll and coll.get("ok") is False:
        add("Market collector", "warn", str(coll.get("error") or "last round failed")[:120])
    return {
        "db_ok": True, "status": status, "reasons": reasons, "checks": checks, "nodes": nodes,
        "cursor": cov.get("cursor"), "head": cov.get("head"), "lag": lag,
        "heartbeat_age_s": age, "last_trade": h.get("last_trade"), "lb_ok": lb, "lb_any": h.get("lb_any"),
        "market_at": m_at, "market_tokens": h.get("market_tokens"),
        "market_since": h.get("market_since"), "trades": h.get("trades"),
        "wallets": h.get("wallets"), "outbox": ob, "coverage_status": cov.get("status"),
        "tracking_since": cov.get("tracking_since"), "indexed_through": cov.get("indexed_through"),
        "warnings": [wn for wn in h.get("warnings", []) if "disk" not in wn.lower()],
    }


def downsample(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    if len(rows) <= n:
        return rows
    step = (len(rows) - 1) / (n - 1)
    return [rows[round(i * step)] for i in range(n)]


def sparkline(series: list[dict[str, Any]], key: str = "price_hype", w: int = 320,
              h: int = 56) -> dict[str, Any] | None:
    """Points for a server-rendered SVG polyline (no JS chart library)."""
    pts = [(r["captured_at"].timestamp(), float(r[key])) for r in series
           if r.get(key) is not None and r[key] > 0]
    if len(pts) < 2:
        return None
    x0, x1 = pts[0][0], pts[-1][0]
    lo, hi = min(p[1] for p in pts), max(p[1] for p in pts)
    span_y = (hi - lo) or 1.0
    span_x = (x1 - x0) or 1.0
    d = " ".join(f"{(x - x0) / span_x * w:.1f},{h - 4 - (y - lo) / span_y * (h - 8):.1f}"
                 for x, y in pts)
    return {"points": d, "w": w, "h": h, "lo": lo, "hi": hi, "first": pts[0][1],
            "last": pts[-1][1], "n": len(pts), "up": pts[-1][1] >= pts[0][1]}


def decimal_or_none(v: Any) -> Decimal | None:
    return None if v is None else Decimal(v)
