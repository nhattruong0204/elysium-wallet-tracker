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


def test_buy_card_layout_links_and_buttons():
    info = parse_token(TOKEN, FIX)
    p = payload(v=2, is_new=True, pos_qty=str(12_400_000 * 10**18), pos_cost_wei=str(32 * 10**17),
                pos_basis="known", notes=[])
    text, kb = cards.render_trade(p, L, info, hype_usd=85.9, now=NOW)
    hrefs = parse(text).hrefs
    for url in (f"https://www.agoradex.app/token/{TOKEN}", f"https://www.thekleos.pro/token/{TOKEN}",
                f"https://boxa.club/token?address={TOKEN}", f"https://elysp.com/#/token/{TOKEN}",
                f"https://elyscan.live/tx/{TX}", f"https://elyscan.live/address/{WALLET}",
                f"https://www.agoradex.app/trader/{WALLET}", FIX["links"]["x"],
                f"https://x.com/search?q={TOKEN}&f=live"):
        assert url in hrefs, url
    lines = text.split("\n")
    assert lines[0].startswith("🆕🟢 <b>BUY</b>") and lines[0].endswith("(ElysPump)")
    assert "<b>whale-1</b>" in lines[1] and "<b>#3</b>" in lines[1] and "PnL +$60.91K" in lines[1]
    assert lines[2] == ""                                         # breathing space, like Raybot
    assert "whale-1 swapped <b>3.20 HYPE ($274.88)</b> for <b>12.40M</b>" in lines[3]
    assert "@$0.0000222" in lines[3]                               # execution price, no 1e-05
    assert lines[4].startswith("✊ Holds: <b>12.40M</b> (") and "uPnL: +$" in lines[4]
    assert any(ln.startswith("📊 24h <b>+73%</b>") and "Vol $195.25K" in ln for ln in lines)
    bar = next(ln for ln in lines if ln.startswith("🔗 "))
    assert bar.startswith("🔗 #BLOB ( <a") and "<b>MC</b>: $856.64K" in bar
    assert "<b>Seen</b>: 6d: " in bar and "9.97K HYPE" not in text
    assert f"<code>{TOKEN}</code>" in text and "2m ago" in text
    assert [[b["text"] for b in r] for r in kb["inline_keyboard"]] == [
        ["⚡ Rabby: BLOB", "⚡ OKX: BLOB"],
        ["🛒 Agora: BLOB", "BOXA: BLOB"], ["Kleos: BLOB", "ElysPump: BLOB"]]
    assert all(b["url"].startswith("https://") for r in kb["inline_keyboard"] for b in r)
    assert cards.preview_url(L, info) == "https://www.agoradex.app/api/img?src=x"


def test_sell_card_shows_sold_share_pnl_in_usd_and_remaining_bag():
    info = parse_token(TOKEN, FIX)
    p = payload(v=2, kind="SELL", notes=[], sold_pct=97.5, realized_status="measured",
                realized_wei=str(-12 * 10**17), realized_pct=-48.48,
                pos_qty=str(310_000 * 10**18), pos_cost_wei=str(8 * 10**16), pos_basis="known")
    text, _ = cards.render_trade(p, L, info, hype_usd=85.9, now=NOW)
    assert text.split("\n")[0].startswith("🔴 <b>SELL</b>")
    assert "swapped <b>12.40M</b>" in text and "for <b>3.20 HYPE ($274.88)</b>" in text
    assert "➖ Sold: 97.5% of bag" in text and "📉 PnL: <b>-$103.08</b> (-48.5%)" in text
    exit_, _ = cards.render_trade({**p, "sold_pct": 100.0, "pos_qty": "0"}, L, info, 85.9)
    assert "🚪 Sold: 100% (full exit)" in exit_ and "Holds" not in exit_
    unk, _ = cards.render_trade({**p, "realized_status": "unknown", "realized_wei": None,
                                 "realized_reason": "before_tracking"}, L, info, 85.9)
    assert "PnL: <i>unknown (bought before tracking)</i>" in unk


def test_transfer_card_names_the_counterparty():
    other = "0x" + "ee" * 20
    p = payload(v=2, kind="TRANSFER_OUT", hype_wei=None, notes=[], counterparty=other,
                sold_pct=20.0, pos_qty=str(10**18), pos_basis="partial_unknown")
    text, _ = cards.render_trade(p, L, parse_token(TOKEN, FIX), 85.9, now=NOW)
    assert text.startswith("📤 <b>SENT</b>")
    assert "sent <b>12.40M</b>" in text and f"https://elyscan.live/address/{other}" in parse(text).hrefs
    assert "➖ Moved: 20.0% of bag" in text and "uPnL" not in text   # unknown cost: no uPnL


def test_card_without_market_data_still_has_all_links():
    text, kb = cards.render_trade(payload(v=2, notes=[]), L, None, None, now=NOW)
    hrefs = parse(text).hrefs
    assert f"https://boxa.club/token?address={TOKEN}" in hrefs
    assert f"https://elyscan.live/tx/{TX}" in hrefs
    assert "📊" not in text and "MC" not in text and "@$" not in text
    assert [b["text"] for b in kb["inline_keyboard"][1]] == ["🛒 Agora: BLOB", "BOXA: BLOB"]
    assert kb["inline_keyboard"][2][1]["text"] == "🧾 Tx"
    assert cards.preview_url(L, None) is None


def test_estimated_and_unknown_are_marked_and_html_is_escaped():
    text, _ = cards.render_trade(payload(confidence="estimated", hype_method="tx_value",
                                         symbol="<b>X&Y"), L, None, None)
    parse(text)
    assert "≈3.20 HYPE" in text and "estimated (tx_value)" in text and "&lt;b&gt;X&amp;Y" in text
    assert "#bXY" in text                                  # hashtag keeps word chars only
    out, _ = cards.render_trade(payload(kind="TRANSFER_OUT", confidence="unknown", hype_wei=None,
                                        notes=[]), L, None, None)
    assert "🟠 <b>OUT (sell?)</b>" in out


def test_legacy_payloads_keep_their_notes():
    text, _ = cards.render_trade(payload(notes=["🆕 new position", "realized +0.300 HYPE"]), L,
                                 None, None)
    assert text.startswith("🆕🟢") and "<i>realized +0.300 HYPE</i>" in text


def test_number_formats():
    assert cards.price_str(0.0000073912) == "$0.00000739" and cards.price_str(1234.5) == "$1,234.50"
    assert cards.usd(-109.63, signed=True) == "-$109.63" and cards.usd(6960) == "$6.96K"
    assert cards.hashtag("e/acc") == "#eacc" and cards.hashtag("$$") == ""


def test_cluster_card_links_each_wallet():
    p = {"type": "cluster", "token": TOKEN, "symbol": "BLOB", "min_hype": 0.5, "window_min": 60,
         "buyers": [{"wallet": WALLET, "rank": 1, "pnl": 10.0, "pnl_unit": "HYPE"},
                    {"wallet": "0x" + "ab" * 20, "rank": None, "pnl": None}]}
    text, kb = cards.render_cluster(p, L, parse_token(TOKEN, FIX), 85.9)
    hrefs = parse(text).hrefs
    assert f"https://elyscan.live/address/0x{'ab' * 20}" in hrefs
    assert "2 tracked wallets" in text and "<b>MC</b>: $856.64K" in text
    assert len(kb["inline_keyboard"]) == 3


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
                 renderer=lambda row: (*cards.render(row["payload"], L, None, None),
                                       "https://www.agoradex.app/api/img?src=x"))
    n.session.post = lambda url, json, timeout: (sent.append(json), FakeResp(200))[1]
    store.enqueue_now("a", "fallback text", payload=payload())
    assert n.drain() == 1
    assert sent[0]["reply_markup"]["inline_keyboard"][1][0]["text"] == "🛒 Agora: BLOB"
    assert sent[0]["parse_mode"] == "HTML"
    assert sent[0]["link_preview_options"] == {          # token picture above the card
        "url": "https://www.agoradex.app/api/img?src=x", "prefer_large_media": True,
        "show_above_text": True}

    def broken(row):
        raise RuntimeError("market API changed shape")
    n.renderer = broken
    store.enqueue_now("b", "fallback text", payload=payload())
    assert n.drain() == 1
    assert sent[1]["text"] == "fallback text" and "reply_markup" not in sent[1]
    assert sent[1]["link_preview_options"] == {"is_disabled": True}


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
    monkeypatch.setattr(cli.Notifier, "_send", lambda self, t, kb=None, img=None: (sent.append((t, kb)) or (True, "", False)))
    assert cli.test_card(cfg) == 0
    assert "Test card" in sent[0][0] and sent[0][1]["inline_keyboard"][1][0]["text"].startswith("🛒 Agora")


def test_market_cap_falls_back_to_hype_without_a_usd_rate():
    from agora_tracker.market import TokenInfo
    info = TokenInfo(address=TOKEN, mcap_hype=9970.0)
    assert "<b>MC</b>: 9.97K HYPE" in cards.link_bar(L, TOKEN, "BLOB", info, None)
    assert "<b>MC</b>: $857.42K" in cards.link_bar(L, TOKEN, "BLOB", info, 86.0)


def test_wallet_trade_buttons_use_the_wallets_universal_links():
    from urllib.parse import parse_qs, unquote, urlsplit

    from agora_tracker.config import LinksConfig
    rabby, okx = Links(LinksConfig()).wallet_buttons(TOKEN, "BLOB")
    page = f"https://www.agoradex.app/token/{TOKEN}"
    u = urlsplit(rabby["url"])               # Rabby: /mobile/* is claimed by the app (AASA)
    assert (u.scheme, u.netloc, u.path) == ("https", "go.rabby.io", "/mobile/")
    assert parse_qs(u.query) == {"_cmd": ["open-dapp"], "dapp": [page]}
    o = urlsplit(okx["url"])                 # OKX: documented download?deeplink= wrapper
    deep = parse_qs(o.query)["deeplink"][0]
    assert o.netloc == "web3.okx.com" and deep.startswith("okx://wallet/dapp/url?dappUrl=")
    assert unquote(deep.split("dappUrl=", 1)[1]) == page
    boxa = Links(LinksConfig(trade_site="boxa", wallet_buttons=["rabby"])).wallet_buttons(TOKEN, "B")
    assert len(boxa) == 1 and "boxa.club" in unquote(boxa[0]["url"])
    assert Links(LinksConfig(wallet_buttons=[])).wallet_buttons(TOKEN, "B") == []
