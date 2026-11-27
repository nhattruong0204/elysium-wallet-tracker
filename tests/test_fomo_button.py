"""Fomo iOS token-share handoff from Elysium alert cards.

The known public Fomo coin-share shape is /coin?address=...&chainId=...
The Elysium iOS universal-link behavior still needs a physical Telegram/Fomo smoke test.
Nothing here executes an order or accesses a wallet.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from test_cards import FIX, TOKEN, TX, WALLET, payload

from agora_tracker import cards
from agora_tracker.cards import FOMO_LABEL, MAESTRO_LABEL, Links
from agora_tracker.config import LinksConfig
from agora_tracker.market import parse_token

URL = f"https://fomo.family/coin?address={TOKEN}&chainId=99901"


def _buttons(kb: dict, label: str) -> list[dict]:
    return [button for row in kb["inline_keyboard"] for button in row
            if button["text"] == label]


def test_fomo_link_is_exactly_an_elysium_share_url():
    links = Links(LinksConfig())
    assert links.fomo_token(TOKEN) == URL
    assert links.fomo_token("0x" + TOKEN[2:].upper()) == URL
    assert links.fomo_token(f"  {TOKEN}  ") == URL
    assert links.fomo_row(TOKEN) == [{"text": FOMO_LABEL, "url": URL}]
    parsed = urlsplit(URL)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "fomo.family", "/coin")
    assert parse_qs(parsed.query) == {"address": [TOKEN], "chainId": ["99901"]}
    assert parsed.fragment == ""


@pytest.mark.parametrize("invalid", [
    None, "", "USDC", TOKEN[:-1], TOKEN + "0", TOKEN[2:],
    "0x" + "0" * 40, "0x" + "g" * 40,
    "https://evil.example/" + TOKEN, TOKEN + "&r=attacker", TOKEN + "#malicious",
    TOKEN + "/../../other", 123, [TOKEN],
])
def test_fomo_rejects_everything_except_one_contract(invalid):
    links = Links(LinksConfig())
    assert links.fomo_token(invalid) is None
    assert links.fomo_row(invalid) == []


def test_wrong_chain_does_not_create_fomo_button():
    assert Links(LinksConfig(), 99901).fomo_token(TOKEN) == URL
    assert Links(LinksConfig(), 1).fomo_token(TOKEN) is None
    assert Links(LinksConfig(), 99801).fomo_token(TOKEN) is None


def test_fomo_toggle_does_not_disable_maestro(monkeypatch):
    monkeypatch.setenv("FOMO_BUTTON", "off")
    links = Links(LinksConfig())
    assert links.fomo_token(TOKEN) is None
    kb = cards.render(payload(), links)[1]
    assert _buttons(kb, FOMO_LABEL) == []
    assert len(_buttons(kb, MAESTRO_LABEL)) == 1

    monkeypatch.setenv("FOMO_BUTTON", "on")
    monkeypatch.setenv("MAESTRO_BUTTON", "off")
    links = Links(LinksConfig())
    kb = cards.render(payload(), links)[1]
    assert _buttons(kb, MAESTRO_LABEL) == []
    assert _buttons(kb, FOMO_LABEL) == [{"text": FOMO_LABEL, "url": URL}]


@pytest.mark.parametrize("kind", [
    "BUY", "SELL", "SWAP", "TRANSFER_IN", "TRANSFER_OUT", "MULTI",
])
def test_fomo_and_maestro_stay_first_for_eligible_trade_alerts(kind):
    links = Links(LinksConfig())
    for info in (parse_token(TOKEN, FIX), None):
        kb = cards.render(payload(kind=kind), links, info)[1]
        assert kb["inline_keyboard"][:2] == [
            [{"text": MAESTRO_LABEL, "url": f"https://t.me/Maestro?start={TOKEN}"}],
            [{"text": FOMO_LABEL, "url": URL}],
        ]
        assert len(_buttons(kb, FOMO_LABEL)) == 1
        assert any("Agora" in b["text"] for row in kb["inline_keyboard"]
                   for b in row)


def test_fomo_on_clusters_new_pool_and_unknown_venues():
    links = Links(LinksConfig())
    buyers = [{"wallet": WALLET, "rank": 1, "pnl": 10.0}]
    cluster = {"type": "cluster", "token": TOKEN, "symbol": "BLOB",
               "min_hype": 0.5, "window_min": 60, "buyers": buyers}
    pool = {"type": "launch", "kind": "new_pool", "token": TOKEN,
            "symbol": "BLOB", "pool": "0x" + "9a" * 20,
            "liquidity_hype": 12.5, "age_s": 300}
    for p in (cluster, {**cluster, "strong": True, "independent": 3}, pool):
        kb = cards.render(p, links)[1]
        assert kb["inline_keyboard"][1] == [{"text": FOMO_LABEL, "url": URL}]
    unknown = cards.keyboard(links, TOKEN, "BLOB", None, TX, force_unknown=True)
    assert unknown["inline_keyboard"][1][0]["url"] == URL


def test_fomo_link_uses_contract_not_symbol_and_does_not_change_tracking():
    links = Links(LinksConfig())
    fake = "0x" + "5c" * 20
    for token in (TOKEN, fake):
        kb = cards.render(payload(token=token, symbol="USDC"), links)[1]
        assert _buttons(kb, FOMO_LABEL)[0]["url"] == (
            f"https://fomo.family/coin?address={token}&chainId=99901")
    kb = cards.render(payload(symbol="0x" + "e" * 40), links)[1]
    assert _buttons(kb, FOMO_LABEL)[0]["url"] == URL


def test_no_fomo_button_without_a_single_valid_token():
    links = Links(LinksConfig())
    pad = {"type": "launch", "kind": "new_launchpad", "launchpad": "any",
           "tokens": [{"token": TOKEN, "symbol": "BLOB"}]}
    kb = cards.render(pad, links)[1]
    assert _buttons(kb, FOMO_LABEL) == []
    kb = cards.keyboard(links, "bad-token", "BLOB", None, TX)
    assert _buttons(kb, FOMO_LABEL) == []


# --- only the Fomo button under alert cards (owner, 2026-11-27) -------------------------------
def _sent(monkeypatch, mode, p):
    if mode is None:
        monkeypatch.delenv("ALERT_BUTTONS", raising=False)
    else:
        monkeypatch.setenv("ALERT_BUTTONS", mode)
    links = Links(LinksConfig())
    full = cards.render(p, links)[1]
    return cards.alert_keyboard(links, p, full), full


@pytest.mark.parametrize("kind", ["BUY", "SELL"])
def test_trade_cards_carry_only_the_fomo_button_by_default(monkeypatch, kind):
    sent, full = _sent(monkeypatch, None, payload(kind=kind))
    assert sent == {"inline_keyboard": [[{"text": FOMO_LABEL, "url": URL}]]}
    assert len([b for row in full["inline_keyboard"] for b in row]) > 1   # the full set exists


def test_cluster_and_burst_cards_too(monkeypatch):
    for p in ({"type": "cluster", "token": TOKEN, "symbol": "KLEOS", "wallets": [WALLET],
               "window_s": 600, "total_hype": 3.0},
              {"type": "burst", "token": TOKEN, "symbol": "KLEOS", "wallet": WALLET,
               "stage": "start", "count": 4, "recipients": 4, "first_ts": 1, "last_ts": 2,
               "window_s": 120.0, "sample": []}):
        monkeypatch.delenv("ALERT_BUTTONS", raising=False)
        links = Links(LinksConfig())
        assert cards.alert_keyboard(links, p, None) == {
            "inline_keyboard": [[{"text": FOMO_LABEL, "url": URL}]]}, p["type"]


def test_no_token_or_fomo_off_means_no_buttons(monkeypatch):
    monkeypatch.delenv("ALERT_BUTTONS", raising=False)
    links = Links(LinksConfig())
    assert cards.alert_keyboard(links, {"type": "launch", "kind": "new_launchpad"},
                                {"inline_keyboard": [[{"text": "x", "url": "https://a"}]]}) is None
    monkeypatch.setenv("FOMO_BUTTON", "off")
    assert cards.alert_keyboard(Links(LinksConfig()), payload(kind="BUY"), None) is None


def test_all_mode_restores_the_full_keyboard(monkeypatch):
    sent, full = _sent(monkeypatch, "all", payload(kind="BUY"))
    assert sent is full and _buttons(full, MAESTRO_LABEL) and _buttons(full, FOMO_LABEL)
    sent, _ = _sent(monkeypatch, "bogus", payload(kind="BUY"))      # unknown value: default
    assert sent == {"inline_keyboard": [[{"text": FOMO_LABEL, "url": URL}]]}
