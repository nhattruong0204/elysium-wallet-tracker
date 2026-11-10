# ruff: noqa: F811
"""Automated-wallet filter: wallets whose transaction timing is machine-like get no Telegram
trade/transfer alerts and never count as independent buyers. Everything is still indexed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from conftest import CHAIN
from test_telegram_mute import TOKEN_X, apply, eng, ev, outbox, state  # noqa: F401
from test_tracker import E18, W1, W2, W3

from agora_tracker import schema as t
from agora_tracker.config import IntelConfig, load_config

NOW = datetime.now(timezone.utc)
BOT, HUMAN, NEWBIE = W1, W2, W3


def seed(store, wallet, n, gap_s, *, start_ago_h=20.0, tokens=50, tag="a"):
    """n transactions by `wallet`, `gap_s` apart, each a single transfer leg."""
    t0 = NOW - timedelta(hours=start_ago_h)
    rows = [dict(chain_id=CHAIN, tx_hash=f"0x{tag}{wallet[2:8]}{i:054x}"[:66], wallet=wallet,
                 token="0x" + f"{i % tokens:040x}", kind="TRANSFER_IN" if i % 2 == 0 else "TRANSFER_OUT",
                 confidence="high", hype_method="none", qty=10**18, block_number=1000 + i,
                 block_time=t0 + timedelta(seconds=i * gap_s)) for i in range(n)]
    with store.tx() as c:
        c.execute(sa.insert(t.transactions), [dict(chain_id=CHAIN, tx_hash=r["tx_hash"],
                                                   block_number=r["block_number"]) for r in rows])
        c.execute(sa.insert(t.trades), rows)


def judged(store, **kw):
    ic = IntelConfig()
    args = dict(lookback_days=ic.bot_lookback_days, min_txs=ic.bot_min_txs,
                fast_gap_s=ic.bot_fast_gap_s, fast_share=ic.bot_fast_share) | kw
    return store.automated_wallets(**args)


def test_only_machine_like_timing_is_flagged(store):
    seed(store, BOT, 150, 1.0)            # one tx per second, 150 times
    seed(store, HUMAN, 150, 45.0)         # busy, but human-paced
    seed(store, NEWBIE, 30, 0.5)          # fast, but too little history to judge
    got = judged(store)
    assert set(got) == {BOT}
    assert got[BOT] == {"txs": 150, "fast_share": 1.0, "tokens": 50}
    # thresholds are what decides, not the address
    assert set(judged(store, min_txs=20)) == {BOT, NEWBIE}
    assert judged(store, fast_gap_s=0.5) == {}
    assert set(judged(store, fast_gap_s=60.0)) == {BOT, HUMAN}


def test_mixed_wallet_is_judged_by_the_share_of_fast_gaps(store):
    seed(store, BOT, 100, 30.0, tag="a")                      # 100 slow ...
    seed(store, BOT, 30, 1.0, start_ago_h=10, tag="b")        # ... plus a 30-tx burst = 23% fast
    seed(store, HUMAN, 100, 30.0, tag="a")
    seed(store, HUMAN, 8, 1.0, start_ago_h=10, tag="b")       # approve+swap pairs: 7% fast
    got = judged(store)
    assert set(got) == {BOT} and 0.2 < got[BOT]["fast_share"] < 0.25


def test_old_activity_ages_out(store):
    seed(store, BOT, 150, 1.0, start_ago_h=24 * 9)            # nine days ago
    assert judged(store) == {} and set(judged(store, lookback_days=10)) == {BOT}


def test_flagged_wallet_gets_no_alert_but_is_fully_indexed(store, eng):
    seed(store, BOT, 150, 1.0)
    events = [ev("BUY", TOKEN_X, tx="0x01", wallet=BOT), ev("TRANSFER_OUT", TOKEN_X, tx="0x02", wallet=BOT),
              ev("BUY", TOKEN_X, tx="0x03", wallet=HUMAN)]
    apply(store, eng, events)
    sent_for = [r["payload"]["wallet"] for r in outbox(store)]
    assert sent_for == [HUMAN]                                 # the bot's two alerts are not queued
    s = state(store)
    new = [r for r in s["trades"] if r[0] in ("0x01", "0x02", "0x03")]
    assert {(r[0], r[1]) for r in new} == {("0x01", BOT), ("0x02", BOT), ("0x03", HUMAN)}
    assert {p[0] for p in s["positions"]} == {BOT, HUMAN} and s["cursor"] == 10


def test_filter_off_changes_nothing(store, eng):
    seed(store, BOT, 150, 1.0)
    eng.intel.bot_filter = False
    apply(store, eng, [ev("BUY", TOKEN_X, tx="0x01", wallet=BOT)])
    assert [r["payload"]["wallet"] for r in outbox(store)] == [BOT]
    assert eng.automated_wallets() == set()


def test_rejudged_on_refresh_and_a_failed_refresh_keeps_indexing(store, eng, monkeypatch):
    apply(store, eng, [ev("BUY", TOKEN_X, tx="0x01", wallet=BOT)])            # not flagged yet
    assert len(outbox(store)) == 1
    seed(store, BOT, 150, 1.0)
    apply(store, eng, [ev("BUY", TOKEN_X, tx="0x02", wallet=BOT)], cursor=11)  # cached: still alerts
    assert len(outbox(store)) == 2
    eng._bots_at = float("-inf")                                               # refresh is due
    apply(store, eng, [ev("BUY", TOKEN_X, tx="0x03", wallet=BOT)], cursor=12)
    assert len(outbox(store)) == 2 and eng.automated_wallets() == {BOT}
    eng._bots_at = float("-inf")
    monkeypatch.setattr(store, "automated_wallets", lambda **kw: 1 / 0)       # database hiccup
    apply(store, eng, [ev("BUY", TOKEN_X, tx="0x04", wallet=HUMAN)], cursor=13)
    assert [r["payload"]["wallet"] for r in outbox(store)][-1] == HUMAN       # indexing continued
    assert eng.automated_wallets() == {BOT} and store.get_cursor() == 13      # last verdict kept


def test_a_bot_never_counts_as_an_independent_buyer(store, eng):
    seed(store, BOT, 150, 1.0)
    rows = [(BOT, 1, 100, E18), (HUMAN, 2, 101, E18), (NEWBIE, 3, 102, E18)]
    assert eng._score(rows, 1800)["independent"] == 3 - 1 + 1   # bot collapses into "one operator"
    eng.intel.bot_filter = False
    eng._bots_at = float("-inf")
    assert eng._score(rows, 1800)["independent"] == 3


def test_env_off_switch_and_defaults(monkeypatch):
    monkeypatch.delenv("ALERTS_BOT_FILTER", raising=False)
    ic = load_config("config.example.yaml").intel
    assert (ic.bot_filter, ic.bot_min_txs, ic.bot_fast_gap_s, ic.bot_fast_share) == (True, 100, 2.0, 0.15)
    monkeypatch.setenv("ALERTS_BOT_FILTER", "off")
    assert load_config("config.example.yaml").intel.bot_filter is False
