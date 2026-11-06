# ruff: noqa: F811
"""Cluster pages and API: they render from public views only, never expose a cluster with a
private member, bundle trades by the membership valid at trade time, and describe launch
proximity without conclusions."""

from __future__ import annotations

import sqlalchemy as sa
from conftest import CHAIN
from test_clustering import (  # noqa: F401
    D0,
    E18,
    W,
    _tx,
    at,
    buy,
    cfg,
    same_block,
    tok,
    transfer,
    world,
)
from test_web import make_cfg, make_live, web  # noqa: F401

from agora_tracker import clustering as K
from agora_tracker.classify import TradeEvent

PRIVATE = "0x" + "b9" * 20


def live_cfg(**kw):
    c = make_cfg(**kw)
    c.clustering = cfg(version="web1", lookback_days=30)
    return c


def build(world):
    """A cluster {W1,W2} (3 tokens in the same block) evaluated at day 2, a lone pair W3,W4."""
    for i in range(3):
        same_block(world, [W[1], W[2]], tok(i), 20_001 + i, 1.0 + i / 100)
    same_block(world, [W[3], W[4]], tok(7), 20_010, 1.1)
    K.run_once(world, cfg(version="web1", lookback_days=30), as_of=at(2), now=at(2))
    with world.tx() as c:
        return c.execute(sa.text("SELECT id FROM wallet_clusters")).scalar_one()


def get(client, url):
    r = client.get(url)
    return r


def test_cluster_list_detail_and_api(world, make_live):
    cid = build(world)
    client = make_live(live_cfg())
    r = client.get("/clusters")
    assert r.status_code == 200 and f"/clusters/{cid}" in r.text and "not an allegation" in r.text
    assert W[1][:6] in r.text
    r = client.get(f"/clusters/{cid}")
    assert r.status_code == 200 and "Evidence graph" in r.text and "<svg" in r.text
    assert "Launch proximity" in r.text and "research, not an accusation" in r.text
    j = client.get("/api/v1/clusters").json()
    assert j["kpis"]["active"] == 1 and j["kpis"]["wallets"] == 2 and j["version"] == "web1"
    assert j["page"]["items"][0]["members"] == [W[1], W[2]] and "all_rows" not in j
    d = client.get(f"/api/v1/clusters/{cid}").json()
    assert d["cluster"]["status"] == "active" and d["current"] == [W[1], W[2]]
    assert d["evidence"]["same_block_tokens"] == 3 and len(d["graph"]["nodes"]) == 2
    assert d["edges"][0]["strength"] == "strong"
    assert client.get(f"/api/v1/clusters/{cid}/members").json()["members"]
    assert client.get(f"/api/v1/clusters/{cid}/edges").json()["edges"]
    assert client.get(f"/api/v1/clusters/{cid}/events").json()["events"][0]["kind"] == "CREATED"


def test_bad_parameters_and_unknown_clusters(world, make_live):
    build(world)
    client = make_live(live_cfg())
    for url in ("/clusters?status=bogus", "/clusters?size=7", "/clusters?page=abc",
                "/clusters/bundled?window=1y", "/clusters/bundled?kind=x",
                "/clusters/bundled?min_wallets=0", "/api/v1/clusters/1/launch-proximity?within=7"):
        assert client.get(url).status_code == 400, url
    for url in ("/clusters/999999", "/clusters/abc", "/api/v1/clusters/999999",
                "/api/v1/clusters/999999/launch-proximity"):
        assert client.get(url).status_code == 404, url


def test_a_cluster_with_a_private_member_does_not_exist_for_the_website(world, make_live):
    cid = build(world)
    with world.tx() as c:
        run_id = c.execute(sa.text("SELECT id FROM cluster_runs")).scalar()
        hid = c.execute(sa.text(
            "INSERT INTO wallet_clusters (chain_id, version, created_run_id, created_at) "
            "VALUES (:c, 'web1', :r, now()) RETURNING id"), {"c": CHAIN, "r": run_id}).scalar()
        c.execute(sa.text(
            "INSERT INTO wallet_cluster_members (cluster_id, chain_id, address, valid_from, "
            "from_run_id) VALUES (:i, :c, :a, now(), :r), (:i, :c, :b, now(), :r)"),
            {"i": hid, "c": CHAIN, "a": W[6], "b": PRIVATE, "r": run_id})
    client = make_live(live_cfg())
    assert client.get(f"/clusters/{hid}").status_code == 404
    assert client.get(f"/api/v1/clusters/{hid}").status_code == 404
    body = client.get("/clusters?status=all").text + client.get("/api/v1/clusters?status=all").text
    assert PRIVATE not in body and PRIVATE[2:10] not in body
    assert f"/clusters/{cid}" in body


def test_bundled_activity_uses_membership_valid_at_trade_time(world, make_live):
    build(world)                                  # first run at(2): W1,W2 become a cluster then
    buy(world, W[1], tok(40), 30_001, 1.5)        # before the cluster was recognised: not bundled
    buy(world, W[1], tok(41), 30_002, 2.1)
    buy(world, W[2], tok(41), 30_002, 2.1)
    buy(world, W[3], tok(41), 30_002, 2.1)        # W3 is in no cluster
    client = make_live(live_cfg())
    j = client.get("/api/v1/clusters/bundled?window=30d&kind=buy").json()
    rows = {(i["token"], i["wallets"]): i for i in j["page"]["items"]}
    assert (tok(41), 2) in rows and rows[(tok(41), 2)]["legs"] == 2
    assert tok(40) not in {t for t, _ in rows}
    assert all(W[3] not in i["wallets_list"] for i in j["page"]["items"])
    assert j["kpis"]["multi_wallet"] >= 1 and "all_rows" not in j
    page = client.get("/clusters/bundled?window=30d")
    assert page.status_code == 200 and "Bundled activity" in page.text
    assert client.get("/clusters/bundled?window=30d&min_wallets=3").status_code == 200


def test_launch_proximity_is_withheld_on_small_samples_and_never_accuses(world, make_live):
    cid = build(world)
    client = make_live(live_cfg())
    j = client.get(f"/api/v1/clusters/{cid}/launch-proximity?within=5").json()
    assert j["enough"] is False and j["share"] is None and j["min_sample"] == 10
    assert "not evidence of advance knowledge" in j["note"] and "wrongdoing" in j["note"]
    for bad in ("insider", "guilty", "fraud", "illegal"):
        assert bad not in client.get(f"/clusters/{cid}").text.lower()


def test_wallet_profile_lists_cluster_membership_and_csv_is_safe(world, make_live):
    cid = build(world)
    client = make_live(live_cfg())
    r = client.get(f"/leaderboard/{W[1]}")
    assert r.status_code == 200 and f"/clusters/{cid}" in r.text
    assert client.get(f"/api/v1/leaderboard/{W[1]}").json()["clusters"][0]["cluster_id"] == cid
    r = client.get(f"/leaderboard/{W[7]}")
    assert "Not part of any cluster" in r.text
    csvr = client.get("/api/v1/clusters.csv?status=all")
    assert csvr.status_code == 200 and "private, no-store" in csvr.headers["cache-control"]
    assert csvr.text.splitlines()[0].startswith("cluster_version,cluster_id")
    assert client.get("/api/v1/clusters/bundled.csv").status_code == 200
    assert "clusters" in client.get("/api/v1/meta").json()


def test_csv_cells_that_start_like_formulas_are_neutralised():
    from agora_tracker.web.clusters import _cell
    assert _cell("=1+1") == "'=1+1" and _cell("@x") == "'@x" and _cell("abc") == "abc"
    assert _cell(5) == 5


def test_launch_proximity_statistic_with_baseline(world, make_live):
    from datetime import timedelta

    from agora_tracker import schema as t
    cid = build(world)
    with world.tx() as c:
        c.execute(sa.insert(t.token_profiles), [
            {"chain_id": CHAIN, "address": tok(100 + i), "symbol": f"T{i}", "launchpad": "boxa",
             "launched_at": at(1.2)} for i in range(12)])
    for i in range(12):                    # W1 buys 8 tokens within 30 s of launch, 4 hours later
        delay = 30 if i < 8 else 4 * 3600
        with world.tx() as c:
            world.insert_trade(c, TradeEvent("BUY", _tx(), 40_000 + i, W[1], tok(100 + i), E18, E18),
                               int((at(1.2) + timedelta(seconds=delay)).timestamp()))
    # one buy recorded before the stored launch time is reported, not counted
    with world.tx() as c:
        c.execute(sa.insert(t.token_profiles).values(chain_id=CHAIN, address=tok(200), symbol="E",
                                                     launched_at=at(1.3)))
        world.insert_trade(c, TradeEvent("BUY", _tx(), 40_100, W[1], tok(200), E18, E18),
                           int(at(1.2).timestamp()))
    client = make_live(live_cfg())
    j = client.get(f"/api/v1/clusters/{cid}/launch-proximity?within=1").json()
    assert j["enough"] and j["sample"] == 12 and j["near"] == 8 and j["before_launch"] == 1
    assert abs(j["share"] - 8 / 12) < 1e-9
    assert j["baseline"]["sample"] >= 12 and j["baseline"]["share"] is not None
    assert j["without_launch_time"] >= 3                      # the three same-block buys of build()
    html = client.get(f"/clusters/{cid}?within=1").text
    assert "8 of 12" in html and "For all public wallets" in html
    assert "not evidence of advance knowledge" in html
