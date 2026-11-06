"""Page builders: one function per dashboard section, shared by the HTML pages and the JSON API.

Each builder returns plain data (dicts / lists / Decimals / datetimes) computed by
`PublicReports`, i.e. by the same SQL the Telegram menu uses, restricted by the database to
public wallets and tokens. Results are cached per (section, window, parameters).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..config import Config
from ..timerange import TimeRange
from .activity import (
    ActivityParams,
    encode_cursor,
)
from .amounts import fmt_hype
from .cache import Hit, TTLCache
from .clusters import NOTE as CLUSTER_NOTE
from .clusters import BundleParams, ClusterParams, ClusterViews
from .consensus import ConsensusParams
from .intel import SORTS as INTEL_SORTS
from .intel import IntelParams, WalletIntel
from .intel import matches as intel_matches
from .intel import order as intel_order
from .params import ListParams, paginate, range_dict, range_key
from .reports import PublicReports, sort_key

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


log = logging.getLogger(__name__)

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
        # Activity is polled by every open tab, so it has its own short-lived, single-flight
        # cache: N tabs asking the same question cost one query per few seconds.
        self.act_cache = TTLCache(self.w.activity_cache_ttl_s, 512, 60.0)
        self.intel = WalletIntel(reports, cfg.scoring.version)
        self.clusters = ClusterViews(reports, cfg.clustering.version)
        self.links: Any = None        # set by create_app (needs the Links helper)
        self._bundles: set[str] = set()
        self._bundles_at = float("-inf")

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

    def _acached(self, key: tuple, fn: Callable[[], Any]) -> Hit:
        def guarded() -> Any:
            if not self._sem.acquire(timeout=8):
                raise Busy("too many page builds in flight")
            try:
                return fn()
            finally:
                self._sem.release()
        return self.act_cache.get(key, guarded)

    @staticmethod
    def _meta(hit: Hit, tr: TimeRange | None, now: datetime) -> dict[str, Any]:
        return {"generated_at": now - timedelta(seconds=hit.age_s), "age_s": round(hit.age_s),
                "stale": hit.stale, "range": range_dict(tr) if tr else None}

    def _attach_market(self, rows: list[dict[str, Any]], now: datetime,
                       token_key: str = "token") -> list[dict[str, Any]]:
        """Add `market_cap` (the public USD contract of each token's newest stored snapshot,
        or None) to rows that carry a token address. One query for the whole list."""
        addrs = sorted({r[token_key] for r in rows if r.get(token_key)})
        usd = self.rep.latest_usd(addrs, now)
        for r in rows:
            tk = r.get(token_key)
            r["market_cap"] = usd.get(tk) if tk else None
        return rows

    # --- overview ------------------------------------------------------------------------------
    def overview(self, tr: TimeRange, now: datetime) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            o = self.rep.market_overview(tr)
            return {
                "totals": o["totals"], "prev": o["prev"], "summary": o["summary"],
                "universe": o["universe"], "launchpads": o["launchpads"],
                "top_tokens": self._attach_market(list(o["top_tokens"]), now), "rising": o["rising"], "selling": o["selling"],
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
            # USD on a single scale; tokens without a valid USD value sort last either way
            "mcap": lambda r: ((r["market"] or {}).get("usd") or {}).get("market_cap_usd"),
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
                    "series": downsample(series, 120), "seen": seen,
                    "market_cap": self.rep.latest_usd([addr], now).get(addr)}
        hit = self._cached(("token", addr, range_key(tr)), build)
        trades = self._cached(("token_trades", addr, range_key(tr), page), lambda: self._trades(
            tr, page, token=addr))
        v = hit.value
        return {"token": addr, "meta_token": v["detail"]["meta"], "flow": v["detail"]["flow"],
                "info": v["detail"]["info"], "perf": v["detail"]["perf"],
                "universe": v["detail"]["universe"], "buyers": v["buyers"], "series": v["series"],
                "seen": v["seen"], "market_cap": v["market_cap"], "trades": trades.value,
                "meta": self._meta(hit, tr, now)}

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
            if view != "clusters":
                self._attach_market(d["rows"], now)
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
            for r in t["rows"]:
                r.pop("mcap", None)          # HYPE-denominated; the USD contract replaces it
            self._attach_market(t["rows"][:30], now)
            new = [r for r in tbl["rows"] if r["new_in_window"]]
            new.sort(key=lambda r: (-r["q_buyers"], -r["buyers"], r["first_ever"], r["token"]))
            clusters = self.rep.smart_money(tr, "clusters")["rows"][:15]
            return {"trending": t["rows"][:30], "note": t["note"], "live": t["live"],
                    "universe": t["universe"], "new": new[:30], "rising": ov["rising"],
                    "multi_qualified": ov["multi_qualified"], "clusters": clusters,
                    "window": t["window"]}
        hit = self._cached(("early", sort, range_key(tr)), build)
        return {"sort": sort, **hit.value, "meta": self._meta(hit, tr, now)}

    # --- consensus ---------------------------------------------------------------------------------
    def consensus(self, p: ConsensusParams, now: datetime) -> dict[str, Any]:
        """Consensus cards for these thresholds. Cached per setting (short: it is a live view),
        bundle wallets per 30 min (they move slowly and cost a 14-day pass)."""
        if time.monotonic() - self._bundles_at > 1800:
            self._bundles_at = time.monotonic()      # also on failure: retry next interval
            try:
                self._bundles = self.rep.consensus_bundles(now)
            except Exception:  # noqa: BLE001 - without them buyers are just not collapsed
                log.warning("consensus: bundle wallet refresh failed", exc_info=True)
        bundles = self._bundles
        hit = self._cached(("consensus", p.key()), lambda: self.rep.consensus(p, now, bundles))
        return {**hit.value, "bundle_wallets": len(bundles),
                "meta": {"generated_at": now, "age_s": round(hit.age_s), "stale": hit.stale}}

    # --- wallet intelligence -----------------------------------------------------------------------
    def _intel_rows(self, p: IntelParams) -> tuple[dict[str, Any], list[dict[str, Any]], Hit]:
        hit = self._cached(("intel", p.window), lambda: self.intel.table(p.window, datetime.now(UTC)))
        v = hit.value
        return v, intel_order([w for w in v["wallets"] if intel_matches(w, p)], p), hit

    def intel_table(self, p: IntelParams, now: datetime) -> dict[str, Any]:
        v, rows, hit = self._intel_rows(p)
        return {"params": p, "page": paginate(rows, p.page, p.size), "kpis": v["kpis"],
                "snapshot": v["snapshot"], "truncated": v["truncated"], "window": p.window,
                "since": v["since"], "filtered": len(rows), "tracked": len(v["wallets"]),
                "score_version": self.cfg.scoring.version,
                "sorts": {k: lbl for k, (lbl, _) in INTEL_SORTS.items()},
                "meta": {"generated_at": now, "age_s": round(hit.age_s), "stale": hit.stale,
                         "timezone": "UTC"}}

    def intel_export(self, p: IntelParams) -> list[dict[str, Any]]:
        return self._intel_rows(p)[1]

    def intel_wallet(self, addr: str, step: str, now: datetime) -> dict[str, Any]:
        v, _, _ = self._intel_rows(IntelParams(window="all"))     # outside the build: no nesting
        base = next((w for w in v["wallets"] if w["wallet"] == addr), None)
        if base is None:
            raise NotFound("this wallet has never been on the public leaderboard")

        def build() -> dict[str, Any]:
            prof = self.intel.profile(addr, datetime.now(UTC), base, step=step)
            if prof:
                prof["clusters"] = self.clusters.for_wallet(addr)
                prof["clusters_version"] = self.cfg.clustering.version
                prof["clusters_note"] = CLUSTER_NOTE
            return prof
        hit = self._cached(("intel_wallet", addr, step), build)
        if not hit.value:
            raise NotFound("this wallet has never been on the public leaderboard")
        return {**hit.value, "meta": {"generated_at": now, "age_s": round(hit.age_s),
                                      "stale": hit.stale, "timezone": "UTC"}}

    # --- wallet clusters ---------------------------------------------------------------------------
    def _meta_now(self, hit: Hit, now: datetime) -> dict[str, Any]:
        return {"generated_at": now, "age_s": round(hit.age_s), "stale": hit.stale,
                "timezone": "UTC"}

    def cluster_table(self, p: ClusterParams, now: datetime) -> dict[str, Any]:
        hit = self._cached(("clusters",), lambda: self.clusters.table(datetime.now(UTC)))
        v = hit.value
        rows = [r for r in v["clusters"] if p.status == "all" or
                (p.status == "active") == (r["status"] == "active")]
        if p.q:
            q = p.q.lstrip("#")
            rows = [r for r in rows if q.isdigit() and str(r["id"]) == q
                    or any(q in m for m in r["members"])]
        rows.sort(key=lambda r: (r["status"] != "active", -r["size"], r["id"]))
        active = [r for r in v["clusters"] if r["status"] == "active"]
        return {"params": p, "page": paginate(rows, p.page, p.size), "run": v["run"],
                "version": self.cfg.clustering.version, "versions": v["versions"],
                "kpis": {"active": len(active), "ended": len(v["clusters"]) - len(active),
                         "wallets": sum(r["size"] for r in active),
                         "largest": max((r["size"] for r in active), default=0)},
                "filtered": len(rows), "truncated": v["truncated"], "note": CLUSTER_NOTE,
                "all_rows": rows, "meta": self._meta_now(hit, now)}

    def cluster_detail(self, cid: int, now: datetime) -> dict[str, Any]:
        hit = self._cached(("cluster", cid), lambda: self.clusters.detail(cid, datetime.now(UTC)))
        if not hit.value:
            raise NotFound("no such cluster (or it includes a wallet that is not public)")
        return {**hit.value, "meta": self._meta_now(hit, now)}

    def cluster_bundles(self, p: BundleParams, now: datetime) -> dict[str, Any]:
        hit = self._cached(("bundles", p.window, p.kind, p.min_wallets),
                           lambda: self.clusters.bundles(p, datetime.now(UTC)))
        v = hit.value
        return {"params": p, "page": paginate(v["items"], p.page, p.size), "kpis": v["kpis"],
                "run": v["run"], "since": v["since"], "truncated": v["truncated"],
                "version": self.cfg.clustering.version, "note": CLUSTER_NOTE,
                "all_rows": v["items"], "meta": self._meta_now(hit, now)}

    def cluster_launch(self, cid: int, within: int, now: datetime) -> dict[str, Any]:
        hit = self._cached(("cluster_launch", cid, within),
                           lambda: self.clusters.launch_proximity(cid, within))
        if not hit.value:
            raise NotFound("no such cluster (or it includes a wallet that is not public)")
        return {**hit.value, "meta": self._meta_now(hit, now)}

    # --- activity ----------------------------------------------------------------------------------
    def activity(self, p: ActivityParams, now: datetime) -> dict[str, Any]:
        """A keyset page (or, with since_id, only the rows added since) plus freshness."""
        head_hit = self._acached(("act_head",), lambda: self.rep.activity_head(datetime.now(UTC)))
        head = head_hit.value
        fresh = activity_freshness(head, now, head_hit.age_s)
        base = {"filters": activity_filters(p), "freshness": fresh,
                "meta": {"generated_at": now, "age_s": round(head_hit.age_s),
                         "stale": head_hit.stale,
                         "timezone": "UTC", "interval": "[start, end)"}}
        if p.since_id is not None:
            hit = self._acached(("act_since", p.cache_key()), lambda: self.rep.activity_since(
                p, self.w.activity_since_limit))
            items = [self._finish(i) for i in hit.value["items"]]
            new_head = max([head["head_id"] or 0, p.since_id] + [i["id"] for i in items])
            return {**base, "items": items, "count": len(items),
                    "overflow": hit.value["overflow"], "head_id": new_head,
                    "meta": {**base["meta"], "age_s": round(max(hit.age_s, head_hit.age_s)),
                             "stale": hit.stale or head_hit.stale}}
        page_hit = self._acached(("act_page", p.cache_key()), lambda: self.rep.activity_page(p))
        total_key = ("act_total", (p.action, p.range, p.start and int(p.start.timestamp()),
                                   p.end and int(p.end.timestamp()), p.basis, p.wallet, p.token,
                                   p.tx, p.q, p.sort in ("largest_buy", "largest_sell"),
                                   p.amount.min_hype, p.amount.max_hype))
        total_hit = self._acached(total_key, lambda: self.rep.activity_total(p))
        raw = page_hit.value["items"]
        more = page_hit.value["more"]
        nxt = prv = None
        if raw:
            first, last = raw[0], raw[-1]
            has_next = more if p.direction == "next" else True
            has_prev = (p.cursor is not None) if p.direction == "next" else more
            if has_next:
                nxt = encode_cursor(p.sort, sort_key(p.sort, last), last["id"])
            if has_prev:
                prv = encode_cursor(p.sort, sort_key(p.sort, first), first["id"])
        items = [self._finish(i) for i in raw]
        window = {"range": p.range, "start": p.start, "end": p.end,
                  "retention_start": head["first_trade_at"], "coverage": "full", "note": None}
        if p.start is not None and head["first_trade_at"] and p.start < head["first_trade_at"]:
            window["coverage"] = "partial"
            window["note"] = (f"tracking began {head['first_trade_at']:%Y-%m-%d %H:%M} UTC; "
                              "earlier activity was not observed")
        return {**base, "items": items,
                "page": {"size": p.size, "sort": p.sort, "next_cursor": nxt, "prev_cursor": prv,
                         "has_next": nxt is not None, "has_prev": prv is not None},
                "total": total_hit.value, "window": window, "head_id": head["head_id"],
                "live": p.live,
                "meta": {**base["meta"], "age_s": round(max(page_hit.age_s, head_hit.age_s)),
                         "stale": page_hit.stale or head_hit.stale}}

    def _finish(self, item: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in item.items() if not k.startswith("_")}
        if self.links is not None:
            out["tx_url"] = self.links.tx(item["tx_hash"])
        return out

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
        def build() -> dict[str, Any]:
            res = self.rep.search(q)
            if res.get("kind") == "tokens":
                self._attach_market(res["rows"], datetime.now(UTC), "address")
            return res
        return self._cached(("search", q.strip().lower()), build).value


def activity_filters(p: ActivityParams) -> dict[str, Any]:
    """Echo of the filters that were applied, so a client can see how its input was read."""
    return {"action": p.action, "range": p.range, "start": p.start, "end": p.end,
            "sort": p.sort, "basis": p.basis, "wallet": p.wallet, "token": p.token, "tx": p.tx,
            "q": p.q or None, "limit": p.size,
            "min_hype": fmt_hype(p.amount.min_hype) or None,
            "max_hype": fmt_hype(p.amount.max_hype) or None,
            "amount_filter": None if not p.amount.active else {
                "label": p.amount.label(), "applies_to": ["verified", "estimated"],
                "excludes": "rows without a known HYPE amount (transfers, unknown, legacy)"}}


def activity_freshness(head: dict[str, Any], now: datetime, age_s: float = 0) -> dict[str, Any]:
    """Browser time, scanner heartbeat, indexed block and last observed transaction, kept apart.
    `scanner_status` is "live" only when the indexer heartbeat AND its checkpoint are recent
    and it is not far behind the chain: a stopped scanner can never read as live."""
    hb, lag = head.get("heartbeat_age_s"), head.get("lag_blocks")
    cur_at, last = head.get("cursor_at"), head.get("last_trade_at")
    cur_age = None if cur_at is None else max(0, int((now - cur_at).total_seconds()))
    if hb is None or cur_age is None:
        status, note = "unknown", "no indexer heartbeat or checkpoint recorded"
    elif hb > 300 or cur_age > 900:
        status = "stale"
        note = f"the indexer has not reported for {max(hb, cur_age)}s; new activity is not being recorded"
    elif hb > 120 or cur_age > 300 or (lag is not None and lag > 120):
        status = "delayed"
        note = (f"{lag} blocks behind the chain head" if lag and lag > 120
                else f"heartbeat {hb}s old, checkpoint {cur_age}s old")
    else:
        status, note = "live", "indexer heartbeat and checkpoint are current"
    return {"server_time": now, "scanner_status": status, "scanner_note": note,
            "indexer_heartbeat_age_s": hb,
            "indexer_heartbeat_at": None if hb is None else now - timedelta(seconds=hb),
            "last_indexed_block": head.get("cursor"),
            "indexer_checkpoint_at": cur_at, "indexer_checkpoint_age_s": cur_age,
            "chain_head_block": head.get("chain_head"), "lag_blocks": lag,
            "last_observed_transaction_at": last,
            "last_observed_transaction_age_s": None if last is None
            else max(0, int((now - last).total_seconds())),
            "head_id": head.get("head_id"), "data_age_s": round(age_s)}


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
