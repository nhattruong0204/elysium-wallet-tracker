"""Evidence-based wallet clusters: versioned, point-in-time, explainable, and never an accusation.

A cluster is a set of public wallets that are joined by *observable on-chain evidence* strong
enough that treating them as independent would overstate how many separate actors acted. It says
"these addresses behaved as one operator would", never who the operator is, and it is not a
finding of wrongdoing. Wallets that coordinate with each other can be a team, a shared bot,
copy-trading, or coincidence; the evidence is shown so that the reader can judge.

Evidence classes (all counted over `lookback_days`, all strictly older than `as_of`):

* `same_block_buy`   two enrolled wallets bought the same token in the same block. Counted in
                     distinct tokens. A block where more than `same_block_crowd_cap` enrolled
                     wallets bought one token is a crowd (a launch), not a pair, and is ignored.
* `direct_transfer`  a token moved from one enrolled wallet to another in a single transaction.
* `co_entry`         both wallets made their *first ever* buy of the token within
                     `co_entry_window_s` of each other (different blocks), on a token that at most
                     `co_entry_max_wallets` enrolled wallets entered in the lookback. A hot token
                     that everybody buys is not evidence.

No funding-source evidence is available to this tracker and none is inferred or invented.

Strength of a pair (thresholds are part of the immutable version):

* strong    same-block tokens >= strong_same_block, or transfers >= strong_transfers, or
            (same-block >= moderate_same_block and (a transfer or >= combo_co_entry co-entries))
* moderate  same-block >= moderate_same_block, or transfers >= moderate_transfers, or
            (a transfer and >= combo_co_entry co-entries), or co-entries >= moderate_co_entry
* weak      any single same-block link or transfer, or co-entries >= weak_co_entry

Only strong and moderate pairs form clusters (connected components, size between
`min_cluster_size` and `max_cluster_size`). A weak pair never merges anything.

Lifecycle: every run is compared with the open clusters of the previous run. Each old cluster
continues as the new component it overlaps most (ties: the component with the smallest address).
Several old clusters continuing as one component are merged (the largest survives, the others
become `merged`); a component that no old cluster continues is a new cluster, recorded as a split
when it took members from an older one; an old cluster with no overlap with any component is
`invalidated`. Membership is stored with validity intervals, so "who was in cluster X at time T"
is a range query and later runs never rewrite the past.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from .config import ClusteringConfig

log = logging.getLogger("agora_tracker.clustering")
UTC = timezone.utc
STRENGTHS = ("strong", "moderate", "weak")
EXAMPLES = 3
BATCH = 500


class ClusteringConfigError(RuntimeError):
    pass


def config_hash(model: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
                          ).hexdigest()[:16]


def validate(cfg: ClusteringConfig) -> None:
    ints = ("lookback_days", "same_block_crowd_cap", "co_entry_window_s", "co_entry_max_wallets",
            "strong_same_block", "strong_transfers", "moderate_same_block", "moderate_transfers",
            "moderate_co_entry", "weak_co_entry", "combo_co_entry", "min_cluster_size",
            "max_cluster_size")
    for k in ints:
        if getattr(cfg, k) < 1:
            raise ClusteringConfigError(f"clustering.{k} must be >= 1")
    if cfg.same_block_crowd_cap < 2:
        raise ClusteringConfigError("clustering.same_block_crowd_cap must be >= 2")
    if cfg.min_cluster_size < 2 or cfg.max_cluster_size < cfg.min_cluster_size:
        raise ClusteringConfigError("clustering cluster size bounds are inconsistent")
    if cfg.interval_s < 60:
        raise ClusteringConfigError("clustering.interval_s must be >= 60")


def floor_interval(t: datetime, interval_s: float) -> datetime:
    step = max(1, int(interval_s))
    ts = int(t.astimezone(UTC).timestamp())
    return datetime.fromtimestamp(ts - ts % step, UTC)


# --- evidence ---------------------------------------------------------------------------------
@dataclass
class Pair:
    """Everything observed about one unordered pair of wallets."""
    same_block: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))  # token -> blocks
    transfers: set[tuple[str, str]] = field(default_factory=set)                       # (tx, token)
    co_entry: dict[str, int] = field(default_factory=dict)                            # token -> gap s
    first_at: datetime | None = None
    last_at: datetime | None = None

    def seen(self, at: datetime | None) -> None:
        if at is None:
            return
        if self.first_at is None or at < self.first_at:
            self.first_at = at
        if self.last_at is None or at > self.last_at:
            self.last_at = at

    @property
    def n_same_block(self) -> int:
        return len(self.same_block)

    @property
    def n_transfers(self) -> int:
        return len(self.transfers)

    @property
    def n_co_entry(self) -> int:
        return len(self.co_entry)

    def evidence(self) -> dict[str, Any]:
        return {
            "same_block_buy": {
                "tokens": self.n_same_block,
                "blocks": sum(len(b) for b in self.same_block.values()),
                "examples": [{"token": t, "block": min(b)}
                             for t, b in sorted(self.same_block.items())[:EXAMPLES]]},
            "direct_transfer": {
                "transfers": self.n_transfers,
                "tokens": len({t for _, t in self.transfers}),
                "examples": [{"tx": x, "token": t} for x, t in sorted(self.transfers)[:EXAMPLES]]},
            "co_entry": {
                "tokens": self.n_co_entry,
                "examples": [{"token": t, "gap_s": g}
                             for t, g in sorted(self.co_entry.items())[:EXAMPLES]]},
            "first_at": self.first_at.isoformat() if self.first_at else None,
            "last_at": self.last_at.isoformat() if self.last_at else None,
        }


def key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def classify_edge(p: Pair, cfg: ClusteringConfig) -> str | None:
    sb, tr, co = p.n_same_block, p.n_transfers, p.n_co_entry
    if (sb >= cfg.strong_same_block or tr >= cfg.strong_transfers
            or (sb >= cfg.moderate_same_block and (tr >= 1 or co >= cfg.combo_co_entry))):
        return "strong"
    if (sb >= cfg.moderate_same_block or tr >= cfg.moderate_transfers
            or (tr >= 1 and co >= cfg.combo_co_entry) or co >= cfg.moderate_co_entry):
        return "moderate"
    if sb >= 1 or tr >= 1 or co >= cfg.weak_co_entry:
        return "weak"
    return None


def collect(c: Connection, chain_id: int, as_of: datetime, cfg: ClusteringConfig
            ) -> tuple[dict[tuple[str, str], Pair], dict[str, int]]:
    """Pairwise evidence between enrolled wallets from rows strictly older than `as_of`."""
    since = as_of - timedelta(days=cfg.lookback_days)
    p: dict[str, Any] = {"c": chain_id, "as_of": as_of, "since": since,
                         "cap": cfg.same_block_crowd_cap}
    ws = [r[0] for r in c.execute(sa.text(
        "SELECT address FROM wallet_enrollment WHERE chain_id = :c AND status = 'active' "
        "AND first_top_at < :as_of ORDER BY address"), p)]
    pairs: dict[tuple[str, str], Pair] = defaultdict(Pair)
    stats = {"wallets": len(ws), "crowds_skipped": 0, "hot_tokens_skipped": 0}
    if len(ws) < 2:
        return pairs, stats
    p["ws"] = ws

    # 1. same block, same token
    for token, block, group, at in c.execute(sa.text("""
            SELECT token, block_number, array_agg(DISTINCT wallet), min(block_time)
            FROM trades
            WHERE chain_id = :c AND kind = 'BUY' AND wallet = ANY(:ws)
              AND block_time >= :since AND block_time < :as_of
            GROUP BY token, block_number HAVING count(DISTINCT wallet) >= 2
            ORDER BY token, block_number"""), p):
        if len(group) > cfg.same_block_crowd_cap:
            stats["crowds_skipped"] += 1
            continue
        for a, b in combinations(sorted(group), 2):
            e = pairs[(a, b)]
            e.same_block[token].add(int(block))
            e.seen(at)

    # 2. a token moved wallet -> wallet inside one transaction
    for a, b, tx, token, at in c.execute(sa.text("""
            SELECT o.wallet, i.wallet, o.tx_hash, o.token, o.block_time
            FROM trades o JOIN trades i
              ON i.chain_id = o.chain_id AND i.tx_hash = o.tx_hash AND i.token = o.token
             AND i.kind = 'TRANSFER_IN' AND i.wallet <> o.wallet
            WHERE o.chain_id = :c AND o.kind = 'TRANSFER_OUT'
              AND o.wallet = ANY(:ws) AND i.wallet = ANY(:ws)
              AND o.block_time >= :since AND o.block_time < :as_of
            ORDER BY o.block_number, o.tx_hash"""), p):
        e = pairs[key(a, b)]
        e.transfers.add((tx, token))
        e.seen(at)

    # 3. first-ever buys of the same token close together, on tokens few wallets entered
    firsts: dict[str, list[tuple[datetime, int, str]]] = defaultdict(list)
    for w, token, at, block in c.execute(sa.text("""
            SELECT wallet, token, min(block_time), min(block_number)
            FROM trades
            WHERE chain_id = :c AND kind = 'BUY' AND wallet = ANY(:ws) AND block_time < :as_of
            GROUP BY wallet, token HAVING min(block_time) >= :since
            ORDER BY token, min(block_time), wallet"""), p):
        firsts[token].append((at, int(block), w))
    for token, entries in firsts.items():
        if len(entries) < 2:
            continue
        if len(entries) > cfg.co_entry_max_wallets:
            stats["hot_tokens_skipped"] += 1
            continue
        for (t1, b1, w1), (t2, b2, w2) in combinations(entries, 2):
            gap = abs((t2 - t1).total_seconds())
            if b1 != b2 and gap <= cfg.co_entry_window_s:
                e = pairs[key(w1, w2)]
                e.co_entry[token] = int(gap)
                e.seen(min(t1, t2))
                e.seen(max(t1, t2))
    return pairs, stats


# --- clusters ---------------------------------------------------------------------------------
def build_components(edges: dict[tuple[str, str], str], cfg: ClusteringConfig
                     ) -> tuple[list[frozenset[str]], list[frozenset[str]]]:
    """Connected components over strong + moderate edges. Returns (clusters, oversized)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), strength in sorted(edges.items()):
        if strength in ("strong", "moderate"):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, set[str]] = defaultdict(set)
    for x in list(parent):
        groups[find(x)].add(x)
    good: list[frozenset[str]] = []
    big: list[frozenset[str]] = []
    for g in groups.values():
        if len(g) < cfg.min_cluster_size:
            continue
        (big if len(g) > cfg.max_cluster_size else good).append(frozenset(g))
    key_ = lambda s: min(s)  # noqa: E731
    return sorted(good, key=key_), sorted(big, key=key_)


@dataclass
class Plan:
    """What reconcile() decided, before any row is written (pure, testable)."""
    continues: dict[int, frozenset[str]] = field(default_factory=dict)       # old id -> members
    merges: dict[int, list[int]] = field(default_factory=dict)               # survivor -> absorbed
    creates: list[tuple[frozenset[str], int | None]] = field(default_factory=list)  # (members, split_from)
    invalidates: list[int] = field(default_factory=list)


def plan_lifecycle(old: dict[int, frozenset[str]], new: list[frozenset[str]]) -> Plan:
    """Deterministic matching of the previous run's open clusters to the new components."""
    plan = Plan()
    heir_of: dict[int, int] = {}                  # old id -> index into `new`
    for oid in sorted(old):
        best: tuple[int, str, int] | None = None
        for i, comp in enumerate(new):
            ov = len(old[oid] & comp)
            if ov:
                cand = (-ov, min(comp), i)
                if best is None or cand < best:
                    best = cand
        if best is None:
            plan.invalidates.append(oid)
        else:
            heir_of[oid] = best[2]
    claimed: dict[int, list[int]] = defaultdict(list)
    for oid, i in heir_of.items():
        claimed[i].append(oid)
    for i, comp in enumerate(new):
        owners = sorted(claimed.get(i, []), key=lambda o: (-len(old[o] & comp), o))
        if not owners:
            donors = sorted(((len(old[o] & comp), o) for o in old if old[o] & comp),
                            key=lambda t: (-t[0], t[1]))
            plan.creates.append((comp, donors[0][1] if donors else None))
            continue
        survivor, absorbed = owners[0], owners[1:]
        plan.continues[survivor] = comp
        if absorbed:
            plan.merges[survivor] = absorbed
    return plan


def _open_clusters(c: Connection, chain_id: int, version: str) -> dict[int, frozenset[str]]:
    rows = c.execute(sa.text("""
        SELECT k.id, m.address FROM wallet_clusters k
        JOIN wallet_cluster_members m ON m.cluster_id = k.id AND m.valid_to IS NULL
        WHERE k.chain_id = :c AND k.version = :v AND k.status = 'active'
        ORDER BY k.id, m.address"""), {"c": chain_id, "v": version})
    out: dict[int, set[str]] = defaultdict(set)
    for cid, addr in rows:
        out[int(cid)].add(addr)
    return {k: frozenset(v) for k, v in out.items()}


def _event(c: Connection, cid: int, run_id: int, at: datetime, kind: str,
           detail: dict[str, Any]) -> None:
    c.execute(sa.text("""INSERT INTO cluster_events (cluster_id, run_id, at, kind, detail)
                         VALUES (:i, :r, :a, :k, CAST(:d AS jsonb))"""),
              {"i": cid, "r": run_id, "a": at, "k": kind, "d": json.dumps(detail, sort_keys=True)})


def _close_members(c: Connection, cid: int, addrs: list[str], run_id: int, at: datetime) -> None:
    if addrs:
        c.execute(sa.text("""UPDATE wallet_cluster_members SET valid_to = :a, to_run_id = :r
                             WHERE cluster_id = :i AND valid_to IS NULL AND address = ANY(:w)"""),
                  {"a": at, "r": run_id, "i": cid, "w": addrs})


def _open_members(c: Connection, chain_id: int, cid: int, addrs: list[str], run_id: int,
                  at: datetime) -> None:
    if addrs:
        c.execute(sa.text("""INSERT INTO wallet_cluster_members
                               (cluster_id, chain_id, address, valid_from, from_run_id)
                             VALUES (:i, :c, :a, :t, :r)"""),
                  [{"i": cid, "c": chain_id, "a": a, "t": at, "r": run_id} for a in addrs])


def apply_plan(c: Connection, chain_id: int, version: str, run_id: int, at: datetime,
               old: dict[int, frozenset[str]], plan: Plan) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for oid in plan.invalidates:
        _close_members(c, oid, sorted(old[oid]), run_id, at)
        c.execute(sa.text("""UPDATE wallet_clusters SET status = 'invalidated', ended_run_id = :r,
                             ended_at = :a WHERE id = :i"""), {"r": run_id, "a": at, "i": oid})
        _event(c, oid, run_id, at, "INVALIDATED", {
            "members": sorted(old[oid]),
            "reason": "no strong or moderate evidence links these wallets any more"})
        out["invalidated"] += 1
    for survivor, comp in sorted(plan.continues.items()):
        before = old[survivor]
        gone = sorted(before - comp)
        came = sorted(comp - before)
        _close_members(c, survivor, gone, run_id, at)
        _open_members(c, chain_id, survivor, came, run_id, at)
        absorbed = plan.merges.get(survivor, [])
        for aid in absorbed:
            _close_members(c, aid, sorted(old[aid]), run_id, at)
            c.execute(sa.text("""UPDATE wallet_clusters SET status = 'merged', merged_into = :s,
                                 ended_run_id = :r, ended_at = :a WHERE id = :i"""),
                      {"s": survivor, "r": run_id, "a": at, "i": aid})
            _event(c, aid, run_id, at, "MERGED_INTO", {"into": survivor,
                                                       "members": sorted(old[aid])})
            out["merged"] += 1
        if absorbed:
            _event(c, survivor, run_id, at, "ABSORBED", {
                "clusters": absorbed, "added": came, "removed": gone})
        elif came and gone:
            _event(c, survivor, run_id, at, "EXTENDED", {"added": came, "removed": gone})
        elif came:
            _event(c, survivor, run_id, at, "EXTENDED", {"added": came})
        elif gone:
            _event(c, survivor, run_id, at, "REDUCED", {"removed": gone})
        if came:
            out["extended"] += 1
        if gone:
            out["reduced"] += 1
    for comp, donor in plan.creates:
        cid = c.execute(sa.text("""
            INSERT INTO wallet_clusters (chain_id, version, status, created_run_id, split_from,
                                         created_at)
            VALUES (:c, :v, 'active', :r, :s, :a) RETURNING id"""),
                        {"c": chain_id, "v": version, "r": run_id, "s": donor, "a": at}).scalar_one()
        _open_members(c, chain_id, int(cid), sorted(comp), run_id, at)
        if donor is None:
            _event(c, int(cid), run_id, at, "CREATED", {"members": sorted(comp)})
            out["created"] += 1
        else:
            _event(c, int(cid), run_id, at, "SPLIT_FROM", {"from": donor,
                                                          "members": sorted(comp)})
            out["split"] += 1
    return dict(out)


# --- database ---------------------------------------------------------------------------------
def ensure_version(c: Connection, cfg: ClusteringConfig) -> str:
    validate(cfg)
    model = cfg.model()
    h = config_hash(model)
    row = c.execute(sa.text("SELECT config_hash FROM cluster_versions WHERE version = :v"),
                    {"v": cfg.version}).first()
    if row is None:
        c.execute(sa.text("INSERT INTO cluster_versions (version, config, config_hash) "
                          "VALUES (:v, CAST(:cfg AS jsonb), :h)"),
                  {"v": cfg.version, "cfg": json.dumps(model, sort_keys=True), "h": h})
    elif row[0] != h:
        raise ClusteringConfigError(
            f"cluster version {cfg.version!r} already exists with a different configuration; "
            "a version is immutable, choose a new `clustering.version`")
    return h


def evaluate(c: Connection, chain_id: int, as_of: datetime, cfg: ClusteringConfig
             ) -> dict[str, Any]:
    """Pure read: evidence -> edges -> components. Used by the run and by tests."""
    pairs, stats = collect(c, chain_id, as_of, cfg)
    strengths = {k: s for k, p in pairs.items() if (s := classify_edge(p, cfg))}
    comps, oversized = build_components(strengths, cfg)
    counts = {s: sum(1 for v in strengths.values() if v == s) for s in STRENGTHS}
    return {"pairs": pairs, "strengths": strengths, "components": comps, "oversized": oversized,
            "counts": counts, "stats": stats}


def run_once(store: Any, cfg: ClusteringConfig, now: datetime | None = None,
             as_of: datetime | None = None) -> dict[str, Any]:
    """One clustering run at the current interval boundary. Idempotent per (version, as_of);
    refuses to run out of order, because the lifecycle compares with the previous run."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    as_of = as_of or floor_interval(now, cfg.interval_s)
    with store.tx() as c:
        ensure_version(c, cfg)
        last = c.execute(sa.text("SELECT max(as_of) FROM cluster_runs WHERE version = :v"),
                         {"v": cfg.version}).scalar()
        if last is not None and as_of <= last:
            return {"skipped": True, "reason": "a run at or after this point exists",
                    "as_of": as_of.isoformat(), "version": cfg.version}
        ev = evaluate(c, store.chain_id, as_of, cfg)
        old = _open_clusters(c, store.chain_id, cfg.version)
        plan = plan_lifecycle(old, ev["components"])
        keep = {k: s for k, s in ev["strengths"].items()
                if s != "weak" or cfg.store_weak_edges}
        run_id = int(c.execute(sa.text("""
            INSERT INTO cluster_runs (version, as_of, edges_n, clusters_n)
            VALUES (:v, :a, :e, :n) RETURNING id"""),
                                {"v": cfg.version, "a": as_of, "e": len(ev["strengths"]),
                                 "n": len(ev["components"])}).scalar_one())
        rows = [{"r": run_id, "c": store.chain_id, "a": a, "b": b, "s": s,
                 "e": json.dumps(ev["pairs"][(a, b)].evidence(), sort_keys=True)}
                for (a, b), s in sorted(keep.items())]
        for i in range(0, len(rows), BATCH):
            c.execute(sa.text("""INSERT INTO wallet_edges
                                   (run_id, chain_id, wallet_a, wallet_b, strength, evidence)
                                 VALUES (:r, :c, :a, :b, :s, CAST(:e AS jsonb))"""),
                      rows[i:i + BATCH])
        changes = apply_plan(c, store.chain_id, cfg.version, run_id, as_of, old, plan)
        pruned = prune(c, cfg.version, now, cfg.retention_all_days, cfg.retention_daily_days)
    return {"as_of": as_of.isoformat(), "version": cfg.version, "run_id": run_id,
            "wallets": ev["stats"]["wallets"], "edges": ev["counts"],
            "clusters": len(ev["components"]), "oversized_components": [len(s) for s in ev["oversized"]],
            "changes": changes, "pruned_edges": pruned,
            "crowds_skipped": ev["stats"]["crowds_skipped"],
            "hot_tokens_skipped": ev["stats"]["hot_tokens_skipped"]}


def prune(c: Connection, version: str, now: datetime, keep_all_days: int, keep_daily_days: int
          ) -> int:
    """Edge rows only. Run rows, clusters, memberships and events are kept: they are small and
    they are the history. Edges of runs older than `keep_all_days` are thinned to the 00:00 UTC
    run of each day; edges older than `keep_daily_days` are removed."""
    return c.execute(sa.text("""
        DELETE FROM wallet_edges e USING cluster_runs r
        WHERE e.run_id = r.id AND r.version = :v
          AND ((r.as_of < :all AND r.as_of <> date_trunc('day', r.as_of AT TIME ZONE 'UTC')
                                              AT TIME ZONE 'UTC')
               OR r.as_of < :daily)"""),
                     {"v": version, "all": now - timedelta(days=keep_all_days),
                      "daily": now - timedelta(days=keep_daily_days)}).rowcount or 0


def backfill(store: Any, cfg: ClusteringConfig, days: int, now: datetime | None = None
             ) -> dict[str, Any]:
    """Replay daily 00:00-UTC points oldest first with the live code path. Points at or before
    the latest existing run are skipped, so it can only extend history forward."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    points = [day0 - timedelta(days=d) for d in range(days, -1, -1)]
    done = skipped = 0
    for as_of in (t for t in points if t < now):
        out = run_once(store, cfg, now=now, as_of=as_of)
        skipped += 1 if out.get("skipped") else 0
        done += 0 if out.get("skipped") else 1
    return {"points": done + skipped, "run": done, "skipped": skipped, "version": cfg.version}


def loop(store: Any, cfg: ClusteringConfig, stop: threading.Event) -> None:
    stop.wait(120)                      # let the service settle after a restart
    while not stop.is_set():
        try:
            log.info("clusters: %s", json.dumps(run_once(store, cfg)))
        except ClusteringConfigError:
            log.exception("clustering disabled until the configuration is fixed")
            return
        except Exception:  # noqa: BLE001 - never take the service down for a derived table
            log.exception("clustering round failed")
        stop.wait(max(60.0, cfg.interval_s / 6))   # re-check often; run_once is a no-op off-boundary


# --- point-in-time reads (used by Consensus v2 and the web views) -----------------------------
def membership_at(c: Connection, chain_id: int, version: str, wallets: list[str], at: datetime
                  ) -> dict[str, int]:
    """Cluster id of each wallet *as known at `at`* (valid_from <= at < valid_to). Wallets in no
    cluster are absent. Later merges or splits never change the answer for an earlier `at`."""
    if not wallets:
        return {}
    rows = c.execute(sa.text("""
        SELECT m.address, m.cluster_id FROM wallet_cluster_members m
        JOIN wallet_clusters k ON k.id = m.cluster_id
        WHERE m.chain_id = :c AND k.version = :v AND m.address = ANY(:w)
          AND m.valid_from <= :t AND (m.valid_to IS NULL OR m.valid_to > :t)"""),
                     {"c": chain_id, "v": version, "w": list(wallets), "t": at})
    return {a: int(i) for a, i in rows}
