"""PAPER book (paperbook.py): rules, simulated fills, exits, risk limits, death, persistence and
the engine hook. Montra detail shapes are the API's own (api.montramarkets.xyz, 2026-11-28)."""
from __future__ import annotations

import copy
import json

import pytest
from test_tracker import TOKEN, W1, W2, W3, build, chain, run_all  # noqa: F401

from agora_tracker import cards
from agora_tracker.cards import Links
from agora_tracker.config import LinksConfig, MarketConfig, PaperBookConfig, load_config
from agora_tracker.paperbook import (
    PaperBook,
    Signal,
    entry_check,
    equity,
    exit_rule,
    fill_buy,
    fill_sell,
    new_book,
    pre_filter,
    quote_from,
    reason_code,
)

T0 = 1_795_860_000.0
TOK = "0x" + "ab" * 20
LEADER = "0x" + "c4" * 20
HYPE_USD = 63.0
CFG = PaperBookConfig(enabled=True)


def detail(price=0.0001, mcap=50_000.0, fee=20_000, age_s=3 * 3600, live_buy=None,
           early=None, symbol="GOOD"):
    """A Montra /api/tokens/<token> answer: card fields + pool terms + live read."""
    pool = {"totalFeePips": fee, "earlyBuyFeeStartPips": 0, "earlyBuyFeeWindow": 0,
            "earlyBuyFeeUntil": 0}
    if early:
        start, window, until = early
        pool.update(earlyBuyFeeStartPips=start, earlyBuyFeeWindow=window,
                    earlyBuyFeeUntil=int(until * 1000))
    return {"address": TOK, "symbol": symbol, "priceUsd": price, "marketCapUsd": mcap,
            "totalFeePips": fee, "launchedAt": int((T0 - age_s) * 1000),
            "quote": {"symbol": "HYPE", "native": True, "usd": HYPE_USD}, "pool": pool,
            "live": {"totalFee": fee, "buyEffectiveTotalFee": str(live_buy or fee)}}


def payload(kind="BUY", wallet=LEADER, rank=4, hype=10.0, tokens=6_000_000.0, ts=T0 - 10,
            tx="0x" + "1" * 64, **kw):
    """The engine's trade alert payload (agora_tracker/engine.py _trade_payload)."""
    p = {"type": "trade", "v": 2, "kind": kind, "tx": tx, "block": 1, "ts": ts, "token": TOK,
         "symbol": "GOOD", "decimals": 18, "qty": str(int(tokens * 10**18)),
         "hype_wei": str(int(hype * 10**18)), "confidence": "high", "wallet": wallet,
         "label": None, "pos_qty": str(int(tokens * 10**18)),
         "perf": {"rank": rank, "tier": "elite" if rank and rank <= 10 else "pro"}}
    p.update(kw)
    return p


class FakeStore:
    def __init__(self):
        self.kv, self.cards, self.qty = {}, [], {}

    def get_kv(self, k):
        return self.kv.get(k)

    def set_kv(self, k, v):
        self.kv[k] = v

    def enqueue_now(self, key, text, severity="info", payload=None):
        assert key not in {c[0] for c in self.cards}, f"duplicate card key {key}"
        self.cards.append((key, text, payload))

    def position_qty(self, wallet, token):
        return self.qty.get((wallet, token))


class FakeMontra:
    def __init__(self):
        self.d = {TOK: detail()}

    def card(self, token):
        return copy.deepcopy(self.d.get(token))


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make(cfg=CFG, store=None, montra=None, clock=None):
    store, montra, clock = store or FakeStore(), montra or FakeMontra(), clock or Clock()
    book = PaperBook(store, cfg, MarketConfig(), lambda: HYPE_USD, tz="Asia/Ho_Chi_Minh",
                     now=clock, montra=montra)
    return book, store, montra, clock


def kinds(store):
    return [c[2]["kind"] for c in store.cards]


# --- prices and fees ------------------------------------------------------------------------
def test_quote_reads_the_pools_fee_and_an_active_anti_snipe_window():
    q = quote_from(detail(fee=20_000), T0)
    assert q.pool_fee == pytest.approx(0.02) and q.buy_fee == pytest.approx(0.02)
    assert q.price_usd == 0.0001 and q.mcap_usd == 50_000 and q.launched_at == T0 - 3 * 3600
    # the live read says a buy pays more than the pool fee: the window is open
    assert quote_from(detail(live_buy=400_000), T0).buy_fee == pytest.approx(0.40)
    # pool terms alone: 99% start, 300 s window, half of it left -> ~50.5%
    q = quote_from(detail(fee=10_000, early=(990_000, 300, T0 + 150)), T0)
    assert q.buy_fee == pytest.approx(0.01 + 0.98 * 0.5)
    assert quote_from(detail(fee=10_000, early=(990_000, 300, T0 - 1)), T0).buy_fee == 0.01
    assert quote_from({**detail(), "priceUsd": None}, T0) is None
    assert quote_from(None, T0) is None


def test_fills_pay_fee_slippage_and_gas_both_ways():
    q = quote_from(detail(price=0.0001, fee=20_000), T0)
    tokens, fees = fill_buy(10.0, q, CFG)
    assert tokens == pytest.approx((9.98 * 0.98) / (0.0001 * 1.005))
    assert fees == pytest.approx(9.98 * 0.02 + 0.02)
    back, sell_fees = fill_sell(tokens, 0.0001, 0.02, CFG)
    assert back == pytest.approx(tokens * 0.0001 * 0.995 * 0.98 - 0.02)
    assert 0.94 < back / 10 < 0.96          # a 2% pool costs ~5% round trip, all in


# --- entry rules ----------------------------------------------------------------------------
def test_signal_from_the_engines_payload():
    s = Signal.from_payload(payload())
    assert (s.kind, s.token, s.wallet, s.rank, s.hype) == ("BUY", TOK, LEADER, 4, 10.0)
    assert s.leader_px_usd(HYPE_USD) == pytest.approx(10 * HYPE_USD / 6_000_000)
    assert Signal.from_payload({"type": "cluster"}) is None


@pytest.mark.parametrize("change, why", [
    (dict(rank=31), "leader not ranked top 30"),
    (dict(rank=None), "leader not ranked top 30"),
    (dict(launcher_token=True), "token made by a serial launcher"),
    (dict(cohort_wallets=2), "launch-cohort wallets on the token"),
    (dict(hype=0.6), "leader's buy under 1 HYPE"),
    (dict(ts=T0 - 600), "signal older than 120 s"),
    (dict(kind="SELL"), "not a buy"),
    (dict(confidence="estimated"), "buy amount not measured"),
])
def test_pre_filter_reasons(change, why):
    p = payload(**{k: v for k, v in change.items() if k in ("rank", "hype", "ts", "kind")})
    if "launcher_token" in change:
        p["launcher_token"] = True
    if "cohort_wallets" in change:
        p["cohort_wallets"] = 2
    if "confidence" in change:
        p["confidence"] = "estimated"
    if change.get("rank", 0) is None:
        p["perf"] = {}
    assert pre_filter(Signal.from_payload(p), new_book(CFG, T0), CFG, T0) == why


def test_pre_filter_book_limits():
    s = Signal.from_payload(payload())
    b = new_book(CFG, T0)
    b["open"] = {f"0x{i:040x}": {} for i in range(5)}
    assert pre_filter(s, b, CFG, T0) == "5 positions open"
    b = new_book(CFG, T0)
    b["open"] = {f"0x{i:040x}": {"leader": LEADER} for i in range(2)}
    assert pre_filter(s, b, CFG, T0) == "2 positions already copied from this wallet"
    b = new_book(CFG, T0)
    b["cooldown"][TOK] = T0 + 60
    assert pre_filter(s, b, CFG, T0) == "token in cooldown after an exit"
    b = new_book(CFG, T0)
    b["day"] = {"date": "x", "start_equity": 100, "halted": True}
    assert pre_filter(s, b, CFG, T0) == "daily loss limit hit"
    b = new_book(CFG, T0)
    b["status"] = "dead"
    assert pre_filter(s, b, CFG, T0) == "book is dead"


@pytest.mark.parametrize("d, why", [
    (dict(mcap=8_000), "market cap $8.00K under $10.00K"),
    (dict(age_s=60), "token younger than 3 min"),
    (dict(fee=50_000), "pool fee 5% above 2%"),
    (dict(live_buy=300_000), "anti-snipe fee active (30.0% on a buy)"),
    (dict(price=0.0002), "price +90.5% above the leader's fill"),
])
def test_entry_checks_on_montras_figures(d, why):
    s = Signal.from_payload(payload())
    size, msg = entry_check(s, quote_from(detail(**d), T0), new_book(CFG, T0), CFG, T0, HYPE_USD)
    assert size is None and msg == why


def test_entry_sizes_ten_percent_of_equity_and_never_more_than_cash():
    s = Signal.from_payload(payload())
    b = new_book(CFG, T0)
    q = quote_from(detail(price=0.000105), T0)      # +0% vs the leader's 0.000105 fill
    assert entry_check(s, q, b, CFG, T0, HYPE_USD) == (10.0, "ok")
    b["cash"] = 7.0                                  # 10% of $7 is under the $5 minimum
    assert entry_check(s, q, b, CFG, T0, HYPE_USD) == (5.0, "ok")
    b["cash"] = 4.0
    assert entry_check(s, q, b, CFG, T0, HYPE_USD) == (None, "not enough cash")
    assert entry_check(s, None, b, CFG, T0, HYPE_USD)[1] == "not a Montra token (or no dollar price)"


def test_reason_codes_are_stable_counters():
    assert reason_code("market cap $8.00K under $10.00K") == "market cap under"
    assert reason_code("price +90.5% above the leader's fill") == "price above the leader's"
    assert reason_code("5 positions open") == "positions open"


# --- exits ----------------------------------------------------------------------------------
def test_exit_rules():
    pos = {"entry_px": 1.0, "peak_px": 1.0, "tp_done": False, "opened_at": T0}
    assert exit_rule(pos, 0.9, CFG, T0) is None
    assert exit_rule(pos, 0.64, CFG, T0)[0] == 1.0                        # stop -35%
    assert exit_rule(pos, 2.0, CFG, T0) == (0.5, "take profit (+100.0%), half off")
    pos.update(tp_done=True, peak_px=3.0)
    assert exit_rule(pos, 2.2, CFG, T0) is None
    assert exit_rule(pos, 2.09, CFG, T0)[1].startswith("trailing stop")    # -30% from 3.0
    assert exit_rule(pos, 2.5, CFG, T0 + 72 * 3600)[1] == "time stop (72 h)"


# --- the book end to end --------------------------------------------------------------------
def test_buy_take_profit_trailing_exit_and_the_cards():
    book, store, montra, clock = make()
    assert kinds(store) == ["born"]
    montra.d[TOK] = detail(price=0.000105)
    book.handle(payload())
    pos = book.book["open"][TOK]
    assert book.book["cash"] == 90.0 and pos["cost"] == 10.0 and pos["rank"] == 4
    key, text, card = store.cards[-1]
    assert card["kind"] == "open" and card["token"] == TOK and "PAPER BUY" in text
    assert "ELITE #4" in text and "Stop $0.0000682" in text
    # +110%: half off, the rest trails
    clock.t += 3600
    montra.d[TOK] = detail(price=0.0002205)
    book.poll()
    assert kinds(store)[-1] == "partial" and pos["tp_done"]
    assert book.book["cash"] > 90 + 9
    # peak 0.0003, then -31% from it: trailing stop closes the rest
    clock.t += 600
    montra.d[TOK] = detail(price=0.0003)
    book.poll()
    clock.t += 600
    montra.d[TOK] = detail(price=0.000207)
    book.poll()
    assert kinds(store)[-1] == "close" and TOK not in book.book["open"]
    st = book.book["stats"]
    assert st["trades"] == 1 and st["wins"] == 1 and st["realized"] > 8
    assert book.book["cooldown"][TOK] == pytest.approx(clock.t + 24 * 3600)
    eq = equity(book.book, CFG)
    assert eq == pytest.approx(book.book["cash"]) and eq > 108
    # the same token again: cooldown
    book.handle(payload(tx="0x" + "2" * 64, ts=clock.t - 5))
    assert TOK not in book.book["open"]


def test_leader_sell_closes_at_the_price_after_their_sell():
    book, store, montra, clock = make()
    montra.d[TOK] = detail(price=0.000105)
    book.handle(payload())
    clock.t += 300
    montra.d[TOK] = detail(price=0.00009)
    book.handle(payload(kind="SELL", tx="0x" + "3" * 64, ts=clock.t - 3))
    closed = book.book["closed"][-1]
    assert closed["reason"] == "leader sold" and closed["exit_px"] == 0.00009
    assert closed["pnl"] < 0 and book.book["stats"]["losses"] == 1
    assert "trade #1 closed" in store.cards[-1][1]
    # someone else's sell does nothing
    book.handle(payload(tx="0x" + "4" * 64, ts=clock.t - 1, wallet="0x" + "d5" * 20, rank=2))
    book.handle(payload(kind="SELL", tx="0x" + "5" * 64, wallet="0x" + "e6" * 20))


def test_leader_position_check_catches_a_sell_too_small_to_alert():
    book, store, montra, clock = make()
    montra.d[TOK] = detail(price=0.000105)
    book.handle(payload())
    store.qty[(LEADER, TOK)] = 6_000_000 * 10**18
    book.poll()
    assert TOK in book.book["open"]
    store.qty[(LEADER, TOK)] = 2_000_000 * 10**18           # sold two thirds, under the radar
    clock.t += 60
    book.poll()
    assert book.book["closed"][-1]["reason"] == "leader sold (position check)"


def test_stop_loss_daily_limit_and_death():
    cfg = PaperBookConfig(enabled=True, max_open=5)
    book, store, montra, clock = make(cfg)
    toks = []
    for i in range(5):                                   # five positions, $10 each... nearly
        t = f"0x{i + 1:040x}"
        toks.append(t)
        montra.d[t] = {**detail(price=0.000105), "address": t, "symbol": f"T{i}"}
        p = payload(tx=f"0x{i:064x}", wallet=f"0x{i + 1:02x}" + "c4" * 19)
        p["token"] = t
        book.handle(p)
    assert len(book.book["open"]) == 5
    # everything -50%: stops fire; equity drops below the day's -20% limit
    clock.t += 120
    for t in toks:
        montra.d[t] = {**detail(price=0.0000525), "address": t}
    book.poll()
    assert book.book["open"] == {} and book.book["stats"]["losses"] == 5
    assert "halt" in kinds(store) and book.book["day"]["halted"]
    eq = equity(book.book, cfg)
    assert 70 < eq < 80
    # death: the floor is reached through one more catastrophic position
    book.book["day"]["halted"] = False
    book.book["cash"] = 55.0
    t = toks[0]
    book.book["cooldown"].pop(t)
    montra.d[t] = {**detail(price=0.000105), "address": t}
    p = payload(tx="0x" + "9" * 64, ts=clock.t - 5)
    p["token"] = t
    book.handle(p)
    assert t in book.book["open"]
    clock.t += 60
    montra.d[t] = {**detail(price=0.00000001), "address": t}
    book.poll()
    assert book.book["status"] == "dead" and kinds(store)[-1] == "dead"
    assert book.book["open"] == {}
    n = len(store.cards)
    book.handle(payload(tx="0x" + "8" * 64, ts=clock.t))   # dead: nothing happens
    book.poll()
    assert len(store.cards) == n


def test_book_survives_a_restart_and_a_new_name_is_a_new_life():
    book, store, montra, clock = make()
    montra.d[TOK] = detail(price=0.000105)
    book.handle(payload())
    again, *_ = make(store=store, montra=montra, clock=clock)
    assert again.book["open"][TOK]["cost"] == 10.0 and again.book["cash"] == 90.0
    assert kinds(store).count("born") == 1
    other, *_ = make(PaperBookConfig(enabled=True, book="life-2"), store=store, montra=montra,
                     clock=clock)
    assert other.book["cash"] == 100.0 and other.book["open"] == {}
    assert json.loads(store.kv["paper:life-1"])["open"][TOK]["leader"] == LEADER


def test_daily_report_once_per_local_day():
    book, store, montra, clock = make()
    book.poll()                                          # first day: no report yet
    assert "daily" not in kinds(store)
    clock.t += 86400
    book.poll()
    book.poll()
    assert kinds(store).count("daily") == 1
    assert "Renewal check in 29 days" in store.cards[-1][1]


def test_skips_are_counted_for_the_daily_report():
    book, store, montra, clock = make()
    montra.d[TOK] = detail(price=0.000105, mcap=4_000)
    book.handle(payload())
    assert book.book["stats"]["skipped"] == {"market cap under": 1}
    assert TOK not in book.book["open"]


# --- wiring ---------------------------------------------------------------------------------
def test_paper_cards_keep_the_fomo_button():
    links = Links(LinksConfig())
    out = cards.render({"type": "paper", "text": "🧪 hello", "token": TOK}, links)
    assert out == ("🧪 hello", {})
    kb = cards.alert_keyboard(links, {"type": "paper", "token": TOK}, out[1])
    assert kb and "Fomo" in json.dumps(kb)


def test_engine_calls_listeners_after_commit(store, chain):  # noqa: F811
    store, follower, engine = build(store, chain)
    got = []

    def boom(p):
        raise RuntimeError("listener bug")
    engine.trade_listeners += [boom, got.append]          # a failing listener changes nothing
    run_all(follower, engine, {W1, W2, W3})
    assert got and {p["kind"] for p in got} <= {"BUY", "SELL"}
    assert {p["type"] for p in got} == {"trade"}
    rows = [r for r in store.pending_alerts(500) if (r["payload"] or {}).get("type") == "trade"
            and r["payload"].get("kind") in ("BUY", "SELL")]
    assert len(rows) == len(got)                          # exactly the committed alerts
    assert any(p["token"] == TOKEN for p in got)


def test_config_switch(tmp_path, monkeypatch):
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text()
    p = tmp_path / "config.yaml"
    p.write_text(src)
    monkeypatch.delenv("PAPER_BOOK", raising=False)
    monkeypatch.delenv("PAPER_BOOK_NAME", raising=False)
    assert load_config(p).paper.enabled is False
    monkeypatch.setenv("PAPER_BOOK", "on")
    monkeypatch.setenv("PAPER_BOOK_NAME", "Life-2")
    cfg = load_config(p)
    assert cfg.paper.enabled is True and cfg.paper.book == "life-2"
    with pytest.raises(ValueError, match="paper.book"):
        PaperBookConfig(book="bad name!")
    with pytest.raises(ValueError, match="death_equity_usd"):
        PaperBookConfig(death_equity_usd=150)
