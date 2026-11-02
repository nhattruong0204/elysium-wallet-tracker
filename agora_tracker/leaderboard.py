"""Leaderboard source (Agora's traders endpoint, configured from DevTools) and snapshot diffing."""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import requests

from .config import LeaderboardConfig

log = logging.getLogger(__name__)
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Headers that a copied cURL carries but that should not be replayed forever.
_DROP_HEADERS = {"content-length", "host", "accept-encoding", "priority", "if-none-match",
                 "if-modified-since"}


@dataclass
class Trader:
    address: str
    rank: int
    pnl: float | None = None
    worth: float | None = None
    volume: float | None = None
    trades: int | None = None
    raw: dict = field(default_factory=dict, repr=False)


def dig(obj: Any, path: str) -> Any:
    """dig({'data': {'rows': [..]}}, 'data.rows') ; numeric parts index lists."""
    if not path:
        return obj
    for part in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def parse_items(payload: Any, cfg: LeaderboardConfig) -> list[Trader]:
    items = dig(payload, cfg.items_path)
    if not isinstance(items, list):
        raise ValueError(f"items_path {cfg.items_path!r} did not resolve to a list "
                         f"(got {type(items).__name__}); check the response shape")
    f = cfg.fields
    out: list[Trader] = []
    seen: set[str] = set()
    for it in items:
        addr = dig(it, f.get("address", "address"))
        if not isinstance(addr, str) or not ADDR_RE.match(addr):
            continue
        addr = addr.lower()
        if addr in seen:
            continue
        seen.add(addr)
        trades = _num(dig(it, f["trades"])) if "trades" in f else None
        out.append(Trader(
            address=addr, rank=0,
            pnl=_num(dig(it, f["pnl"])) if "pnl" in f else None,
            worth=_num(dig(it, f["worth"])) if "worth" in f else None,
            volume=_num(dig(it, f["volume"])) if "volume" in f else None,
            trades=int(trades) if trades is not None else None,
            raw=it if isinstance(it, dict) else {}))
    key = cfg.sort_by
    if key and out and getattr(out[0], key, None) is not None:
        out.sort(key=lambda t: float("-inf") if getattr(t, key) is None else getattr(t, key),
                 reverse=True)
    out = out[: cfg.top_n]
    for i, t in enumerate(out, 1):
        t.rank = i
    return out


class HttpLeaderboard:
    def __init__(self, cfg: LeaderboardConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.last_payload: Any = None
        self.last_meta: dict[str, Any] = {}

    def fetch(self) -> list[Trader]:
        c = self.cfg
        kw: dict[str, Any] = {"params": c.params or None, "headers": c.headers,
                              "timeout": c.timeout_s}
        if c.body is not None:
            kw["json" if isinstance(c.body, (dict, list)) else "data"] = c.body
        r = self.session.request(c.method.upper(), c.url, **kw)
        # Request metadata for the snapshot record. Headers (cookies, keys) are never kept.
        self.last_meta = {"url": c.url.split("?")[0], "method": c.method.upper(),
                          "params": c.params or {}, "status": r.status_code,
                          "elapsed_ms": int(r.elapsed.total_seconds() * 1000)
                          if r.elapsed else None}
        self.last_payload = None
        if r.status_code in (401, 403):
            raise PermissionError(f"leaderboard HTTP {r.status_code}: endpoint needs fresh "
                                  "headers/cookies; re-import with --import-curl")
        r.raise_for_status()
        payload = r.json()
        self.last_payload = payload if len(r.content) <= 2_000_000 else None
        traders = parse_items(payload, c)
        if not traders:
            raise ValueError("leaderboard returned 0 traders; check items_path / fields.address")
        return traders


def parse_curl(cmd: str) -> dict[str, Any]:
    """Parse DevTools 'Copy as cURL (bash)' into url/method/params/headers/body."""
    tokens = shlex.split(cmd.replace("\\\n", " "))
    if not tokens or tokens[0] != "curl":
        raise ValueError("expected a command starting with 'curl'")
    url, method, headers, body = None, None, {}, None
    it = iter(tokens[1:])
    for tok in it:
        if tok in ("-H", "--header"):
            k, _, v = next(it).partition(":")
            if k.strip().lower() not in _DROP_HEADERS:
                headers[k.strip()] = v.strip()
        elif tok in ("-X", "--request"):
            method = next(it)
        elif tok in ("-b", "--cookie"):
            headers["cookie"] = next(it)
        elif tok in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            body = next(it)
        elif tok.startswith("-"):
            if tok in ("-A", "--user-agent"):
                headers["user-agent"] = next(it)
            elif tok in ("-e", "--referer"):
                headers["referer"] = next(it)
            continue
        elif url is None:
            url = tok
    if not url:
        raise ValueError("no URL found in curl command")
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if body is not None:
        try:
            import json
            body = json.loads(body)
        except ValueError:
            pass
    return {"url": base, "method": method or ("POST" if body is not None else "GET"),
            "params": params, "headers": headers, "body": body}


def guess_mapping(payload: Any) -> dict[str, Any]:
    """Best-effort: find the list of trader objects and likely field names."""
    def find_lists(obj, path=""):
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            yield path, obj
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from find_lists(v, f"{path}.{k}" if path else k)

    best = None
    for path, lst in find_lists(payload):
        addr_keys = [k for k, v in lst[0].items() if isinstance(v, str) and ADDR_RE.match(v)]
        if addr_keys and (best is None or len(lst) > len(best[1])):
            best = (path, lst, addr_keys[0])
    if not best:
        return {}
    path, lst, addr_key = best
    keys = list(lst[0].keys())

    def pick(*cands):
        for cand in cands:
            for k in keys:
                if k.lower() == cand:
                    return k
        for cand in cands:
            for k in keys:
                if cand in k.lower():
                    return k
        return None

    fields = {"address": addr_key}
    for name, cands in {"pnl": ("pnl", "profit"), "worth": ("totalworth", "worth", "networth"),
                        "volume": ("volume", "vol"), "trades": ("trades", "tradecount", "txs")}.items():
        k = pick(*cands)
        if k:
            fields[name] = k
    return {"items_path": path, "fields": fields, "count": len(lst), "sample_keys": keys}


def diff_leaderboard(prev: dict[str, Any], cur: list[Trader], *, rank_change: int,
                     pnl_change: float, unit: str = "USD") -> list[tuple[str, str, Trader]]:
    """Returns (kind, detail, trader). prev maps address -> row with rank/pnl."""
    out: list[tuple[str, str, Trader]] = []
    if not prev:
        return out  # first snapshot: nothing to compare against
    for t in cur:
        p = prev.get(t.address)
        if p is None:
            out.append(("ENTERED", f"entered top at #{t.rank}", t))
            continue
        old_rank, old_pnl = p["rank"], p["pnl"]
        if old_rank and abs(old_rank - t.rank) >= rank_change:
            arrow = "up" if t.rank < old_rank else "down"
            out.append(("RANK", f"#{old_rank} -> #{t.rank} ({arrow})", t))
        if old_pnl is not None and t.pnl is not None and abs(t.pnl - old_pnl) >= pnl_change:
            out.append(("PNL", f"PnL {old_pnl:+,.0f} -> {t.pnl:+,.0f} {unit} "
                               f"({t.pnl - old_pnl:+,.0f})", t))
    cur_set = {t.address for t in cur}
    for addr, p in prev.items():
        if addr not in cur_set:
            out.append(("EXITED", f"dropped out (was #{p['rank']})",
                        Trader(address=addr, rank=p["rank"], pnl=p["pnl"])))
    return out
