from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from test_paper_trading import TOKEN, FixtureProvider, bot, cq, flow, intent

from agora_tracker.config import TradingConfig
from agora_tracker.trading.indicative import AgoraIndicative
from agora_tracker.trading.journal import PaperJournal
from agora_tracker.trading.paper import TradeRefused


def payload(i):
    return {'token': i.token, 'side': 'buy', 'amountIn': str(i.amount_wei),
            'amountOut': '10000', 'minAmountOut': '9950', 'venue': '<pool>',
            'feeBps': 0, 'priceImpact': 0.001, 'approvals': [], 'transaction': None}


def provider(i, monkeypatch, data=None):
    p = AgoraIndicative(now=lambda: 100., monotonic=lambda: 100.)
    response = Mock(status_code=200)
    response.json.return_value = payload(i) if data is None else data
    monkeypatch.setattr(p.session, 'get', Mock(return_value=response))
    return p


def test_public_quote_is_exact_read_only_rate_bounded_and_not_review(monkeypatch):
    f = flow()
    i = intent(f)
    p = provider(i, monkeypatch)
    q = p.quote(i)
    assert q.impact_bps == Decimal(10)
    args, kw = p.session.get.call_args
    assert args == ('https://api.agoradex.app/api/v1/quote',)
    assert kw['params'] == {'token': TOKEN, 'side': 'buy',
                            'amount': '0.100000000000000000', 'slippageBps': '50'}
    assert not kw['allow_redirects'] and kw['timeout'] == 5
    assert f.reviewed(1, 1, i.id) is None
    with pytest.raises(TradeRefused, match='Fresh review'):
        f.confirm(1, 1, i.id)
    with pytest.raises(TradeRefused, match='rate limit'):
        p.quote(i)
    assert p.session.get.call_count == 1


@pytest.mark.parametrize('change', [
    {'token': 'forged'}, {'side': 'sell'}, {'amountIn': '1'}, {'amountOut': 'NaN'},
    {'amountOut': 10000}, {'minAmountOut': '0'}, {'minAmountOut': '9949'},
    {'feeBps': True}, {'feeBps': 101}, {'priceImpact': 'NaN'}, {'priceImpact': -1},
    {'transaction': {'to': TOKEN}}, {'approvals': [{'to': TOKEN}]}, {'venue': 1},
])
def test_untrusted_market_response_never_becomes_quote(monkeypatch, change):
    i = intent(flow())
    p = provider(i, monkeypatch, {**payload(i), **change})
    with pytest.raises(TradeRefused, match='Invalid indicative'):
        p.quote(i)


def test_network_failure_does_not_expose_error_or_retry(monkeypatch):
    i = intent(flow())
    p = provider(i, monkeypatch)
    p.session.get.side_effect = requests.Timeout('secret URL or key')
    with pytest.raises(TradeRefused, match='^Indicative quote unavailable$'):
        p.quote(i)
    assert p.session.get.call_count == 1


def test_provider_429_cools_down_without_retry(monkeypatch):
    i = intent(flow())
    p = provider(i, monkeypatch)
    p.session.get.return_value.status_code = 429
    with pytest.raises(TradeRefused, match='minute'):
        p.quote(i)
    assert p._next_request == 160.
    with pytest.raises(TradeRefused, match='rate limit'):
        p.quote(i)
    assert p.session.get.call_count == 1


def test_sell_does_not_guess_decimals_or_balances(monkeypatch):
    i = intent(flow(), 'sell')
    p = provider(i, monkeypatch)
    with pytest.raises(TradeRefused, match='verified token decimals'):
        p.quote(i)
    assert not p.session.get.called


def test_custom_amounts_private_binding_and_caps():
    b = bot()
    i = intent(b.paper_trades)
    def message(uid, chat, amount):
        return {'from': {'id': uid}, 'chat': {'id': chat, 'type': 'private'},
                'text': '/paper_amount ' + i.id + ' ' + amount}
    b.on_message(message(2, 2, '0.2'))
    assert b.paper_trades.get(1, 1, i.id).amount_wei == 10**17
    b.on_message(message(1, 1, '0.123456789123456789'))
    assert b.paper_trades.get(1, 1, i.id).amount_wei == 123456789123456789
    b.on_message(message(1, 1, '1.1'))
    assert 'cap' in b._send.call_args.args[1]
    f = flow(FixtureProvider())
    i = intent(f, 'sell')
    assert f.select(1, 1, i.id, '12.34').sell_bps == 1234
    for amount in ['0', '100.01', '1e1', '-1', '12.345']:
        with pytest.raises(TradeRefused):
            f.select(1, 1, i.id, amount)


def test_review_panel_confirmation_is_user_bound_and_replay_safe():
    b = bot()
    b.paper_trades = flow(FixtureProvider())
    i = intent(b.paper_trades)
    b.on_callback(cq('pr|' + i.id, chat=1, kind='private'))
    text, kb = b._show.call_args.args[2:]
    assert 'Minimum output: 995' in text and 'Hypothetical review' in text
    confirm = next(btn['callback_data'] for row in kb['inline_keyboard']
                   for btn in row if btn['text'] == 'Confirm PAPER record')
    assert len(confirm) <= 64
    b.on_callback(cq(confirm, uid=2, chat=2, kind='private'))
    assert b.paper_trades.get(1, 1, i.id).state == 'review'
    b.on_callback(cq(confirm, chat=1, kind='private'))
    assert 'No actual fill' in b._show.call_args.args[2]
    b.on_callback(cq(confirm, chat=1, kind='private'))
    assert 'completed' in b._answer.call_args.args[1]


def test_old_confirmation_cannot_confirm_refreshed_or_changed_review():
    f = flow(FixtureProvider())
    i = intent(f)
    f.review(1, 1, i.id)
    old = f.confirmation_token(1, 1, i.id)
    f.review(1, 1, i.id)
    with pytest.raises(TradeRefused, match='Review changed'):
        f.confirm(1, 1, i.id, old)
    f.select(1, 1, i.id, '0.5')
    assert f.confirmation_token(1, 1, i.id) is None
    with pytest.raises(TradeRefused, match='Fresh review'):
        f.confirm(1, 1, i.id, old)


def test_indicative_panel_has_no_confirmation_or_verified_quote():
    b = bot()
    i = intent(b.paper_trades)
    b.paper_market = SimpleNamespace(quote=lambda _: SimpleNamespace(
        venue='<untrusted pool>', amount_out_raw=1000, min_out_raw=995,
        fee_bps=0, impact_bps=10))
    b.on_callback(cq('pq|' + i.id, chat=1, kind='private'))
    text, kb = b._show.call_args.args[2:]
    assert 'INDICATIVE' in text and '&lt;untrusted pool&gt;' in text
    assert all(not btn.get('callback_data', '').startswith('pf|')
               for row in kb['inline_keyboard'] for btn in row)
    assert not b.paper_trades._quotes


def test_durable_journal_restart_is_private_and_does_not_restore_sessions(tmp_path):
    path = tmp_path / 'private' / 'paper.sqlite'
    journal = PaperJournal(str(path))
    f = flow(FixtureProvider())
    f.journal = journal
    i = intent(f)
    q = f.review(1, 1, i.id)
    order = f.confirm(1, 1, i.id, f.confirmation_token(1, 1, i.id))
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    reopened = PaperJournal(str(path))
    assert reopened.recent(1)[0]['intent_id'] == i.id
    assert reopened.recent(2) == []
    with pytest.raises(TradeRefused, match='duplicate'):
        reopened.save(order, i, q)
    restarted = flow(FixtureProvider())
    restarted.journal = reopened
    assert len(restarted.orders(1, 1)) == 1
    with pytest.raises(TradeRefused):
        restarted.get(1, 1, i.id)
    with pytest.raises(TradeRefused):
        restarted.orders(1, 2)


def test_journal_rejects_shared_directory_and_symlink(tmp_path):
    shared = tmp_path / 'shared'
    shared.mkdir(mode=0o755)
    with pytest.raises(TradeRefused, match='0700'):
        PaperJournal(str(shared / 'paper.sqlite'))
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    target = private / 'target'
    target.touch(mode=0o600)
    link = private / 'link'
    link.symlink_to(target)
    with pytest.raises(OSError):
        PaperJournal(str(link))


def test_journal_failure_does_not_mark_order_done_or_discard_review():
    f = flow(FixtureProvider())
    i = intent(f)
    f.review(1, 1, i.id)
    token = f.confirmation_token(1, 1, i.id)
    f.journal = Mock()
    f.journal.save.side_effect = TradeRefused('storage failed')
    with pytest.raises(TradeRefused, match='storage failed'):
        f.confirm(1, 1, i.id, token)
    assert f.get(1, 1, i.id).state == 'review'
    assert f.confirmation_token(1, 1, i.id) == token


def test_invalid_journal_configuration_rejected():
    for path in ['', 1, True]:
        with pytest.raises(ValueError):
            TradingConfig(paper_order_path=path)


def test_pause_during_network_call_hides_indicative_data():
    b = bot()
    i = intent(b.paper_trades)
    def quote(_):
        b.paper_trades.paused = True
        return None
    b.paper_market = SimpleNamespace(quote=quote)
    b.on_callback(cq('pq|' + i.id, chat=1, kind='private'))
    assert not b._show.called
    assert 'paused' in b._answer.call_args.args[1]



def test_pause_still_allows_cancellation_and_private_history():
    f = flow(FixtureProvider())
    i = intent(f)
    f.review(1, 1, i.id)
    token = f.confirmation_token(1, 1, i.id)
    f.paused = True
    with pytest.raises(TradeRefused, match='paused'):
        f.confirm(1, 1, i.id, token)
    f.cancel(1, 1, i.id)
    assert i.id not in f._quotes and i.id not in f._review_tokens
    assert f.orders(1, 1) == []
    with pytest.raises(TradeRefused):
        f.cancel(2, 2, i.id)


def test_busy_indicative_request_does_not_block_ui_worker(monkeypatch):
    i = intent(flow())
    p = provider(i, monkeypatch)
    with p._lock:
        with pytest.raises(TradeRefused, match='busy'):
            p.quote(i)
    assert not p.session.get.called



def test_quote_fees_and_gas_must_fit_available_hype():
    class ExactFunds(FixtureProvider):
        def quote(self, i, slippage):
            q = super().quote(i, slippage)
            return replace(q, balance_wei=q.amount_in_wei + q.gas_wei, fee_wei=1)
    f = flow(ExactFunds())
    i = intent(f)
    with pytest.raises(TradeRefused, match='including gas'):
        f.review(1, 1, i.id)
    assert f.reviewed(1, 1, i.id) is None


def test_cancelling_review_revokes_confirm_token():
    f = flow(FixtureProvider())
    i = intent(f)
    f.review(1, 1, i.id)
    token = f.confirmation_token(1, 1, i.id)
    f.cancel(1, 1, i.id)
    with pytest.raises(TradeRefused):
        f.confirm(1, 1, i.id, token)
    assert i.id not in f._review_tokens


def test_forged_chat_type_cannot_request_market_or_confirm():
    b = bot()
    i = intent(b.paper_trades)
    b.paper_market = Mock()
    b.on_callback(cq('pq|' + i.id, chat=1, kind='supergroup'))
    assert not b.paper_market.quote.called
    assert 'private' in b._answer.call_args.args[1]
