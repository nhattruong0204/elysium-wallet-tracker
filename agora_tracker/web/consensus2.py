"""Consensus v2: the same consensus events as v1, with independence judged by wallet clusters.

v1 (web/consensus.py, unchanged) collapses buyers that bought in one block or that look like
bundle wallets. v2 replaces the bundle heuristic with the evidence-based clusters of
`agora_tracker/clustering.py` and keeps the same-block rule, so a wallet pair that has been seen
acting together counts as one actor everywhere, not only in the block it was caught in.

Independent actors = connected components of the consensus members, where two members are joined
if they were in the same cluster or bought in the same block.

Views (`view=`), all versioned (`consensus_version: v2`, `cluster_version`) and honest about time:

* ``current``     raw trader threshold (like v1); cluster membership as known **now**. Answers
                  "what do we believe today about the people behind this signal?" It uses
                  knowledge the signal's own time did not have, so it is for reading, not for
                  judging how a signal would have looked when it fired.
* ``historical``  raw trader threshold; membership as known **when each wallet made its first
                  buy** (valid_from <= t < valid_to). No look-ahead: a cluster recognised later
                  does not change an earlier signal.
* ``adjusted``    like historical, but the threshold itself counts independent actors, so a
                  signal built from one cluster's wallets does not qualify as consensus.

v1 stays at /consensus with its own semantics and numbers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .consensus import (
    NET_CHOICES,
    RANGE_ALL,
    RANGE_CHOICES,
    TRADER_CHOICES,
    WINDOW_CHOICES,
    ConsensusParams,
    _pick,
)

VERSION = "v2"
VIEWS = ("current", "historical", "adjusted")
VIEW_LABELS = {
    "current": "Current knowledge",
    "historical": "Historical (as known then)",
    "adjusted": "Cluster-adjusted (as known then)"}
VIEW_NOTES = {
    "current": "Cluster membership as known today. Useful to read a signal now; it uses knowledge "
               "the signal did not have when it fired.",
    "historical": "Cluster membership as it was known when each wallet made its first buy. A "
                  "cluster recognised later does not change an earlier signal.",
    "adjusted": "Like historical, and the trader threshold counts independent actors: wallets "
                "of one cluster, or buying in one block, count once."}
RANGES = {**{k: k for k in RANGE_CHOICES}, "30d": "30d"}
RANGE_LABELS = {"6h": "6 hours", "24h": "24 hours", "3d": "3 days", "7d": "7 days",
                "30d": "30 days"}


@dataclass(frozen=True)
class ConsensusV2Params:
    min_traders: int = 3
    window_min: int = 30
    min_net_hype: float = 1.0
    range: str = "24h"
    launchpad: str = ""
    view: str = "historical"

    def key(self) -> tuple:
        return (self.min_traders, self.window_min, self.min_net_hype, self.range,
                self.launchpad, self.view)

    def as_v1(self) -> ConsensusParams:
        """The sweep parameters: only `adjusted` makes the threshold count independent actors."""
        return ConsensusParams(self.min_traders, self.window_min, self.min_net_hype, self.range,
                               self.launchpad, self.view == "adjusted")


def parse_params(q: Mapping[str, str]) -> ConsensusV2Params:
    d = ConsensusV2Params()
    lp = (q.get("launchpad") or "").strip().lower()
    view = (q.get("view") or d.view).strip().lower()
    rng = q.get("range") if q.get("range") in RANGES else d.range
    if rng == "30d" and view != "historical":
        rng = d.range                       # the long range is for reading history only
    return ConsensusV2Params(
        min_traders=_pick(q.get("traders"), TRADER_CHOICES, d.min_traders, int),
        window_min=_pick(q.get("window"), WINDOW_CHOICES, d.window_min, int),
        min_net_hype=_pick(q.get("min_net"), NET_CHOICES, d.min_net_hype, float),
        range=rng or d.range, launchpad=lp if lp.isalnum() and len(lp) <= 24 else "",
        view=view if view in VIEWS else d.view)


# --- cluster membership over time -------------------------------------------------------------
class Intervals:
    """Cluster membership of wallets over time, from `wallet_cluster_members` rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.by_wallet: dict[str, list[tuple[datetime, datetime | None, int]]] = defaultdict(list)
        for r in rows:
            self.by_wallet[r["address"]].append((r["valid_from"], r["valid_to"],
                                                 int(r["cluster_id"])))
        for v in self.by_wallet.values():
            v.sort()

    def at(self, wallet: str, ts: datetime) -> int | None:
        """The cluster the wallet belonged to at `ts` (valid_from <= ts < valid_to)."""
        for lo, hi, cid in self.by_wallet.get(wallet, ()):
            if lo <= ts and (hi is None or ts < hi):
                return cid
        return None

    def now(self, wallet: str) -> int | None:
        for _lo, hi, cid in self.by_wallet.get(wallet, ()):
            if hi is None:
                return cid
        return None

    def resolver(self, view: str) -> Callable[[str, datetime], int | None]:
        if view == "current":
            return lambda w, _ts: self.now(w)
        return self.at


def count_actors(members: list[tuple[str, datetime, int]],
                 cluster_of: Callable[[str, datetime], int | None]) -> int:
    """Independent actors among (wallet, first buy time, block): members are one actor when they
    share a cluster or a block (transitively)."""
    parent = list(range(len(members)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[tuple[str, int], int] = {}
    for i, (w, ts, blk) in enumerate(members):
        keys = [("blk", blk)]
        cid = cluster_of(w, ts)
        if cid is not None:
            keys.append(("cl", cid))
        for k in keys:
            if k in seen:
                parent[find(i)] = find(seen[k])
            else:
                seen[k] = i
    return len({find(i) for i in range(len(members))})


def actors_counter(iv: Intervals, view: str) -> Callable[[list[tuple[str, datetime, int]]], int]:
    res = iv.resolver(view)
    return lambda members: count_actors(members, res)


def enrich(cards: list[dict[str, Any]], iv: Intervals, view: str) -> list[dict[str, Any]]:
    """Add the v2 fields to v1-shaped cards: `actors`, per-member `cluster_id`, `clusters`."""
    res = iv.resolver(view)
    for k in cards:
        ms = [(m["wallet"], m["first_at"], m["block"]) for m in k["members"]]
        k["actors"] = count_actors(ms, res)
        per: dict[int, int] = defaultdict(int)
        for m in k["members"]:
            cid = res(m["wallet"], m["first_at"])
            m["cluster_id"] = cid
            if cid is not None:
                per[cid] += 1
        k["clusters"] = [{"id": cid, "wallets": n} for cid, n in sorted(per.items())]
        k["clustered"] = sum(per.values())
        k["independent_v1"] = k["independent"]
    return cards
