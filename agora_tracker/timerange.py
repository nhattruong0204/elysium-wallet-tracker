"""Report time windows.

Every report covers the half-open interval [start, end) in UTC. Presets are rolling
("last 6 hours", recomputed whenever a view is opened); custom ranges are fixed and are
never silently moved to the present. Display uses a configurable IANA zone (default
Asia/Ho_Chi_Minh); storage and queries are always UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

PRESETS: dict[str, int] = {
    "15m": 15 * 60, "1h": 3600, "6h": 6 * 3600, "24h": 86400,
    "7d": 7 * 86400, "30d": 30 * 86400,
}
PRESET_LABELS = {"15m": "Last 15 min", "1h": "Last 1 hour", "6h": "Last 6 hours",
                 "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days"}
DEFAULT_PRESET = "24h"
MAX_SPAN = timedelta(days=90)
EARLIEST = datetime(2025, 1, 1, tzinfo=UTC)
_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d",
            "%d/%m/%Y %H:%M", "%d/%m/%Y")


class RangeError(ValueError):
    """User-facing validation error (message is safe to show)."""


@dataclass(frozen=True)
class TimeRange:
    start: datetime          # inclusive, UTC
    end: datetime            # exclusive, UTC
    preset: str | None = None

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeRange needs timezone-aware datetimes")
        if self.end <= self.start:
            raise RangeError("the end must be after the start")

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    @property
    def is_rolling(self) -> bool:
        return self.preset is not None

    def previous(self) -> TimeRange:
        """The immediately preceding window of equal length: [start - d, start)."""
        d = self.end - self.start
        return TimeRange(self.start - d, self.start, None)

    def label(self) -> str:
        return PRESET_LABELS.get(self.preset or "", "Custom range")

    def fmt(self, tz: ZoneInfo) -> str:
        a, b = self.start.astimezone(tz), self.end.astimezone(tz)
        same_year = a.year == b.year == datetime.now(tz).year
        f = "%d %b %H:%M" if same_year else "%d %b %Y %H:%M"
        return f"{a.strftime(f)} → {b.strftime(f)}"

    def file_tag(self, tz: ZoneInfo) -> str:
        a, b = self.start.astimezone(tz), self.end.astimezone(tz)
        if self.seconds < 86400 * 2:
            return f"{a:%Y-%m-%d_%H%M}_{b:%Y-%m-%d_%H%M}"
        return f"{a:%Y-%m-%d}_{b:%Y-%m-%d}"


def tz_label(tz: ZoneInfo, at: datetime | None = None) -> str:
    off = (at or datetime.now(UTC)).astimezone(tz).utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    h, m = divmod(abs(mins), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "") + f" {tz.key}"


def preset_range(preset: str, now: datetime | None = None) -> TimeRange:
    if preset not in PRESETS:
        raise RangeError(f"unknown period {preset!r}")
    end = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    return TimeRange(end - timedelta(seconds=PRESETS[preset]), end, preset)


def parse_local(text: str, tz: ZoneInfo) -> datetime:
    """'2026-11-01 08:00' in the display zone -> aware UTC datetime."""
    s = re.sub(r"\s+", " ", text.strip())
    for f in _FORMATS:
        try:
            naive = datetime.strptime(s, f)
        except ValueError:
            continue
        return naive.replace(tzinfo=tz).astimezone(UTC)
    raise RangeError(f"couldn't read {text.strip()[:40]!r}. Use YYYY-MM-DD HH:MM, "
                     "e.g. 2026-11-01 08:00")


def custom_range(start: datetime, end: datetime, now: datetime | None = None) -> TimeRange:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if end <= start:
        raise RangeError("the end must be after the start")
    if start < EARLIEST:
        raise RangeError(f"the start is before {EARLIEST:%Y-%m-%d}; no data exists that early")
    if start >= now:
        raise RangeError("the start is in the future")
    if end > now + timedelta(minutes=1):
        raise RangeError("the end is in the future. For 'until now' pick a preset instead")
    if end - start > MAX_SPAN:
        raise RangeError(f"the range is longer than {MAX_SPAN.days} days")
    if end - start < timedelta(minutes=1):
        raise RangeError("the range must be at least 1 minute")
    return TimeRange(start, end, None)


def from_pref(preset: str | None, start: datetime | None, end: datetime | None,
              now: datetime | None = None) -> TimeRange:
    """Rebuild the stored selection. Invalid or missing -> the 24h default."""
    if preset in PRESETS:
        return preset_range(preset, now)
    if start is not None and end is not None and end > start:
        return TimeRange(start.astimezone(UTC), end.astimezone(UTC), None)
    return preset_range(DEFAULT_PRESET, now)
