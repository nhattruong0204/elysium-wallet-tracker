# ruff: noqa: F811
"""Operator mute list (TRACKING_MUTED_WALLETS): tracked and shown, never alerted on."""

from __future__ import annotations

import yaml
from test_airdrop_filter import alerted, leg, run, w
from test_dashboard_db import PUB1, PUB2, TOK_PUB, seeded  # noqa: F401
from test_launch_cohorts import _outbox, tok
from test_telegram_mute import eng  # noqa: F401
from test_tracker import E18
from test_web import make_cfg, make_live  # noqa: F401

from agora_tracker.config import load_config

H = ["0x" + c * 64 for c in "123456"]


def test_env_list_is_normalised_and_blocked_wins(monkeypatch):
    monkeypatch.delenv("TRACKING_MUTED_WALLETS", raising=False)
    monkeypatch.delenv("TRACKING_BLOCKED_WALLETS", raising=False)
    assert load_config("config.example.yaml").tracking.muted_wallets == []
    monkeypatch.setenv("TRACKING_MUTED_WALLETS", f"{w(2).upper().replace('0X', '0x')}, {w(1)}")
    monkeypatch.setenv("TRACKING_BLOCKED_WALLETS", w(2))
    cfg = load_config("config.example.yaml")
    assert cfg.tracking.muted_wallets == [w(1)] and cfg.tracking.blocked_wallets == [w(2)]


def test_compose_mutes_nobody_and_keeps_following_the_trading_cohort():
    """Owner decision 2026-11-12: transfer-only groups are blocked; the cohort that really
    buys and sells (cohort 2) is followed as usual, neither blocked nor muted."""
    svc = yaml.safe_load(open("docker-compose.yml"))["services"]
    assert all("TRACKING_MUTED_WALLETS" not in (s.get("environment") or {})
               for s in svc.values())
    blocked = svc["tracker"]["environment"]["TRACKING_BLOCKED_WALLETS"].split()
    cohort2 = ["0x5732b9e35e5c5bc4889bbafce634545247601c9d",
               "0xd8ba9e5886b7000bace2c9dbdd0be98ac77d4a28"]
    assert not set(cohort2) & set(blocked)
    assert "0x0a90d9d0d0c76c5fe79faa6f6de4105fd3e68c79" in blocked     # cohort 1 stays blocked


def test_a_muted_wallet_is_indexed_but_never_alerted(store, eng):
    eng._muted_wallets = {w(1): "muted", w(2): "muted"}
    eng.intel.cohort_filter = False                       # the mute list does not depend on it
    target = tok(1)
    events = [leg("BUY", target, w(1), H[0], hype=2 * E18),
              leg("BUY", target, w(2), H[1], hype=2 * E18),
              leg("BUY", target, w(40), H[2], hype=2 * E18),
              leg("BUY", tok(2), w(41), H[3], hype=2 * E18)]
    run(eng, events, {H[0]: w(1), H[1]: w(2), H[2]: w(40), H[3]: w(41)}, cursor=10)
    assert sorted(alerted(store, target)) == [(w(40), "BUY")]       # only the outsider
    out = {p["wallet"]: p for p in (r["payload"] for r in _outbox(store))
           if p.get("type") == "trade"}
    assert out[w(40)]["cohort_wallets"] == 2                        # ...with the warning
    assert "cohort_wallets" not in out[w(41)]
    with store.engine.connect() as c:
        import sqlalchemy as sa
        n = c.execute(sa.text("SELECT count(*) FROM trades WHERE wallet = ANY(:w)"),
                      {"w": [w(1), w(2)]}).scalar()
    assert n == 2                                                   # still indexed


def test_muted_wallets_never_count_as_independent_buyers(store, eng):
    rows = [(w(1), 1, 100, E18), (w(2), 2, 101, E18), (w(3), 3, 102, E18), (w(40), 4, 103, E18)]
    assert eng._score(rows, 1800)["independent"] == 4
    eng._muted_wallets = {w(1): "muted", w(2): "muted", w(3): "muted"}
    score = eng._score(rows, 1800)
    assert score["independent"] == 2 and score["cohort_wallets"] == 3


def test_website_keeps_them_out_of_the_feed_but_on_wallet_intelligence(seeded, make_live):
    cfg = make_cfg()
    cfg.tracking.muted_wallets = [PUB1]
    lv = make_live(cfg)
    feed = lv.get("/api/v1/activity", params={"range": "7d"}).json()["items"]
    assert feed and all(i["wallet"] != PUB1 for i in feed)
    mine = lv.get("/api/v1/activity", params={"range": "7d", "wallet": PUB1}).json()["items"]
    assert mine and all(i["wallet"] == PUB1 for i in mine)          # explicit filter still works
    assert all(r["wallet"] != PUB1 for r in lv.get("/api/v1/overview").json()["recent"])
    assert lv.get(f"/wallets/{PUB1}").status_code == 200
    got = {x["wallet"]: x for x in lv.get("/api/v1/leaderboard", params={
        "window": "all"}).json()["page"]["items"]}
    assert "muted" in got[PUB1]["label_flags"] and "muted" not in got[PUB2]["label_flags"]
    assert got[PUB1]["legs"] > 0                                    # still tracked and counted
    only = lv.get("/api/v1/leaderboard", params={"window": "all", "flag": "muted"}).json()
    assert [x["wallet"] for x in only["page"]["items"]] == [PUB1]
    plain = make_live()                                             # no mute list: unchanged
    assert any(i["wallet"] == PUB1 for i in plain.get(
        "/api/v1/activity", params={"range": "7d"}).json()["items"])
    assert TOK_PUB
