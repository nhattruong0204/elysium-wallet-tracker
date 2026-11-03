"""JSON for the public API: exact decimals as strings, UTC ISO timestamps."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from ..reports import Universe
from ..timerange import TimeRange


def to_jsonable(o: Any) -> Any:
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, datetime):
        return o.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, TimeRange):
        return {"preset": o.preset, "start": to_jsonable(o.start), "end": to_jsonable(o.end)}
    if isinstance(o, Universe):
        return {"basis": o.basis, "snapshot_id": o.snapshot_id,
                "captured_at": to_jsonable(o.captured_at), "qualified_wallets": len(o.qualified),
                "ranked_wallets": len(o.ranks), "note": o.note}
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(o).items()}
    if isinstance(o, dict):
        return {str(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o
