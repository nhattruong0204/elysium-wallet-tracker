"""Token market data from Agora's public API, for alert cards.

Endpoints (found in agoradex.app's own frontend, 2026-11-02):
  GET /api/tokens/<address>?range=24h  -> name, symbol, launchpad, holders, createdAt (ms),
                                          stats: price, mcap, volume{1h,6h,24h,7d},
                                          change{1h,6h,24h,7d} (fractions), buys24h, trades24h
  GET /api/traders?limit=1             -> hypeUsd (HYPE/USD rate Agora uses)
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
    vol_24h_hype: float | None = None
    change_1h: float | None = None     # fraction: 0.12 = +12 %
    change_24h: float | None = None
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
        address=address, symbol=j.get("symbol"), name=j.get("name"),
        launchpad=(j.get("launchpad") or None),
        price_hype=_f(st.get("price")), mcap_hype=_f(st.get("mcap")),
        vol_1h_hype=_f(vol.get("1h")), vol_24h_hype=_f(vol.get("24h")),
        change_1h=_f(ch.get("1h")), change_24h=_f(ch.get("24h")),
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
