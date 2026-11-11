"""Isolated Consensus group sender, durable web settings and public/privileged boundary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from conftest import TEST_URL
from sqlalchemy import exc as sa_exc
from test_dashboard_db import TOK_PUB
from test_web import make_live  # noqa: F401

from agora_tracker.config import LinksConfig
from agora_tracker.consensus_delivery import (
    ConsensusSender,
    ConsensusSettingsStore,
    make_settings_engine,
    render_card,
    validate_settings,
)
from agora_tracker.migrate import set_consensus_password
from agora_tracker.web.format import Links

NOW = datetime.now(timezone.utc)
DB_PASSWORD = "consensus-test-password-abcdefgh12345"
ADMIN_KEY = "super-long-owner-consensus-test-key-1234567890123456789"

DEFAULT = {"version": 0, "enabled": True, "traders": 2, "window": 30,
           "min_net": 1.0, "range": "24h", "launchpad": "", "independent": True}
TOKEN = TOK_PUB


@pytest.mark.parametrize("change", [
    {"traders": 100},
    {"window": 1},
    {"min_net": -1},
    {"range": "99d"},
    {"launchpad": "';DROP"},
    {"launchpad": "Kleos"},
    {"enabled": "yes"},
    {"independent": "yes"},
    {"version": -1},
    {"version": True},
    {"random": "injected"},
])
def test_reject_invalid_saved_thresholds(change):
    with pytest.raises(ValueError):
        validate_settings({**DEFAULT, **change})


def test_valid_shared_thresholds():
    assert validate_settings(DEFAULT)["window"] == 30
    assert validate_settings({**DEFAULT, "enabled": False})["enabled"] is False


def test_sender_requires_other_bot_and_other_group(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "existing-bot-token")
    monkeypatch.setenv("TG_CHAT_ID", "-1001234567890")
    with pytest.raises(ValueError):
        ConsensusSender("existing-bot-token", "-1004567890123")
    with pytest.raises(ValueError):
        ConsensusSender("different-token", "-1001234567890")
    with pytest.raises(ValueError):
        ConsensusSender("different-token", "1234567")
    assert ConsensusSender("different-token", "-1004567890123").group == "-1004567890123"


def test_render_html_escape_and_exact_elysium_token_links():
    c = {"token": TOKEN, "symbol": "<img src=evil>", "traders": 3,
         "independent": 2, "bundled": True, "first_n": 3, "net_hype": 4.5,
         "net_usd": 322.0, "mcap_at_usd": 14800.0,
         "mcap_now": {"market_cap_usd": 3550}, "members": [],
         "trigger_tx": "0x" + "1" * 64}
    text, buttons = render_card(c, Links(LinksConfig()))
    assert "&lt;img src=evil&gt;" in text
    assert "<img" not in text
    assert "4.50 HYPE" in text and "14.80K" in text
    assert any("fomo.family/coin?" in b["url"] for row in buttons["inline_keyboard"]
               for b in row)
    assert any("t.me/Maestro?start=" in b["url"] for row in buttons["inline_keyboard"]
               for b in row)
    with pytest.raises(ValueError):
        render_card({**c, "token": "USDC"}, Links(LinksConfig()))


@pytest.fixture
def setting_db(pg_engine):
    """Disposable role and isolated consensus schema, never the production database."""
    if not TEST_URL:
        pytest.skip("TEST_DATABASE_URL required")
    set_consensus_password(pg_engine, DB_PASSWORD)
    with pg_engine.begin() as c:
        c.execute(sa.text("TRUNCATE consensus_alerts.settings_audit, "
                          "consensus_alerts.deliveries RESTART IDENTITY CASCADE"))
        c.execute(sa.text("UPDATE consensus_alerts.settings SET enabled=false, "
                          "min_traders=3, window_min=30, min_net_hype=1.0, "
                          "period='24h', launchpad='', independent=false, version=0, "
                          "updated_at=now() WHERE id=1"))
    url = str(sa.engine.make_url(TEST_URL).set(
        username="consensus_rw", password=DB_PASSWORD))
    eng = make_settings_engine(url)
    yield ConsensusSettingsStore(eng)
    eng.dispose()


def test_narrow_role_cannot_touch_tracker_or_public_views(setting_db):
    with setting_db.engine.connect() as c:
        assert c.execute(sa.text(
            "SELECT has_table_privilege(current_user, "
            "'consensus_alerts.settings', 'UPDATE')")).scalar()
        assert not c.execute(sa.text(
            "SELECT has_table_privilege(current_user, "
            "'public.notification_outbox', 'SELECT')")).scalar()
        assert not c.execute(sa.text(
            "SELECT has_table_privilege(current_user, "
            "'public.trades', 'INSERT')")).scalar()
        with pytest.raises(sa_exc.DBAPIError):
            c.execute(sa.text("SELECT * FROM public.notification_outbox"))
    # Transaction is rolled back by connection context manager.


def test_owner_settings_apply_and_replay_protection(setting_db):
    initial = setting_db.read()
    assert not initial["enabled"] and initial["version"] == 0
    new = setting_db.save(DEFAULT)
    assert new["enabled"] and new["version"] == 1 and new["independent"]
    assert setting_db.read()["traders"] == 2
    with pytest.raises(RuntimeError):
        setting_db.save(DEFAULT)
    last = setting_db.save({**DEFAULT, "version": 1, "enabled": False})
    assert not last["enabled"] and last["version"] == 2
    with setting_db.engine.connect() as c:
        assert c.execute(sa.text(
            "SELECT count(*) FROM consensus_alerts.settings_audit")).scalar() == 2


def _card(at):
    return {"token": TOKEN, "symbol": "PUB", "traders": 2,
            "independent": 2, "bundled": False, "first_n": 2,
            "trigger_at": at, "trigger_tx": "0x" + "f" * 64,
            "net_hype": 3.5, "net_usd": 250.0, "members": [],
            "mcap_at_usd": 15000, "mcap_now": {"market_cap_usd": 17500}}


class StubSender:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = 0

    def send(self, body, buttons):
        self.sent += 1
        assert "CONSENSUS" in body
        assert buttons["inline_keyboard"]
        return ((True, False, "", 0) if self.ok
                else (False, False, "rate_limited", 2))


def test_exactly_one_queued_on_repoll_and_no_pre_enable_blast(setting_db):
    setting_db.save(DEFAULT)
    saved = setting_db.read()
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=1)
    assert setting_db.queue(saved, [_card(old)], now, Links(LinksConfig())) == 0
    assert setting_db.queue(saved, [_card(now)], now, Links(LinksConfig())) == 1
    assert setting_db.queue(saved, [_card(now)], now, Links(LinksConfig())) == 0
    stub = StubSender()
    assert setting_db.send_due(saved, stub) == 1
    assert stub.sent == 1
    assert setting_db.send_due(saved, stub) == 0
    with setting_db.engine.connect() as c:
        assert c.execute(sa.text(
            "SELECT status FROM consensus_alerts.deliveries")).scalar() == "sent"


def test_disabling_alerts_cancels_pending_and_cannot_send(setting_db):
    saved = setting_db.save(DEFAULT)
    now = datetime.now(timezone.utc)
    assert setting_db.queue(saved, [_card(now)], now, Links(LinksConfig())) == 1
    disabled = setting_db.save({**DEFAULT, "version": 1, "enabled": False})
    stub = StubSender()
    assert setting_db.send_due(disabled, stub) == 0
    assert stub.sent == 0
    with setting_db.engine.connect() as c:
        assert c.execute(sa.text(
            "SELECT status FROM consensus_alerts.deliveries")).scalar() == "cancelled"


def test_delivery_failure_uses_backoff_without_losing_queued_message(setting_db):
    setting_db.save(DEFAULT)
    saved = setting_db.read()
    now = datetime.now(timezone.utc)
    setting_db.queue(saved, [_card(now)], now, Links(LinksConfig()))
    stub = StubSender(ok=False)
    assert setting_db.send_due(saved, stub) == 0
    with setting_db.engine.connect() as c:
        r = c.execute(sa.text(
            "SELECT status, attempts, next_retry_at FROM consensus_alerts.deliveries"
        )).one()
        assert r.status == "pending" and r.attempts == 1
        assert r.next_retry_at > now


def test_web_consensus_settings_endpoint_rejects_anonymous_and_cross_origin(
        setting_db, make_live, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CONSENSUS_DB_PASSWORD", DB_PASSWORD)
    monkeypatch.setenv("CONSENSUS_ADMIN_KEY", ADMIN_KEY)
    web = make_live()
    response = web.get("/api/v1/consensus/settings")
    assert response.status_code == 200 and response.json()["enabled"] is False
    url = web.base + "/api/v1/consensus/settings"
    valid = {"Authorization": "Bearer " + ADMIN_KEY,
             "Origin": "https://onchainflow.app",
             "X-Forwarded-Proto": "https", "Content-Type": "application/json"}
    assert web.s.post(url, json=DEFAULT, timeout=15).status_code == 401
    assert web.s.post(url, json=DEFAULT, timeout=15,
                      headers={**valid, "Origin": "https://evil.example"}).status_code == 403
    assert web.s.post(url, json=DEFAULT, timeout=15,
                      headers={**valid, "X-Forwarded-Proto": "http"}).status_code == 403
    ok = web.s.post(url, json=DEFAULT, timeout=15, headers=valid)
    assert ok.status_code == 200, ok.text[:350]
    assert ok.json()["version"] == 1
    conflict = web.s.post(url, json=DEFAULT, timeout=15, headers=valid)
    assert conflict.status_code == 409
    assert "CONSENSUS_ADMIN_KEY" not in web.get("/consensus").text
    assert ADMIN_KEY not in web.get("/consensus").text
