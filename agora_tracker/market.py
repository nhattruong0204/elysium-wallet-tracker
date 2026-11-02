"""Token market data from Agora's public API, for alert cards.

Endpoints (found in agoradex.app's own frontend, 2026-11-02):
  GET /api/tokens/<address>?range=24h  -> name, symbol, launchpad, holders, createdAt (ms),
                                          stats: price, mcap, volume{1h,6h,24h,7d},
                                          change{1h,6h,24h,7d} (fractions), buys24h, trades24h
  GET /api/traders?limit=1             -> hypeUsd (HYPE/USD rate Agora uses)
  GET /api/tokens?sort=volume|gainers|new|mcap|trades|holders&window=1h|6h|24h|7d
               &launchpad=all&limit<=200&offset=N
                                       -> chain-wide token list (same stats, no holders),
                                          totals{tokens, launchpads}, synced{head, at, live}
  GET /api/tokens/<address>?range=1h|6h|24h|7d|all
                                       -> also candles [[t, o, h, l, c, vol_hype], ...]:
                                          1h ~1-min, 24h ~15-min, 7d/all ~120 buckets.
                                          Unknown ranges silently fall back to 24h.
All prices/caps/volumes are denominated in HYPE. Values are indicative (last trade), not
executable quotes.

Never raises: a failure returns None and the alert is sent without market data.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import MarketConfig

log = logging.getLogger(__name__)
UA = "elysium-wallet-tracker (+https://github.com/nhattruong0204/elysium-wallet-tracker)"


@dataclass
class TokenInfo:
    address: str
    symbol: str | None = None
    name: str | None = None
    launchpad: str | None = None
    price_hype: float | None = None
    mcap_hype: float | None = None
    vol_1h_hype: float | None = None
    vol_6h_hype: float | None = None
    vol_24h_hype: float | None = None
    vol_7d_hype: float | None = None
    change_1h: float | None = None     # fraction: 0.12 = +12 %
    change_6h: float | None = None
    change_24h: float | None = None
    change_7d: float | None = None
    last_trade_at: int | None = None   # unix seconds
    version: str | None = None
    holders: int | None = None
    buys_24h: int | None = None
    trades_24h: int | None = None
    created_at: int | None = None      # unix seconds
    x_url: str | None = None


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return None if f is None else int(f)


def parse_token(address: str, j: dict) -> TokenInfo:
    st = j.get("stats") or {}
    vol = st.get("volume") or {}
    ch = st.get("change") or {}
    created = _i(j.get("createdAt"))
    if created and created > 10**11:  # milliseconds
        created //= 1000
    links = j.get("links") or {}
    return TokenInfo(
        address=address.lower(), symbol=j.get("symbol"), name=j.get("name"),
        launchpad=(j.get("launchpad") or None),
        price_hype=_f(st.get("price")), mcap_hype=_f(st.get("mcap")),
        vol_1h_hype=_f(vol.get("1h")), vol_6h_hype=_f(vol.get("6h")),
        vol_24h_hype=_f(vol.get("24h")), vol_7d_hype=_f(vol.get("7d")),
        change_1h=_f(ch.get("1h")), change_6h=_f(ch.get("6h")),
        change_24h=_f(ch.get("24h")), change_7d=_f(ch.get("7d")),
        last_trade_at=_i(st.get("lastTradeAt")), version=j.get("version"),
        holders=_i(j.get("holders")), buys_24h=_i(st.get("buys24h")),
        trades_24h=_i(st.get("trades24h")), created_at=created,
        x_url=links.get("x") if isinstance(links.get("x"), str) else None)


class AgoraMarket:
    def __init__(self, cfg: MarketConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"user-agent": UA, "accept": "application/json"})
        self._tokens: dict[str, tuple[float, TokenInfo | None]] = {}
        self._hype_usd: tuple[float, float | None] = (0.0, None)
        self._lists: dict[tuple, tuple[float, dict | None]] = {}
        self._details: dict[tuple, tuple[float, dict | None]] = {}

    def _get(self, path: str, **params: Any) -> Any:
        r = self.session.get(f"{self.cfg.api}{path}", params=params or None,
                             timeout=self.cfg.timeout_s)
        r.raise_for_status()
        return r.json()

    def token(self, address: str) -> TokenInfo | None:
        if not self.cfg.enabled:
            return None
        now = time.monotonic()
        hit = self._tokens.get(address)
        if hit and now - hit[0] < self.cfg.cache_s:
            return hit[1]
        try:
            info: TokenInfo | None = parse_token(address, self._get(f"/api/tokens/{address}",
                                                                    range="24h"))
        except (requests.RequestException, ValueError, AttributeError) as e:
            log.info("market data for %s unavailable: %s", address, type(e).__name__)
            info = None
        self._tokens[address] = (now, info)
        if len(self._tokens) > 2000:  # bound memory on long runs
            self._tokens = dict(sorted(self._tokens.items(), key=lambda kv: kv[1][0])[-1000:])
        return info

    def hype_usd(self) -> float | None:
        if not self.cfg.enabled:
            return None
        now = time.monotonic()
        if now - self._hype_usd[0] < 300:
            return self._hype_usd[1]
        try:
            rate = _f(self._get("/api/traders", limit=1).get("hypeUsd"))
        except (requests.RequestException, ValueError, AttributeError):
            rate = self._hype_usd[1]  # keep the last known rate
        self._hype_usd = (now, rate)
        return rate

    # --- chain-wide list and candles (Intelligence Center) -----------------------------
    def token_list(self, *, sort: str = "volume", window: str = "24h", limit: int = 200,
                   offset: int = 0, launchpad: str = "all") -> dict | None:
        """Raw page of Agora's token list, or None. Cached for cache_s."""
        if not self.cfg.enabled:
            return None
        key = (sort, window, limit, offset, launchpad)
        now = time.monotonic()
        hit = self._lists.get(key)
        if hit and now - hit[0] < self.cfg.cache_s:
            return hit[1]
        try:
            j = self._get("/api/tokens", sort=sort, window=window, limit=min(limit, 200),
                          offset=offset, launchpad=launchpad)
            out = j if isinstance(j, dict) and isinstance(j.get("tokens"), list) else None
        except (requests.RequestException, ValueError) as e:
            log.info("token list unavailable: %s", type(e).__name__)
            out = None
        self._lists[key] = (now, out)
        if out is not None and out.get("hypeUsd"):
            self._hype_usd = (now, _f(out["hypeUsd"]))
        return out

    def top_tokens(self, n: int, *, window: str = "24h", sort: str = "volume"
                   ) -> tuple[list[TokenInfo], dict]:
        """Up to n tokens chain-wide, plus {"totals", "synced", "hypeUsd"} metadata."""
        out: list[TokenInfo] = []
        meta: dict = {}
        offset = 0
        while len(out) < n:
            want = min(200, n - len(out))
            page = self.token_list(sort=sort, window=window, limit=want, offset=offset)
            if page is None:
                break
            meta = {k: page.get(k) for k in ("totals", "synced", "hypeUsd", "total")}
            items = page["tokens"]
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("address"), str):
                    out.append(parse_token(it["address"], it))
            if len(items) < want:
                break
            offset += len(items)
        return out[:n], meta

    def token_detail(self, address: str, range_: str = "24h") -> dict | None:
        """Raw token detail including candles (cached for cache_s)."""
        if not self.cfg.enabled:
            return None
        key = (address, range_)
        now = time.monotonic()
        hit = self._details.get(key)
        if hit and now - hit[0] < self.cfg.cache_s:
            return hit[1]
        try:
            j = self._get(f"/api/tokens/{address}", range=range_)
            out = j if isinstance(j, dict) else None
        except (requests.RequestException, ValueError) as e:
            log.info("token detail %s unavailable: %s", address, type(e).__name__)
            out = None
        self._details[key] = (now, out)
        if len(self._details) > 500:
            self._details = dict(sorted(self._details.items(), key=lambda kv: kv[1][0])[-250:])
        return out


def candles(detail: dict | None) -> list[tuple[int, float, float, float, float, float]]:
    """[(t, open, high, low, close, volume_hype)] sorted by t; malformed rows dropped."""
    out = []
    for c in (detail or {}).get("candles") or []:
        try:
            t, o, h, lo, cl, v = c[:6]
            out.append((int(t), float(o), float(h), float(lo), float(cl), float(v or 0)))
        except (TypeError, ValueError):
            continue
    return sorted(out)
