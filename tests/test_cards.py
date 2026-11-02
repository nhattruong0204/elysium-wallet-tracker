"""Alert cards: links everywhere, market data, buttons, and never failing delivery."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import requests

from agora_tracker import cards
from agora_tracker.cards import Links
from agora_tracker.config import LinksConfig, MarketConfig, TelegramConfig
from agora_tracker.market import AgoraMarket, parse_token
from agora_tracker.notify import Notifier

FIX = json.loads((Path(__file__).parent / "fixtures" / "agora_token_blob.json").read_text())
TOKEN = FIX["address"]
WALLET = "0x7df2a4a7b374157fcc9edb43e593f941bdda9158"
TX = "0x25a2e17032a8576e3386f3a323b49f8f66e6d6f3c91511e4142dca4ede9504eb"
L = Links(LinksConfig())
NOW = 1793614273 + 120


def payload(**kw) -> dict:
    p = {"type": "trade", "kind": "BUY", "tx": TX, "block": 199694, "ts": 1793614273,
         "token": TOKEN, "symbol": "BLOB", "decimals": 18, "qty": str(12_400_000 * 10**18),
         "hype_wei": str(32 * 10**17), "confidence": "high", "hype_method": "balance_diff",
         "notes": ["🆕 new position"], "wallet": WALLET, "label": "whale-1", "rank": 3,
         "pnl": 709.12, "pnl_unit": "HYPE"}
    p.update(kw)
    return p


class Tags(HTMLParser):
    """Telegram rejects unbalanced HTML; make sure every opened tag is closed."""
    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
        self.hrefs += [v for k, v in attrs if k == "href"]

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, f"unbalanced </{tag}>"


def parse(text: str) -> Tags:
    t = Tags()
    t.feed(text)
    assert t.stack == [], f"unclosed tags {t.stack}"
    return t


def test_parse_real_agora_token_response():
    info = parse_token(TOKEN, FIX)
    assert (info.symbol, info.launchpad, info.holders) == ("BLOB", "elyspump", 697)
    assert round(info.mcap_hype) == 9973 and round(info.vol_24h_hype) == 2273
    assert round(info.change_24h * 100) == 73 and info.created_at == 1793077114


def test_trade_card_links_market_data_and_buttons():
    info = parse_token(TOKEN, FIX)
    text, kb = cards.render_trade(payload(), L, info, hype_usd=85.9, now=NOW)
    hrefs = parse(text).hrefs
    for url in (f"https://www.agoradex.app/token/{TOKEN}", f"https://www.thekleos.xyz/token/{TOKEN}",
                f"https://boxa.club/token?address={TOKEN}", f"https://elysp.com/#/token/{TOKEN}",
                f"https://elyscan.live/token/{TOKEN}", f"https://elyscan.live/tx/{TX}",
                f"https://elyscan.live/address/{WALLET}", f"https://www.agoradex.app/trader/{WALLET}"):
        assert url in hrefs, url
    assert "MC <b>9.97K HYPE</b> ($856.64K)" in text
    assert "24h Vol 2.27K HYPE" in text and "24h +73%" in text and "👥 697" in text
    assert "3.200 HYPE ($274.88) → 12.40M BLOB" in text
    assert f"<code>{TOKEN}</code>" in text            # full CA, tap to copy
    assert "2m ago" in text and "PnL 709.12 HYPE" in text
    row1, row2 = kb["inline_keyboard"]
    assert [b["text"] for b in row1] == ["🛒 Agora", "Kleos", "BOXA", "ElysPump"]
    assert [b["text"] for b in row2] == ["🧾 Tx", "👤 Trader"]
    assert all(b["url"].startswith("https://") for r in kb["inline_keyboard"] for b in r)


def test_card_without_market_data_still_has_all_links():
    text, kb = cards.render_trade(payload(), L, None, None, now=NOW)
    hrefs = parse(text).hrefs
    assert f"https://boxa.club/token?address={TOKEN}" in hrefs
    assert f"https://elyscan.live/tx/{TX}" in hrefs
    assert "📊" not in text
    assert [b["text"] for b in kb["inline_keyboard"][0]] == ["🛒 Agora", "Kleos", "BOXA"]


def test_estimated_and_unknown_are_marked_and_html_is_escaped():
    text, _ = cards.render_trade(payload(confidence="estimated", hype_method="tx_value",
                                         symbol="<b>X&Y"), L, None, None)
    parse(text)
    assert "≈3.200 HYPE" in text and "estimated (tx_value)" in text and "&lt;b&gt;X&amp;Y" in text
    out, _ = cards.render_trade(payload(kind="TRANSFER_OUT", confidence="unknown", hype_wei=None,
                                        notes=[]), L, None, None)
    assert "OUT (sell?)" in out


def test_cluster_card_links_each_wallet():
    p = {"type": "cluster", "token": TOKEN, "symbol": "BLOB", "min_hype": 0.5, "window_min": 60,
         "buyers": [{"wallet": WALLET, "rank": 1, "pnl": 10.0, "pnl_unit": "HYPE"},
                    {"wallet": "0x" + "ab" * 20, "rank": None, "pnl": None}]}
    text, kb = cards.render_cluster(p, L, parse_token(TOKEN, FIX), 85.9)
    hrefs = parse(text).hrefs
    assert f"https://elyscan.live/address/0x{'ab' * 20}" in hrefs
    assert "2 tracked wallets" in text and "MC" in text
    assert len(kb["inline_keyboard"]) == 1


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b, self.text = status, body or {}, ""

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def test_market_failure_returns_none_and_is_cached():
    m = AgoraMarket(MarketConfig(cache_s=60))
    calls = []
    m.session.get = lambda *a, **k: (calls.append(1), FakeResp(503))[1]
    assert m.token(TOKEN) is None and m.token(TOKEN) is None
    assert len(calls) == 1   # cached failure: no hammering while Agora is down


def test_notifier_sends_buttons_and_falls_back_when_rendering_breaks(store):
    sent = []
    n = Notifier(store, TelegramConfig(bot_token="1:x", chat_id="1", min_interval_s=0),
                 renderer=lambda row: cards.render(row["payload"], L, None, None))
    n.session.post = lambda url, json, timeout: (sent.append(json), FakeResp(200))[1]
    store.enqueue_now("a", "fallback text", payload=payload())
    assert n.drain() == 1
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "🛒 Agora"
    assert sent[0]["parse_mode"] == "HTML"

    def broken(row):
        raise RuntimeError("market API changed shape")
    n.renderer = broken
    store.enqueue_now("b", "fallback text", payload=payload())
    assert n.drain() == 1
    assert sent[1]["text"] == "fallback text" and "reply_markup" not in sent[1]


def test_test_card_sends_latest_trade(store, monkeypatch):
    from agora_tracker import __main__ as cli
    from agora_tracker.classify import TradeEvent
    from agora_tracker.config import (
        AlertConfig,
        ChainConfig,
        Config,
        LeaderboardConfig,
        MarketConfig,
    )
    cfg = Config(chain=ChainConfig(rpc_urls=["x"]), leaderboard=LeaderboardConfig(),
                 alerts=AlertConfig(), telegram=TelegramConfig(bot_token="1:x", chat_id="1"),
                 market=MarketConfig(enabled=False))
    with store.tx() as c:
        store.insert_trade(c, TradeEvent("BUY", TX, 7, WALLET, TOKEN, 10**18, 2 * 10**18), 1)
    monkeypatch.setattr(cli, "Store", lambda **kw: store)
    sent = []
    monkeypatch.setattr(cli.Notifier, "_send", lambda self, t, kb=None: (sent.append((t, kb)) or (True, "", False)))
    assert cli.test_card(cfg) == 0
    assert "test card" in sent[0][0] and sent[0][1]["inline_keyboard"][0][0]["text"] == "🛒 Agora"
