"""Configuration, startup wiring and the durable Telegram pause (issue #91)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from conftest import TEST_URL
from test_trading_ledger import (
    OWNER,
    TRADING_PASSWORD,
    U1,
    Clock,
    create,
    reset_ledger,
    trading_url,
)

from agora_tracker.config import TradingConfig
from agora_tracker.migrate import set_trading_password
from agora_tracker.trading.ledger import Actor, OrderLedger, make_ledger_engine
from agora_tracker.trading.ledger.runtime import LedgerConfigError, open_ledger, owner_ids

needs_pg = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")


def cfg(**trading):
    return SimpleNamespace(trading=TradingConfig(**trading),
                           telegram=SimpleNamespace(chat_id=str(OWNER)))


def test_ledger_is_off_by_default_and_flags_are_validated():
    assert TradingConfig().ledger_enabled is False
    assert open_ledger(cfg()) is None
    for bad in ({"ledger_enabled": "yes"}, {"ledger_enabled": 1},
                {"ledger_db_url_env": "lower"}, {"ledger_owner_ids_env": "X; rm -rf"}):
        with pytest.raises(ValueError):
            TradingConfig(**bad)


def test_enabled_but_misconfigured_is_fatal_not_unlimited():
    c = cfg(ledger_enabled=True)
    with pytest.raises(LedgerConfigError, match="TRADING_DB_URL is not set"):
        open_ledger(c, environ={})
    with pytest.raises(LedgerConfigError, match="must connect as trading_rw"):
        open_ledger(c, environ={"TRADING_DB_URL": "postgresql+psycopg://tracker:x@h/db"})
    nobody = SimpleNamespace(trading=c.trading, telegram=SimpleNamespace(chat_id="-100123"))
    with pytest.raises(LedgerConfigError, match="no ledger owner"):
        open_ledger(nobody, environ={"TRADING_DB_URL": "postgresql+psycopg://trading_rw:x@h/db"})


def test_owner_ids_come_from_the_alert_chat_and_the_owner_variable_only():
    c = cfg()
    assert owner_ids(c, {}) == {OWNER}
    assert owner_ids(c, {"TRADING_OWNER_IDS": "7; 8,x,-5"}) == {OWNER, 7, 8}


def test_ledger_engine_never_exceeds_the_role_connection_cap():
    eng = make_ledger_engine("postgresql+psycopg://trading_rw:x@127.0.0.1:1/none")
    assert eng.pool.size() + eng.pool._max_overflow <= 5     # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        make_ledger_engine("")


@needs_pg
def test_open_ledger_bootstraps_recovers_and_stays_paused_when_chain_is_bad(pg_engine):
    reset_ledger(pg_engine)
    set_trading_password(pg_engine, TRADING_PASSWORD)
    env = {"TRADING_DB_URL": trading_url()}
    lg = open_ledger(cfg(ledger_enabled=True), env)
    try:
        assert lg is not None and lg.controls().policy_version is not None
        assert lg.controls().execution_enabled is False and lg.is_paused() is False
        lg.pause(Actor.owner(OWNER), "keep")
        again = open_ledger(cfg(ledger_enabled=True), env)       # a restart keeps the pause
        assert again is not None and again.is_paused()
        again.engine.dispose()
        # a tampered chain pauses trading at startup
        with pg_engine.begin() as c:
            c.execute(sa.text("ALTER TABLE trading_private.order_events DISABLE TRIGGER USER"))
            c.execute(sa.text("UPDATE trading_private.order_events SET detail='{}'::jsonb "
                              "WHERE chain_no = 1"))
            c.execute(sa.text("ALTER TABLE trading_private.order_events ENABLE TRIGGER USER"))
        lg.resume(Actor.owner(OWNER), "clear")
        bad = open_ledger(cfg(ledger_enabled=True), env)
        assert bad is not None and bad.is_paused()
        bad.engine.dispose()
    finally:
        lg.engine.dispose()
        reset_ledger(pg_engine)


# ----------------------------------------------------------------- Telegram commands

def tg_bot(ledger, allowed=(OWNER, U1)):
    from test_paper_trading import bot
    b = bot()
    b.allowed = set(allowed)
    b.ledger = ledger
    b._send = Mock()
    return b


def say(b, uid, text):
    b.on_message({"from": {"id": uid}, "chat": {"id": uid, "type": "private"}, "text": text})
    return b._send.call_args.args[1]


@needs_pg
def test_pause_command_is_durable_and_resume_is_owner_only(pg_engine):
    reset_ledger(pg_engine)
    set_trading_password(pg_engine, TRADING_PASSWORD)
    eng = make_ledger_engine(trading_url())
    lg = OrderLedger(eng, owner_ids={OWNER}, clock=Clock())
    lg.bootstrap()
    b2 = None
    try:
        b = tg_bot(lg)
        assert "saved" in say(b, U1, "/pause_trading")             # any operator can stop trading
        assert lg.is_paused() and b.paper_trades.paused
        with pg_engine.connect() as c:
            ev = c.execute(sa.text(
                "SELECT actor_kind, detail->>'reason' FROM trading_private.order_events "
                "WHERE kind='PAUSED'")).one()
        assert ev[0] == "system" and str(U1) in ev[1]              # attributed in the reason
        assert "owner" in say(b, U1, "/resume_trading").lower() and lg.is_paused()
        assert b.paper_trades.paused
        # a *new process* (fresh bot, fresh pool) is still paused before anyone acts
        b2 = tg_bot(OrderLedger(make_ledger_engine(trading_url()), owner_ids={OWNER}))
        b2._sync_pause()
        assert b2.paper_trades.paused
        assert say(b2, OWNER, "/resume_trading") == "Trading resumed."
        assert not lg.is_paused() and not b2.paper_trades.paused
        assert "saved" in say(b2, OWNER, "/pause_trading")
        assert lg.is_paused()
    finally:
        eng.dispose()
        if b2 is not None:
            b2.ledger.engine.dispose()
        reset_ledger(pg_engine)


def test_pause_state_fails_closed_and_ledger_pause_wins_over_memory():
    from test_paper_trading import bot
    b = bot()
    b.ledger = Mock(is_paused=Mock(return_value=True))
    b._sync_pause()
    assert b.paper_trades.paused
    b.ledger.is_paused.return_value = False
    b._sync_pause()
    assert not b.paper_trades.paused
    b._paper_broken = True                                          # journal failure stays paused
    b._sync_pause()
    assert b.paper_trades.paused


def test_without_a_ledger_the_old_behaviour_is_unchanged():
    from test_paper_trading import bot
    b = bot()
    b.ledger = None
    b._send = Mock()
    b.allowed = {1}
    b.on_message({"from": {"id": 1}, "chat": {"id": 1, "type": "private"},
                  "text": "/pause_trading"})
    assert b.paper_trades.paused and "until process restart" in b._send.call_args.args[1]


@needs_pg
def test_a_paused_ledger_blocks_new_orders_made_through_the_ledger_api(pg_engine):
    reset_ledger(pg_engine)
    set_trading_password(pg_engine, TRADING_PASSWORD)
    from test_trading_ledger import permissive
    eng = make_ledger_engine(trading_url())
    lg = OrderLedger(eng, owner_ids={OWNER}, clock=Clock())
    try:
        lg.bootstrap()
        lg.set_policy(Actor.owner(OWNER), permissive())
        create(lg)                                                  # fine before the pause
        b = tg_bot(lg)
        say(b, OWNER, "/pause_trading")
        from agora_tracker.trading.ledger import LedgerRefused
        with pytest.raises(LedgerRefused, match="^paused"):
            create(lg, "second", wallet="0x" + "a2" * 20)
    finally:
        eng.dispose()
        reset_ledger(pg_engine)
