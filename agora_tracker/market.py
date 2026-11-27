"""Token market data for alert cards and snapshots: Agora's public API for every token, with
Montra's own figures for tokens launched on Montra.

Agora endpoints (found in agoradex.app's own frontend, 2026-11-02):
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
Agora's prices, caps and volumes are in the token's quote asset, `base` ("HYPE" or "KNTQ"),
not always HYPE. Responses carry `kntqHype` (HYPE per KNTQ; on the list page at the top
level). parse_token and candles convert to HYPE; a quote they cannot price leaves the value
unknown. Taking KNTQ amounts as HYPE overstated KNTQ-quoted tokens by HYPE/KNTQ (~320x on
2026-11-27: SENDU on Montra showed $4.89M on our card against $13.7K on Montra).

Montra (montramarkets.xyz/docs/api, public, no key, per-client rate limit, 429 = slow down):
  GET /api/tokens/<address>            -> token card + pool details
  GET /api/tokens?sort=volume&limit<=100&page=N
                                       -> token cards, {tokens, total, page, pages}
                                          (list views: montra_list_pages of these, plus up
                                          to montra_list_lookups single cards for the rest)
A card has price (pool price after the last trade, quote per whole token), marketCap
(price x (supply - burned)), volume1h/volume24h (quote smallest units), the same in dollars
(priceUsd, marketCapUsd, volume*Usd: valued at quote.usd), change24hPct, holders,
launchedAt/lastTradeAt (ms) and quote{symbol, decimals, native, usd, usdSource}.
Agora is not that figure for Montra tokens (all 400 busiest Montra tokens compared on
2026-11-27): it ignores burns (HYPE pools up to 16% high; MONTRA itself has burned 77% of
its supply), and pools quoted in MONTRA were 10-20x off. So for a token Agora tags as Montra,
with_montra replaces price, market cap, 1h/24h volume, 24h change and holders with Montra's:
the native HYPE figures for HYPE pools, otherwise the dollar figure / the HYPE rate the card
uses, so the card shows Montra's dollar value. Agora's remaining figures (1h/6h/7d change,
6h/7d volume, buys) are kept only when Agora's market cap agrees with Montra's.
Values are indicative (last trade), not executable quotes.

Never raises: a failure returns None (or Agora's figures) and the alert is sent anyway.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any

import requests

from .config import MarketConfig

log = logging.getLogger(__name__)
UA = "elysium-wallet-tracker (+https://github.com/nhattruong0204/elysium-wallet-tracker)"
MONTRA_LAUNCHPADS = frozenset({"montra"})   # Agora's `launchpad` for Montra tokens
# Agora's market cap within this of Montra's: Agora's other figures for the token are usable.
# Burns explain up to ~16% on HYPE pools (2026-11-27); MONTRA-quoted pools were 10-20x off.
AGREE_TOL = 0.25


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
    telegram_url: str | None = None
    website_url: str | None = None
    image: str | None = None           # Agora image proxy path or URL
    quote: str | None = None           # quote asset the pool trades in (values above: HYPE)
    source: str = "agora"              # "montra" when Montra's own figures were applied


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return None if f is None else int(f)


def to_hype(j: dict | None, kntq_hype: Any = None) -> float | None:
    """HYPE per unit of the quote asset Agora used for this token (1.0 for HYPE), None when
    the quote cannot be priced. `kntq_hype`: the list page's top-level rate."""
    base = str((j or {}).get("base") or "HYPE").strip().upper()
    if base in ("HYPE", "WHYPE"):
        return 1.0
    if base == "KNTQ":
        rate = _f((j or {}).get("kntqHype")) or _f(kntq_hype)
        return rate if rate and rate > 0 else None
    return None


def _scaled(v: Any, k: float | None) -> float | None:
    x = _f(v)
    return x * k if x is not None and k is not None else None


def parse_token(address: str, j: dict, kntq_hype: Any = None) -> TokenInfo:
    st = j.get("stats") or {}
    vol = st.get("volume") or {}
    k = to_hype(j, kntq_hype)
    ch = st.get("change") or {}
    created = _i(j.get("createdAt"))
    if created and created > 10**11:  # milliseconds
        created //= 1000
    links = j.get("links") or {}
    return TokenInfo(
        address=address.lower(), symbol=j.get("symbol"), name=j.get("name"),
        launchpad=(j.get("launchpad") or None),
        price_hype=_scaled(st.get("price"), k), mcap_hype=_scaled(st.get("mcap"), k),
        vol_1h_hype=_scaled(vol.get("1h"), k), vol_6h_hype=_scaled(vol.get("6h"), k),
        vol_24h_hype=_scaled(vol.get("24h"), k), vol_7d_hype=_scaled(vol.get("7d"), k),
        change_1h=_f(ch.get("1h")), change_6h=_f(ch.get("6h")),
        change_24h=_f(ch.get("24h")), change_7d=_f(ch.get("7d")),
        last_trade_at=_i(st.get("lastTradeAt")), version=j.get("version"),
        holders=_i(j.get("holders")), buys_24h=_i(st.get("buys24h")),
        trades_24h=_i(st.get("trades24h")), created_at=created,
        x_url=_link(links, "x", "twitter"), telegram_url=_link(links, "telegram", "tg"),
        website_url=_link(links, "website", "web", "site"),
        image=j.get("image") if isinstance(j.get("image"), str) else None,
        quote=str(j.get("base") or "HYPE").upper())


def _link(links: dict, *keys: str) -> str | None:
    """First http(s) link under any of `keys` (Agora's `links` object)."""
    for k in keys:
        v = links.get(k)
        if isinstance(v, str) and v.startswith(("https://", "http://")):
            return v
    return None


def is_montra(info: TokenInfo | None) -> bool:
    return bool(info and (info.launchpad or "").strip().lower() in MONTRA_LAUNCHPADS)


def _ms(v: Any) -> int | None:
    x = _i(v)
    return x // 1000 if x and x > 10**11 else x


def _montra_hype(card: dict, usd_key: str, native_key: str, hype_usd: float | None, *,
                 amount: bool = False) -> float | None:
    """A Montra figure in HYPE: native for a HYPE pool (amounts in wei), otherwise its dollar
    value / hype_usd. None when neither is available (e.g. a quote Montra cannot price)."""
    q = card.get("quote") or {}
    if q.get("native") or str(q.get("symbol") or "").upper() in ("HYPE", "WHYPE"):
        x = _f(card.get(native_key))
        if x is not None and amount:
            x /= 10 ** int(_i(q.get("decimals")) or 18)
        return x
    u = _f(card.get(usd_key))
    return u / hype_usd if u is not None and hype_usd else None


def with_montra(address: str, agora: TokenInfo | None, card: dict | None,
                hype_usd: float | None) -> TokenInfo | None:
    """Agora's TokenInfo with Montra's own figures applied (module doc). Agora's info is
    returned unchanged when there is no card or Montra's market cap cannot be valued. Pure."""
    if not isinstance(card, dict):
        return agora
    mcap = _montra_hype(card, "marketCapUsd", "marketCap", hype_usd)
    if mcap is None or mcap < 0:
        return agora
    base = agora or TokenInfo(address=address.lower(), symbol=card.get("symbol"),
                              name=card.get("name"), launchpad="montra")
    agrees = (agora is not None and agora.mcap_hype is not None and mcap > 0
              and abs(agora.mcap_hype / mcap - 1) <= AGREE_TOL)

    def kept(v: Any) -> Any:
        return v if agrees else None

    ch24 = _f(card.get("change24hPct"))
    holders = _i(card.get("holders"))
    quote = (card.get("quote") or {}).get("symbol")
    return replace(
        base,
        price_hype=_montra_hype(card, "priceUsd", "price", hype_usd), mcap_hype=mcap,
        vol_1h_hype=_montra_hype(card, "volume1hUsd", "volume1h", hype_usd, amount=True),
        vol_24h_hype=_montra_hype(card, "volume24hUsd", "volume24h", hype_usd, amount=True),
        vol_6h_hype=kept(base.vol_6h_hype), vol_7d_hype=kept(base.vol_7d_hype),
        change_1h=kept(base.change_1h), change_6h=kept(base.change_6h),
        change_7d=kept(base.change_7d),
        change_24h=ch24 / 100 if ch24 is not None else kept(base.change_24h),
        buys_24h=kept(base.buys_24h), trades_24h=kept(base.trades_24h),
        holders=holders if holders is not None else base.holders,
        last_trade_at=_ms(card.get("lastTradeAt")) or base.last_trade_at,
        created_at=base.created_at or _ms(card.get("launchedAt")),
        quote=str(quote) if quote else base.quote, source="montra")


class MontraMarket:
    """Montra's own token cards (montramarkets.xyz/docs/api). Never raises.

    Cached for cache_s; an unknown token for NOT_FOUND_S. A 429 pauses every Montra call for
    the server's Retry-After (bounded), and callers keep Agora's figures meanwhile."""

    NOT_FOUND_S = 3600.0
    PAUSE_S = (5.0, 60.0, 600.0)        # (min, default, max) pause after a 429

    def __init__(self, cfg: MarketConfig, session: requests.Session):
        self.cfg, self.session = cfg, session
        self._cards: dict[str, tuple[float, float, dict | None]] = {}   # at, ttl, card
        self._list: tuple[float, dict[str, dict]] = (float("-inf"), {})
        self._paused_until = 0.0

    def _get(self, path: str, **params: Any) -> Any:
        """JSON, or None for 404. Raises requests/ValueError errors; RuntimeError when paused."""
        if time.monotonic() < self._paused_until:
            raise RuntimeError("montra paused after a 429")
        r = self.session.get(f"{self.cfg.montra_api}{path}", params=params or None,
                             timeout=self.cfg.timeout_s)
        if r.status_code == 429:
            lo, dflt, hi = self.PAUSE_S
            wait = min(max(_f((r.headers or {}).get("retry-after")) or dflt, lo), hi)
            self._paused_until = time.monotonic() + wait
            log.warning("montra: rate limited (429), pausing Montra calls for %.0f s", wait)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def card(self, address: str) -> dict | None:
        address = address.lower()
        now = time.monotonic()
        hit = self._cards.get(address)
        if hit and now - hit[0] < hit[1]:
            return hit[2]
        ttl = self.cfg.cache_s
        try:
            j = self._get(f"/api/tokens/{address}")
            card = j if isinstance(j, dict) and j.get("marketCap") is not None else None
            if j is None:
                ttl = self.NOT_FOUND_S
        except (requests.RequestException, ValueError, RuntimeError) as e:
            log.info("montra card for %s unavailable: %s", address, type(e).__name__)
            card = None
        self._cards[address] = (now, ttl, card)
        if len(self._cards) > 2000:  # bound memory on long runs
            self._cards = dict(sorted(self._cards.items(), key=lambda kv: kv[1][0])[-1000:])
        return card

    def by_volume(self) -> dict[str, dict]:
        """address -> card for Montra's busiest tokens (montra_list_pages x 100 by volume),
        for overlaying list pages. Cached for cache_s; {} when unavailable."""
        now = time.monotonic()
        if now - self._list[0] < self.cfg.cache_s:
            return self._list[1]
        out: dict[str, dict] = {}
        try:
            for page in range(1, max(0, self.cfg.montra_list_pages) + 1):
                j = self._get("/api/tokens", sort="volume", limit=100, page=page)
                items = (j or {}).get("tokens") if isinstance(j, dict) else None
                if not isinstance(items, list):
                    break
                for c in items:
                    if isinstance(c, dict) and isinstance(c.get("address"), str):
                        out[c["address"].lower()] = c
                if len(items) < 100 or page >= int(_i((j or {}).get("pages")) or page):
                    break
        except (requests.RequestException, ValueError, RuntimeError) as e:
            log.info("montra token list unavailable: %s", type(e).__name__)
        self._list = (now, out)
        return out


class AgoraMarket:
    def __init__(self, cfg: MarketConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"user-agent": UA, "accept": "application/json"})
        self._tokens: dict[str, tuple[float, TokenInfo | None]] = {}
        self._hype_usd: tuple[float, float | None] = (0.0, None)
        self._lists: dict[tuple, tuple[float, dict | None]] = {}
        self._details: dict[tuple, tuple[float, dict | None]] = {}
        self.montra = MontraMarket(cfg, self.session) if cfg.montra else None

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
        if self.montra is not None and is_montra(info):
            info = with_montra(address, info, self.montra.card(address), self.hype_usd())
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
                    out.append(parse_token(it["address"], it, page.get("kntqHype")))
            if len(items) < want:
                break
            offset += len(items)
        out = out[:n]
        if self.montra is not None and any(is_montra(x) for x in out):
            cards = dict(self.montra.by_volume())
            # Montra tokens outside Montra's busiest list: looked up one by one, bounded
            # (14 of 103 on Agora's top 150 on 2026-11-27); the rest keep Agora's figures.
            missing = [x.address for x in out if is_montra(x) and x.address not in cards]
            for a in missing[:max(0, self.cfg.montra_list_lookups)]:
                c = self.montra.card(a)
                if c is not None:
                    cards[a] = c
            rate = _f(meta.get("hypeUsd")) or self.hype_usd()
            out = [(with_montra(x.address, x, cards.get(x.address), rate) or x)
                   if is_montra(x) else x for x in out]
        return out, meta

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
    """[(t, open, high, low, close, volume_hype)] sorted by t, in HYPE (converted from the
    token's quote asset; none when that quote cannot be priced); malformed rows dropped."""
    k = to_hype(detail)
    if k is None:
        return []
    out = []
    for c in (detail or {}).get("candles") or []:
        try:
            t, o, h, lo, cl, v = c[:6]
            out.append((int(t), float(o) * k, float(h) * k, float(lo) * k, float(cl) * k,
                        float(v or 0) * k))
        except (TypeError, ValueError):
            continue
    return sorted(out)
