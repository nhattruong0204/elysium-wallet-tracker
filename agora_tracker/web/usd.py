"""USD market capitalisation from the provider's HYPE figures and a stored HYPE/USD rate.

Units, confirmed against the provider (market.py): Agora quotes `mcap`, `price` and volumes
in HYPE and attaches the HYPE/USD rate it uses (`hypeUsd`) to its responses. The tracker
persists that rate every ~5 minutes (`hype_usd_rates`, migration 0005). A snapshot's USD
market cap is therefore

    market_cap_usd = mcap_hype * hype_usd(rate observed closest in time to the snapshot)

computed with Decimal, never with today's rate, so history does not move when HYPE does.
The web request path performs no provider call; everything here works on stored rows.

Provenance of the rate, by |snapshot time - rate time|:

    exact        <= fx_exact_s      (default 60 s)
    aligned      <= fx_aligned_s    (default 600 s, two leaderboard cycles)
    stale        <= fx_max_gap_s    (default 3600 s): value produced but flagged
    unavailable  beyond / no rate : no USD value (never fabricated, never $0)

`market_cap_status` summarises a value for clients: valid | stale | unavailable | invalid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

UTC = timezone.utc
SOURCE_LABEL = "stored_snapshot"          # token_market_snapshots.mcap_hype * stored HYPE/USD
USD_QUANT = Decimal("0.01")

# SQL fragment: the rate observed closest in time to `{ts}` (one index probe on each side).
# Exposes fx.observed_at and fx.hype_usd; both NULL when the table is empty.
FX_LATERAL = """
LEFT JOIN LATERAL (
    SELECT c.observed_at, c.hype_usd FROM (
        (SELECT r.observed_at, r.hype_usd FROM hype_usd_rates r
          WHERE r.observed_at <= {ts} ORDER BY r.observed_at DESC LIMIT 1)
        UNION ALL
        (SELECT r.observed_at, r.hype_usd FROM hype_usd_rates r
          WHERE r.observed_at > {ts} ORDER BY r.observed_at ASC LIMIT 1)
    ) c
    ORDER BY abs(extract(epoch FROM c.observed_at - {ts})) LIMIT 1
) fx ON true"""


@dataclass(frozen=True)
class FxPolicy:
    exact_s: int = 60
    aligned_s: int = 600
    max_gap_s: int = 3600
    snapshot_max_age_s: int = 1800

    @classmethod
    def from_web(cls, w: Any) -> FxPolicy:
        return cls(w.fx_exact_s, w.fx_aligned_s, w.fx_max_gap_s, w.market_max_age_s)


def rate_quality(gap_s: float | None, p: FxPolicy) -> str:
    if gap_s is None:
        return "unavailable"
    g = abs(gap_s)
    if g <= p.exact_s:
        return "exact"
    if g <= p.aligned_s:
        return "aligned"
    if g <= p.max_gap_s:
        return "stale"
    return "unavailable"


def dec(v: Any) -> Decimal | None:
    """Exact Decimal from a DB numeric / int / str; floats go through repr (no binary noise)."""
    if v is None:
        return None
    try:
        d = v if isinstance(v, Decimal) else Decimal(repr(v) if isinstance(v, float) else str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def to_usd(mcap_hype: Any, rate: Any, rate_at: datetime | None, snap_at: datetime | None,
           p: FxPolicy, now: datetime | None = None) -> dict[str, Any]:
    """One market-cap observation as the public contract. Field names carry their unit."""
    cap, fx = dec(mcap_hype), dec(rate)
    gap = None if rate_at is None or snap_at is None else (rate_at - snap_at).total_seconds()
    quality = rate_quality(gap, p) if fx is not None and fx > 0 else "unavailable"
    out: dict[str, Any] = {
        "market_cap_usd": None, "market_cap_hype": cap,
        "market_cap_source": SOURCE_LABEL, "market_cap_as_of": snap_at,
        "market_cap_status": "unavailable", "hype_usd": fx if quality != "unavailable" else None,
        "hype_usd_as_of": rate_at if quality != "unavailable" else None,
        "hype_usd_gap_s": None if gap is None else round(gap),
        "hype_usd_quality": quality, "snapshot_age_s": None,
    }
    if snap_at is not None and now is not None:
        out["snapshot_age_s"] = max(0, round((now - snap_at).total_seconds()))
    if cap is None:
        return out
    if cap < 0:
        out["market_cap_status"] = "invalid"
        return out
    if quality == "unavailable" or fx is None:
        return out
    out["market_cap_usd"] = (cap * fx).quantize(USD_QUANT, rounding=ROUND_HALF_EVEN)
    too_old = out["snapshot_age_s"] is not None and out["snapshot_age_s"] > p.snapshot_max_age_s
    out["market_cap_status"] = "stale" if (quality == "stale" or too_old) else "valid"
    return out


# --- display ----------------------------------------------------------------------------------
_UNITS = ((Decimal("1e12"), "T"), (Decimal("1e9"), "B"), (Decimal("1e6"), "M"), (Decimal("1e3"), "K"))


def _fixed(d: Decimal, places: int, min_places: int = 0) -> str:
    s = f"{d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):f}"
    if "." in s:
        whole, frac = s.split(".")
        s = whole + ("." + frac.rstrip("0").ljust(min_places, "0") if frac.rstrip("0") or min_places else "")
    return s


def format_usd(v: Any) -> str:
    """$830 / $12.5K / $1.28M / $3.45B. Missing or negative is an en dash, never $0; a real
    zero is $0 and a positive sub-cent value is <$0.01."""
    d = dec(v)
    if d is None or d < 0:
        return "–"
    if d == 0:
        return "$0"
    if d < Decimal("0.01"):
        return "<$0.01"
    if d < 1:
        return "$" + _fixed(d, 4, 2)
    if d < 100:
        return "$" + _fixed(d, 2, 2)
    if d < 1000 and d.quantize(Decimal(1), rounding=ROUND_HALF_UP) < 1000:
        return "$" + _fixed(d, 0)
    for i, (size, suffix) in enumerate(_UNITS):
        if d >= size or suffix == "K":
            q = d / size
            places = 2 if q < 10 else 1 if q < 100 else 0
            if q.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP) >= 1000 and i > 0:
                size, suffix = _UNITS[i - 1]                 # 999.96K -> $1M, not $1000K
                q, places = d / size, 2
            return "$" + _fixed(q, places) + suffix
    return "$" + _fixed(d, 0)
