"""Query-string parsing and validation. Everything user-controlled is clamped here."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..timerange import EARLIEST, PRESET_LABELS, PRESETS, RangeError, TimeRange

UTC = timezone.utc
PRESET_ORDER = list(PRESETS)
BUCKET_S = 30            # rolling windows end on a 30 s grid so cached pages are shared
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_ts(text: str) -> datetime:
    """'2026-11-01T08:00', '2026-11-01 08:00:30', '2026-11-01', optional trailing Z -> UTC."""
    s = re.sub(r"\s+", " ", text.strip().replace("T", " ").rstrip("Zz")).strip()
    for f in _TS_FORMATS:
        try:
            return datetime.strptime(s, f).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise RangeError("couldn't read that date. Use YYYY-MM-DD HH:MM (UTC).")


def parse_range(q: Mapping[str, str], now: datetime, max_days: int = 30) -> TimeRange:
    """`range=24h` (default) or `range=custom&start=..&end=..` in UTC, half-open [start, end)."""
    now = now.astimezone(UTC)
    choice = (q.get("range") or "24h").strip().lower()
    if choice in PRESETS:
        end = datetime.fromtimestamp(int(now.timestamp()) // BUCKET_S * BUCKET_S, UTC)
        return TimeRange(end - timedelta(seconds=PRESETS[choice]), end, choice)
    if choice != "custom":
        raise RangeError(f"unknown range {choice[:20]!r}")
    if not q.get("start") or not q.get("end"):
        raise RangeError("a custom range needs both a start and an end")
    start, end = parse_ts(q["start"]), parse_ts(q["end"])
    if end <= start:
        raise RangeError("the end must be after the start")
    if start < EARLIEST:
        raise RangeError(f"no data exists before {EARLIEST:%Y-%m-%d}")
    if start >= now:
        raise RangeError("the start is in the future")
    if end > now + timedelta(minutes=1):
        raise RangeError("the end is in the future; pick a preset for 'until now'")
    if end - start > timedelta(days=max_days):
        raise RangeError(f"the range is longer than {max_days} days")
    if end - start < timedelta(minutes=1):
        raise RangeError("the range must be at least 1 minute")
    return TimeRange(start, end, None)


def range_key(tr: TimeRange) -> tuple:
    return (tr.preset, int(tr.start.timestamp()), int(tr.end.timestamp()))


def range_dict(tr: TimeRange) -> dict:
    return {"preset": tr.preset, "label": PRESET_LABELS.get(tr.preset or "", "Custom range"),
            "start": tr.start, "end": tr.end, "seconds": tr.seconds}


@dataclass(frozen=True)
class ListParams:
    q: str = ""
    sort: str = ""
    desc: bool = True
    page: int = 1


def list_params(q: Mapping[str, str], sorts: tuple[str, ...], default_sort: str,
                default_desc: bool = True) -> ListParams:
    sort = q.get("sort", default_sort)
    if sort not in sorts:
        sort = default_sort
    d = q.get("dir")
    desc = default_desc if d not in ("asc", "desc") else d == "desc"
    try:
        page = min(max(int(q.get("page", "1")), 1), 10_000)
    except ValueError:
        page = 1
    text = re.sub(r"[\x00-\x1f\x7f]", "", q.get("q", "")).strip()[:80]
    return ListParams(text, sort, desc, page)


def paginate(rows: list, page: int, size: int) -> dict:
    total = len(rows)
    pages = max(1, -(-total // size))
    page = min(page, pages)
    return {"items": rows[(page - 1) * size: page * size], "page": page, "pages": pages,
            "total": total, "size": size}
