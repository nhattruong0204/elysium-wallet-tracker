"""Trader performance badges on alert cards (owner, 2026-11-27): ranking rules and card design."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import sqlalchemy as sa
from test_cards import TOKEN, WALLET, Tags, payload

from agora_tracker import cards
from agora_tracker import schema as t
from agora_tracker.cards import Links
from agora_tracker.classify import TradeEvent
from agora_tracker.config import LinksConfig
from agora_tracker.performance import TIERS, Rules, build_board, load_board, tier_of

E18 = 10**18
NOW = datetime.now(timezone.utc)
A = lambda n: "0x" + f"{n:02x}" * 20    # noqa: E731


# --- ranking (pure) --------------------------------------------------------------------------
def row(w, pnl, closes=10, wins=7, tokens=4, p7=0.0, weeks=(1, None, -1, 1)):
    return (w, int(pnl * E18), int(p7 * E18), closes, wins, tokens,
            *[None if x is None else int(x * E18) for x in weeks])


def test_tiers():
    assert [tier_of(r)[0] if tier_of(r) else None for r in (1, 10, 11, 30, 31, 100, 101, None)] \
        == ["elite", "elite", "pro", "pro", "top100", "top100", None, None]
    assert [x[2] for x in TIERS] == ["👑", "💎", "🏅"]


def test_rank_by_pnl_only_among_qualified_winners():
    board = build_board([
        row(A(1), 50), row(A(2), 300), row(A(3), 120),
        row(A(4), 900, closes=4),                 # too few closed trades: not ranked
        row(A(5), 800, tokens=1),                 # one token: luck, not ranked
        row(A(6), -40, wins=2),                   # losing: numbers only
    ])
    assert [board[A(i)].get("rank") for i in (2, 3, 1)] == [1, 2, 3]
    assert all(board[A(i)]["of"] == 3 for i in (1, 2, 3))
    assert board[A(2)]["tier"] == "elite"
    for i in (4, 5, 6):
        assert "rank" not in board[A(i)] and "tier" not in board[A(i)]
    assert board[A(6)]["pnl"] == -40 and board[A(6)]["closes"] == 10
    assert board[A(1)]["weeks"] == [1, None, -1, 1]


def test_tier_boundaries_follow_rank():
    board = build_board([row(A(i), 1000 - i) for i in range(1, 106)])
    tiers = {board[A(i)]["rank"]: board[A(i)].get("tier") for i in range(1, 106)}
    assert tiers[10] == "elite" and tiers[11] == "pro" and tiers[30] == "pro"
    assert tiers[31] == "top100" and tiers[100] == "top100" and tiers[101] is None


# --- ranking on the database -----------------------------------------------------------------
def _seed(store):
    """W1 wins on an ordinary token; W2 made its money sniping a launch (bought 5 s after);
    W3 is muted; W4's win is on a skipped token; estimated PnL never counts."""
    W1, W2, W3, W4 = A(0x11), A(0x22), A(0x33), A(0x44)
    TOKS = [A(0x60 + i) for i in range(6)]
    launch = NOW - timedelta(days=3)
    with store.tx() as c:
        c.execute(sa.insert(t.token_profiles), [
            {"chain_id": store.chain_id, "address": tok, "symbol": f"T{i}", "name": f"T{i}",
             "launchpad": "eleos", "launched_at": launch} for i, tok in enumerate(TOKS)])
        n = 0

        def leg(w, tok, kind, hype, ts):
            nonlocal n
            n += 1
            ev = TradeEvent(kind, f"0x{n:064x}", 1000 + n, w, tok, 100 * E18,
                            int(Decimal(str(hype)) * E18), confidence="high",
                            hype_method="balance_diff")
            return store.insert_trade(c, ev, int(ts.timestamp()))

        def round_trip(w, tok, pnl, bought, status="measured"):
            leg(w, tok, "BUY", 1, bought)
            sid = leg(w, tok, "SELL", 1 + pnl, bought + timedelta(hours=1))
            store.record_realized(c, sid, w, tok, qty_sold=100 * E18,
                                  proceeds_wei=int((1 + pnl) * E18), cost_basis_wei=E18,
                                  pnl_wei=int(pnl * E18), status=status)
        later = launch + timedelta(hours=2)
        for i in range(6):                                  # W1: 6 wins on 3 tokens
            round_trip(W1, TOKS[i % 3], 10, later + timedelta(hours=i))
        for i in range(6):                                  # W2: launch snipes (5 s)
            round_trip(W2, TOKS[i % 3], 50, launch + timedelta(seconds=5 + i))
        for i in range(6):                                  # W3: muted wallet
            round_trip(W3, TOKS[i % 3], 99, later + timedelta(hours=i))
        for i in range(6):                                  # W4: wins only on a skipped token
            round_trip(W4, TOKS[5] if i < 3 else TOKS[3 + i % 2], 40 if i < 3 else 1,
                       later + timedelta(hours=i))
        round_trip(W1, TOKS[4], 500, later, status="estimated")   # not measured: ignored
        round_trip(W1, TOKS[4], 700, NOW - timedelta(days=40))    # outside the window
    return W1, W2, W3, W4, TOKS


def test_board_leaves_out_snipes_skipped_wallets_and_tokens(store):
    W1, W2, W3, W4, TOKS = _seed(store)
    with store.engine.connect() as c:
        board = load_board(c, chain_id=store.chain_id, skip_wallets={W3}, skip_tokens={TOKS[5]})
    assert board[W1]["rank"] == 1 and board[W1]["pnl"] == 60 and board[W1]["closes"] == 6
    assert board[W1]["tier"] == "elite" and board[W1]["weeks"][-1] == 1
    assert W2 not in board                       # every close was a launch snipe
    assert W3 not in board                       # muted / cohort / launcher wallets
    assert board[W4]["pnl"] == 3 and board[W4]["closes"] == 3 and board[W4].get("rank") is None
    loose = Rules(snipe_s=1)                     # snipe rule off: W2 becomes #1
    with store.engine.connect() as c:
        board = load_board(c, chain_id=store.chain_id, rules=loose, skip_wallets={W3})
    assert board[W2]["rank"] == 1 and board[W2]["pnl"] == 300


def test_store_and_engine_wiring(store):
    W1, _, W3, W4, TOKS = _seed(store)
    board = store.performance(Rules(), skip_wallets=(), skip_tokens=())
    assert [board[w]["rank"] for w in (W3, W4, W1)] == [1, 2, 3]     # nothing skipped
    board = store.performance(Rules(), skip_wallets=[W3], skip_tokens=[TOKS[5]])
    assert board[W1]["rank"] == 1 and W3 not in board


# --- card design -----------------------------------------------------------------------------
ELITE = {"rank": 3, "of": 52, "tier": "elite", "pnl": 278.4, "pnl_7d": 42.0, "closes": 24,
         "wins": 21, "tokens": 16, "weeks": [1, 1, None, 1], "days": 30}
LOSER = {"pnl": -42.5, "pnl_7d": -3.0, "closes": 16, "wins": 5, "tokens": 9,
         "weeks": [-1, -1, None, 1], "days": 30}


def card(perf, **kw):
    p = payload(**kw)
    if perf is not None:
        p["perf"] = perf
    text, _ = cards.render(p, Links(LinksConfig()), hype_usd=62.87)
    tags = Tags()
    tags.feed(text)
    assert not tags.stack, "unbalanced HTML"
    return text


def test_elite_buyer_gets_the_badge_first_and_a_banner():
    text = card(ELITE)
    first, *rest = text.split("\n")
    assert first.startswith("👑#3 ") and "BUY" in first          # notification preview
    body = "\n".join(rest)
    assert body.startswith("<blockquote>👑 <b>ELITE TRADER · #3</b> of 52 by 30d PnL")
    assert "💹 30d <b>+278 HYPE</b> ≈ +$17.50K · 7d +42.0" in body
    assert "🎯 88% win (21/24) · 🟩🟩⬜🟩</blockquote>" in body
    assert "PnL +$60.91K" not in text          # Agora's PnL left out: one PnL on the card
    assert ">Agora #3</a>" in text             # Agora's own rank, named as such


def test_each_tier_has_its_own_badge():
    for rank, badge, title in ((7, "👑", "ELITE"), (14, "💎", "PRO"), (64, "🏅", "TOP 100")):
        text = card({**ELITE, "rank": rank, "of": 120})
        assert text.startswith(f"{badge}#{rank} ")
        assert f"{badge} <b>{title} TRADER · #{rank}</b> of 120" in text


def test_unranked_wallets_get_one_line_and_a_warning_when_losing():
    text = card(LOSER, kind="SELL")
    assert not text.startswith(("👑", "💎", "🏅")) and "<blockquote>" not in text
    assert "⚠️ 30d −42.5 HYPE · 31% win (5/16) · 🟥🟥⬜🟩" in text
    text = card({**ELITE, "rank": 140, "tier": None})              # ranked, below the tiers
    assert "▫️ #140 · 30d +278 HYPE" in text and "<blockquote>" not in text


def test_no_record_no_noise():
    assert card(None) == card({"closes": 0, "pnl": 0, "pnl_7d": 0, "wins": 0, "weeks": []})
    assert "TRADER" not in card(None) and "win (" not in card(None)


def test_cluster_card_shows_who_is_ranked():
    p = {"type": "cluster", "token": TOKEN, "symbol": "LEASH", "min_hype": 0.5, "window_min": 10,
         "buyers": [{"wallet": A(1), "perf": {**ELITE, "rank": 14}},
                    {"wallet": A(2), "perf": ELITE}, {"wallet": A(3)}], "tx": "0x" + "1" * 64}
    text, _ = cards.render(p, Links(LinksConfig()))
    assert text.startswith("👑#3 🚨 <b>CLUSTER BUY</b>")
    assert "🏆 <b>👑#3 · 💎#14</b> among 3 buyers" in text
    assert "💎#14" in text and "30d +278 HYPE · 88% win" in text


def test_sell_card_of_a_ranked_wallet_keeps_its_pnl_line():
    text = card(ELITE, kind="SELL", realized_status="measured", realized_wei=str(5 * E18),
                realized_pct=40.0, sold_pct=100.0)
    assert text.startswith("👑#3 ") and "PnL: <b>" in text and "ELITE TRADER" in text
    assert WALLET[:6] in text


# --- engine: refresh, payload, failure, switch ------------------------------------------------
class _Store:
    """Just what Engine.performance/_wallet_ctx touch."""
    def __init__(self, board):
        self.board, self.calls, self.fail = board, [], False

    def performance(self, rules, *, skip_wallets, skip_tokens):
        self.calls.append((rules, set(skip_wallets), set(skip_tokens)))
        if self.fail:
            raise RuntimeError("db down")
        return self.board

    def trader(self, wallet):
        return None


def _engine(store, **cfg):
    from agora_tracker.config import AlertConfig, ChainConfig, IntelConfig
    from agora_tracker.engine import Engine
    return Engine(store, None, ChainConfig(rpc_urls=["http://rpc.test"]), AlertConfig(**cfg), {},
                  intel=IntelConfig(cohort_filter=False, launcher_filter=False),
                  muted_wallets=[A(9)])


def test_engine_attaches_the_record_and_survives_a_failed_refresh():
    st = _Store({A(1): ELITE})
    eng = _engine(st, mute_tokens=[TOKEN])
    assert eng._wallet_ctx(A(1))["perf"] == ELITE
    assert "perf" not in eng._wallet_ctx(A(2))
    rules, skip_w, skip_t = st.calls[0]
    assert rules.window_days == 30 and rules.snipe_s == 60 and rules.min_closes == 5
    assert A(9) in skip_w and TOKEN in skip_t            # muted wallets and tokens left out
    eng._wallet_ctx(A(1))
    assert len(st.calls) == 1                             # cached until perf_refresh_s
    st.fail, eng._perf_at = True, float("-inf")
    assert eng._wallet_ctx(A(1))["perf"] == ELITE         # failure keeps the last board


def test_switched_off_means_no_query_and_no_record():
    st = _Store({A(1): ELITE})
    eng = _engine(st, perf_badges=False)
    assert "perf" not in eng._wallet_ctx(A(1)) and st.calls == []


def test_environment_switch(tmp_path, monkeypatch):
    from pathlib import Path

    from agora_tracker.config import load_config
    cfg = tmp_path / "config.yaml"
    cfg.write_text((Path(__file__).resolve().parent.parent / "config.example.yaml").read_text())
    monkeypatch.delenv("ALERTS_PERF_BADGES", raising=False)
    assert load_config(cfg).alerts.perf_badges is True
    monkeypatch.setenv("ALERTS_PERF_BADGES", "off")
    assert load_config(cfg).alerts.perf_badges is False
