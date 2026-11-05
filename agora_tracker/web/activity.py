"""Activity monitor: parameter parsing, keyset cursors and the SQL filter builder.

Everything user-controlled is validated here and turned into bound parameters; nothing from
the query string is ever interpolated into SQL. The query itself runs against the
`public_api.trades` view, so private wallets cannot appear whatever the filters say.

Ordering and pagination
-----------------------
A page is keyed by `(block_time, id)` (newest/oldest first) or `(hype_wei, id)` (largest
verified buy/sell). `id` is the unique leg id, so the order is total and a cursor names
exactly one row: no row is skipped or repeated when new rows arrive between page loads.
Cursors are opaque, bound to the sort they were issued for, and validated on input.

Incremental updates use the insertion watermark instead (`id > since_id`): that also catches
a leg the indexer wrote late for an older block, which a time cursor would miss.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ..timerange import EARLIEST
from .params import BUCKET_S, ParamError, parse_ts

UTC = timezone.utc

# public name -> stored trades.kind
ACTIONS: dict[str, str | None] = {
    "all": None, "buy": "BUY", "sell": "SELL", "transfer_in": "TRANSFER_IN",
    "transfer_out": "TRANSFER_OUT", "swap": "SWAP", "other": "MULTI"}
ACTION_LABELS = {"all": "All activity", "buy": "Buy", "sell": "Sell",
                 "transfer_in": "Transfer in", "transfer_out": "Transfer out",
                 "swap": "Swap", "other": "Multi / other"}
KIND_TO_ACTION = {v: k for k, v in ACTIONS.items() if v}

RANGES: dict[str, int | None] = {"15m": 900, "1h": 3600, "4h": 14400, "12h": 43200,
                                 "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
RANGE_LABELS = {"15m": "15 min", "1h": "1 hour", "4h": "4 hours", "12h": "12 hours",
                "24h": "24 hours", "7d": "7 days", "30d": "30 days", "all": "All history"}
SORTS = ("newest", "oldest", "largest_buy", "largest_sell")
SORT_LABELS = {"newest": "Newest first", "oldest": "Oldest first",
               "largest_buy": "Largest verified buy", "largest_sell": "Largest verified sell"}
BASES = {"all": None, "verified": ("high",), "estimated": ("estimated",),
         "unknown": ("unknown", "legacy")}

TOTAL_CAP = 10_000
MAX_CUSTOM_DAYS = 366

HEX = re.compile(r"^0x[0-9a-f]+$")
SYMBOL = re.compile(r"^[A-Za-z0-9$._/-]{1,20}$")
HEX_PREFIX_MIN = 6                                   # "0x" + 6 hex digits at least


@dataclass(frozen=True)
class Search:
    """Resolved free-text search."""
    kind: str                       # "tx" | "address" | "hex" | "symbol"
    value: str                      # lower-cased text without LIKE metacharacters


@dataclass(frozen=True)
class ActivityParams:
    action: str = "all"
    range: str = "24h"
    start: datetime | None = None          # inclusive; None = from the beginning
    end: datetime | None = None            # exclusive; None = open (up to now and beyond)
    sort: str = "newest"
    basis: str = "all"
    wallet: str | None = None
    token: str | None = None
    tx: str | None = None
    search: Search | None = None
    q: str = ""
    size: int = 50
    cursor: tuple[Any, int] | None = None
    direction: str = "next"                # next = further along the order, prev = back
    since_id: int | None = None
    echo: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def live(self) -> bool:
        """Can the browser poll for new rows? Only the newest-first head of an open window."""
        return self.sort == "newest" and self.cursor is None and self.end is None

    def cache_key(self) -> tuple:
        return (self.action, self.range, self.start and int(self.start.timestamp()),
                self.end and int(self.end.timestamp()), self.sort, self.basis, self.wallet,
                self.token, self.tx, self.q, self.size, self.cursor, self.direction,
                self.since_id)


# --- cursors ------------------------------------------------------------------------------------
def encode_cursor(sort: str, key: Any, row_id: int) -> str:
    raw = json.dumps({"s": sort, "k": key, "i": int(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(text: str, sort: str) -> tuple[Any, int]:
    if len(text) > 200:
        raise ParamError("that cursor is not valid")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        o = json.loads(raw)
        if o["s"] != sort:
            raise ParamError("that cursor belongs to a different sort order")
        row_id = int(o["i"])
        if row_id < 0 or row_id > 2 ** 62:
            raise ValueError
        if sort in ("newest", "oldest"):
            micros = int(o["k"])
            if abs(micros) > 10 ** 18:
                raise ValueError
            key: Any = micros                                  # block_time, epoch microseconds
        else:
            key = str(Decimal(str(o["k"])))                    # hype_wei, exact
            if not Decimal(key).is_finite():
                raise ValueError
        return key, row_id
    except ParamError:
        raise
    except (binascii.Error, ValueError, KeyError, TypeError, InvalidOperation, json.JSONDecodeError):
        raise ParamError("that cursor is not valid") from None


def micros_to_dt(micros: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(micros))


def dt_to_micros(d: datetime) -> int:
    return int((d.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1))


# --- parsing --------------------------------------------------------------------------------------
def _addr(raw: str, what: str) -> str:
    a = raw.strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", a):
        raise ParamError(f"{what} must be a full 0x address (42 characters)")
    return a


def parse_search(text: str) -> Search | None:
    q = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    if not q:
        return None
    if len(q) > 80:
        raise ParamError("the search text is too long")
    low = q.lower()
    if HEX.match(low) and low.startswith("0x") and len(low) > 2:
        digits = len(low) - 2
        if digits == 64:
            return Search("tx", low)
        if digits == 40:
            return Search("address", low)
        if digits < HEX_PREFIX_MIN:
            raise ParamError(f"a partial hash or address needs at least {HEX_PREFIX_MIN} hex "
                             "digits after 0x")
        if digits > 64:
            raise ParamError("that is longer than a transaction hash")
        return Search("hex", low)
    sym = q.lstrip("$")
    if SYMBOL.match(sym):
        return Search("symbol", sym.lower())
    raise ParamError("search by token symbol, token or wallet address, or transaction hash")


def _int(q: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = q.get(key)
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ParamError(f"{key} must be a whole number") from None
    if not lo <= v <= hi:
        raise ParamError(f"{key} must be between {lo} and {hi}")
    return v


def parse_activity(q: Mapping[str, str], now: datetime, *, default_size: int = 50,
                   max_size: int = 100) -> ActivityParams:
    now = now.astimezone(UTC)
    action = (q.get("action") or "all").strip().lower()
    if action not in ACTIONS:
        raise ParamError(f"unknown action {action[:20]!r}; use one of {', '.join(ACTIONS)}")
    sort = (q.get("sort") or "newest").strip().lower()
    if sort not in SORTS:
        raise ParamError(f"unknown sort {sort[:20]!r}; use one of {', '.join(SORTS)}")
    basis = (q.get("basis") or "all").strip().lower()
    if basis not in BASES:
        raise ParamError(f"unknown basis {basis[:20]!r}; use one of {', '.join(BASES)}")
    if sort in ("largest_buy", "largest_sell"):
        want = "buy" if sort == "largest_buy" else "sell"
        if action not in ("all", want):
            raise ParamError(f"sort {sort} only ranks {want} activity; set action to {want} or all")
        action, basis = want, "verified"        # a size ranking is only meaningful when measured
    rng = (q.get("range") or "24h").strip().lower()
    start: datetime | None
    end: datetime | None = None
    if rng in RANGES:
        secs = RANGES[rng]
        start = None if secs is None else datetime.fromtimestamp(
            int(now.timestamp()) // BUCKET_S * BUCKET_S - secs, UTC)
    elif rng == "custom":
        if not q.get("start") or not q.get("end"):
            raise ParamError("a custom range needs both start and end (UTC)")
        start, end = parse_ts(q["start"]), parse_ts(q["end"])
        if end <= start:
            raise ParamError("the end must be after the start")
        if start < EARLIEST:
            raise ParamError(f"no data exists before {EARLIEST:%Y-%m-%d}")
        if end - start > timedelta(days=MAX_CUSTOM_DAYS):
            raise ParamError(f"the range is longer than {MAX_CUSTOM_DAYS} days")
        if start >= now:
            raise ParamError("the start is in the future")
        if end > now + timedelta(minutes=1):
            end = None                         # "until now": stays open so polling still works
    else:
        raise ParamError(f"unknown range {rng[:20]!r}; use one of {', '.join(RANGES)} or custom")

    wallet = _addr(q["wallet"], "wallet") if q.get("wallet") else None
    token = _addr(q["token"], "token") if q.get("token") else None
    tx = None
    if q.get("tx"):
        tx = q["tx"].strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]{64}", tx):
            raise ParamError("tx must be a full transaction hash (0x + 64 hex digits); "
                             "use q for a partial hash")
    text = q.get("q", "")
    search = parse_search(text)
    size = _int(q, "limit", default_size, 1, max_size)
    direction = (q.get("dir") or "next").strip().lower()
    if direction not in ("next", "prev"):
        raise ParamError("dir must be next or prev")
    cursor = decode_cursor(q["cursor"], sort) if q.get("cursor") else None
    since_id = _int(q, "since_id", -1, -1, 2 ** 62)
    if since_id >= 0 and (cursor is not None or sort != "newest"):
        raise ParamError("since_id only works with sort=newest and no cursor")
    return ActivityParams(
        action=action, range=rng, start=start, end=end, sort=sort, basis=basis, wallet=wallet,
        token=token, tx=tx, search=search, q=re.sub(r"[\x00-\x1f\x7f]", "", text).strip()[:80],
        size=size, cursor=cursor, direction=direction,
        since_id=None if since_id < 0 else since_id)


# --- SQL ----------------------------------------------------------------------------------------------
def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def filter_sql(p: ActivityParams, *, with_cursor: bool = True,
               hidden: tuple[str, ...] = ()) -> tuple[str, dict[str, Any]]:
    """WHERE clause (without the keyword) over `trades t`, plus its bound parameters.

    `hidden` tokens (alerts.mute_tokens) are left out unless the request filters on exactly
    that token, so their own token page and an explicit filter still work."""
    cond: list[str] = ["t.chain_id = :c"]
    prm: dict[str, Any] = {}
    hide = [h for h in hidden if h != p.token]
    if hide:
        cond.append("t.token <> ALL(:hidden)")
        prm["hidden"] = hide
    if p.start is not None:
        cond.append("t.block_time >= :start")
        prm["start"] = p.start
    if p.end is not None:
        cond.append("t.block_time < :end")
        prm["end"] = p.end
    kind = ACTIONS[p.action]
    if kind:
        cond.append("t.kind = :kind")
        prm["kind"] = kind
    conf = BASES[p.basis]
    if conf:
        cond.append("t.confidence = ANY(:conf)")
        prm["conf"] = list(conf)
    if p.sort in ("largest_buy", "largest_sell"):
        cond.append("t.hype_wei IS NOT NULL")
    if p.wallet:
        cond.append("t.wallet = :w")
        prm["w"] = p.wallet
    if p.token:
        cond.append("t.token = :tok")
        prm["tok"] = p.token
    if p.tx:
        cond.append("t.tx_hash = :tx")
        prm["tx"] = p.tx
    s = p.search
    if s is not None:
        if s.kind == "tx":
            cond.append("t.tx_hash = :s")
            prm["s"] = s.value
        elif s.kind == "address":
            cond.append("(t.wallet = :s OR t.token = :s)")
            prm["s"] = s.value
        elif s.kind == "hex":
            cond.append("(t.tx_hash LIKE :s ESCAPE '\\' OR t.wallet LIKE :s ESCAPE '\\' "
                        "OR t.token LIKE :s ESCAPE '\\')")
            prm["s"] = _like_escape(s.value) + "%"
        else:
            cond.append("""t.token IN (
                (SELECT m.address FROM token_metadata m
                  WHERE m.chain_id = :c AND lower(m.symbol) LIKE :s ESCAPE '\\' LIMIT 200)
                UNION
                (SELECT pr.address FROM token_profiles pr
                  WHERE pr.chain_id = :c AND lower(pr.symbol) LIKE :s ESCAPE '\\' LIMIT 200))""")
            prm["s"] = _like_escape(s.value) + "%"
    return " AND ".join(cond), prm


def order_and_cursor(p: ActivityParams) -> tuple[str, str, dict[str, Any]]:
    """(ORDER BY clause, cursor predicate or '', its parameters) for the requested direction."""
    desc_base = p.sort != "oldest"                     # newest, largest_* are descending
    backwards = p.direction == "prev"
    desc = desc_base != backwards                      # the scan direction actually used
    d = "DESC" if desc else "ASC"
    if p.sort in ("newest", "oldest"):
        order = f"t.block_time {d}, t.id {d}"
        left = "(t.block_time, t.id)"
    else:
        order = f"t.hype_wei {d}, t.id {d}"
        left = "(t.hype_wei, t.id)"
    if p.cursor is None:
        return order, "", {}
    key, row_id = p.cursor
    op = "<" if desc else ">"
    if p.sort in ("newest", "oldest"):
        return order, f"{left} {op} (:ck, :ci)", {"ck": micros_to_dt(key), "ci": row_id}
    return order, f"{left} {op} (:ck, :ci)", {"ck": Decimal(key), "ci": row_id}
