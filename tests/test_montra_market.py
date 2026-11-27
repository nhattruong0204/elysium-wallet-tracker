"""Montra tokens carry Montra's own price and market cap (owner request, 2026-11-27: make the
card match Montra). Card values below are Montra's API (api.montramarkets.xyz) on 2026-11-27."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import requests

from agora_tracker import cards
from agora_tracker.collector import Collector
from agora_tracker.config import CollectorConfig, MarketConfig
from agora_tracker.market import AgoraMarket, MontraMarket, parse_token, with_montra

HYPE_USD = 65.5355
KNTQ_HYPE = 0.003077721565940432
SENDU = "0x56e1e08fe536de0f18dcee008f57068788d61d33"
ARTNOM = "0x345d1e58ea14d2bfeeaadcb9d5c13aa74d30c590"
MONTRA = "0x063f7cc5339f92a5fd7baf3d22233044fda1f327"
LIQUIDV = "0x" + "4c" * 20
OTHER = "0x" + "e1" * 20
KNTQ = {"address": "0xe45a355d9e9cad2ad85f2d03e3ee3102a036acf0", "symbol": "KNTQ",
        "decimals": 18, "native": False, "usd": 0.20375, "usdSource": "hypercore"}
HYPE_Q = {"address": "0x" + "0" * 40, "symbol": "HYPE", "decimals": 18, "native": True,
          "usd": 65.5965, "usdSource": "hypercore"}

SENDU_AGORA = {
    "address": SENDU, "symbol": "SENDU", "name": "SENDU", "launchpad": "montra", "base": "KNTQ",
    "kntqHype": KNTQ_HYPE, "holders": 40,
    "stats": {"price": 6.740423823558739e-05, "mcap": 67404.2382355874,
              "volume": {"1h": 42893.39, "6h": 42893.39, "24h": 43666.89, "7d": 43666.89},
              "change": {"1h": 0.0614, "24h": 0.0745}, "buys24h": 90, "trades24h": 120}}
SENDU_CARD = {
    "address": SENDU, "symbol": "SENDU", "name": "SENDU", "quote": KNTQ,
    "price": "0.000067404238235587390000000000000000", "priceUsd": 0.013734113541,
    "marketCap": "67404.2382355874", "marketCapUsd": 13713.73,
    "supply": "1000000000000000000000000000", "burned": "196",
    "volume1h": "42893390000000000000000", "volume1hUsd": 8739.53,
    "volume24h": "43666890000000000000000", "volume24hUsd": 8897.13,
    "change24hPct": 7.2, "holders": 37, "launchedAt": 1795766400000,
    "lastTradeAt": 1795775100000}
# A pool quoted in MONTRA: Agora calls it HYPE and its numbers are far off.
ARTNOM_AGORA = {"address": ARTNOM, "symbol": "ARTNOM", "launchpad": "montra", "base": "HYPE",
                "stats": {"price": 3e-08, "mcap": 30.0, "volume": {"24h": 2.0, "7d": 5.0},
                          "change": {"1h": 0.0, "7d": 0.0}, "buys24h": 3, "trades24h": 4}}
ARTNOM_CARD = {"address": ARTNOM, "symbol": "ARTNOM",
               "quote": {"symbol": "MONTRA", "decimals": 18, "native": False,
                         "usd": 0.0156436297206432, "usdSource": "launch"},
               "price": "0.002270010640398147924460383679464693",
               "priceUsd": 3.551120592030877e-05,
               "marketCap": "2270010.640398147924460383659034597236416668",
               "marketCapUsd": 35511.20592030877, "volume24hUsd": 4100.0, "holders": 210}
# Montra's own token: KNTQ pool, 77% of the supply burned.
MONTRA_CARD = {"address": MONTRA, "symbol": "MONTRA", "quote": {**KNTQ, "usd": 0.20409},
               "price": "0.076991555846465682024728710009914023",
               "priceUsd": 0.01571320663270518,
               "marketCap": "69440021.7246646037204200272620552625659946",
               "marketCapUsd": 14172014.033786798,
               "supply": "4000000000000000000000000000",
               "burned": "3098082627877531500736364029",
               "volume24h": "11117456128214177012606769", "volume24hUsd": 2268961.6212071953,
               "volume1h": "68354326548529215765838", "volume1hUsd": 13950.43450518132,
               "change24hPct": 65.83630081986169, "holders": 4909}
LIQUIDV_CARD = {"address": LIQUIDV, "symbol": "LiquidV", "quote": HYPE_Q,
                "price": "0.000054172784489053859137301029598052",
                "priceUsd": 0.003553545057736222,
                "marketCap": "53996.989430623666374105294893771680873942",
                "marketCapUsd": 3542013.517185906,
                "volume24h": "15270066188447988155681", "volume24hUsd": 1001662.8967011422}


def rel(a: float, b: float) -> float:
    return abs(a / b - 1)


def test_kntq_pool_shows_montras_dollar_market_cap():
    agora = parse_token(SENDU, SENDU_AGORA)
    info = with_montra(SENDU, agora, SENDU_CARD, HYPE_USD)
    assert info is not None and info.source == "montra" and info.quote == "KNTQ"
    assert rel(info.mcap_hype * HYPE_USD, 13713.73) < 1e-12          # Montra's figure, exactly
    assert rel(info.price_hype * HYPE_USD, 0.013734113541) < 1e-12
    assert rel(info.vol_24h_hype * HYPE_USD, 8897.13) < 1e-12
    assert info.holders == 37 and info.change_24h == pytest.approx(0.072)
    assert info.created_at == 1795766400 and info.last_trade_at == 1795775100
    # Agora agrees once converted (0.9% off), so its other figures stay
    assert info.change_1h == 0.0614 and info.buys_24h == 90 and info.vol_7d_hype is not None
    assert cards.market_cap_label(info, HYPE_USD) == "<b>MC</b>: $13.71K"  # Montra: $13.7K


def test_montra_quoted_pool_uses_montra_and_drops_agoras_other_figures():
    agora = parse_token(ARTNOM, ARTNOM_AGORA)
    assert agora.mcap_hype == 30.0                                    # Agora: ~$2K, wrong
    info = with_montra(ARTNOM, agora, ARTNOM_CARD, HYPE_USD)
    assert rel(info.mcap_hype * HYPE_USD, 35511.20592030877) < 1e-12
    assert info.quote == "MONTRA" and info.holders == 210
    # 18x apart: nothing else Agora said about this pool is trusted
    assert info.change_1h is None and info.change_7d is None and info.vol_7d_hype is None
    assert info.buys_24h is None and info.trades_24h is None
    assert info.vol_1h_hype is None                                   # Montra card lacked it


def test_hype_pool_uses_montras_native_hype_figures():
    for rate in (HYPE_USD, None):                                     # no rate needed
        info = with_montra(LIQUIDV, None, LIQUIDV_CARD, rate)
        assert info.mcap_hype == float("53996.989430623666374105294893771680873942")
        assert info.price_hype == float("0.000054172784489053859137301029598052")
        assert info.vol_24h_hype == 15270066188447988155681 / 1e18     # wei -> HYPE
        assert info.launchpad == "montra" and info.symbol == "LiquidV"


def test_burned_supply_is_left_out_like_on_montra():
    info = with_montra(MONTRA, None, MONTRA_CARD, HYPE_USD)
    assert rel(info.mcap_hype * HYPE_USD, 14172014.033786798) < 1e-12
    circulating = (4 * 10**27 - 3098082627877531500736364029) / 1e18
    assert rel(cards.supply(info), circulating) < 1e-6                # 902M of 4B


def test_unvalued_card_keeps_agora():
    agora = parse_token(SENDU, SENDU_AGORA)
    unpriced = {**SENDU_CARD, "quote": {**KNTQ, "usd": None}, "marketCapUsd": None}
    assert with_montra(SENDU, agora, unpriced, HYPE_USD) is agora
    assert with_montra(SENDU, agora, SENDU_CARD, None) is agora       # no HYPE rate
    assert with_montra(SENDU, agora, None, HYPE_USD) is agora


class Resp:
    def __init__(self, status: int = 200, body: object = None, headers: dict | None = None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeHttp:
    """Routes Agora and Montra URLs; records the Montra paths asked for."""

    def __init__(self, agora: dict, montra: dict):
        self.agora, self.montra, self.montra_calls = agora, montra, []

    def __call__(self, url, params=None, timeout=None):
        if "montramarkets" in url:
            path = url.split("montramarkets.xyz", 1)[1]
            self.montra_calls.append((path, dict(params or {})))
            r = self.montra.get(path, Resp(404))
            return r(params) if callable(r) else r
        if url.endswith("/api/traders"):
            return Resp(200, {"hypeUsd": HYPE_USD})
        for k, v in self.agora.items():
            if url.endswith(k):
                return v
        return Resp(404)


def market(http: FakeHttp, **kw) -> AgoraMarket:
    m = AgoraMarket(MarketConfig(**kw))
    m.session.get = http
    return m


def test_token_uses_montra_for_montra_tokens_only():
    other = {**SENDU_AGORA, "address": OTHER, "launchpad": "elyspump", "base": "HYPE"}
    http = FakeHttp({f"/api/tokens/{SENDU}": Resp(200, SENDU_AGORA),
                     f"/api/tokens/{OTHER}": Resp(200, other)},
                    {f"/api/tokens/{SENDU}": Resp(200, SENDU_CARD)})
    m = market(http)
    info = m.token(SENDU)
    assert info.source == "montra" and rel(info.mcap_hype * m.hype_usd(), 13713.73) < 1e-12
    assert m.token(OTHER).source == "agora"
    assert [p for p, _ in http.montra_calls] == [f"/api/tokens/{SENDU}"]
    assert market(http, montra=False).token(SENDU).source == "agora"


def test_rate_limit_pauses_montra_and_keeps_agoras_converted_figures():
    http = FakeHttp({f"/api/tokens/{SENDU}": Resp(200, SENDU_AGORA)},
                    {f"/api/tokens/{SENDU}": Resp(429, None, {"retry-after": "30"})})
    m = market(http, cache_s=0)
    for _ in range(3):
        info = m.token(SENDU)
        assert info.source == "agora" and info.mcap_hype == 67404.2382355874 * KNTQ_HYPE
    assert len(http.montra_calls) == 1                                # paused after the 429
    assert 25 < m.montra._paused_until - time.monotonic() <= 30


def test_unknown_token_is_remembered_for_an_hour():
    http = FakeHttp({}, {})
    mm = MontraMarket(MarketConfig(cache_s=0), requests.Session())
    mm.session.get = http
    assert mm.card(SENDU) is None and mm.card(SENDU) is None
    assert len(http.montra_calls) == 1


def test_list_views_get_montras_figures_from_its_busiest_tokens():
    other = {**SENDU_AGORA, "address": OTHER, "launchpad": "elyspump", "base": "HYPE"}
    page = Resp(200, {"tokens": [SENDU_AGORA, other], "hypeUsd": HYPE_USD,
                      "kntqHype": KNTQ_HYPE})
    lst = Resp(200, {"tokens": [SENDU_CARD], "total": 1, "page": 1, "pages": 1})
    http = FakeHttp({"/api/tokens": page}, {"/api/tokens": lst})
    m = market(http)
    for _ in range(2):
        toks, _meta = m.top_tokens(2)
        assert [x.source for x in toks] == ["montra", "agora"]
        assert rel(toks[0].mcap_hype * HYPE_USD, 13713.73) < 1e-12
    assert http.montra_calls == [("/api/tokens", {"sort": "volume", "limit": 100, "page": 1})]


def test_snapshots_record_where_the_figures_came_from():
    col = Collector(SimpleNamespace(chain_id=999), None, CollectorConfig())
    info = with_montra(SENDU, parse_token(SENDU, SENDU_AGORA), SENDU_CARD, HYPE_USD)
    assert col._row(info, None, None, None)["source"] == "montra"
    assert col._row(parse_token(SENDU, SENDU_AGORA), None, None, None)["source"] == "agora"


def test_list_tokens_outside_montras_busiest_are_looked_up_one_by_one():
    arto = parse_token(ARTNOM, ARTNOM_AGORA)
    page = Resp(200, {"tokens": [SENDU_AGORA, ARTNOM_AGORA], "hypeUsd": HYPE_USD,
                      "kntqHype": KNTQ_HYPE})
    lst = Resp(200, {"tokens": [SENDU_CARD], "total": 1, "page": 1, "pages": 1})
    http = FakeHttp({"/api/tokens": page},
                    {"/api/tokens": lst, f"/api/tokens/{ARTNOM}": Resp(200, ARTNOM_CARD)})
    toks, _ = market(http).top_tokens(2)
    assert [x.source for x in toks] == ["montra", "montra"]
    assert toks[1].mcap_hype != arto.mcap_hype
    assert rel(toks[1].mcap_hype * HYPE_USD, 35511.20592030877) < 1e-12
    toks, _ = market(http, montra_list_lookups=0).top_tokens(2)
    assert [x.source for x in toks] == ["montra", "agora"]            # bounded: none looked up


def test_hype_rate_is_fetched_right_after_a_host_boot(monkeypatch):
    """time.monotonic() is near 0 after a boot (CI runners): the rate cache must still miss."""
    http = FakeHttp({}, {})
    m = market(http)
    monkeypatch.setattr("agora_tracker.market.time.monotonic", lambda: 12.0)
    assert m.hype_usd() == HYPE_USD
