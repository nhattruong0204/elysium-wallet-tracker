"""Maestro quick-trade button on token alerts: a LINK to Maestro's token page, nothing more.

The tracker never trades: the button opens https://t.me/Maestro?start=<address>, built
only from an exact contract address on chain 99901, with a fixed host and chain slug."""

from __future__ import annotations

import inspect

import pytest
from test_cards import FIX, TOKEN, TX, WALLET, FakeResp, payload

from agora_tracker import cards
from agora_tracker.cards import MAESTRO_LABEL, Links
from agora_tracker.config import LinksConfig, TelegramConfig
from agora_tracker.market import parse_token
from agora_tracker.notify import Notifier

L = Links(LinksConfig())
URL = f"https://t.me/Maestro?start={TOKEN}"
USDC = "0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65"


def quick(kb: dict | None) -> list[dict]:
    return [b for row in (kb or {}).get("inline_keyboard", []) for b in row
            if b["text"] == MAESTRO_LABEL]


def test_url_is_the_fixed_host_elysium_slug_and_lowercase_address():
    assert TOKEN == TOKEN.lower()
    assert L.maestro_token(TOKEN) == URL
    mixed = "0x" + TOKEN[2:].upper()
    assert L.maestro_token(mixed) == URL and L.maestro_token(f"  {TOKEN} ") == URL
    assert L.maestro_row(TOKEN) == [{"text": "⚡ Quick Buy / Sell · Maestro", "url": URL}]
    # nothing but the address ever reaches the URL: no query, fragment, referral or user data
    assert URL.removeprefix("https://t.me/Maestro?start=") == TOKEN
    assert not any(c in L.maestro_token(TOKEN) for c in "#&@ ")


@pytest.mark.parametrize("bad", [
    None, "", "USDC", "BLOB", TOKEN[:-1], TOKEN + "0", TOKEN[2:], "0x" + "g" * 40,
    "0x" + "0" * 40, TOKEN + "?ref=x", TOKEN + "/../../solana", f"{TOKEN}#x",
    "https://evil.example/" + TOKEN, 123, b"0x" + b"a" * 40, ["0x" + "a" * 40],
])
def test_anything_that_is_not_one_exact_contract_address_gets_no_link(bad):
    assert L.maestro_token(bad) is None and L.maestro_row(bad) == []


def test_only_elysium_mainnet_and_an_operator_off_switch(monkeypatch):
    assert Links(LinksConfig(), 99801).maestro_token(TOKEN) is None      # testnet
    assert Links(LinksConfig(), 1).maestro_token(TOKEN) is None          # another chain
    assert Links(LinksConfig(), 99901).maestro_token(TOKEN) == URL
    for off in ("off", "0", "false", "OFF"):
        monkeypatch.setenv("MAESTRO_BUTTON", off)
        assert Links(LinksConfig()).maestro_token(TOKEN) is None
        assert quick(cards.render(payload(), Links(LinksConfig()))[1]) == []
    monkeypatch.setenv("MAESTRO_BUTTON", "on")
    assert Links(LinksConfig()).maestro_token(TOKEN) == URL
    # the destination is not configurable: no LinksConfig field can redirect it
    assert not [f for f in vars(LinksConfig()) if "maestro" in f.lower()]
    assert "self.c." not in inspect.getsource(Links.maestro_token)


def test_deeplink_off_opens_the_existing_maestro_chat_without_a_token_payload(monkeypatch):
    monkeypatch.setenv("MAESTRO_DEEPLINK", "off")
    l = Links(LinksConfig())
    assert l.maestro_token(TOKEN) == "https://t.me/Maestro"
    assert l.maestro_row(TOKEN) == [{"text": MAESTRO_LABEL, "url": "https://t.me/Maestro"}]
    assert l.maestro_token("USDC") is None


@pytest.mark.parametrize("kind", ["BUY", "SELL", "SWAP", "TRANSFER_IN", "TRANSFER_OUT", "MULTI"])
def test_every_trade_alert_has_the_button_first_and_keeps_the_others(kind):
    for info in (parse_token(TOKEN, FIX), None):
        text, kb = cards.render(payload(kind=kind), L, info, 85.9)
        rows = kb["inline_keyboard"]
        assert rows[0] == [{"text": MAESTRO_LABEL, "url": URL}] and len(quick(kb)) == 1
        labels = [b["text"] for r in rows[1:] for b in r]
        assert any(x.startswith("🛒 Agora") for x in labels) and any("BOXA" in x for x in labels)
        assert any("Rabby" in x for x in labels) and any("Kleos" in x for x in labels)


def test_the_symbol_never_decides_the_link():
    fake = "0x" + "5c" * 20                       # another contract calling itself USDC
    for token in (fake, USDC):
        _, kb = cards.render(payload(token=token, symbol="USDC"), L)
        assert [b["url"] for b in quick(kb)] == [f"https://t.me/Maestro?start={token}"]
    _, kb = cards.render(payload(symbol="0x" + "ee" * 20), L)      # a symbol shaped like an address
    assert [b["url"] for b in quick(kb)] == [URL]


def test_cluster_strong_signal_and_new_pool_alerts_have_it():
    buyers = [{"wallet": WALLET, "rank": 1, "pnl": 10.0, "pnl_unit": "HYPE"}]
    plain = {"type": "cluster", "token": TOKEN, "symbol": "BLOB", "min_hype": 0.5,
             "window_min": 60, "buyers": buyers}
    for p in (plain, {**plain, "strong": True, "independent": 3}, {**plain, "tx": TX}):
        out = cards.render(p, L, parse_token(TOKEN, FIX), 85.9)
        assert out and out[1]["inline_keyboard"][0] == [{"text": MAESTRO_LABEL, "url": URL}]
    pool = {"type": "launch", "kind": "new_pool", "token": TOKEN, "symbol": "BLOB",
            "pool": "0x" + "9a" * 20, "liquidity_hype": 12.5, "age_s": 300}
    out = cards.render(pool, L)
    assert out and out[1]["inline_keyboard"][0] == [{"text": MAESTRO_LABEL, "url": URL}]
    # a token on an unrecognised venue keeps its explorer buttons and still gets the link
    kb = cards.keyboard(L, TOKEN, "BLOB", None, TX, force_unknown=True)
    assert kb["inline_keyboard"][0][0]["url"] == URL
    assert any(b["text"] == "🔍 Token" for r in kb["inline_keyboard"] for b in r)


def test_alerts_without_one_valid_token_have_no_button():
    pad = {"type": "launch", "kind": "new_launchpad", "launchpad": "somepad",
           "tokens": [{"token": TOKEN, "symbol": "BLOB"}]}
    out = cards.render(pad, L)                    # several tokens on an unknown venue
    assert out and quick(out[1]) == []
    assert cards.render({"type": "ops", "text": "RPC degraded"}, L) is None     # operational
    assert cards.render(None, L) is None
    kb = cards.keyboard(L, "not-an-address", "BLOB", None, TX)
    assert quick(kb) == [] and kb["inline_keyboard"]             # other buttons still there


def test_keyboard_stays_inside_telegram_limits():
    for p in (payload(), payload(kind="SELL"), payload(symbol="X" * 60)):
        for info in (parse_token(TOKEN, FIX), None):
            kb = cards.render(p, L, info, 85.9)[1]
            rows = kb["inline_keyboard"]
            assert len(rows) <= 6 and all(1 <= len(r) <= 3 for r in rows)
            for b in (b for r in rows for b in r):
                assert set(b) == {"text", "url"} and len(b["text"]) <= 40
                assert b["url"].startswith("https://") and len(b["url"]) <= 512


def test_delivery_and_retry_are_unchanged_and_the_button_is_sent(store):
    sent, fail = [], [True]

    def post(url, json, timeout):
        sent.append(json)
        return FakeResp(500 if fail[0] else 200)
    n = Notifier(store, TelegramConfig(bot_token="1:x", chat_id="1", min_interval_s=0),
                 renderer=lambda row: (*cards.render(row["payload"], L, None, None), None))
    n.session.post = post
    store.enqueue_now("k1", "fallback", payload=payload(kind="SELL"))
    assert n.drain() == 0 and store.outbox_counts().get("pending") == 1     # failed: stays queued
    fail[0] = False
    with store.tx() as c:
        c.exec_driver_sql("UPDATE notification_outbox SET next_attempt_at = now() - interval '1 s'")
    assert n.drain() == 1 and store.outbox_counts() == {"sent": 1}          # retried, sent once
    assert sent[-1]["reply_markup"]["inline_keyboard"][0] == [{"text": MAESTRO_LABEL, "url": URL}]
    store.enqueue_now("k1", "fallback", payload=payload(kind="SELL"))        # same key: no duplicate
    assert n.drain() == 0 and store.outbox_counts() == {"sent": 1}


def test_muted_tokens_still_get_no_telegram_alert(store):
    """PR #118 is untouched: a Telegram-muted token is closed unsent, button or not."""
    sent = []
    n = Notifier(store, TelegramConfig(bot_token="1:x", chat_id="1", min_interval_s=0),
                 renderer=lambda row: (*cards.render(row["payload"], L, None, None), None),
                 suppress_tokens=[USDC])
    n.session.post = lambda url, json, timeout: (sent.append(json), FakeResp(200))[1]
    store.enqueue_now("u1", "usdc", payload=payload(token=USDC, symbol="USDC"))
    store.enqueue_now("b1", "blob", payload=payload())
    assert n.drain() == 1 and len(sent) == 1
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["url"] == URL
    assert store.outbox_counts() == {"sent": 1, "suppressed": 1}
