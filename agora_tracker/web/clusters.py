"""Wallet clusters: the research view over `agora_tracker/clustering.py`.

Read model only, from `public_api` views as the read-only dashboard role, so a cluster that
contains a non-public wallet does not exist as far as the website is concerned.

What is shown is evidence, never a conclusion about people: the pairs of wallets that bought the
same token in the same block, moved a token to each other inside one transaction, or entered the
same selective tokens within seconds. A cluster is "these addresses behaved as one operator would
in this period", it can be a team, a shared bot or copy-trading, and it is not an allegation.

Bundled activity collapses the trades of a cluster's members into one action per (cluster,
token), using the membership that was valid *when the trade happened*.
"""

from __future__ import annotations

import csv
import io
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .params import ADDR_RE, ParamError

UTC = timezone.utc
E18 = Decimal(10) ** 18
STATUSES = ("active", "ended", "all")
WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
KINDS = {"buy": ("BUY",), "sell": ("SELL",), "all": ("BUY", "SELL")}
WITHIN = (1, 5, 15, 60)                 # minutes after launch for the launch-proximity research
MIN_SAMPLE = 10                         # below this many first buys with a known launch time: no statistic
MAX_CLUSTERS = 2000
MAX_BUNDLES = 2000
PAGE_SIZES = (25, 50, 100)
NOTE = ("A cluster is a set of public wallets joined by observable on-chain evidence. It means "
        "these addresses behaved as one operator would in the period shown; it can equally be a "
        "team, a shared bot or copy-trading. It is not a statement about who controls them and "
        "not an allegation of any kind.")
STRENGTH_NOTES = {
    "strong": "several independent pieces of evidence (e.g. 3+ tokens bought in the same block)",
    "moderate": "repeated evidence of one kind (e.g. 2 tokens bought in the same block)",
}


# --- parameters -------------------------------------------------------------------------------
@dataclass(frozen=True)
class ClusterParams:
    status: str = "active"
    q: str = ""
    page: int = 1
    size: int = 25


@dataclass(frozen=True)
class BundleParams:
    window: str = "24h"
    kind: str = "all"
    min_wallets: int = 1
    page: int = 1
    size: int = 50


def _int(raw: str | None, name: str, lo: int, hi: int, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    t = raw.strip()
    if not (t.isascii() and t.isdigit()):
        raise ParamError(f"{name} must be a whole number")
    v = int(t)
    if not lo <= v <= hi:
        raise ParamError(f"{name} must be between {lo} and {hi}")
    return v


def _page(q: Mapping[str, str], size_default: int) -> tuple[int, int]:
    size = _int(q.get("size"), "size", 1, 100, size_default)
    if size not in PAGE_SIZES:
        raise ParamError(f"size must be one of {', '.join(map(str, PAGE_SIZES))}")
    return _int(q.get("page"), "page", 1, 10_000, 1), size


def parse_clusters(q: Mapping[str, str], *, default_size: int = 25) -> ClusterParams:
    status = (q.get("status") or "active").strip().lower()
    if status not in STATUSES:
        raise ParamError(f"status must be one of {', '.join(STATUSES)}")
    text = (q.get("q") or "").strip().lower()[:44]
    page, size = _page(q, default_size)
    return ClusterParams(status=status, q=text, page=page, size=size)


def parse_bundles(q: Mapping[str, str], *, default_size: int = 50) -> BundleParams:
    window = (q.get("window") or "24h").strip().lower()
    if window not in WINDOWS:
        raise ParamError(f"window must be one of {', '.join(WINDOWS)}")
    kind = (q.get("kind") or "all").strip().lower()
    if kind not in KINDS:
        raise ParamError(f"kind must be one of {', '.join(KINDS)}")
    page, size = _page(q, default_size)
    return BundleParams(window=window, kind=kind,
                        min_wallets=_int(q.get("min_wallets"), "min_wallets", 1, 50, 1),
                        page=page, size=size)


def parse_within(q: Mapping[str, str]) -> int:
    v = _int(q.get("within"), "within", 1, 1440, 5)
    if v not in WITHIN:
        raise ParamError(f"within must be one of {', '.join(map(str, WITHIN))} (minutes)")
    return v


# --- graph layout (server side, no script needed) ---------------------------------------------
WIDTH = {"strong": 3.5, "moderate": 1.8, "weak": 0.8}


def layout(members: list[str], edges: list[dict[str, Any]], size: int = 360) -> dict[str, Any]:
    """Nodes on a circle (ordered by address, so the picture is stable between runs), edges as
    straight lines whose width is the strength of the evidence."""
    n = len(members)
    cx = cy = size / 2
    r = size / 2 - 34
    pos: dict[str, tuple[float, float]] = {}
    for i, m in enumerate(sorted(members)):
        ang = -math.pi / 2 + (2 * math.pi * i / n if n else 0)
        pos[m] = (round(cx + r * math.cos(ang), 1), round(cy + r * math.sin(ang), 1))
    out_edges = []
    for e in edges:
        a, b = e["wallet_a"], e["wallet_b"]
        if a in pos and b in pos:
            out_edges.append({"a": a, "b": b, "strength": e["strength"],
                              "x1": pos[a][0], "y1": pos[a][1], "x2": pos[b][0], "y2": pos[b][1],
                              "w": WIDTH.get(e["strength"], 1.0), "label": edge_label(e)})
    deg: dict[str, int] = defaultdict(int)
    for e in out_edges:
        deg[e["a"]] += 1
        deg[e["b"]] += 1
    return {"size": size, "nodes": [{"addr": m, "x": x, "y": y, "degree": deg[m]}
                                    for m, (x, y) in pos.items()], "edges": out_edges}


def edge_label(e: dict[str, Any]) -> str:
    ev = e.get("evidence") or {}
    sb = (ev.get("same_block_buy") or {}).get("tokens", 0)
    tr = (ev.get("direct_transfer") or {}).get("transfers", 0)
    co = (ev.get("co_entry") or {}).get("tokens", 0)
    parts = []
    if sb:
        parts.append(f"{sb} token(s) bought in the same block")
    if tr:
        parts.append(f"{tr} same-transaction transfer(s)")
    if co:
        parts.append(f"{co} near-simultaneous first buy(ies)")
    return f"{e['strength']}: " + (", ".join(parts) or "no counts")


def summarise_edges(edges: list[dict[str, Any]]) -> dict[str, Any]:
    out = {"strong": 0, "moderate": 0, "same_block_tokens": 0, "transfers": 0, "co_entries": 0}
    for e in edges:
        if e["strength"] in ("strong", "moderate"):
            out[e["strength"]] += 1
        ev = e.get("evidence") or {}
        out["same_block_tokens"] += (ev.get("same_block_buy") or {}).get("tokens", 0)
        out["transfers"] += (ev.get("direct_transfer") or {}).get("transfers", 0)
        out["co_entries"] += (ev.get("co_entry") or {}).get("tokens", 0)
    return out


# --- queries ----------------------------------------------------------------------------------
class ClusterViews:
    """`rep` is a PublicReports (read-only connection as dashboard_ro)."""

    def __init__(self, rep: Any, version: str) -> None:
        self.rep = rep
        self.version = version

    # -- helpers ---------------------------------------------------------------------------
    def _latest_run(self, c: Any) -> dict[str, Any] | None:
        return self.rep._one(c, """
            SELECT id, as_of, edges_n, clusters_n, created_at FROM cluster_runs
            WHERE version = :ver ORDER BY as_of DESC LIMIT 1""", ver=self.version)

    def _members(self, c: Any, ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        out: dict[int, list[dict[str, Any]]] = defaultdict(list)
        if not ids:
            return out
        for r in self.rep._all(c, """
                SELECT cluster_id, address, valid_from, valid_to FROM wallet_cluster_members
                WHERE chain_id = :c AND cluster_id = ANY(:ids)
                ORDER BY cluster_id, valid_to IS NOT NULL, address, valid_from""", ids=ids):
            out[int(r["cluster_id"])].append(r)
        return out

    # -- list ------------------------------------------------------------------------------
    def table(self, now: datetime) -> dict[str, Any]:
        with self.rep.ro() as c:
            run = self._latest_run(c)
            rows = self.rep._all(c, """
                SELECT id, status, created_at, ended_at, merged_into, split_from
                FROM wallet_clusters WHERE chain_id = :c AND version = :ver
                ORDER BY id DESC LIMIT :cap""", ver=self.version, cap=MAX_CLUSTERS + 1)
            truncated = len(rows) > MAX_CLUSTERS
            rows = rows[:MAX_CLUSTERS]
            mem = self._members(c, [r["id"] for r in rows])
            edges = self.rep._all(c, """
                SELECT wallet_a, wallet_b, strength, evidence FROM wallet_edges
                WHERE run_id = :run""", run=run["id"]) if run else []
            versions = self.rep._all(c, """
                SELECT version, config, config_hash, created_at FROM cluster_versions
                ORDER BY created_at""")
        by_wallet: dict[str, int] = {}
        items = []
        for r in rows:
            ms = mem.get(int(r["id"]), [])
            current = sorted({m["address"] for m in ms if m["valid_to"] is None})
            ever = sorted({m["address"] for m in ms})
            if r["status"] == "active":
                for a in current:
                    by_wallet[a] = int(r["id"])
            shown = current if r["status"] == "active" else ever
            eds = [e for e in edges if e["wallet_a"] in shown and e["wallet_b"] in shown] \
                if r["status"] == "active" else []
            items.append({**r, "size": len(shown), "members": shown,
                          "evidence": summarise_edges(eds) if eds else None,
                          "age_s": int(((r["ended_at"] or now) - r["created_at"]).total_seconds())})
        return {"clusters": items, "run": run, "truncated": truncated, "versions": versions,
                "wallet_cluster": by_wallet}

    # -- one cluster -----------------------------------------------------------------------
    def detail(self, cid: int, now: datetime) -> dict[str, Any]:
        with self.rep.ro() as c:
            k = self.rep._one(c, """
                SELECT id, status, created_at, ended_at, merged_into, split_from, created_run_id,
                       ended_run_id FROM wallet_clusters
                WHERE chain_id = :c AND version = :ver AND id = :i""", ver=self.version, i=cid)
            if not k:
                return {}
            members = self._members(c, [cid])[cid]
            events = self.rep._all(c, """
                SELECT id, at, kind, detail, run_id FROM cluster_events
                WHERE cluster_id = :i ORDER BY at DESC, id DESC LIMIT 200""", i=cid)
            children = self.rep._all(c, """
                SELECT id, status, created_at, 'absorbed' AS relation FROM wallet_clusters
                WHERE chain_id = :c AND version = :ver AND merged_into = :i
                UNION ALL
                SELECT id, status, created_at, 'split' AS relation FROM wallet_clusters
                WHERE chain_id = :c AND version = :ver AND split_from = :i
                ORDER BY id""", ver=self.version, i=cid)
            run = self.rep._one(c, """
                SELECT id, as_of FROM cluster_runs WHERE version = :ver
                  AND (CAST(:end AS timestamptz) IS NULL OR as_of < :end)
                ORDER BY as_of DESC LIMIT 1""", ver=self.version, end=k["ended_at"])
            current = sorted({m["address"] for m in members if m["valid_to"] is None})
            ever = sorted({m["address"] for m in members})
            shown = current if k["status"] == "active" else ever
            edges = self.rep._all(c, """
                SELECT wallet_a, wallet_b, strength, evidence FROM wallet_edges
                WHERE run_id = :run AND wallet_a = ANY(:ws) AND wallet_b = ANY(:ws)
                ORDER BY strength, wallet_a, wallet_b""", run=run["id"], ws=shown) if run else []
        return {"cluster": k, "version": self.version, "note": NOTE,
                "members": members, "current": current, "ever": ever, "events": events,
                "related": children, "edges": edges, "edges_run": run,
                "edges_retained": bool(edges),
                "evidence": summarise_edges(edges), "graph": layout(shown, edges),
                "strength_notes": STRENGTH_NOTES}

    # -- bundled activity ------------------------------------------------------------------
    def bundles(self, p: BundleParams, now: datetime) -> dict[str, Any]:
        since = now - WINDOWS[p.window]
        with self.rep.ro() as c:
            rows = self.rep._all(c, """
                SELECT m.cluster_id, t.token, t.kind, count(*) AS legs,
                       count(DISTINCT t.wallet) AS wallets, array_agg(DISTINCT t.wallet) AS addrs,
                       min(t.block_time) AS first_at, max(t.block_time) AS last_at,
                       coalesce(sum(t.hype_wei) FILTER (WHERE t.confidence = 'high'), 0) AS high_wei,
                       coalesce(sum(t.hype_wei) FILTER (WHERE t.confidence = 'estimated'), 0) AS est_wei,
                       count(*) FILTER (WHERE t.hype_wei IS NULL
                                        OR t.confidence NOT IN ('high', 'estimated')) AS unknown_legs
                FROM trades t
                JOIN wallet_cluster_members m
                  ON m.chain_id = t.chain_id AND m.address = t.wallet
                 AND m.valid_from <= t.block_time AND (m.valid_to IS NULL OR m.valid_to > t.block_time)
                JOIN wallet_clusters k ON k.id = m.cluster_id AND k.version = :ver
                WHERE t.chain_id = :c AND t.kind = ANY(:kinds) AND t.block_time >= :since
                GROUP BY m.cluster_id, t.token, t.kind
                HAVING count(DISTINCT t.wallet) >= :minw
                ORDER BY max(t.block_time) DESC, m.cluster_id, t.token LIMIT :cap""",
                                ver=self.version, kinds=list(KINDS[p.kind]), since=since,
                                minw=p.min_wallets, cap=MAX_BUNDLES + 1)
            truncated = len(rows) > MAX_BUNDLES
            rows = rows[:MAX_BUNDLES]
            names = self.rep.token_names(c, sorted({r["token"] for r in rows}))
            run = self._latest_run(c)
            total = self.rep._one(c, """
                SELECT count(*) AS legs, count(DISTINCT t.wallet) AS wallets
                FROM trades t
                JOIN wallet_cluster_members m
                  ON m.chain_id = t.chain_id AND m.address = t.wallet
                 AND m.valid_from <= t.block_time AND (m.valid_to IS NULL OR m.valid_to > t.block_time)
                JOIN wallet_clusters k ON k.id = m.cluster_id AND k.version = :ver
                WHERE t.chain_id = :c AND t.kind = ANY(:kinds) AND t.block_time >= :since""",
                                  ver=self.version, kinds=list(KINDS[p.kind]), since=since) or {}
        items = []
        for r in rows:
            nm = names.get(r["token"], {})
            items.append({**r, "symbol": nm.get("symbol"), "name": nm.get("name"),
                          "high_hype": Decimal(r["high_wei"]) / E18,
                          "est_hype": Decimal(r["est_wei"]) / E18,
                          "wallets_list": sorted(r["addrs"])})
        return {"items": items, "truncated": truncated, "run": run, "since": since,
                "kpis": {"actions": len(items), "legs": int(total.get("legs") or 0),
                         "wallets": int(total.get("wallets") or 0),
                         "multi_wallet": sum(1 for i in items if i["wallets"] > 1)}}

    # -- launch proximity (descriptive research, not an accusation) ---------------------------
    def launch_proximity(self, cid: int, within_min: int) -> dict[str, Any]:
        win_s = within_min * 60
        with self.rep.ro() as c:
            k = self.rep._one(c, """
                SELECT id, status FROM wallet_clusters
                WHERE chain_id = :c AND version = :ver AND id = :i""", ver=self.version, i=cid)
            if not k:
                return {}
            ws = sorted({r["address"] for r in self.rep._all(c, """
                SELECT address FROM wallet_cluster_members WHERE cluster_id = :i""", i=cid)})
            per_wallet = self.rep._all(c, """
                WITH fb AS (SELECT t.wallet, t.token, min(t.block_time) AS first_at
                            FROM trades t WHERE t.chain_id = :c AND t.kind = 'BUY'
                              AND t.wallet = ANY(:ws) GROUP BY t.wallet, t.token)
                SELECT fb.wallet,
                       count(*) AS first_buys,
                       count(*) FILTER (WHERE p.launched_at IS NOT NULL) AS with_launch,
                       count(*) FILTER (WHERE p.launched_at IS NOT NULL
                                        AND fb.first_at < p.launched_at) AS before_launch,
                       count(*) FILTER (WHERE p.launched_at IS NOT NULL
                                        AND fb.first_at >= p.launched_at
                                        AND extract(epoch FROM fb.first_at - p.launched_at) <= :win)
                           AS near_launch
                FROM fb LEFT JOIN token_profiles p ON p.chain_id = :c AND p.address = fb.token
                GROUP BY fb.wallet ORDER BY fb.wallet""", ws=ws, win=win_s)
            base = self._baseline(c, win_s)
        n = sum(int(r["with_launch"]) - int(r["before_launch"]) for r in per_wallet)
        near = sum(int(r["near_launch"]) for r in per_wallet)
        return {"cluster_id": cid, "within_min": within_min, "windows": list(WITHIN),
                "sample": n, "near": near, "min_sample": MIN_SAMPLE,
                "share": (near / n) if n >= MIN_SAMPLE else None,
                "enough": n >= MIN_SAMPLE,
                "first_buys": sum(int(r["first_buys"]) for r in per_wallet),
                "without_launch_time": sum(int(r["first_buys"]) - int(r["with_launch"])
                                           for r in per_wallet),
                "before_launch": sum(int(r["before_launch"]) for r in per_wallet),
                "baseline": base, "wallets": per_wallet,
                "note": ("Descriptive only. Buying a token soon after it launches is also what "
                         "fast traders, bots and sniping tools do, and many wallets share the "
                         "same launch feeds. This statistic is not evidence of advance knowledge, "
                         "coordination with token creators, or wrongdoing. It is shown beside "
                         "the same measure for all public wallets so the reader can judge the "
                         "difference, and it is withheld when the sample is below "
                         f"{MIN_SAMPLE} first buys.")}

    def _baseline(self, c: Any, win_s: int) -> dict[str, Any]:
        r = self.rep._one(c, """
            WITH fb AS (SELECT t.wallet, t.token, min(t.block_time) AS first_at
                        FROM trades t WHERE t.chain_id = :c AND t.kind = 'BUY'
                        GROUP BY t.wallet, t.token)
            SELECT count(*) AS n,
                   count(*) FILTER (WHERE extract(epoch FROM fb.first_at - p.launched_at) <= :win)
                       AS near
            FROM fb JOIN token_profiles p ON p.chain_id = :c AND p.address = fb.token
            WHERE p.launched_at IS NOT NULL AND fb.first_at >= p.launched_at""",
                          win=win_s) or {"n": 0, "near": 0}
        n, near = int(r["n"]), int(r["near"])
        return {"sample": n, "near": near, "share": (near / n) if n >= MIN_SAMPLE else None,
                "population": "every public wallet's first buys with a known launch time"}

    # -- per wallet ------------------------------------------------------------------------
    def for_wallet(self, wallet: str) -> list[dict[str, Any]]:
        if not ADDR_RE.match(wallet):
            raise ParamError("not a valid address")
        with self.rep.ro() as c:
            return self.rep._all(c, """
                SELECT m.cluster_id, k.status, m.valid_from, m.valid_to, k.merged_into
                FROM wallet_cluster_members m JOIN wallet_clusters k ON k.id = m.cluster_id
                WHERE m.chain_id = :c AND m.address = :w AND k.version = :ver
                ORDER BY m.valid_from DESC LIMIT 50""", w=wallet, ver=self.version)


# --- exports ----------------------------------------------------------------------------------
def _cell(v: Any) -> Any:
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":
        return "'" + v
    return v


def clusters_csv(items: list[dict[str, Any]], version: str) -> str:
    out = io.StringIO()
    wr = csv.writer(out, lineterminator="\n")
    wr.writerow(["cluster_version", "cluster_id", "status", "size", "created_at", "ended_at",
                 "merged_into", "split_from", "strong_pairs", "moderate_pairs",
                 "same_block_tokens", "transfers", "co_entries", "members"])
    for r in items:
        ev = r.get("evidence") or {}
        wr.writerow([_cell(x) for x in [
            version, r["id"], r["status"], r["size"], r["created_at"].isoformat(),
            r["ended_at"].isoformat() if r["ended_at"] else "", r["merged_into"] or "",
            r["split_from"] or "", ev.get("strong", ""), ev.get("moderate", ""),
            ev.get("same_block_tokens", ""), ev.get("transfers", ""), ev.get("co_entries", ""),
            " ".join(r["members"])]])
    return out.getvalue()


def bundles_csv(items: list[dict[str, Any]], window: str, version: str) -> str:
    out = io.StringIO()
    wr = csv.writer(out, lineterminator="\n")
    wr.writerow(["cluster_version", "window", "cluster_id", "token", "symbol", "kind", "wallets",
                 "legs", "verified_hype", "estimated_hype", "legs_without_amount", "first_at",
                 "last_at", "wallet_addresses"])
    for r in items:
        wr.writerow([_cell(x) for x in [
            version, window, r["cluster_id"], r["token"], r["symbol"] or "", r["kind"],
            r["wallets"], r["legs"], format(r["high_hype"], "f"), format(r["est_hype"], "f"),
            r["unknown_legs"], r["first_at"].isoformat(), r["last_at"].isoformat(),
            " ".join(r["wallets_list"])]])
    return out.getvalue()
