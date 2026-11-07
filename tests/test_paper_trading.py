from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agora_tracker.cards import Links
from agora_tracker.config import LinksConfig, TradingConfig
from agora_tracker.tgui import IntelBot
from agora_tracker.trading.paper import (
    ExecutionRoute,
    PaperFlow,
    Quote,
    RouteUnavailable,
    TradeRefused,
    hype_wei,
)
from agora_tracker.trading.telegram import alert_controls

TOKEN = '0x' + 'ab' * 20
ROUTE = ExecutionRoute(99901, '0x' + 'cd' * 20, '0x'+'12'*32, 'fixture-only')


class FixtureProvider:
    def quote(self, i, slippage):
        n = i.amount_wei if i.side == 'buy' else 1000 * i.sell_bps // 10000
        return Quote(i.id, 99901, i.token, i.side, n, 1000,
                     1000*(10000-slippage)//10000, 10**20 if i.side == 'buy' else 1000,
                     1, 10, 100., 125., ROUTE, True, native_balance_wei=10**18)


def flow(provider=None):
    return PaperFlow({1, 2}, provider=provider, routes=frozenset({ROUTE}), now=lambda: 100.)


def intent(f, side='buy'):
    i = f.create(1, 1, TOKEN, side, -100123, 55)
    return f.select(1, 1, i.id, '0.1' if side == 'buy' else '50')


def test_exact_wei_no_float_or_decimal_context_rounding():
    assert hype_wei('999999999999.999999999999999999') == 10**30 - 1
    assert hype_wei('0.000000000000000001') == 1
    for v in ('0', '-1', '1e9', 'NaN', '0.0000000000000000001'):
        with pytest.raises(TradeRefused):
            hype_wei(v)


def test_config_is_off_and_no_live_switch_exists():
    assert TradingConfig().paper_enabled is False
    with pytest.raises(TypeError):
        TradingConfig(live_enabled=True)
    with pytest.raises(ValueError):
        TradingConfig(paper_enabled='false')


def test_auth_chain_binding_and_duplicate_alert_are_safe():
    f = flow()
    i = intent(f)
    assert f.create(1, 1, TOKEN, 'buy', -100123, 55).id == i.id
    for uid, chat in [(3, 3), (1, -100123), (2, 2)]:
        with pytest.raises(TradeRefused):
            f.get(uid, chat, i.id)
    with pytest.raises(TradeRefused):
        f.create(1, 1, TOKEN, 'buy', -100123, 55, 1339)
    with pytest.raises(TradeRefused):
        f.create(1, 1, 'forged', 'buy', -100123, 55)


def test_production_no_quote_fails_closed_and_broadcast_is_absent():
    f = flow()
    i = intent(f)
    with pytest.raises(RouteUnavailable):
        f.review(1, 1, i.id)
    with pytest.raises(TradeRefused):
        f.confirm(1, 1, i.id)
    with pytest.raises(TradeRefused, match='not implemented'):
        f.broadcast('0xsigned')


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_paper_review_confirm_once_does_not_submit(side):
    f = flow(FixtureProvider())
    i = intent(f, side)
    q = f.review(1, 1, i.id, 100)
    assert q.min_out_wei == 990
    order = f.confirm(1, 1, i.id)
    assert order.mode == 'PAPER' and order.status == 'hypothetical_only'
    with pytest.raises(TradeRefused):
        f.confirm(1, 1, i.id)


@pytest.mark.parametrize('changes', [
    {'chain_id':1339}, {'token':'0x'+'ee'*20}, {'intent_id':'forged'},
    {'expires_at':99.}, {'created_at':101.}, {'simulated':False},
    {'min_out_wei':0}, {'balance_wei':0}, {'price_impact_bps':101},
    {'amount_in_wei':1}, {'gas_wei':-1}, {'amount_out_wei':0},
    {'route':ExecutionRoute(99901,'0x'+'ee'*20,'unverified','v4')},
])
def test_bad_quotes_cannot_be_reviewed(changes):
    class Bad(FixtureProvider):
        def quote(self, i, slippage):
            return replace(super().quote(i, slippage), **changes)
    f = flow(Bad())
    i = intent(f)
    with pytest.raises(TradeRefused):
        f.review(1, 1, i.id)
    with pytest.raises(TradeRefused):
        f.confirm(1, 1, i.id)


def test_expiry_cancellation_pause_and_caps():
    f = flow(FixtureProvider())
    i = intent(f)
    with pytest.raises(TradeRefused):
        f.select(1, 1, i.id, '1.000000000000000001')
    f.cancel(1, 1, i.id)
    with pytest.raises(TradeRefused):
        f.review(1, 1, i.id)
    f = flow(FixtureProvider())
    i = intent(f)
    f.review(1, 1, i.id)
    f.now = lambda: 126.
    with pytest.raises(TradeRefused):
        f.confirm(1, 1, i.id)
    f.paused = True
    with pytest.raises(TradeRefused):
        intent(f)
    f.paused = False
    f.now = lambda: 300.
    with pytest.raises(TradeRefused):
        f.get(1, 1, i.id)


def bot(enabled=True):
    b = IntelBot.__new__(IntelBot)
    b.cfg = SimpleNamespace(trading=TradingConfig(enabled), chain=SimpleNamespace(chain_id=99901))
    b.allowed, b._refused = {1, 2}, {}
    b.paper_trades = flow()
    b.L = Links(LinksConfig())
    b._send, b._answer, b._show = Mock(), Mock(), Mock()
    return b


def cq(data, uid=1, chat=-100123, kind='supergroup'):
    return {'id':'callback', 'from':{'id':uid}, 'data':data,
            'message':{'message_id':55, 'chat':{'id':chat, 'type':kind}}}


def test_group_alert_opens_only_authorized_private_panel():
    b = bot()
    b.on_callback(cq('tp|b|'+TOKEN))
    assert b._send.call_args.args[0] == 1
    assert 'PAPER' in b._send.call_args.args[1]
    assert 'unknown' in b._send.call_args.args[1]
    assert not b._show.called
    sid = next(iter(b.paper_trades._intents))
    b.on_callback(cq('ps|'+sid+'|0.5'))
    assert 'private' in b._answer.call_args.args[1]
    b.on_callback(cq('ps|'+sid+'|0.5', uid=2, chat=2, kind='private'))
    assert not b._show.called
    b.on_callback(cq('ps|'+sid+'|0.5', chat=1, kind='private'))
    assert b._show.called
    b.on_callback(cq('pr|'+sid, chat=1, kind='private'))
    assert 'No verified quote adapter' in b._answer.call_args.args[1]


def test_disabled_or_unauthorized_callback_cannot_create_session():
    b = bot(False)
    b.on_callback(cq('tp|b|'+TOKEN))
    assert not b.paper_trades._intents
    b = bot()
    b.on_callback(cq('tp|b|'+TOKEN, uid=3))
    assert not b.paper_trades._intents


def test_existing_alert_keyboard_is_preserved_not_mutated():
    original = {'inline_keyboard':[[{'text':'original','url':'https://example.test'}]]}
    markup = alert_controls(original, TOKEN, 99901)
    assert len(original['inline_keyboard']) == 1
    assert markup['inline_keyboard'][0] == original['inline_keyboard'][0]
    assert all(len(b['callback_data']) <= 64 for b in markup['inline_keyboard'][-1])
    assert alert_controls(original, TOKEN, 1339) == original


def test_concurrent_confirmation_creates_only_one_hypothetical_order():
    from concurrent.futures import ThreadPoolExecutor
    f = flow(FixtureProvider())
    i = intent(f)
    f.review(1, 1, i.id)
    def confirm(_):
        try:
            return f.confirm(1, 1, i.id).status
        except TradeRefused:
            return 'refused'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(confirm, range(2))) == ['hypothetical_only', 'refused']


def test_sell_percentage_requires_native_gas_balance():
    class NoGas(FixtureProvider):
        def quote(self, i, slippage):
            return replace(super().quote(i, slippage), native_balance_wei=0)
    f = flow(NoGas())
    i = intent(f, 'sell')
    with pytest.raises(TradeRefused, match='sell gas'):
        f.review(1, 1, i.id)


def test_alert_renderer_is_unchanged_when_flag_off(monkeypatch):
    from agora_tracker import cards
    from agora_tracker.__main__ import Tracker
    t = Tracker.__new__(Tracker)
    t.cfg = SimpleNamespace(trading=TradingConfig(), alerts=SimpleNamespace(token_image=False),
                            chain=SimpleNamespace(chain_id=99901))
    t.dd, t.links = None, Links(LinksConfig())
    t.market = SimpleNamespace(token=lambda _: None, hype_usd=lambda: None)
    original = {'inline_keyboard':[[{'text':'Original','url':'https://example.test'}]]}
    monkeypatch.setattr(cards, 'render', lambda *a, **kw: ('Original alert', original))
    row = {'payload':{'type':'trade','token':TOKEN}, 'text':'fallback'}
    assert t.render_alert(row) == ('Original alert', original, None)
    assert len(original['inline_keyboard']) == 1
    t.cfg.trading.paper_enabled = True
    text, markup, _ = t.render_alert(row)
    assert text == 'Original alert' and len(markup['inline_keyboard']) == 2
    assert len(original['inline_keyboard']) == 1


def test_failed_quote_refresh_revokes_previous_review():
    f = flow(FixtureProvider())
    i = intent(f)
    f.review(1, 1, i.id)
    f.provider = None
    with pytest.raises(RouteUnavailable):
        f.review(1, 1, i.id)
    with pytest.raises(TradeRefused, match='Fresh review'):
        f.confirm(1, 1, i.id)


def test_bounded_sessions_prune_after_expiry():
    f = flow()
    for message in range(1, 257):
        f.create(1, 1, TOKEN, 'buy', -100123, message)
    with pytest.raises(TradeRefused, match='capacity'):
        f.create(1, 1, TOKEN, 'buy', -100123, 257)
    f.now = lambda: 300.
    f.create(1, 1, TOKEN, 'buy', -100123, 257)
    assert len(f._intents) == 1


def test_forged_private_chat_and_group_command_are_rejected():
    b = bot()
    b.on_callback(cq('tp|b|'+TOKEN, chat=2, kind='private'))
    assert not b.paper_trades._intents
    b.on_message({'from':{'id':1}, 'chat':{'id':-100123,'type':'supergroup'},
                  'message_id':55, 'text':'/trade '+TOKEN})
    assert not b.paper_trades._intents
    b.on_message({'from':{'id':1}, 'chat':{'id':1,'type':'private'},
                  'message_id':55, 'text':'/trade '+TOKEN})
    assert len(b.paper_trades._intents) == 1
    b.on_message({'from':{'id':1}, 'chat':{'id':1,'type':'private'},
                  'message_id':56, 'text':'/pause_trading'})
    assert b.paper_trades.paused
