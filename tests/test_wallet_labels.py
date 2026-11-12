# ruff: noqa: F811
"""Wallet labels on Wallet intelligence: the rules (pure), the read-only feature queries, the
leaderboard filters, the page and the CSV export."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from test_dashboard_db import PRIV, PUB1, PUB2, ago, seeded  # noqa: F401
from test_web import make_cfg, make_live  # noqa: F401

from agora_tracker.classify import TradeEvent
from agora_tracker.leaderboard import Trader
from agora_tracker.web import intel as I
from agora_tracker.web.labels import (
    VERSION,
    LabelRules,
    bundle_partners,
    classify,
    label_kpis,
    unlabeled,
)

UTC = timezone.utc
E18 = 10 ** 18
NOW = datetime.now(UTC)
R = LabelRules()
API = "/api/v1/leaderboard"


def feat(**kw):
    base = {"legs": 0, "buys": 0, "sells": 0, "transfers": 0, "tokens": 0, "bought_tokens": 0,
            "active_days": 1, "med_buy_wei": None, "verified_buys": 0, "buy_wei": 0,
            "last_own_at": NOW - timedelta(hours=1), "launches": 0, "early": 0,
            "round_trips": 0, "med_hold_s": None, "top_buy_wei": 0, "internal": 0, "peers": 0,
            "bot_txs": 0, "bot_fast": None, "bot_trade_share": None, "closes": 0, "wins": 0,
            "pnl_wei": 0}
    base.update(kw)
    if "legs" not in kw:
        base["legs"] = base["buys"] + base["sells"] + base["transfers"]
    return base


def lab(f=None, **kw):
    return classify("0x" + "11" * 20, f, R, now=NOW, **kw)


# --- the rules (pure) -------------------------------------------------------------------------
def test_status_is_the_most_severe_flag_and_flags_keep_all():
    me = "0x" + "11" * 20
    co = {"id": "0x" + "0a" * 20, "size": 14, "launches": 21}
    x = lab(feat(buys=10, sells=10), blocked={me}, cohort=co,
            bundle={"partners": 2, "blocks": 9, "tokens": 4})
    assert x["label_status"] == "blocked" and x["label_flags"] == ["blocked", "cohort", "bundler"]
    assert x["cohort_id"] == co["id"] and x["cohort_size"] == 14
    assert any("14 wallets entering 21 launches" in r for r in x["label_reasons"])
    assert lab(feat(buys=10, sells=10), cohort=co)["label_status"] == "cohort"
    assert lab(feat(buys=3), enrollment="excluded")["label_status"] == "contract"
    assert lab(feat(buys=3), enrollment="excluded", blocked={me})["label_flags"] == ["blocked"]


def test_receipts_alone_are_dormant_not_active():
    spam = lab(feat(transfers=3, last_own_at=None))     # only airdrop TRANSFER_INs
    assert spam["label_status"] == "dormant" and spam["activity"] == "dormant"
    assert spam["style"] == "thin" and spam["label_flags"] == []
    assert lab(None)["label_status"] == "dormant"       # no legs at all in the lookback
    old = lab(feat(buys=9, sells=9, last_own_at=NOW - timedelta(days=10)))
    assert old["label_status"] == "organic" and old["activity"] == "cooling"


def test_automated_uses_the_trackers_rule():
    bot = feat(transfers=400, bot_txs=300, bot_fast=0.4, bot_trade_share=0.01)
    x = lab(bot)
    assert x["label_status"] == "automated" and x["style"] == "mover"
    assert lab({**bot, "bot_trade_share": 0.2})["label_status"] == "organic"   # it really trades
    assert lab({**bot, "bot_txs": 50})["label_status"] == "organic"


def test_farm_needs_volume_peers_and_share():
    f = feat(buys=20, sells=20, transfers=10, internal=8, peers=3)
    assert "farm" in lab(f)["label_flags"]
    assert "farm" not in lab({**f, "peers": 1})["label_flags"]
    assert "farm" not in lab({**f, "internal": 4})["label_flags"]
    assert "linked" in lab({**f, "internal": 4})["style_tags"]


@pytest.mark.parametrize("kw,style", [
    ({"buys": 10, "sells": 10, "launches": 10, "early": 6, "round_trips": 8, "med_hold_s": 120},
     "sniper"),
    ({"buys": 10, "sells": 10, "launches": 10, "early": 2, "round_trips": 8, "med_hold_s": 300},
     "scalper"),
    ({"buys": 10, "sells": 10, "round_trips": 8, "med_hold_s": 6 * 3600}, "swing"),
    ({"buys": 10, "sells": 10, "round_trips": 8, "med_hold_s": 5 * 86400}, "holder"),
    ({"buys": 12, "sells": 2}, "holder"),
    ({"buys": 10, "sells": 10}, "mixed"),
    ({"buys": 3, "sells": 2}, "thin"),
    ({"buys": 1, "transfers": 40}, "mover"),
])
def test_styles(kw, style):
    assert lab(feat(**kw))["style"] == style


def test_tags_size_and_edge():
    x = lab(feat(buys=60, sells=40, active_days=2, launches=10, early=3, bought_tokens=35,
                 verified_buys=60, buy_wei=100 * E18, top_buy_wei=60 * E18,
                 med_buy_wei=Decimal(7 * E18), closes=12, wins=8, pnl_wei=3 * E18))
    assert set(x["style_tags"]) == {"high_frequency", "early_buyer", "concentrated",
                                    "diversified"}
    assert x["size_tier"] == "mid" and x["edge"] == "proven"
    assert x["label_metrics"]["median_buy_hype"] == Decimal(7)
    assert lab(feat(buys=9, sells=9, closes=4, wins=1, pnl_wei=-E18))["edge"] == "losing"
    assert lab(feat(buys=9, sells=9, closes=2, pnl_wei=E18))["edge"] == "unmeasured"
    sizes = [lab(feat(buys=9, sells=9, med_buy_wei=Decimal(int(h * E18))))["size_tier"]
             for h in (0.5, 1, 6, 30, 150)]
    assert sizes == ["micro", "small", "mid", "large", "whale"]


def test_bundler_needs_the_same_partner_again_and_again():
    a, b = "0x" + "aa" * 20, "0x" + "bb" * 20
    rows = [(w, f"t{i % 3}", 100 + i) for i in range(5) for w in (a, b)]
    got = bundle_partners(rows, min_blocks=5, min_tokens=3)
    assert got[a] == {"partners": 1, "blocks": 5, "tokens": 3} and b in got
    # five blocks shared with five different wallets: no partner, no flag
    rot = [(a, f"t{i}", 200 + i) for i in range(5)] + \
          [("0x" + f"{i:02x}" * 20, f"t{i}", 200 + i) for i in range(5)]
    assert bundle_partners(rot, min_blocks=5, min_tokens=3) == {}
    assert bundle_partners(rows, min_blocks=5, min_tokens=4) == {}
    crowd = [(w, f"t{i % 3}", 300 + i) for i in range(5) for w in [a, b] +
             ["0x" + f"{k:02x}" * 20 for k in range(60)]]
    assert bundle_partners(crowd, min_blocks=5, min_tokens=3, max_block_wallets=50) == {}


def test_unlabeled_and_kpis():
    assert set(unlabeled()) == set(lab(None))
    assert label_kpis([{"label_status": "cohort"}, {"label_status": None},
                       {"label_status": "cohort"}])["cohort"] == 2


def test_params():
    p = I.parse_intel({"label": "Cohort", "style": "sniper", "flag": "clean"})
    assert (p.label, p.style, p.flag) == ("cohort", "sniper", "clean")
    for bad in ({"label": "x"}, {"style": "degen"}, {"flag": "'; drop"}):
        with pytest.raises(I.ParamError):
            I.parse_intel(bad)


# --- database, page, CSV ----------------------------------------------------------------------
SNIPER, BUN1, BUN2, MOVER, SPAM, BLOCK = ("0x" + h * 20 for h in ("c1", "c2", "c3", "c4", "c5",
                                                                  "c6"))


def tk(i: int) -> str:
    return "0x" + f"{0xBEEF0000 + i:040x}"


@pytest.fixture
def labelled(seeded):
    s = seeded
    s.record_snapshot([Trader(address=a, rank=i + 3, pnl=10.0) for i, a in
                       enumerate((SNIPER, BUN1, BUN2, MOVER, SPAM, BLOCK))] +
                      [Trader(address=PUB1, rank=1, pnl=500.0), Trader(address=PUB2, rank=2, pnl=1.0)],
                      source="agora", status="ok", complete=True, row_count=8)
    n = [0]

    def leg(c, kind, wallet, token, at, hype=1.0, block=None):
        n[0] += 1
        ev = TradeEvent(kind, f"0x{n[0]:064x}", block or 50_000 + n[0], wallet, token, 10 * E18,
                        int(Decimal(str(hype)) * E18) if kind in ("BUY", "SELL") else None,
                        confidence="high" if kind in ("BUY", "SELL") else "unknown",
                        hype_method="balance_diff" if kind in ("BUY", "SELL") else "none")
        s.insert_trade(c, ev, at)

    with s.tx() as c:
        leg(c, "BUY", PUB2, tk(99), ago(hours=24 * 6))          # history starts 6 days ago
        for i in range(6):                                      # the sniper opens 6 launches
            t0 = ago(hours=40 - i)
            leg(c, "BUY", SNIPER, tk(i), t0, hype=2)
            leg(c, "SELL", SNIPER, tk(i), t0 + 240, hype=3)
        for i in range(5):                                      # the same pair, same block
            for w in (BUN1, BUN2):
                leg(c, "BUY", w, tk(10 + i % 3), ago(hours=30 - i), block=90_000 + i)
        for i in range(25):
            leg(c, "TRANSFER_OUT", MOVER, tk(20), ago(hours=10, minutes=i))
        for i in range(3):
            leg(c, "TRANSFER_IN", SPAM, tk(30 + i), ago(hours=2))
        leg(c, "BUY", BLOCK, tk(40), ago(hours=3))
    return s


@pytest.fixture
def live(labelled, make_live):
    cfg = make_cfg()
    cfg.tracking.blocked_wallets = [BLOCK]
    return make_live(cfg)


def items(lv, **q):
    r = lv.get(API, params={"window": "all", "limit": 100, **q})
    assert r.status_code == 200, r.text
    return {w["wallet"]: w for w in r.json()["page"]["items"]}, r.json()


def test_labels_from_real_rows(live):
    got, j = items(live)
    assert got[SNIPER]["style"] == "sniper" and got[SNIPER]["label_status"] == "organic"
    assert got[SNIPER]["label_metrics"]["early_launches"] == 6
    assert got[BUN1]["label_status"] == got[BUN2]["label_status"] == "bundler"
    assert got[MOVER]["style"] == "mover" and got[MOVER]["activity"] == "active"
    assert got[SPAM]["label_status"] == "dormant" and got[SPAM]["activity"] == "dormant"
    assert got[BLOCK]["label_status"] == "blocked"
    assert j["label_kpis"]["bundler"] == 2 and j["labels_as_of"]
    assert j["label_rules"]["version"] == VERSION
    assert PRIV not in got and PRIV not in str(j)


def test_label_filters(live):
    assert set(items(live, label="bundler")[0]) == {BUN1, BUN2}
    assert set(items(live, style="sniper")[0]) == {SNIPER}
    assert set(items(live, flag="blocked")[0]) == {BLOCK}
    clean = items(live, flag="clean")[0]
    assert SNIPER in clean and not {BUN1, BUN2, BLOCK} & set(clean)
    assert live.get(API, params={"label": "nope"}).status_code == 400


def test_page_shows_labels_and_both_exports(live):
    html = live.get("/leaderboard", params={"window": "all"}).text
    assert "Labels:" in html and 'name="label"' in html and 'name="style"' in html
    assert ">bundler</a>" in html and "Export all wallets" in html
    assert 'href="/api/v1/leaderboard.csv?window=all"' in html
    assert "same-block buys on 3 tokens" in html                   # evidence on hover
    page = live.get(f"/leaderboard/{BUN1}").text
    assert "<h2>Labels" in page and "same-block buys on 3 tokens" in page


def test_csv_carries_every_wallet_and_label(live):
    r = live.get(f"{API}.csv", params={"window": "all"})
    assert r.status_code == 200 and "wallet-intelligence-all-" in r.headers["content-disposition"]
    got = {g["wallet"]: g for g in csv.DictReader(io.StringIO(r.text))}
    assert tuple(next(iter(got.values())).keys()) == I.CSV_COLUMNS
    assert {SNIPER, BUN1, BUN2, MOVER, SPAM, BLOCK, PUB1, PUB2} <= set(got)
    b = got[BUN1]
    assert b["label_status"] == "bundler" and b["label_flags"] == "bundler"
    assert b["label_version"] == VERSION and b["lb_bundle_blocks"] == "5"
    assert "same tracked wallet" in b["label_reasons"]
    assert got[SNIPER]["style"] == "sniper" and got[SNIPER]["lb_early_launches"] == "6"
    assert got[BLOCK]["label_flags"] == "blocked"
    only = list(csv.DictReader(io.StringIO(live.get(f"{API}.csv", params={
        "window": "all", "label": "bundler"}).text)))
    assert {g["wallet"] for g in only} == {BUN1, BUN2}


def test_a_failed_refresh_keeps_the_page_working(labelled, make_live, monkeypatch):
    from agora_tracker.web import service
    monkeypatch.setattr(service, "compute_labels", lambda *a, **k: 1 / 0)
    lv = make_live()
    got, j = items(lv)
    assert got[SNIPER]["label_status"] is None and j["labels_as_of"] is None
    assert lv.get("/leaderboard").status_code == 200
    assert lv.get(f"{API}.csv").status_code == 200
