"""CLI commands that don't need a database."""

from __future__ import annotations

from agora_tracker import __main__ as cli
from agora_tracker.config import AlertConfig, ChainConfig, Config, LeaderboardConfig, TelegramConfig


def _cfg(token, chat) -> Config:
    return Config(chain=ChainConfig(rpc_urls=["http://x"]), leaderboard=LeaderboardConfig(),
                  alerts=AlertConfig(), telegram=TelegramConfig(bot_token=token, chat_id=chat))


def test_test_telegram_reports_success_and_failure(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(cli.Notifier, "_send",
                        lambda self, text: (sent.append(text) or (True, "", False)))
    assert cli.test_telegram(_cfg("123:abc", "-1")) == 0
    assert len(sent) == 1 and "telegram ok" in capsys.readouterr().out

    monkeypatch.setattr(cli.Notifier, "_send", lambda self, text: (False, "HTTP 401", True))
    assert cli.test_telegram(_cfg("123:abc", "-1")) == 1
    out = capsys.readouterr().out
    assert "FAILED: HTTP 401" in out and "123:abc" not in out


def test_test_telegram_without_credentials():
    assert cli.test_telegram(_cfg(None, None)) == 1


def test_leaderboard_values_keep_their_unit(tmp_path):
    from agora_tracker.config import load_config
    from agora_tracker.engine import fmt_value
    assert fmt_value(63160.5, "USD") == "$63.16K"
    assert fmt_value(720.4, "HYPE") == "720.40 HYPE"
    cfg = tmp_path / "c.yaml"
    cfg.write_text("chain: {rpc_urls: [x]}\nalerts: {pnl_change_usd: 42}\n")
    assert load_config(cfg).alerts.pnl_change == 42      # pre-0.3 key still honoured
