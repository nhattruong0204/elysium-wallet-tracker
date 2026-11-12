# ruff: noqa: F811
"""Transfer bursts: one wallet splitting a token over many wallets is one alert, not fifty."""

from __future__ import annotations

from test_airdrop_filter import TS, leg, w
from test_launch_cohorts import _outbox, tok
from test_telegram_mute import eng  # noqa: F401
from test_tracker import E18

from agora_tracker import cards
from agora_tracker.bursts import BurstTracker
from agora_tracker.cards import Links
from agora_tracker.chain import ScanResult
from agora_tracker.config import LinksConfig, load_config

ME, T = w(1), tok(7)


def feed(bt, n, *, start=0, t0=1000.0, gap=1.0, wallet=ME, token=T):
    return [bt.observe(wallet, token, t0 + i * gap, 9 * E18, key=f"k{wallet}{token}{i}",
                       counterparty=f"0xr{i}", before_qty=1000 * E18, tx=f"0xt{i}")
            for i in range(start, start + n)]


# --- the rule (pure) --------------------------------------------------------------------------
def test_first_transfers_alert_then_one_start_then_silence_then_one_final():
    bt = BurstTracker(min_n=4, window_s=120, quiet_s=90)
    got = feed(bt, 50)
    assert [v for v, _ in got[:4]] == ["single", "single", "single", "start"]
    assert {v for v, _ in got[4:]} == {"more"}
    start = got[3][1]
    assert start["count"] == 4 and start["stage"] == "started" and start["recipients"] == 4
    assert bt.finished(1049 + 60) == []                         # still within the quiet period
    (end,) = bt.finished(1049 + 91)
    assert end["stage"] == "finished" and end["count"] == 50 and end["recipients"] == 50
    assert int(end["qty"]) == 450 * E18 and end["span_s"] == 49
    assert end["start_qty"] == str(1000 * E18) and end["first_ts"] == start["first_ts"]
    assert bt.finished(99999) == []                             # reported once


def test_slow_or_separate_transfers_are_not_a_burst():
    bt = BurstTracker(min_n=4, window_s=120, quiet_s=90)
    assert {v for v, _ in feed(bt, 10, gap=100)} == {"single"}          # 100 s apart
    a = feed(bt, 3, t0=5000, token=tok(1)) + feed(bt, 3, t0=5000, token=tok(2)) \
        + feed(bt, 3, t0=5000, wallet=w(2))
    assert {v for v, _ in a} == {"single"}                              # per wallet and token


def test_a_burst_that_never_grew_has_no_final_and_replays_do_not_count_twice():
    bt = BurstTracker(min_n=4, window_s=120, quiet_s=90)
    feed(bt, 4)
    assert bt.finished(1e9) == []
    bt2 = BurstTracker(min_n=4, window_s=120, quiet_s=90)
    feed(bt2, 3)
    assert feed(bt2, 3)[-1][0] == "single"                      # the same three legs again
    feed(bt2, 2, start=3)
    feed(bt2, 2, start=3)                                       # replay inside the burst
    (end,) = bt2.finished(1e9)
    assert end["count"] == 5


# --- tracker ----------------------------------------------------------------------------------
def split(eng, n, *, start=0, cursor=10, block=10, ts=TS):
    events = [leg("TRANSFER_OUT", T, ME, f"0x{0xB00 + i:064x}", block=block)
              for i in range(start, start + n)]
    transfers = [dict(tx_hash=e.tx, token=T, from_address=ME, to_address=w(100 + start + i),
                      amount=e.qty, log_index=i, block_number=block)
                 for i, e in enumerate(events)]
    txs = [dict(tx_hash=e.tx, block_number=block, from_address=ME, to_address=T, status=1)
           for e in events]
    eng.apply_range(ScanResult(events=events, block_times={block: ts}, transactions=txs,
                               transfers=transfers), cursor)


def kinds(store):
    return [(r["payload"].get("type"), r["payload"].get("stage")) for r in _outbox(store)
            if r["payload"].get("token") == T]


def test_fifty_transfers_make_five_alerts_and_stay_indexed(store, eng):
    eng.apply_range(ScanResult(events=[leg("BUY", T, ME, "0x" + "a" * 64, hype=50 * E18)],
                               block_times={10: TS - 60}), 9)
    split(eng, 20)
    split(eng, 30, start=20, cursor=11, block=11, ts=TS + 20)
    assert kinds(store) == [("trade", None)] * 4 + [("burst", "started")]
    eng.apply_range(ScanResult(events=[], block_times={12: TS + 60}), 12)
    assert ("burst", "finished") not in kinds(store)                    # not quiet yet
    eng.apply_range(ScanResult(events=[], block_times={13: TS + 200}), 13)
    assert kinds(store)[-1] == ("burst", "finished") and len(kinds(store)) == 6
    end = [r["payload"] for r in _outbox(store) if r["payload"].get("stage") == "finished"][0]
    assert end["count"] == 50 and end["recipients"] == 50 and end["wallet"] == ME
    text, _ = cards.render_burst(end, Links(LinksConfig()))
    assert "SPLIT DONE" in text and "50 transfers" in text and "50 wallets" in text
    assert "% of what it held" in text
    import sqlalchemy as sa
    with store.engine.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM trades WHERE token = :t AND kind = "
                              "'TRANSFER_OUT'"), {"t": T}).scalar()
    assert n == 50 and store.get_cursor() == 13


def test_switch_off_and_dispatch(store, eng, monkeypatch):
    eng.intel.burst_filter = False
    split(eng, 8)
    assert kinds(store) == [("trade", None)] * 8
    p = {"type": "burst", "stage": "started", "wallet": ME, "token": T, "count": 4,
         "qty": str(36 * E18), "recipients": 4, "start_qty": None, "span_s": 3, "tx": None}
    text, _ = cards.render(p, Links(LinksConfig()))
    assert "SPLITTING" in text and "so far" in text and "final summary follows" in text
    monkeypatch.delenv("ALERTS_TRANSFER_BURST", raising=False)
    assert load_config("config.example.yaml").intel.burst_filter is True
    monkeypatch.setenv("ALERTS_TRANSFER_BURST", "off")
    assert load_config("config.example.yaml").intel.burst_filter is False
