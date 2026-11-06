"""Explainable, versioned, point-in-time wallet scores.

What a score is: a weighted mean of up to five components, each a documented function of facts
the database already holds, scaled to 0-100. What it is not: a prediction, a probability, or a
verdict. There is no confidence percentage; instead every score says how much of the model's
weight actually had data (`weight_available`) and which inputs were missing.

Rules that make it trustworthy:

* **No look-ahead.** `compute()` reads only rows strictly older than `as_of`: trades
  (`block_time < as_of`), snapshots (`captured_at < as_of`), wallets that had already appeared on
  the list. Re-running it later for the same `as_of` gives the same answer (tested), so history
  can be replayed (`--score-backfill`) with the exact code path of the live job.
* **Missing is not zero.** A component without enough evidence is `available: false` and drops
  out of the weighted mean; the remaining weights are renormalised, and `weight_available` shows
  by how much. Below `min_weight_available` there is no score at all.
* **Immutable versions.** A version's configuration is stored with its hash. Running the same
  version name with different weights or parameters is refused.
* **Quality label.** `sufficient` (both measured components present, >= `sufficient_weight` of
  the weight available), `limited` (a score exists but rests on list position and recency
  rather than measured trades; it is capped at `limited_cap` so that missing evidence can never
  outrank measured evidence), `insufficient` (no score).

Components (all in [0, 1]):

* `win_rate_lb`       Wilson lower bound of the share of measured closing trades with PnL > 0,
                      over `lookback_days`, needs >= `min_closes`. Penalises small samples.
* `return_on_cost`    measured PnL / cost basis of those closes, mapped from [-cap, +cap] to
                      [0, 1]. Needs >= `min_closes` and a positive cost basis.
* `recency`           0.5 ** (days since the last trade / half-life). Needs one trade before as_of.
* `list_persistence`  share of accepted Top-100 lists in the lookback that contained the wallet.
                      Needs >= `min_snapshots` lists.
* `rank_strength`     mean of (top_n - rank + 1) / top_n over the lists that contained it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from .config import ScoringConfig

log = logging.getLogger("agora_tracker.scoring")
UTC = timezone.utc
COMPONENTS = ("win_rate_lb", "return_on_cost", "recency", "list_persistence", "rank_strength")
MEASURED = ("win_rate_lb", "return_on_cost")
BATCH = 500


class ScoringConfigError(RuntimeError):
    """A stored score version has a different configuration than the one being run."""


def config_hash(model: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
                          ).hexdigest()[:16]


def validate(cfg: ScoringConfig) -> None:
    if set(cfg.weights) != set(COMPONENTS):
        raise ScoringConfigError(f"weights must name exactly {', '.join(COMPONENTS)}")
    if any(w < 0 for w in cfg.weights.values()) or sum(cfg.weights.values()) <= 0:
        raise ScoringConfigError("weights must be non-negative and not all zero")
    if not cfg.version or len(cfg.version) > 32:
        raise ScoringConfigError("version must be 1-32 characters")


# --- the pure parts -----------------------------------------------------------------------------
def wilson_lower(wins: int, n: int, z: float) -> float:
    if n <= 0:
        raise ValueError("n must be positive")
    p = wins / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, min(1.0, (centre - margin) / (1 + z2 / n)))


def floor_hour(t: datetime) -> datetime:
    t = t.astimezone(UTC)
    return t.replace(minute=0, second=0, microsecond=0)


@dataclass
class Evidence:
    closes: int = 0
    wins: int = 0
    pnl_wei: Decimal = Decimal(0)
    cost_wei: Decimal = Decimal(0)
    last_trade_at: datetime | None = None
    snapshots: int = 0                 # accepted lists in the lookback
    present: int = 0                   # ... that contained the wallet
    strength_sum: float = 0.0


def components(ev: Evidence, as_of: datetime, cfg: ScoringConfig) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    def put(name: str, value: float | None, inputs: dict[str, Any], why: str | None = None) -> None:
        w = float(cfg.weights[name])
        out[name] = {"available": value is not None, "value": None if value is None else round(value, 6),
                     "weight": w, "inputs": inputs, **({"missing": why} if why else {})}

    enough = ev.closes >= cfg.min_closes
    why_closes = f"{ev.closes} measured closing trades in the lookback, needs {cfg.min_closes}"
    put("win_rate_lb", wilson_lower(ev.wins, ev.closes, cfg.wilson_z) if enough else None,
        {"closes": ev.closes, "wins": ev.wins}, None if enough else why_closes)
    if enough and ev.cost_wei > 0:
        r = float(ev.pnl_wei / ev.cost_wei)
        v = min(1.0, max(0.0, (r + cfg.return_cap) / (2 * cfg.return_cap)))
        put("return_on_cost", v, {"closes": ev.closes, "return": round(r, 6)})
    else:
        put("return_on_cost", None, {"closes": ev.closes},
            why_closes if not enough else "no positive cost basis")
    if ev.last_trade_at is not None:
        days = max(0.0, (as_of - ev.last_trade_at).total_seconds() / 86400)
        put("recency", 0.5 ** (days / cfg.recency_half_life_days),
            {"days_since_last_trade": round(days, 4)})
    else:
        put("recency", None, {}, "no trade recorded before as_of")
    if ev.snapshots >= cfg.min_snapshots:
        put("list_persistence", ev.present / ev.snapshots,
            {"lists": ev.snapshots, "present": ev.present})
    else:
        put("list_persistence", None, {"lists": ev.snapshots},
            f"{ev.snapshots} accepted lists in the lookback, needs {cfg.min_snapshots}")
    if ev.present >= 1:
        put("rank_strength", ev.strength_sum / ev.present, {"present": ev.present})
    else:
        put("rank_strength", None, {"present": 0}, "not on any accepted list in the lookback")
    return out


def combine(comps: dict[str, dict[str, Any]], cfg: ScoringConfig
            ) -> tuple[Decimal | None, Decimal, str, str | None]:
    """(score, weight_available, quality, reason)."""
    total = sum(c["weight"] for c in comps.values())
    avail = {k: c for k, c in comps.items() if c["available"] and c["weight"] > 0}
    w_av = sum(c["weight"] for c in avail.values())
    share = Decimal(str(round(w_av / total, 4))) if total else Decimal(0)
    for c in comps.values():
        c["contribution"] = (round(c["weight"] * c["value"] / w_av * 100, 4)
                             if c["available"] and c["weight"] > 0 and w_av else None)
    if not avail or w_av / total < cfg.min_weight_available:
        return None, share, "insufficient", (
            f"{float(share):.0%} of the model weight had data, at least "
            f"{cfg.min_weight_available:.0%} is needed")
    score = sum(c["weight"] * c["value"] for c in avail.values()) / w_av * 100
    both = all(m in avail for m in MEASURED)
    if both and w_av / total >= cfg.sufficient_weight:
        return Decimal(str(round(score, 2))), share, "sufficient", None
    missing = [k for k in MEASURED if k not in avail]
    capped = min(score, cfg.limited_cap)
    note = ("no measured trade evidence yet" if missing else
            f"only {float(share):.0%} of the model weight had data")
    if capped < score:
        note += f"; capped at {cfg.limited_cap:g} (uncapped {score:.1f})"
    return Decimal(str(round(capped, 2))), share, "limited", note


# --- database ---------------------------------------------------------------------------------------
def ensure_version(c: Connection, cfg: ScoringConfig) -> str:
    validate(cfg)
    model = cfg.model()
    h = config_hash(model)
    row = c.execute(sa.text("SELECT config_hash FROM score_versions WHERE version = :v"),
                    {"v": cfg.version}).first()
    if row is None:
        c.execute(sa.text("INSERT INTO score_versions (version, config, config_hash) "
                          "VALUES (:v, CAST(:cfg AS jsonb), :h)"),
                  {"v": cfg.version, "cfg": json.dumps(model, sort_keys=True), "h": h})
    elif row[0] != h:
        raise ScoringConfigError(
            f"score version {cfg.version!r} already exists with a different configuration; "
            "a version is immutable, choose a new `scoring.version`")
    return h


def gather(c: Connection, chain_id: int, as_of: datetime, cfg: ScoringConfig
           ) -> dict[str, Evidence]:
    """Evidence per eligible wallet, from rows strictly older than `as_of`."""
    since = as_of - timedelta(days=cfg.lookback_days)
    p = {"c": chain_id, "as_of": as_of, "since": since}
    ws = [r[0] for r in c.execute(sa.text(
        "SELECT address FROM wallet_enrollment WHERE chain_id = :c AND status = 'active' "
        "AND first_top_at < :as_of ORDER BY address"), p)]
    ev = {w: Evidence() for w in ws}
    if not ws:
        return ev
    p["ws"] = ws
    for w, n, wins, pnl, cost in c.execute(sa.text("""
            SELECT r.wallet, count(*), count(*) FILTER (WHERE r.pnl_wei > 0),
                   coalesce(sum(r.pnl_wei), 0), coalesce(sum(r.cost_basis_wei), 0)
            FROM realized_pnl r JOIN trades t ON t.id = r.trade_id
            WHERE r.chain_id = :c AND r.status = 'measured' AND r.pnl_wei IS NOT NULL
              AND t.block_time >= :since AND t.block_time < :as_of AND r.wallet = ANY(:ws)
            GROUP BY r.wallet"""), p):
        e = ev[w]
        e.closes, e.wins, e.pnl_wei, e.cost_wei = int(n), int(wins), Decimal(pnl), Decimal(cost)
    for w, last in c.execute(sa.text("""
            SELECT wallet, max(block_time) FROM trades
            WHERE chain_id = :c AND block_time < :as_of AND wallet = ANY(:ws) GROUP BY wallet"""), p):
        ev[w].last_trade_at = last
    snaps = c.execute(sa.text("""
        SELECT count(*) FROM leaderboard_snapshots
        WHERE chain_id = :c AND status IN ('ok', 'imported') AND complete
          AND captured_at >= :since AND captured_at < :as_of"""), p).scalar() or 0
    for e in ev.values():
        e.snapshots = int(snaps)
    p["top_n"] = cfg.top_n
    for w, present, strength in c.execute(sa.text("""
            SELECT e.address, count(*),
                   coalesce(sum(greatest(0, (:top_n - e.rank + 1.0) / :top_n)), 0)
            FROM leaderboard_snapshot_entries e JOIN leaderboard_snapshots s ON s.id = e.snapshot_id
            WHERE s.chain_id = :c AND s.status IN ('ok', 'imported') AND s.complete
              AND s.captured_at >= :since AND s.captured_at < :as_of AND e.address = ANY(:ws)
            GROUP BY e.address"""), p):
        ev[w].present, ev[w].strength_sum = int(present), float(strength)
    return ev


def compute(c: Connection, chain_id: int, as_of: datetime, cfg: ScoringConfig
            ) -> list[dict[str, Any]]:
    rows = []
    for w, e in gather(c, chain_id, as_of, cfg).items():
        comps = components(e, as_of, cfg)
        score, share, quality, reason = combine(comps, cfg)
        rows.append({"chain_id": chain_id, "address": w, "version": cfg.version, "as_of": as_of,
                     "score": score, "weight_available": share, "quality": quality,
                     "reason": reason, "components": comps})
    return rows


def persist(c: Connection, rows: list[dict[str, Any]]) -> int:
    n = 0
    stmt = sa.text("""
        INSERT INTO wallet_scores (chain_id, address, version, as_of, score, weight_available,
                                   quality, reason, components)
        VALUES (:chain_id, :address, :version, :as_of, :score, :weight_available, :quality,
                :reason, CAST(:components AS jsonb))
        ON CONFLICT ON CONSTRAINT uq_wallet_scores_point DO NOTHING""")
    for i in range(0, len(rows), BATCH):
        chunk = [{**r, "components": json.dumps(r["components"], sort_keys=True)}
                 for r in rows[i:i + BATCH]]
        n += c.execute(stmt, chunk).rowcount or 0
    return n


def prune(c: Connection, version: str, now: datetime, keep_hourly_h: int) -> int:
    """Hourly rows older than `keep_hourly_h` are thinned to the 00:00 UTC row of each day."""
    return c.execute(sa.text("""
        DELETE FROM wallet_scores WHERE version = :v AND as_of < :cut
          AND as_of <> date_trunc('day', as_of AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"""),
                     {"v": version, "cut": now - timedelta(hours=keep_hourly_h)}).rowcount or 0


def run_once(store: Any, cfg: ScoringConfig, now: datetime | None = None) -> dict[str, Any]:
    """Score every eligible wallet at the current hour boundary. Idempotent."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    as_of = floor_hour(now)
    with store.tx() as c:
        ensure_version(c, cfg)
        rows = compute(c, store.chain_id, as_of, cfg)
        inserted = persist(c, rows)
        pruned = prune(c, cfg.version, now, cfg.retention_hourly_h)
    q: dict[str, int] = defaultdict(int)
    for r in rows:
        q[r["quality"]] += 1
    return {"as_of": as_of.isoformat(), "version": cfg.version, "wallets": len(rows),
            "inserted": inserted, "pruned": pruned, "quality": dict(q)}


def backfill(store: Any, cfg: ScoringConfig, days: int, now: datetime | None = None,
             step_hours: int = 24) -> dict[str, Any]:
    """Replay the score at past 00:00-UTC points with the live code path (point-in-time)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    points = [day0 - timedelta(days=d) for d in range(days, -1, -1)]
    points = [t for t in points if t < now][::max(1, step_hours // 24)]
    inserted = 0
    with store.tx() as c:
        ensure_version(c, cfg)
    for as_of in points:
        with store.tx() as c:
            inserted += persist(c, compute(c, store.chain_id, as_of, cfg))
    return {"points": len(points), "inserted": inserted, "version": cfg.version}


def loop(store: Any, cfg: ScoringConfig, stop: threading.Event) -> None:
    stop.wait(60)                       # let the service settle after a restart
    while not stop.is_set():
        try:
            out = run_once(store, cfg)
            log.info("scores: %s", json.dumps(out))
        except ScoringConfigError:
            log.exception("scoring disabled until the configuration is fixed")
            return
        except Exception:  # noqa: BLE001 - never take the service down for a derived table
            log.exception("scoring round failed")
        stop.wait(cfg.interval_s)
