# ruff: noqa: F811
"""Airdrop spam filter: a token pushed into many tracked wallets by someone else produces no
Telegram alerts for those unsolicited receipts. Everything is still indexed."""

from __future__ import annotations

from test_telegram_mute import TOKEN_X, eng, outbox, state  # noqa: F401
from test_tracker import E18

from agora_tracker.chain import ScanResult
from agora_tracker.classify import TradeEvent
from agora_tracker.config import load_config

SPAMMER = "0x2c6c9b59db2c7fce80614f4f19982702fb751951"
SPAM = "0x6f0954d9f9ed79fd5f5c745bd3dd6b14aec68af6"
TS = 1_800_000_600


def w(i: int) -> str:
    return "0x" + f"{0xA000 + i:040x}"


def leg(kind, token, wallet, tx, *, hype=None, block=10):
    return TradeEvent(kind=kind, tx=tx, block=block, wallet=wallet, token=token, qty=200_000 * E18,
                      hype_wei=hype)


def run(engine, events, senders, cursor, block=10):
    """`senders`: {tx_hash: address that sent the transaction}."""
    txs = [dict(tx_hash=h, block_number=block, from_address=s, to_address=SPAM, status=1)
           for h, s in senders.items()]
    engine.apply_range(ScanResult(events=events, block_times={block: TS}, transactions=txs), cursor)


def alerted(store, token=None):
    return [(r["payload"]["wallet"], r["payload"]["kind"]) for r in outbox(store)
            if r["payload"].get("type") == "trade"
            and (token is None or r["payload"]["token"] == token)]


def test_one_range_airdrop_to_many_wallets_sends_nothing_but_is_indexed(store, eng):
    events = [leg("TRANSFER_IN", SPAM, w(i), f"0x{i // 3:064x}") for i in range(12)]
    run(eng, events, {f"0x{t:064x}": SPAMMER for t in range(4)}, cursor=10)
    assert alerted(store, SPAM) == []
    s = state(store)
    assert len([r for r in s["trades"] if r[2] == SPAM]) == 12          # every leg stored
    assert len([p for p in s["positions"] if p[1] == SPAM]) == 12       # positions updated
    assert s["cursor"] == 10


def test_a_slow_airdrop_is_cut_off_once_the_threshold_is_reached(store, eng):
    assert eng.intel.airdrop_min_wallets == 5
    for i in range(9):                                 # one recipient per range
        run(eng, [leg("TRANSFER_IN", SPAM, w(i), f"0x{i:064x}")], {f"0x{i:064x}": SPAMMER},
            cursor=10 + i)
    # the first four are indistinguishable from an ordinary transfer; from the fifth on: silence
    assert [a[0] for a in alerted(store, SPAM)] == [w(0), w(1), w(2), w(3)]
    assert len([r for r in state(store)["trades"] if r[2] == SPAM]) == 9


def test_ordinary_activity_is_untouched(store, eng):
    # a wallet moving a token to itself / pulling a claim: it sent the transaction
    own = [leg("TRANSFER_IN", TOKEN_X, w(i), f"0x{100 + i:064x}") for i in range(8)]
    run(eng, own, {f"0x{100 + i:064x}": w(i) for i in range(8)}, cursor=10)
    assert len(alerted(store, TOKEN_X)) == 8
    # a few unsolicited receipts (fewer than the threshold) still alert
    few = [leg("TRANSFER_IN", SPAM, w(i), f"0x{200 + i:064x}") for i in range(4)]
    run(eng, few, {f"0x{200 + i:064x}": SPAMMER for i in range(4)}, cursor=11)
    assert len(alerted(store, SPAM)) == 4


def test_a_really_traded_token_is_not_spam_even_when_it_is_distributed(store, eng):
    buys = [leg("BUY", TOKEN_X, w(i), f"0x{300 + i:064x}", hype=E18) for i in range(6)]
    run(eng, buys, {f"0x{300 + i:064x}": w(i) for i in range(6)}, cursor=10)
    drops = [leg("TRANSFER_IN", TOKEN_X, w(10 + i), f"0x{400 + i:064x}") for i in range(8)]
    run(eng, drops, {f"0x{400 + i:064x}": SPAMMER for i in range(8)}, cursor=11)
    kinds = [k for _, k in alerted(store, TOKEN_X)]
    assert kinds.count("BUY") == 6 and kinds.count("TRANSFER_IN") == 8   # 6 trades vs 8 receipts


def test_selling_or_sending_a_spam_token_still_alerts(store, eng):
    run(eng, [leg("TRANSFER_IN", SPAM, w(i), "0x" + "5" * 64) for i in range(10)],
        {"0x" + "5" * 64: SPAMMER}, cursor=10)
    assert alerted(store, SPAM) == []
    # the wallet itself acts on the token later: that is real behaviour and is reported
    run(eng, [leg("SELL", SPAM, w(0), "0x" + "6" * 64, hype=E18),
              leg("TRANSFER_OUT", SPAM, w(1), "0x" + "7" * 64)],
        {"0x" + "6" * 64: w(0), "0x" + "7" * 64: w(1)}, cursor=11)
    assert sorted(alerted(store, SPAM)) == sorted([(w(0), "SELL"), (w(1), "TRANSFER_OUT")])
    # ... and a further airdrop round of the same token stays silent (1 sale vs 10+ receipts)
    run(eng, [leg("TRANSFER_IN", SPAM, w(20), "0x" + "8" * 64)], {"0x" + "8" * 64: SPAMMER},
        cursor=12)
    assert (w(20), "TRANSFER_IN") not in alerted(store, SPAM)


def test_unknown_sender_is_never_treated_as_unsolicited(store, eng):
    events = [leg("TRANSFER_IN", SPAM, w(i), f"0x{500 + i:064x}") for i in range(8)]
    engine_result = ScanResult(events=events, block_times={10: TS})      # no transaction evidence
    eng.apply_range(engine_result, 10)
    assert len(alerted(store, SPAM)) == 8


def test_filter_off_and_lookup_failure(store, eng, monkeypatch):
    monkeypatch.setattr(store, "unsolicited_receipts", lambda *a, **k: 1 / 0)   # database hiccup
    events = [leg("TRANSFER_IN", SPAM, w(i), f"0x{i:064x}") for i in range(6)]
    run(eng, events, {f"0x{i:064x}": SPAMMER for i in range(6)}, cursor=10)
    assert alerted(store, SPAM) == [] and store.get_cursor() == 10   # judged on the range alone
    eng.intel.airdrop_filter = False
    more = [leg("TRANSFER_IN", SPAM, w(10 + i), f"0x{50 + i:064x}") for i in range(6)]
    run(eng, more, {f"0x{50 + i:064x}": SPAMMER for i in range(6)}, cursor=11)
    assert len(alerted(store, SPAM)) == 6


def test_env_off_switch(monkeypatch):
    monkeypatch.delenv("ALERTS_AIRDROP_FILTER", raising=False)
    ic = load_config("config.example.yaml").intel
    assert (ic.airdrop_filter, ic.airdrop_min_wallets, ic.airdrop_window_h) == (True, 5, 24.0)
    monkeypatch.setenv("ALERTS_AIRDROP_FILTER", "off")
    assert load_config("config.example.yaml").intel.airdrop_filter is False
