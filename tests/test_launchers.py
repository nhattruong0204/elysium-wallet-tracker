# ruff: noqa: F811
"""Serial token launchers and the Consensus quality gate (review of 2026-11-19..26)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from conftest import CHAIN
from test_airdrop_filter import TS, alerted, leg, w
from test_launch_cohorts import _outbox, tok
from test_telegram_mute import eng  # noqa: F401
from test_tracker import E18

from agora_tracker import cards
from agora_tracker import schema as t
from agora_tracker.cards import Links
from agora_tracker.chain import TRANSFER_TOPIC, ScanResult
from agora_tracker.config import LinksConfig, load_config
from agora_tracker.consensus_delivery import skip_reason
from agora_tracker.launchers import (
    ZERO_TOPIC,
    find_launchers,
    load_launchers,
    minted_tokens,
)
from agora_tracker.web.consensus import operators, same_size_groups
from agora_tracker.web.labels import LabelRules, classify

NOW = datetime.now(timezone.utc)
LAUNCHER, SNIPER, FEW = w(1), w(2), w(3)


# --- the rule (pure) ----------------------------------------------------------------------------
def test_three_launches_make_a_launcher_and_only_its_tokens_count():
    rows = [(LAUNCHER, tok(i)) for i in range(3)] + [(FEW, tok(10)), (FEW, tok(11))]
    found = find_launchers(rows, min_launches=3)
    assert found.wallets == {LAUNCHER: 3, FEW: 2}
    assert found.is_launcher(LAUNCHER, 3) and not found.is_launcher(FEW, 3)
    assert found.tokens == {tok(0), tok(1), tok(2)}               # FEW's tokens are not flagged


def test_a_token_minted_in_the_buy_transaction_is_detected():
    me = tok(5)
    rcpt = {"logs": [
        {"address": me.upper().replace("0X", "0x"), "topics": [TRANSFER_TOPIC, ZERO_TOPIC, "0x1"]},
        {"address": tok(6), "topics": [TRANSFER_TOPIC, "0x" + "1" * 64, "0x2"]},   # a transfer
        {"address": tok(7), "topics": ["0xdeadbeef", ZERO_TOPIC]},                 # not Transfer
    ]}
    assert minted_tokens(rcpt, TRANSFER_TOPIC) == {me}
    assert minted_tokens({}, TRANSFER_TOPIC) == set()


# --- stored rows (database) ---------------------------------------------------------------------
def seed(store, rows, profiles):
    """rows: (wallet, token, kind, seconds after NOW-1d); profiles: token -> launch offset (s)."""
    base = NOW - timedelta(days=1)
    with store.tx() as c:
        for i, (wal, token, kind, off) in enumerate(rows):
            h = f"0x{i:064x}"
            c.execute(sa.insert(t.transactions).values(chain_id=CHAIN, tx_hash=h, block_number=100 + i))
            c.execute(sa.insert(t.trades).values(
                chain_id=CHAIN, tx_hash=h, wallet=wal, token=token, kind=kind, confidence="high",
                hype_method="balance_diff", qty=E18, hype_wei=E18, block_number=100 + i,
                block_time=base + timedelta(seconds=off)))
        for token, off in profiles.items():
            c.execute(sa.insert(t.token_profiles).values(
                chain_id=CHAIN, address=token, symbol="T", launchpad="eleos",
                launched_at=base + timedelta(seconds=off)))


def test_the_database_rule_finds_the_launcher_and_not_the_late_buyers(store):
    rows, prof = [], {}
    for i in range(4):
        prof[tok(i)] = i * 600
        rows += [(LAUNCHER, tok(i), "BUY", i * 600), (SNIPER, tok(i), "BUY", i * 600 + 40),
                 (LAUNCHER, tok(i), "SELL", i * 600 + 60)]
    prof[tok(20)] = 9000                     # FEW: launch-second buys on 2 tokens only
    prof[tok(21)] = 9600
    rows += [(FEW, tok(20), "BUY", 9000), (FEW, tok(21), "BUY", 9601)]
    prof[tok(30)] = 12000                    # bought in the launch second but NOT the first trade
    rows += [(SNIPER, tok(30), "BUY", 11000), (w(9), tok(30), "BUY", 12000)]
    seed(store, rows, prof)
    with store.engine.connect() as c:
        found = load_launchers(c, chain_id=CHAIN, lookback_days=7, window_s=2, min_launches=3)
    assert found.wallets == {LAUNCHER: 4, FEW: 2}
    assert found.tokens == {tok(i) for i in range(4)}
    with store.engine.connect() as c:
        old = load_launchers(c, chain_id=CHAIN, lookback_days=0.5, window_s=2, min_launches=3)
    assert old.wallets == {}                                       # outside the lookback


# --- tracker alerts -----------------------------------------------------------------------------
def apply(eng, events, block=10, ts=TS, creator=()):
    eng.apply_range(ScanResult(events=events, block_times={block: ts}, creator_legs=set(creator)),
                    block)


def test_creator_buys_and_the_dump_are_not_alerted_nor_others_on_that_token(store, eng):
    me = tok(1)
    buy = leg("BUY", me, LAUNCHER, "0x" + "a" * 64, hype=50 * E18)
    apply(eng, [buy], creator={(buy.tx, LAUNCHER, me)})
    apply(eng, [leg("BUY", me, SNIPER, "0x" + "b" * 64, hype=5 * E18, block=11),
                leg("SELL", me, LAUNCHER, "0x" + "c" * 64, hype=80 * E18, block=11)], block=11)
    apply(eng, [leg("BUY", tok(2), SNIPER, "0x" + "d" * 64, hype=5 * E18, block=12)], block=12)
    assert alerted(store, me) == []                                # creator, its dump, the sniper
    assert alerted(store, tok(2)) == [(SNIPER, "BUY")]             # unrelated token: alerted
    with store.engine.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM trades WHERE token = :t"),
                         {"t": me}).scalar() == 3                  # all indexed


def test_warn_mode_and_switch_off(store, eng):
    me = tok(1)
    eng.intel.launcher_tokens = "warn"
    buy = leg("BUY", me, LAUNCHER, "0x" + "a" * 64, hype=50 * E18)
    apply(eng, [buy], creator={(buy.tx, LAUNCHER, me)})
    apply(eng, [leg("BUY", me, SNIPER, "0x" + "b" * 64, hype=5 * E18, block=11)], block=11)
    out = [r["payload"] for r in _outbox(store) if r["payload"].get("type") == "trade"]
    assert [(p["wallet"], p.get("launcher_token")) for p in out] == [(SNIPER, True)]
    text, _ = cards.render_trade(out[0], Links(LinksConfig()))
    assert "serial token launcher" in text
    eng.intel.launcher_filter = False
    apply(eng, [leg("BUY", tok(3), LAUNCHER, "0x" + "e" * 64, hype=50 * E18, block=12)],
          block=12, creator={("0x" + "e" * 64, LAUNCHER, tok(3))})
    assert alerted(store, tok(3)) == [(LAUNCHER, "BUY")]               # old behaviour


def test_a_stored_rule_launcher_gets_no_alerts_and_does_not_count_as_independent(store, eng):
    rows, prof = [], {}
    for i in range(3):
        prof[tok(i)] = i * 600
        rows.append((LAUNCHER, tok(i), "BUY", i * 600))
    seed(store, rows, prof)
    eng._launchers_at = float("-inf")
    apply(eng, [leg("BUY", tok(40), LAUNCHER, "0x" + "f" * 64, hype=5 * E18, block=13)], block=13)
    assert alerted(store, tok(40)) == []
    assert LAUNCHER in eng.cohort_wallets() and eng.cohort_wallets()[LAUNCHER] == "launcher"
    score = eng._score([(LAUNCHER, 1, 100, E18), (w(40), 2, 101, E18)], 1800)
    assert score["independent"] == 1


def test_config_defaults_and_env(monkeypatch):
    for k in ("ALERTS_LAUNCHER_FILTER", "ALERTS_LAUNCHER_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    ic = load_config("config.example.yaml").intel
    assert (ic.launcher_filter, ic.launcher_min, ic.launcher_window_s, ic.launcher_tokens) == \
        (True, 3, 2.0, "mute")
    monkeypatch.setenv("ALERTS_LAUNCHER_FILTER", "off")
    monkeypatch.setenv("ALERTS_LAUNCHER_TOKENS", "warn")
    ic = load_config("config.example.yaml").intel
    assert ic.launcher_filter is False and ic.launcher_tokens == "warn"


# --- website label ------------------------------------------------------------------------------
def test_launcher_label_ranks_above_cohort():
    x = classify(LAUNCHER, {"legs": 40, "buys": 20, "sells": 20, "last_own_at": NOW}, LabelRules(),
                 now=NOW, launches=12, launcher_min=3,
                 cohort={"id": LAUNCHER, "size": 5, "launches": 6})
    assert x["label_status"] == "launcher" and x["label_flags"][:2] == ["launcher", "cohort"]
    assert x["flag_confidence"]["launcher"] == "high"
    assert any("12 tokens in the second they were launched" in r for r in x["label_reasons"])
    assert "launcher" not in classify(SNIPER, {"legs": 40, "buys": 20, "sells": 20,
                                               "last_own_at": NOW}, LabelRules(), now=NOW,
                                      launches=2)["label_flags"]


# --- Consensus ----------------------------------------------------------------------------------
def test_same_size_buyers_count_as_one_operator():
    net = {w(i): int(1.30 * E18) for i in range(5)} | {w(10): 5 * E18, w(11): 13 * E18}
    assert set(same_size_groups(net, list(net)).values()) == {0}
    assert len(same_size_groups(net, list(net))) == 5
    buyers = [(w(i), 100 + i) for i in range(5)] + [(w(10), 200), (w(11), 300)]
    assert operators(buyers, set(), net) == 3                      # one script + two traders
    varied = {w(1): 5 * E18, w(2): int(4.07 * E18), w(3): 13 * E18}
    assert same_size_groups(varied, list(varied)) == {}
    two = {w(1): E18, w(2): E18}                                   # two equal buys: coincidence
    assert same_size_groups(two, list(two)) == {}
    at = {w(i): NOW + timedelta(seconds=3 * i) for i in range(5)}
    assert len(same_size_groups(net, list(net), at)) == 5          # 12 s apart: one script
    apart = {w(i): NOW + timedelta(minutes=4 * i) for i in range(5)}
    assert same_size_groups(net, list(net), apart) == {}           # 5 HYPE each, minutes apart


def card(**kw):
    base = {"independent": 3, "traders": 3, "cohort_wallets": 0, "launcher_wallets": 0,
            "launcher_token": False, "mcap_at_usd": 24_570.0}
    return {**base, **kw}


def test_quality_gate_on_the_cards_of_that_week():
    # posted then, and how they went: (card, posted now?)
    week = {
        "PERP 3 indep $24.6k (7.9x)": (card(), True),
        "LQDS 3 indep $22k (5.5x)": (card(mcap_at_usd=21_900.0), True),
        "MUSHROOM 3 indep $4k (-27%)": (card(mcap_at_usd=4_030.0), False),
        "e/acc 5 traders 1 indep, MC unknown (-31%)": (card(independent=1, traders=5,
                                                            mcap_at_usd=None), False),
        "MONTRADAR 1 indep, MC unknown": (card(independent=1, mcap_at_usd=None), False),
        "2 indep, MC unknown": (card(independent=2, mcap_at_usd=None), False),
        "3 indep, MC unknown": (card(mcap_at_usd=None), True),
        "launcher among 3": (card(launcher_wallets=1), True),
        "launcher among 2": (card(independent=2, launcher_wallets=1), False),
        "token created by a launcher": (card(launcher_token=True), False),
        "two cohort wallets": (card(cohort_wallets=2), False),
    }
    for name, (c, posted) in week.items():
        assert (skip_reason(c) is None) == posted, (name, skip_reason(c))
    assert "one operator" in skip_reason(card(independent=1))


def test_the_scanner_marks_a_buy_in_the_minting_transaction(store):
    from test_tracker import POOL, TOKEN, W1, W2, FakeChain, build

    class MintChain(FakeChain):
        def call(self, method, params):
            out = super().call(method, params)
            if method == "eth_getTransactionReceipt" and params[0] == "0xa":
                out = {**out, "logs": [{"address": TOKEN, "topics": [TRANSFER_TOPIC, ZERO_TOPIC,
                                                                    "0x" + "0" * 24 + POOL[2:]]}]}
            return out

    ch = MintChain()
    ch.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 1000 * E18)],
              bal={W1: (10 * E18, 9 * E18 - 10 ** 15)})
    ch.add_tx("0xb", 12, W2, value=E18, transfers=[(POOL, W2, 500 * E18)],
              bal={W2: (5 * E18, 4 * E18 - 10 ** 15)})
    _, follower, _ = build(store, ch)
    res = follower.scan(1, 15, {W1, W2})
    assert res.creator_legs == {("0xa", W1, TOKEN)}                 # W2 bought later: a buyer


def test_heavy_cached_queries_get_a_longer_timeout(store):
    from agora_tracker.reports import Reports
    rep = Reports.__new__(Reports)
    rep.engine = store.engine
    rep.ui = type("U", (), {"query_timeout_ms": 5000})()
    with rep.ro() as c:
        assert c.execute(sa.text("SHOW statement_timeout")).scalar() == "5s"
    with rep.ro(30_000) as c:
        assert c.execute(sa.text("SHOW statement_timeout")).scalar() == "30s"
