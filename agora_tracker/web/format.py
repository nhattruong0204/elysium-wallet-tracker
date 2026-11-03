"""Jinja filters and helpers: numbers, addresses, times, external links."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from markupsafe import Markup, escape

from ..config import LinksConfig
from .activity import ACTION_LABELS
from .usd import format_usd

UTC = timezone.utc


def hype(v: Any, places: int = 2) -> str:
    if v is None:
        return "–"
    f = float(v)
    if f != 0 and abs(f) < 10 ** -places:
        return f"{f:.2e}"
    return f"{f:,.{places}f}"


def signed(v: Any, places: int = 2) -> str:
    if v is None:
        return "–"
    f = float(v)
    return f"{f:+,.{places}f}"


def pct(v: Any, places: int = 1) -> str:
    """A fraction (0.12) as '+12.0%'."""
    if v is None:
        return "–"
    return f"{float(v) * 100:+.{places}f}%"


def number(v: Any) -> str:
    return "–" if v is None else f"{int(v):,}"


def price(v: Any) -> str:
    if v is None:
        return "–"
    f = float(v)
    if f == 0:
        return "0"
    if abs(f) >= 1:
        return f"{f:,.4f}"
    return f"{f:.3g}" if abs(f) >= 1e-4 else f"{f:.2e}"


def usd(v: Any) -> str:
    """$830 / $12.5K / $1.28M / $3.45B; missing is an en dash, never $0."""
    return format_usd(v)


def tstext(v: datetime | None) -> str:
    return "–" if v is None else v.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def mcap_title(m: Any) -> str:
    """Plain-text tooltip that explains where a USD market cap came from (or why it is absent)."""
    if not m:
        return "No stored market snapshot for this token."
    st = m.get("market_cap_status")
    if st == "unavailable":
        if m.get("market_cap_hype") is None:
            return "The stored snapshot has no market cap."
        return ("No HYPE/USD rate was stored close enough to this snapshot; "
                "nothing is estimated.")
    if st == "invalid":
        return "The stored market cap is not a valid number."
    bits = [f"{format(m['market_cap_hype'].normalize(), 'f') if m.get('market_cap_hype') is not None else '?'} HYPE "
            f"(provider, snapshot {tstext(m.get('market_cap_as_of'))})",
            f"× {price(m.get('hype_usd'))} USD per HYPE (rate {tstext(m.get('hype_usd_as_of'))}, "
            f"{m.get('hype_usd_quality')}, {m.get('hype_usd_gap_s'):+d}s from snapshot)"]
    if st == "stale":
        bits.append("STALE: the snapshot or the rate is older than the freshness limit")
    return "; ".join(bits)


def short_addr(a: str | None, head: int = 6, tail: int = 4) -> str:
    if not a:
        return "–"
    return a if len(a) <= head + tail + 2 else f"{a[:head]}…{a[-tail:]}"


def utc(v: datetime | None) -> datetime | None:
    return None if v is None else v.astimezone(UTC)


def ts(v: datetime | None, with_seconds: bool = False) -> Markup:
    """<time> with a machine-readable value; app.js can show it in the visitor's zone."""
    if v is None:
        return Markup("–")
    v = v.astimezone(UTC)
    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return Markup('<time datetime="{}" data-ts>{} UTC</time>').format(
        v.strftime("%Y-%m-%dT%H:%M:%SZ"), v.strftime(fmt))


def ago(v: datetime | None, now: datetime | None = None) -> str:
    if v is None:
        return "never"
    s = int(((now or datetime.now(UTC)) - v).total_seconds())
    return duration(s) + (" ago" if s >= 0 else " from now")


def duration(s: int | float | None) -> str:
    if s is None:
        return "–"
    s = int(abs(s))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} d"


class Links:
    """External pages for an address, built from config.links (same targets as the alerts)."""

    def __init__(self, c: LinksConfig):
        self.c = c

    def tx(self, h: str) -> str:
        return f"{self.c.explorer}/tx/{h}"

    def address(self, a: str) -> str:
        return f"{self.c.explorer}/address/{a}"

    def token(self, a: str) -> dict[str, str]:
        return {"Elyscan": f"{self.c.explorer}/token/{a}", "Agora": f"{self.c.agora}/token/{a}",
                "Kleos": f"{self.c.kleos}/token/{a}", "BOXA": f"{self.c.boxa}/token?address={a}",
                "ElysPump": f"{self.c.elyspump}/#/token/{a}"}

    def trader(self, a: str) -> dict[str, str]:
        return {"Elyscan": self.address(a), "Agora": f"{self.c.agora}/trader/{a}"}


def tone(v: Any) -> str:
    """CSS class for a signed number."""
    if v is None:
        return ""
    return "pos" if Decimal(str(v)) > 0 else "neg" if Decimal(str(v)) < 0 else ""


def register(env: Any, links: Links) -> None:
    env.filters.update(usd=usd, tstext=tstext, mcap_title=mcap_title, hype=hype, signed=signed, pct=pct, number=number, price=price,
                       short=short_addr, ts=ts, ago=ago, duration=duration, tone=tone)
    env.globals.update(links=links, esc=escape, action_labels=ACTION_LABELS)
