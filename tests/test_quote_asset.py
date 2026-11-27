"""Agora prices KNTQ-quoted tokens in KNTQ, not HYPE (owner report, 2026-11-27: SENDU on Montra,
$13.7K market cap, showed $4.89M on our card)."""
from __future__ import annotations

from agora_tracker import cards
from agora_tracker.cards import Links
from agora_tracker.config import LinksConfig
from agora_tracker.market import candles, parse_token, to_hype

SENDU = "0x56e1e08fe536de0f18dcee008f57068788d61d33"
KNTQ_HYPE = 0.003077721565940432          # Agora's kntqHype, 2026-11-27 10:30 UTC
HYPE_USD = 65.5355
# Agora's detail response for SENDU (stats are in KNTQ, the pool's quote)
SENDU_DETAIL = {
    "address": SENDU, "symbol": "SENDU", "name": "SENDU", "launchpad": "montra", "base": "KNTQ",
    "kntqHype": KNTQ_HYPE,
    "stats": {"price": 6.740423823558739e-05, "mcap": 67404.2382355874,
              "volume": {"1h": 42893.39, "6h": 42893.39, "24h": 43666.89, "7d": 43666.89},
              "change": {"1h": 6.14, "24h": 7.45}},
    "candles": [[1795770000000, 1.0e-05, 7.0e-05, 1.0e-05, 6.7e-05, 43666.0]]}
# Montra's own token card at the same time (api.montramarkets.xyz/api/tokens/<token>)
MONTRA_MCAP_USD = 13713.73


def test_kntq_quote_is_converted_to_hype_and_matches_montra():
    info = parse_token(SENDU, SENDU_DETAIL)
    assert info.quote == "KNTQ"
    assert info.mcap_hype == 67404.2382355874 * KNTQ_HYPE
    usd = info.mcap_hype * HYPE_USD
    assert abs(usd / MONTRA_MCAP_USD - 1) < 0.015            # within 1.5% of Montra
    assert info.price_hype == 6.740423823558739e-05 * KNTQ_HYPE
    assert info.vol_24h_hype == 43666.89 * KNTQ_HYPE
    assert info.change_1h == 6.14                            # fractions need no conversion


def test_card_shows_montra_like_market_cap_not_hype_times_kntq():
    info = parse_token(SENDU, SENDU_DETAIL)
    label = cards.market_cap_label(info, HYPE_USD)
    assert "$13.5" in label or "$13.6" in label, label        # was $4.4M+ before the fix
    p = {"type": "trade", "v": 2, "kind": "BUY", "tx": "0x" + "1" * 64, "block": 1, "ts": 1,
         "token": SENDU, "symbol": "SENDU", "decimals": 18, "qty": str(17_600_000 * 10**18),
         "hype_wei": str(5 * 10**18), "confidence": "high", "hype_method": "balance_diff",
         "notes": [], "wallet": "0x" + "c4" * 20, "pos_qty": str(17_600_000 * 10**18),
         "pos_cost_wei": str(5 * 10**18), "pos_basis": "known"}
    text, _ = cards.render(p, Links(LinksConfig()), info, HYPE_USD)
    assert "$4." not in text.split("\n")[1] and "M</" not in text.split("\n")[1]


def test_list_items_use_the_page_rate_and_hype_is_untouched():
    item = {k: v for k, v in SENDU_DETAIL.items() if k != "kntqHype"}
    assert parse_token(SENDU, item).mcap_hype is None                    # no rate: unknown
    assert parse_token(SENDU, item, KNTQ_HYPE).mcap_hype == 67404.2382355874 * KNTQ_HYPE
    hype = {**SENDU_DETAIL, "base": "HYPE", "kntqHype": None}
    assert parse_token(SENDU, hype).mcap_hype == 67404.2382355874
    legacy = {k: v for k, v in hype.items() if k != "base"}           # no `base`: HYPE
    assert parse_token(SENDU, legacy).mcap_hype == 67404.2382355874


def test_unknown_quote_is_unknown_not_wrong():
    assert to_hype({"base": "USDC"}) is None
    info = parse_token(SENDU, {**SENDU_DETAIL, "base": "USDC"})
    assert info.mcap_hype is None and info.price_hype is None and info.vol_24h_hype is None
    assert candles({**SENDU_DETAIL, "base": "USDC"}) == []


def test_candles_are_converted_too():
    (t, o, h, lo, c, v), = candles(SENDU_DETAIL)
    assert h == 7.0e-05 * KNTQ_HYPE and v == 43666.0 * KNTQ_HYPE
    assert candles({"base": "HYPE", "candles": [[1, 1, 2, 0.5, 1.5, 3]]}) == [(1, 1, 2, 0.5, 1.5, 3)]
