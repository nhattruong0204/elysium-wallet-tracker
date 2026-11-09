"""/trading_status (#95/#96 operations): read-only, owner-only, private chat; states explicitly
whether wallet requests are possible. No database needed (a fake ledger)."""

from __future__ import annotations

import html
from types import SimpleNamespace

from agora_tracker.config import TradingConfig
from agora_tracker.tgui import IntelBot, render_trading_status
from agora_tracker.trading.ledger.core import Controls


class Ledger:
    owner_ids = frozenset({7})

    def __init__(self, **kw):
        self.c = Controls(**{"paused": False, "pause_reason": None, "paused_by": None,
                             "execution_enabled": False, "policy_version": 1, "version": 1, **kw})
        self.calls = []

    def controls(self):
        self.calls.append("controls")
        return self.c


def cfg(**t):
    return SimpleNamespace(trading=TradingConfig(**t))


def test_default_state_says_no_wallet_requests_are_possible():
    text = render_trading_status(cfg(), None, None, html.escape)
    assert "Wallet requests possible: NO" in text and "ledger off" in text
    assert "finalized tag" in text


def test_every_switch_must_be_on_and_the_ledger_unpaused_for_yes():
    on = dict(ledger_enabled=True, wallet_execution_enabled=True, ingress_enabled=True,
              paper_enabled=True, paper_order_path="/tmp/p/x.sqlite",
              ingress_origin="https://app.example.test", miniapp_url="https://app.example.test/",
              ingress_auth_path="/tmp/a/x.sqlite")
    assert "possible: NO" in render_trading_status(cfg(**on), Ledger(), None, html.escape)
    assert "possible: YES" in render_trading_status(cfg(**on), Ledger(execution_enabled=True),
                                                    None, html.escape)
    paused = Ledger(execution_enabled=True, paused=True, pause_reason="<b>incident</b>")
    text = render_trading_status(cfg(**on), paused, None, html.escape)
    assert "possible: NO" in text and "&lt;b&gt;incident" in text


def test_depth_only_finality_is_labelled_as_a_downgrade():
    text = render_trading_status(cfg(finality_require_finalized_tag=False), None, None, html.escape)
    assert "REVIEWED DOWNGRADE" in text


def test_the_command_is_owner_private_only_and_reads_nothing_for_others():
    sent = []
    led = Ledger()
    bot = SimpleNamespace(cfg=cfg(), ledger=led, _send=lambda chat, text, kb: sent.append(text))
    priv = {"chat": {"type": "private"}}
    IntelBot._trading_status(bot, 7, 7, priv)
    assert "Trading status" in sent[-1] and led.calls == ["controls"]
    IntelBot._trading_status(bot, 8, 8, priv)                       # not an owner
    IntelBot._trading_status(bot, -100, 7, {"chat": {"type": "group"}})
    assert sent[1:] == ["Not available.", "Not available."] and led.calls == ["controls"]
