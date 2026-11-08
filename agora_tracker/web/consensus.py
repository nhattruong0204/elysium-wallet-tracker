"""Consensus buys: several tracked wallets net-buying the same token inside one time window.

A *consensus* opens the moment at least `min_traders` distinct public wallets each hold a net
buy (buys minus sells, measured on-chain) of at least `min_net_hype` in the trailing
`window_min` minutes. It stays open while further wallets keep joining, and closes once a full
window passes without a new qualifying buy. Each card reports:

* who joined, in order, with their net HYPE at the end of the consensus (sells count against
  them, so a wallet that bought and dumped falls out),
* how many of them are *independent*: wallets that bought in the same block as each other, or
  that are known bundle wallets (repeated same-block buying across tokens), count once,
* the market cap when the threshold was reached and now.

Only verified (`confidence = 'high'`) buys and sells with a measured HYPE leg are used, and
only public (leaderboard) wallets, because the site reads through `public_api`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..launchintel import count_independent

WEI = 10**18

TRADER_CHOICES = (2, 3, 4, 5, 6, 8, 10)
WINDOW_CHOICES = (10, 15, 30, 60, 120, 240)
NET_CHOICES = (0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0)
RANGE_CHOICES = {"6h": timedelta(hours=6), "24h": timedelta(hours=24), "3d": timedelta(days=3),
                 "7d": timedelta(days=7)}
RANGE_LABELS = {"6h": "6 hours", "24h": "24 hours", "3d": "3 days", "7d": "7 days"}
RANGE_ALL = {**RANGE_CHOICES, "30d": timedelta(days=30)}      # v2 historical view only
MAX_CARDS = 60


@dataclass(frozen=True)
class ConsensusParams:
    min_traders: int = 3
    window_min: int = 30
    min_net_hype: float = 1.0
    range: str = "24h"
    launchpad: str = ""            # "" = all
    independent: bool = False      # count only independent buyers towards the threshold

    def key(self) -> tuple:
        return (self.min_traders, self.window_min, self.min_net_hype, self.range,
                self.launchpad, self.independent)


def _pick(raw: str | None, choices: tuple, default: Any, cast: Any) -> Any:
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        return default
    return v if v in choices else default


def parse_params(q: Any) -> ConsensusParams:
    """Query string -> params. Unknown or out-of-range values fall back to the defaults, so a
    hand-edited URL can never make the page expensive."""
    d = ConsensusParams()
    lp = (q.get("launchpad") or "").strip().lower()
    return ConsensusParams(
        min_traders=_pick(q.get("traders"), TRADER_CHOICES, d.min_traders, int),
        window_min=_pick(q.get("window"), WINDOW_CHOICES, d.window_min, int),
        min_net_hype=_pick(q.get("min_net"), NET_CHOICES, d.min_net_hype, float),
        range=q.get("range") if q.get("range") in RANGE_CHOICES else d.range,
        launchpad=lp if lp.isalnum() and len(lp) <= 24 else "",
        independent=q.get("independent") in ("1", "on", "true"),
    )


@dataclass
class Leg:
    wallet: str
    signed_wei: int          # + buy, - sell
    ts: datetime
    block: int
    tx: str


@dataclass
class Event:
    token: str
    trigger_at: datetime
    start: datetime
    last_join: datetime
    net: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    first: dict[str, tuple[datetime, int]] = field(default_factory=dict)
    trigger_tx: str = ""
    first_n: int = 0

    def members(self, min_wei: int) -> list[str]:
        return sorted((w for w in self.first if self.net[w] > 0 and self.net[w] >= min_wei),
                      key=lambda w: self.first[w])


Actors = Callable[[list[tuple[str, datetime, int]]], int]     # [(wallet, first buy time, block)] -> n


def sweep(token: str, legs: list[Leg], *, window: timedelta, min_traders: int, min_wei: int,
          bundles: set[str], independent: bool, actors: Actors | None = None) -> list[Event]:
    """Consensus events for one token. `legs` must be in time order. With `independent`, the
    threshold counts independent buyers: by the v1 same-block / bundle rule, or by `actors`
    (the cluster-aware counter of Consensus v2) when one is given."""
    out: list[Event] = []
    trail: list[Leg] = []           # legs inside the trailing window
    ev: Event | None = None

    def qualifiers(ls: list[Leg]) -> tuple[dict[str, int], dict[str, tuple[datetime, int]]]:
        net: dict[str, int] = defaultdict(int)
        first: dict[str, tuple[datetime, int]] = {}
        for x in ls:
            net[x.wallet] += x.signed_wei
            if x.signed_wei > 0:
                first.setdefault(x.wallet, (x.ts, x.block))
        return net, first

    def size(members: list[str], first: dict[str, tuple[datetime, int]]) -> int:
        if not independent:
            return len(members)
        if actors is not None:
            return actors([(w, first[w][0], first[w][1]) for w in members])
        return count_independent([(w, first[w][1]) for w in members], bundles)

    for leg in legs:
        if ev is not None and leg.ts > ev.last_join + window:
            out.append(ev)
            ev = None
        if ev is not None:
            ev.net[leg.wallet] += leg.signed_wei
            if leg.signed_wei > 0:
                ev.first.setdefault(leg.wallet, (leg.ts, leg.block))
                if ev.net[leg.wallet] >= min_wei:
                    ev.last_join = leg.ts
            continue
        trail.append(leg)
        cut = leg.ts - window
        while trail and trail[0].ts < cut:
            trail.pop(0)
        if leg.signed_wei <= 0:
            continue
        net, first = qualifiers(trail)
        members = [w for w in first if net[w] > 0 and net[w] >= min_wei]
        if leg.wallet in members and size(members, first) >= min_traders:
            ev = Event(token=token, trigger_at=leg.ts, start=trail[0].ts, last_join=leg.ts,
                       trigger_tx=leg.tx, first_n=len(members))
            for x in trail:
                ev.net[x.wallet] += x.signed_wei
                if x.signed_wei > 0:
                    ev.first.setdefault(x.wallet, (x.ts, x.block))
            trail = []
    if ev is not None:
        out.append(ev)
    return out


def build_cards(rows: list[dict[str, Any]], p: ConsensusParams, since: datetime,
                bundles: set[str], actors: Actors | None = None) -> list[dict[str, Any]]:
    """rows: verified BUY/SELL legs (token, wallet, kind, hype_wei, block_time, block_number,
    tx_hash) ordered by token then time. Returns cards newest first (trigger inside range)."""
    window = timedelta(minutes=p.window_min)
    min_wei = int(p.min_net_hype * WEI)
    by_token: dict[str, list[Leg]] = defaultdict(list)
    for r in rows:
        wei = int(r["hype_wei"])
        by_token[r["token"]].append(Leg(r["wallet"], wei if r["kind"] == "BUY" else -wei,
                                        r["block_time"], int(r["block_number"]), r["tx_hash"]))
    cards: list[dict[str, Any]] = []
    for token, legs in by_token.items():
        for ev in sweep(token, legs, window=window, min_traders=p.min_traders, min_wei=min_wei,
                        bundles=bundles, independent=p.independent, actors=actors):
            if ev.trigger_at < since:
                continue
            members = ev.members(min_wei)
            if not members:
                continue
            indep = count_independent([(w, ev.first[w][1]) for w in members], bundles)
            cards.append({
                "token": token, "trigger_at": ev.trigger_at, "trigger_tx": ev.trigger_tx,
                "last_join": ev.last_join, "first_n": ev.first_n,
                "duration_min": round((ev.last_join - ev.start).total_seconds() / 60),
                "traders": len(members), "independent": indep,
                "bundled": any(w in bundles for w in members),
                "net_hype": sum(ev.net[w] for w in members) / WEI,
                "members": [{"wallet": w, "net_hype": ev.net[w] / WEI,
                             "first_at": ev.first[w][0], "block": ev.first[w][1],
                             "bundle": w in bundles,
                             "same_block": sum(1 for o in members
                                               if ev.first[o][1] == ev.first[w][1]) > 1}
                            for w in members],
            })
    cards.sort(key=lambda c: c["trigger_at"], reverse=True)
    return cards
