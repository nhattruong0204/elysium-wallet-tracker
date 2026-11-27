# ruff: noqa: F811
"""Log-only Consensus rules from the 30-day replay (2026-11-27): repeat consensus and falling price."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from test_consensus_delivery import DEFAULT, _card, setting_db  # noqa: F401

from agora_tracker import consensus_delivery as cd
from agora_tracker.config import LinksConfig
from agora_tracker.web.consensus import ConsensusParams, count_prior_events
from agora_tracker.web.format import Links as WebLinks

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
E = 10**18
A = lambda n: "0x" + f"{n:02x}" * 20   # noqa: E731
TOK = "0x" + "ab" * 20


def burst(start, wallets, tok=TOK):
    """three wallets buying 2 HYPE each a minute apart: one consensus event"""
    return [{"token": tok, "wallet": A(w), "kind": "BUY", "hype_wei": 2 * E,
             "block_time": start + timedelta(minutes=i), "block_number": 1000 + i,
             "tx_hash": f"0x{w:02x}{int(start.timestamp())}"} for i, w in enumerate(wallets)]


def test_prior_events_counts_earlier_consensus_on_the_token_within_the_span():
    rows = (burst(T0 - timedelta(days=9), [1, 2, 3])          # outside 7 days
            + burst(T0 - timedelta(days=2), [4, 5, 6])
            + burst(T0 - timedelta(hours=3), [7, 8, 9])
            + burst(T0, [10, 11, 12]))
    rows.sort(key=lambda r: r["block_time"])
    p = ConsensusParams(min_traders=3, window_min=30, min_net_hype=0.0)
    cards = [{"token": TOK, "trigger_at": T0 + timedelta(minutes=2)},
             {"token": TOK, "trigger_at": T0 - timedelta(hours=3) + timedelta(minutes=2)},
             {"token": "0x" + "cd" * 20, "trigger_at": T0}]
    count_prior_events(cards, rows, p, set(), days=7)
    assert [k["prior_events"] for k in cards] == [2, 1, 0]


def test_extra_reasons():
    assert cd.extra_reasons({}) == []
    assert cd.extra_reasons({"prior_events": 1, "change_1h": 0.4}) == []
    assert cd.extra_reasons({"prior_events": 2})[0].startswith("consensus no. 3")
    assert "-12%" in cd.extra_reasons({"change_1h": -0.12})[0]
    assert len(cd.extra_reasons({"prior_events": 4, "change_1h": -0.5})) == 2
    assert cd.extra_reasons({"change_1h": None}) == []          # no snapshot: no opinion


ROUND = iter(range(1, 100))


def _queued(setting_db, cards, now):
    """queue a fresh copy of the cards (new trigger txs, so earlier rounds do not dedupe them)"""
    r = next(ROUND)
    fresh = [{**k, "trigger_tx": f"0x{r:02x}{i:02x}" + "f" * 60} for i, k in enumerate(cards)]
    return setting_db.queue(setting_db.read(), fresh, now, WebLinks(LinksConfig()))


def test_log_mode_posts_and_logs_enforce_mode_skips_off_mode_ignores(setting_db, monkeypatch,
                                                                    caplog):
    setting_db.save(DEFAULT)
    now = datetime.now(timezone.utc)
    falling = {**_card(now), "change_1h": -0.3, "prior_events": 2}
    clean = {**_card(now), "trigger_tx": "0x" + "e" * 64, "change_1h": 0.5, "prior_events": 0}
    caplog.set_level(logging.INFO, logger="agora_tracker.consensus_delivery")
    monkeypatch.setattr(cd, "EXTRA_RULES", "log")
    assert _queued(setting_db, [falling, clean], now) == 2
    assert "shadow rule would skip" in caplog.text and "consensus no. 3" in caplog.text
    caplog.clear()
    monkeypatch.setattr(cd, "EXTRA_RULES", "enforce")
    assert _queued(setting_db, [falling, clean], now) == 1
    assert "not posting" in caplog.text and "-30%" in caplog.text
    caplog.clear()
    monkeypatch.setattr(cd, "EXTRA_RULES", "off")
    assert _queued(setting_db, [falling, clean], now) == 2
    assert "shadow rule" not in caplog.text


def test_gate_skips_are_not_reported_as_shadow(setting_db, monkeypatch, caplog):
    setting_db.save(DEFAULT)
    now = datetime.now(timezone.utc)
    tiny = {**_card(now), "mcap_at_usd": 3000, "change_1h": -0.3}
    caplog.set_level(logging.INFO, logger="agora_tracker.consensus_delivery")
    monkeypatch.setattr(cd, "EXTRA_RULES", "log")
    assert _queued(setting_db, [tiny], now) == 0
    assert "below $10,000" in caplog.text and "shadow rule" not in caplog.text
