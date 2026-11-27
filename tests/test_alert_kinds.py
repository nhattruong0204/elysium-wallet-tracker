"""Telegram alert cards for buys and sells only (owner, 2026-11-27); every kind stays indexed."""
from __future__ import annotations

import dataclasses

import pytest
from test_tracker import TOKEN, W1, W2, W3, build, chain, get_pos, run_all  # noqa: F401

from agora_tracker.config import ALERT_KINDS, AlertConfig, load_config


def test_default_is_buys_and_sells():
    assert AlertConfig().alert_kinds == ["BUY", "SELL"]
    assert set(AlertConfig().alert_kinds) <= set(ALERT_KINDS)


def test_transfers_are_indexed_but_not_alerted_by_default(store, chain):  # noqa: F811
    store, follower, engine = build(store, chain)
    engine.cfg = dataclasses.replace(engine.cfg, alert_kinds=["BUY", "SELL"])
    events = run_all(follower, engine, {W1, W2, W3})
    assert (W1, "TRANSFER_OUT") in [(e.wallet, e.kind) for e in events]       # still indexed
    assert get_pos(store, W1, TOKEN).qty == 400 * 10**18     # the transfer still left the bag
    texts = [r["text"] for r in store.pending_alerts(100)]
    assert any("<b>BUY</b>" in t for t in texts) and any("SELL" in t for t in texts)
    assert not any("OUT (sell?)" in t or "SENT" in t or "RECEIVED" in t for t in texts)
    assert any("CLUSTER BUY" in t for t in texts)                 # buy-driven cards stay


def _cfg(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_config_and_environment(tmp_path, monkeypatch):
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text()
    monkeypatch.delenv("ALERTS_KINDS", raising=False)
    assert load_config(_cfg(tmp_path, src)).alerts.alert_kinds == ["BUY", "SELL"]
    monkeypatch.setenv("ALERTS_KINDS", "buy, sell transfer_out")
    assert load_config(_cfg(tmp_path, src)).alerts.alert_kinds == ["BUY", "SELL", "TRANSFER_OUT"]
    monkeypatch.setenv("ALERTS_KINDS", "BUY,RECEIVE")
    with pytest.raises(ValueError, match="RECEIVE"):
        load_config(_cfg(tmp_path, src))
