"""HYPE amount filters for the Activity tab (`min_hype`, `max_hype`).

Amounts are typed by people and compared against `trades.hype_wei` (NUMERIC(78,0), never
negative, NULL when the amount is not known). The rules:

* Input is plain decimal text only: digits with an optional fraction of at most 18 places.
  No sign, no exponent (`1e3`), no `nan` / `inf`, no thousands separators, no whitespace
  inside. Anything else is a clear validation error, never a SQL error.
* The value is converted with `Decimal` arithmetic to an exact integer number of wei
  (`min_hype * 10**18`). No float is involved at any step.
* Bounds: 0 <= value <= MAX_HYPE (one billion HYPE is far above any trade), and
  min <= max when both are given.
* A row without a known amount (NULL) never matches a numeric bound: it is not converted to
  zero. The filter applies to verified and estimated amounts only; legacy rows keep an amount
  of unknown provenance and are therefore excluded from numeric filtering (they remain
  visible when no amount filter is set).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, localcontext

WEI = 10 ** 18
MAX_HYPE = Decimal(1_000_000_000)
MAX_WEI = int(MAX_HYPE) * WEI
# Which stored confidences carry an amount that may be compared (see module docstring).
AMOUNT_CONFIDENCES = ("high", "estimated")
PRESETS = ("0.1", "0.5", "1", "5", "10", "25", "50", "100")
_AMOUNT = re.compile(r"^([0-9]{1,10})(?:\.([0-9]{1,18}))?$")


class AmountError(ValueError):
    """Invalid amount text; the message is safe to show to the user."""


def parse_hype(raw: str | None, name: str) -> Decimal | None:
    """`None` / blank -> no bound. Otherwise a validated Decimal in HYPE."""
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return None
    if len(text) > 40:
        raise AmountError(f"{name} is too long")
    m = _AMOUNT.match(text)
    if m is None:
        raise AmountError(
            f"{name} must be a plain number such as 5 or 0.25 (digits and one decimal point, "
            "at most 18 decimals; no sign, exponent, commas or spaces)")
    value = Decimal(text)
    if value > MAX_HYPE:
        raise AmountError(f"{name} must be at most {MAX_HYPE:,} HYPE")
    return value


def to_wei(value: Decimal) -> int:
    """Exact: at most 18 decimals by construction, so the product is an integer."""
    with localcontext() as ctx:
        ctx.prec = 60
        wei = value * WEI
    if wei != wei.to_integral_value():
        raise AmountError("amount has more than 18 decimals")     # unreachable after parse_hype
    return int(wei)


def fmt_hype(value: Decimal | None) -> str:
    """Canonical text for echo / cache keys / URLs: no exponent, no trailing zeros."""
    if value is None:
        return ""
    s = format(value.normalize(), "f")
    return s if "." not in s else s.rstrip("0").rstrip(".")


@dataclass(frozen=True)
class AmountRange:
    min_hype: Decimal | None = None
    max_hype: Decimal | None = None

    @property
    def active(self) -> bool:
        return self.min_hype is not None or self.max_hype is not None

    @property
    def min_text(self) -> str:
        return fmt_hype(self.min_hype)

    @property
    def max_text(self) -> str:
        return fmt_hype(self.max_hype)

    @property
    def min_wei(self) -> int | None:
        return None if self.min_hype is None else to_wei(self.min_hype)

    @property
    def max_wei(self) -> int | None:
        return None if self.max_hype is None else to_wei(self.max_hype)

    def label(self) -> str:
        lo, hi = fmt_hype(self.min_hype), fmt_hype(self.max_hype)
        if lo and hi:
            return f"{lo} to {hi} HYPE"
        if lo:
            return f"at least {lo} HYPE"
        if hi:
            return f"at most {hi} HYPE"
        return ""


def parse_range(min_raw: str | None, max_raw: str | None) -> AmountRange:
    lo, hi = parse_hype(min_raw, "min_hype"), parse_hype(max_raw, "max_hype")
    if lo is not None and hi is not None and lo > hi:
        raise AmountError("min_hype must not be greater than max_hype")
    return AmountRange(lo, hi)
