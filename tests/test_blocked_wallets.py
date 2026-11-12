"""Operator block list (TRACKING_BLOCKED_WALLETS) and the restricted automated-wallet rule."""

from __future__ import annotations

import yaml
from test_automated_wallets import BOT, HUMAN, judged, seed
from test_cumulative_tracking import (
    E_,
    MANUAL,
    TG,
    A,
    B,
    C,
    D,
    enrollment,
    make_tracker,
    no_sleep,  # noqa: F401
    rows,
    snap,
)

from agora_tracker.config import IntelConfig, load_config

FIVE = ["0xf70da97812cb96acdf810712aa562db8dfa3dbef", "0xf8fe0956cc3e5c3df0da1085bf40b397318624e6",
        "0x00c18c85bf479ba2768d9faa7c02a39f2617592e", "0x75d2ae0861661ef7479e9229a3e3e2cdd14dfb08",
        "0xfffff389f8b090073efb9ac908323b2d5895f7d9"]


def test_blocked_wallet_leaves_the_scan_set_from_every_source(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    t.watchlist = {MANUAL: "mine"}
    store.sync_manual_watchlist(t.watchlist)
    store.watch_add(TG)
    lb.set([A, B, C, D])
    t.refresh_leaderboard()
    lb.set([A, B, D, E_])                                   # C is now a former top wallet
    t.refresh_leaderboard()
    assert t.tracked == {A, B, C, D, E_, MANUAL, TG}
    t.cfg.tracking.blocked_wallets = [B, C, MANUAL, TG]     # current top, former, manual, telegram
    t.refresh_leaderboard()
    assert t.tracked == {A, D, E_} and t.tracked_leaderboard == {A, D, E_}
    en = enrollment(store)
    assert en[B]["status"] == en[C]["status"] == "excluded"
    assert {r["excluded_reason"] for r in rows(
        store, "SELECT excluded_reason FROM wallet_enrollment WHERE address = ANY(:a)",
        a=[B, C])} == {"blocked"}
    assert en[A]["status"] == "active" and store.enrolled_addresses() == {A, D, E_}
    rep = store.tracking_report(scanned=t.tracked)
    assert rep["excluded_reasons"] == {"blocked": 2} and rep["ever_in_top"] == 5   # still counted


def test_block_survives_refreshes_and_is_reversible(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    lb.set([A, B, C, D])
    t.refresh_leaderboard()
    t.cfg.tracking.blocked_wallets = [B]
    for _ in range(3):                                      # B is still in the live top list
        t.refresh_leaderboard()
        assert B not in t.tracked
    t2, lb2 = make_tracker(store, monkeypatch)              # restart with the same setting
    t2.cfg.tracking.blocked_wallets = [B]
    lb2.set([A, B, C, D])
    t2.refresh_leaderboard()
    assert t2.tracked == {A, C, D}
    t2.cfg.tracking.blocked_wallets = []                    # operator takes it off the list
    assert store.sync_blocked(set()) == (0, 1)
    t2.refresh_leaderboard()
    assert t2.tracked == {A, B, C, D} and enrollment(store)[B]["status"] == "active"


def test_block_does_not_touch_contract_exclusions_or_history(store):
    snap(store, [A, B, C, D])
    store.put_is_contract(C, True)
    assert store.sync_exclusions(True) == (1, 0)
    n_trades = rows(store, "SELECT count(*) AS n FROM leaderboard_snapshot_entries")[0]["n"]
    assert store.sync_blocked({B}) == (1, 0) and store.sync_blocked({B}) == (0, 0)   # idempotent
    assert store.sync_exclusions(True) == (0, 0)            # the contract policy leaves B alone
    assert store.sync_blocked(set()) == (0, 1)              # ... and unblocking leaves C alone
    reasons = {r["address"]: r["excluded_reason"] for r in rows(
        store, "SELECT address, excluded_reason FROM wallet_enrollment")}
    assert reasons[C] == "contract" and reasons[B] is None
    assert rows(store, "SELECT count(*) AS n FROM leaderboard_snapshot_entries")[0]["n"] == n_trades


def test_no_leaderboard_alerts_for_a_blocked_wallet(store, monkeypatch):
    t, lb = make_tracker(store, monkeypatch)
    t.cfg.alerts.leaderboard_alerts = True
    lb.set([A, B, C, D])
    t.refresh_leaderboard()
    t.cfg.tracking.blocked_wallets = [E_]
    lb.set([A, B, D, E_])                                   # E enters the top while blocked
    t.refresh_leaderboard()
    assert E_ not in t.tracked
    sent = [r["text"] for r in rows(store, "SELECT text FROM notification_outbox")]
    assert any(C in x for x in sent)                        # C leaving the top still alerts
    assert not any(E_ in x for x in sent)                   # nothing about the blocked wallet


def test_env_and_compose_list_the_five_wallets_on_the_tracker_only(monkeypatch):
    svc = yaml.safe_load(open("docker-compose.yml"))["services"]
    listed = svc["tracker"]["environment"]["TRACKING_BLOCKED_WALLETS"].split()
    assert listed[:5] == FIVE and len(listed) == 32 == len(set(listed))   # + transfer-only groups
    assert all(len(a) == 42 and a == a.lower() and a.startswith("0x") for a in listed)
    # the tracker enforces the list; the read-only website only labels those wallets "blocked"
    # (the identical list, by YAML anchor); no other service sees it
    assert all("TRACKING_BLOCKED_WALLETS" not in (s.get("environment") or {})
               for name, s in svc.items() if name not in ("tracker", "web"))
    assert svc["web"]["environment"]["TRACKING_BLOCKED_WALLETS"].split() == listed
    monkeypatch.delenv("TRACKING_BLOCKED_WALLETS", raising=False)
    assert load_config("config.example.yaml").tracking.blocked_wallets == []
    monkeypatch.setenv("TRACKING_BLOCKED_WALLETS", f"{FIVE[4].upper().replace('0X', '0x')}, {FIVE[0]}")
    assert load_config("config.example.yaml").tracking.blocked_wallets == sorted([FIVE[0], FIVE[4]])
    monkeypatch.setenv("TRACKING_BLOCKED_WALLETS", "not-a-wallet")
    try:
        load_config("config.example.yaml")
        raise AssertionError("a malformed address must be rejected")
    except ValueError:
        pass


# --- the automatic rule is now for transfer-only machines ---------------------------------
def test_fast_wallet_that_really_trades_is_not_flagged(store):
    seed(store, BOT, 150, 1.0)                              # transfers only
    seed(store, HUMAN, 150, 1.0)                            # same timing ...
    with store.tx() as c:                                   # ... but most legs are real trades
        c.exec_driver_sql("UPDATE trades SET kind = 'BUY' WHERE wallet = %s AND id %% 10 <> 0",
                          (HUMAN,))
    ic = IntelConfig()
    assert ic.bot_max_trade_share == 0.05
    got = judged(store, max_trade_share=ic.bot_max_trade_share)
    assert set(got) == {BOT} and got[BOT]["trade_share"] == 0.0
    assert set(judged(store, max_trade_share=1.0)) == {BOT, HUMAN}   # the old, wider rule
    with store.tx() as c:                                   # a handful of decoded trades is fine
        c.exec_driver_sql("UPDATE trades SET kind = 'SELL' WHERE wallet = %s AND id IN "
                          "(SELECT id FROM trades WHERE wallet = %s ORDER BY id LIMIT 5)",
                          (BOT, BOT))
    assert set(judged(store, max_trade_share=ic.bot_max_trade_share)) == {BOT}
