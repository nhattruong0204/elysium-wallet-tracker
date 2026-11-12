# ruff: noqa: F811
"""Coordinated launch cohorts: detection rule, tracker alerts, website labels, Consensus skip."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from conftest import CHAIN
from test_airdrop_filter import alerted, leg, run, w
from test_consensus_delivery import DEFAULT, _card, setting_db  # noqa: F401
from test_dashboard_db import PUB1, PUB2, TOK_PUB, seeded  # noqa: F401
from test_telegram_mute import eng  # noqa: F401
from test_tracker import E18
from test_web import make_live  # noqa: F401

from agora_tracker import cards
from agora_tracker import schema as t
from agora_tracker.cards import Links
from agora_tracker.cohorts import Cohort, find_cohorts, load_cohorts, warning_line
from agora_tracker.config import IntelConfig, LinksConfig, load_config
from agora_tracker.web.format import Links as WebLinks

NOW = datetime.now(timezone.utc)
IC = IntelConfig()


def tok(i: int) -> str:
    return "0x" + f"{0xC0DE0000 + i:040x}"


def cohort_rows(members: list[str], launches: int, *, start: int = 0) -> list[tuple[str, str]]:
    return [(tok(start + i), m) for i in range(launches) for m in members]


# --- the rule (pure) -------------------------------------------------------------------------------
def test_a_group_entering_launch_after_launch_together_is_a_cohort():
    gang = [w(i) for i in range(8)]
    rows = cohort_rows(gang, 6)
    rows += [(tok(i), w(50 + i)) for i in range(6)]          # a different stranger each launch
    found = find_cohorts(rows)
    assert len(found) == 1
    assert found[0].wallets == frozenset(gang) and found[0].tokens == {tok(i) for i in range(6)}
    assert found[0].as_dict()["launches"] == 6 and found[0].key == min(gang)


def test_partial_attendance_still_links_the_group():
    gang = [w(i) for i in range(10)]
    rows = []
    for i in range(18):                                       # 9-16 of them on each launch
        rows += [(tok(i), m) for k, m in enumerate(gang) if (i + k) % 5 != 0]
    assert find_cohorts(rows)[0].wallets == frozenset(gang)


def test_coincidental_overlap_is_not_a_cohort():
    rows = []
    for i in range(12):                                       # 12 launches, 6 random-ish snipers each
        rows += [(tok(i), w((i * 7 + k * 3) % 40)) for k in range(6)]
    assert find_cohorts(rows) == []
    assert find_cohorts(cohort_rows([w(i) for i in range(8)], 4)) == []        # too few launches
    assert find_cohorts(cohort_rows([w(i) for i in range(4)], 9)) == []        # group too small


def test_a_sniper_that_enters_every_launch_is_not_pulled_in():
    gang = [w(i) for i in range(6)]
    sniper = w(99)
    rows = cohort_rows(gang, 6) + [(tok(i), sniper) for i in range(40)]
    found = find_cohorts(rows)
    assert len(found) == 1 and sniper not in found[0].wallets
    loose = find_cohorts(rows, min_overlap_busy=0.0)              # without the guard it would be
    assert sniper in loose[0].wallets


# --- the burst query (database) --------------------------------------------------------------------
def seed_burst(store, wallets, launches, *, gap_s=1.0, start_ago_h=10, tag="a", tok0=0):
    rows = []
    for i in range(launches):
        t0 = NOW - timedelta(hours=start_ago_h) + timedelta(minutes=29 * i)
        for k, wal in enumerate(wallets):
            rows.append(dict(chain_id=CHAIN, tx_hash=f"0x{tag}{i:03d}{k:03d}".ljust(66, "0"),
                             wallet=wal, token=tok(tok0 + i), kind="TRANSFER_IN", confidence="high",
                             hype_method="none", qty=E18, block_number=1000 + i * 100 + k,
                             block_time=t0 + timedelta(seconds=k * gap_s)))
    with store.tx() as c:
        c.execute(sa.insert(t.transactions), [dict(chain_id=CHAIN, tx_hash=r["tx_hash"],
                                                   block_number=r["block_number"]) for r in rows])
        c.execute(sa.insert(t.trades), rows)


def test_the_database_rule_finds_the_cohort_and_respects_the_window(store):
    gang = [w(i) for i in range(7)]
    seed_burst(store, gang, 6)                                    # 1 s apart: one burst each
    seed_burst(store, [w(20 + i) for i in range(7)], 6, gap_s=30, tag="b", start_ago_h=40,
               tok0=100)
    found = store.launch_cohorts(IC)
    assert [c.wallets for c in found] == [frozenset(gang)]        # 30 s apart is not a burst
    assert store.launch_cohorts(IC, skip_tokens=[tok(i) for i in range(6)]) == []
    old = IntelConfig(cohort_lookback_days=0)
    assert store.launch_cohorts(old) == []                        # outside the lookback


def test_the_same_query_runs_on_the_websites_read_only_view(store):
    with store.engine.connect() as c:                             # columns and table name exist
        assert load_cohorts(c, table="public_api.trades", chain_id=CHAIN, ic=IC) == []
    try:
        with store.engine.connect() as c:
            load_cohorts(c, table="trades; DROP TABLE trades", chain_id=CHAIN, ic=IC)
        raise AssertionError("arbitrary table names must be refused")
    except ValueError:
        pass


# --- tracker: alerts ---------------------------------------------------------------------------------
def test_cohort_wallets_get_no_alerts_and_others_get_a_warning(store, eng):
    gang = [w(i) for i in range(6)]
    seed_burst(store, gang, 6)
    target = tok(3)                                               # a token the cohort traded
    events = [leg("BUY", target, gang[0], "0x" + "1" * 64, hype=2 * E18),
              leg("BUY", target, w(40), "0x" + "2" * 64, hype=2 * E18),
              leg("BUY", tok(90), w(41), "0x" + "3" * 64, hype=2 * E18)]
    run(eng, events, {"0x" + "1" * 64: gang[0], "0x" + "2" * 64: w(40), "0x" + "3" * 64: w(41)},
        cursor=10)
    out = {(p["wallet"], p["token"]): p for p in (r["payload"] for r in _outbox(store))
           if p.get("type") == "trade"}
    assert (gang[0], target) not in out                           # the cohort's own buy: silent
    assert out[(w(40), target)]["cohort_wallets"] == 6            # an outsider buying it: warned
    assert "cohort_wallets" not in out[(w(41), tok(90))]          # unrelated token: no warning
    text, _ = cards.render_trade(out[(w(40), target)], Links(LinksConfig()))
    assert "Launch cohort: 6 linked wallets" in text and "exit liquidity" in text
    with store.engine.connect() as c:
        s = c.execute(sa.text("SELECT count(*) FROM trades WHERE tx_hash = :h"),
                      {"h": "0x" + "1" * 64}).scalar()
    assert s == 1                                                 # still indexed


def test_cohort_wallets_count_as_one_operator(store, eng):
    gang = [w(i) for i in range(6)]
    seed_burst(store, gang, 6)
    rows = [(gang[0], 1, 100, E18), (gang[1], 2, 101, E18), (gang[2], 3, 102, E18),
            (w(40), 4, 103, E18)]
    score = eng._score(rows, 1800)
    assert score["independent"] == 2 and score["cohort_wallets"] == 3
    eng.intel.cohort_filter = False
    eng._cohort_of = {}
    assert eng._score(rows, 1800)["independent"] == 4


def test_filter_off_and_refresh_failure(store, eng, monkeypatch):
    gang = [w(i) for i in range(6)]
    seed_burst(store, gang, 6)
    eng.intel.cohort_filter = False
    run(eng, [leg("BUY", tok(1), gang[0], "0x" + "4" * 64, hype=2 * E18)],
        {"0x" + "4" * 64: gang[0]}, cursor=10)
    assert [a for a in alerted(store, tok(1))] == [(gang[0], "BUY")]
    eng.intel.cohort_filter = True
    monkeypatch.setattr(store, "launch_cohorts", lambda *a, **k: 1 / 0)
    run(eng, [leg("BUY", tok(2), gang[1], "0x" + "5" * 64, hype=2 * E18)],
        {"0x" + "5" * 64: gang[1]}, cursor=11)
    assert store.get_cursor() == 11                               # indexing never blocked


def test_cluster_cards_carry_the_warning():
    payload = {"type": "cluster", "token": tok(1), "symbol": "X", "min_hype": 0.5,
               "window_min": 30, "buyers": [{"wallet": w(1)}], "tx": "0x" + "1" * 64,
               "cohort_wallets": 4}
    text, _ = cards.render_cluster(payload, Links(LinksConfig()))
    assert warning_line(4) in text.replace("&#x27;", "'")


def test_env_switch_and_defaults(monkeypatch):
    monkeypatch.delenv("ALERTS_COHORT_FILTER", raising=False)
    ic = load_config("config.example.yaml").intel
    assert (ic.cohort_filter, ic.cohort_window_s, ic.cohort_min_size) == (True, 10.0, 5)
    monkeypatch.setenv("ALERTS_COHORT_FILTER", "off")
    assert load_config("config.example.yaml").intel.cohort_filter is False


# --- website and Consensus ---------------------------------------------------------------------------
class _Rep:
    hidden_tokens = ()
    chain_id = CHAIN


def _dash(cohorts):
    from agora_tracker.web.service import Dashboard
    cfg = load_config("config.example.yaml")
    d = Dashboard.__new__(Dashboard)
    d.cfg, d.rep = cfg, _Rep()
    d._cohorts, d._cohorts_at = cohorts, time.monotonic()
    return d


def test_website_token_warning_and_api():
    gang = frozenset(w(i) for i in range(6))
    d = _dash([Cohort(gang, frozenset({tok(1)}))])
    assert d.token_cohort(tok(1), [], NOW) == {"wallets": [], "n": 0,
                                               "launch_cohorts": [min(gang)]}
    hit = d.token_cohort(tok(9), [w(0), w(1), w(50)], NOW)
    assert hit["n"] == 2 and hit["wallets"] == sorted([w(0), w(1)])
    assert d.token_cohort(tok(9), [w(0), w(50)], NOW) is None    # one wallet: below the bar
    api = d.cohort_api(NOW)
    assert api["wallets"] == sorted(gang) and api["tokens"] == [tok(1)]
    assert api["cohorts"][0]["size"] == 6 and api["rule"]["window_s"] == 10.0


def test_token_page_and_api_show_the_warning(seeded, make_live, monkeypatch):
    from agora_tracker.web.service import Dashboard
    flagged = {"wallets": [PUB1, PUB2], "n": 2, "launch_cohorts": [PUB1]}
    monkeypatch.setattr(Dashboard, "token_cohort",
                        lambda self, token, wallets, now: flagged if token == TOK_PUB else None)
    lv = make_live()
    html = lv.get(f"/tokens/{TOK_PUB}").text
    assert "Coordinated launch cohort" in html and "2 tracked wallets" in html
    assert f"/wallets/{PUB1}" in html
    assert lv.get(f"/api/v1/tokens/{TOK_PUB}").json()["cohort"]["n"] == 2
    r = lv.get("/api/v1/cohorts")
    assert r.status_code == 200 and set(r.json()) >= {"cohorts", "wallets", "tokens", "rule"}


def test_consensus_channel_skips_cohort_cards(setting_db):
    setting_db.save(DEFAULT)
    saved = setting_db.read()
    now = datetime.now(timezone.utc)
    clean = _card(now)
    flagged = {**_card(now), "trigger_tx": "0x" + "e" * 64, "cohort_wallets": 2}
    single = {**_card(now), "trigger_tx": "0x" + "d" * 64, "cohort_wallets": 1}
    assert setting_db.queue(saved, [clean, flagged, single], now, WebLinks(LinksConfig())) == 2
    with setting_db.engine.connect() as c:
        keys = [r[0] for r in c.execute(sa.text(
            "SELECT event_key FROM consensus_alerts.deliveries ORDER BY id"))]
    assert not any("e" * 64 in k for k in keys) and len(keys) == 2


def _outbox(store):
    with store.engine.connect() as c:
        return [dict(r) for r in c.execute(sa.text(
            "SELECT payload FROM notification_outbox ORDER BY id")).mappings()]
