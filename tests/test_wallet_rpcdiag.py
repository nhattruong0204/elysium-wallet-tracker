"""Read-only RPC diagnostics (#95 F-07) against an ISOLATED loopback mock (tests/rpcmock.py):
honest, lying, forked, stale, history-ignoring, pruned, rate-limited, slow, malformed and
wrong-chain providers. No real endpoint is contacted and nothing is signed or sent."""

from __future__ import annotations

import json

import pytest
from rpcmock import Behaviour, Chain, Provider

from agora_tracker.trading.wallet import rpcdiag as D

FAST = dict(duration_s=0.6, interval_s=0.2, max_rps=50.0, timeout=2.0)


@pytest.fixture
def chain():
    return Chain(block_time=0.05)


@pytest.fixture
def made():
    ps: list[Provider] = []
    yield ps
    for p in ps:
        p.close()


def prov(made, chain, **beh) -> Provider:
    p = Provider(chain, Behaviour(**beh))
    made.append(p)
    return p


def checks(rep, i=0) -> dict:
    return {c["name"]: c for c in rep["providers"][i]["checks"]}


def cross(rep) -> dict:
    return {c["name"]: c for c in rep["cross"]}


def test_an_honest_independent_pair_passes_and_no_url_or_key_is_ever_reported(made, chain):
    a = prov(made, chain, server_header="node-a", client_version="Nitro/a")
    b = prov(made, chain, lag=3, server_header="node-b", client_version="Nitro/b")
    rep = D.run([("alpha", a.url), ("beta", b.url)], **FAST)
    c0 = checks(rep)
    for name in ("chain_id", "parent_hash_continuity", "finality_tags", "historical_block_param",
                 "historical_balance_n_minus_1", "historical_eth_call", "archive_depth",
                 "tx_receipt_consistency", "head_freshness", "reorgs_observed", "transport"):
        assert c0[name]["status"] == "PASS", (name, c0[name])
    x = cross(rep)
    assert x["same_canonical_chain"]["status"] == "PASS"
    assert x["same_historical_state"]["status"] == "PASS"
    assert x["same_finalized_ancestry"]["status"] == "PASS"
    assert x["distinct_operators"]["status"] == "PASS"
    assert x["upstream_indicators"]["status"] == "PASS"
    assert rep["verdict"] == "PASS"
    text = json.dumps(rep) + D.render_markdown(rep)
    assert "SECRETKEY" not in text and "/v1/" not in text
    lat = c0["transport"]["data"]["latency_ms"]["eth_getBlockByNumber"]
    assert lat["n"] > 3 and lat["p50"] is not None and lat["p99"] is not None


def test_only_read_methods_ever_reach_the_wire_and_writes_are_refused_before_io(made, chain):
    a = prov(made, chain)
    b = prov(made, chain)
    D.run([("alpha", a.url), ("beta", b.url)], **FAST)
    sent = set(a.methods) | set(b.methods)
    assert sent <= D.DIAG_METHODS
    rpc = D.RawRpc(a.url, max_rps=50)
    before = len(a.methods)
    for m in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_sign",
              "debug_traceTransaction", "admin_peers", "txpool_content", "miner_start"):
        with pytest.raises(PermissionError):
            rpc.call(m, [])
    assert len(a.methods) == before and rpc.calls == 0


def test_a_provider_lying_about_block_hashes_is_flagged(made, chain):
    a = prov(made, chain)
    b = prov(made, chain, lie_from_head_minus=5)
    rep = D.run([("alpha", a.url), ("beta", b.url)], **FAST)
    assert cross(rep)["same_canonical_chain"]["status"] == "FAIL"
    assert rep["verdict"] == "FAIL"


def test_a_node_that_ignores_the_block_parameter_is_caught(made, chain):
    a = prov(made, chain, ignore_block_param=True)
    rep = D.run([("alpha", a.url)], **FAST)
    c = checks(rep)
    assert c["historical_block_param"]["status"] == "FAIL"
    assert "IGNORE" in c["historical_block_param"]["detail"]
    assert c["historical_eth_call"]["status"] == "FAIL"
    # (the balance comparison is only a WARN-level hint: on a moving head an ignoring node can still
    # return two different "latest" balances, so it is not asserted here)


def test_historical_disagreement_between_providers_is_flagged(made, chain):
    a = prov(made, chain)
    b = prov(made, chain, ignore_block_param=True)
    rep = D.run([("alpha", a.url), ("beta", b.url)], **FAST)
    assert cross(rep)["same_historical_state"]["status"] == "FAIL"


def test_a_stalled_head_fails_freshness(made, chain):
    a = prov(made, chain, stale_at=chain.head() - 2000)        # ~100 s old and not moving
    rep = D.run([("alpha", a.url)], **FAST)
    assert checks(rep)["head_freshness"]["status"] == "FAIL"


def test_a_pruned_node_reports_its_archive_depth(made, chain):
    a = prov(made, chain, archive_depth=5_000)
    rep = D.run([("alpha", a.url)], **FAST)
    c = checks(rep)["archive_depth"]
    assert c["status"] == "FAIL" and c["data"]["max_ok_depth"] == 1_000
    assert c["data"]["refused_at"] == 10_000


def test_missing_finalized_tag_fails_the_finality_checks(made, chain):
    a = prov(made, chain, finalized_lag=None)
    b = prov(made, chain)
    rep = D.run([("alpha", a.url), ("beta", b.url)], **FAST)
    assert checks(rep)["finality_tags"]["status"] == "FAIL"
    assert cross(rep)["same_finalized_ancestry"]["status"] == "FAIL"


def test_finalized_equal_to_latest_is_a_warning_not_evidence(made, chain):
    a = prov(made, chain, finalized_lag=0)
    rep = D.run([("alpha", a.url)], **FAST)
    assert checks(rep)["finality_tags"]["status"] == "WARN"


def test_rate_limits_and_retry_after_are_recorded_not_hidden(made, chain):
    a = prov(made, chain, http_429_every=4, retry_after="7")
    rep = D.run([("alpha", a.url)], **FAST)
    t = checks(rep)["transport"]
    assert t["data"]["outcomes"].get("http_429", 0) > 0
    assert "7" in t["data"]["retry_after_values"]
    assert t["status"] in ("WARN", "FAIL")


def test_malformed_responses_and_timeouts_are_classified(made, chain):
    a = prov(made, chain, malformed_every=3)
    rep = D.run([("alpha", a.url)], **FAST)
    assert checks(rep)["transport"]["data"]["outcomes"].get("malformed", 0) > 0
    slow = prov(made, chain, slow_s=0.5)
    rpc = D.RawRpc(slow.url, timeout=0.1, max_rps=50)
    with pytest.raises(D.DiagError) as e:
        rpc.call("eth_blockNumber", [])
    assert e.value.kind == "timeout" and rpc.records[-1].outcome == "timeout"


def test_the_wrong_chain_is_refused_and_nothing_else_is_probed(made, chain):
    a = prov(made, chain, chain_id=1)
    rep = D.run([("alpha", a.url)], **FAST)
    assert checks(rep)["chain_id"]["status"] == "FAIL" and rep["verdict"] == "BLOCKED"
    assert set(a.methods) <= {"eth_chainId", "web3_clientVersion"}


def test_a_reorg_during_sampling_is_observed(made, chain):
    a = prov(made, chain)
    rpc = D.RawRpc(a.url, max_rps=50)
    p = D.ProviderProbe(rpc)
    p.observe_head()
    p.observe_head()
    chain.reorg(depth=5)
    p.observe_head()
    p.summarize(1.0)
    c = {x.name: x for x in p.checks}["reorgs_observed"]
    assert c.status == "WARN" and c.data["reorgs"]


def test_the_call_budget_is_a_hard_ceiling(made, chain):
    a = prov(made, chain)
    rep = D.run([("alpha", a.url)], max_calls=15, **FAST)
    assert rep["providers"][0]["calls"] <= 15 and len(a.methods) <= 15


def test_one_upstream_behind_two_names_is_indicated(made, chain):
    a = prov(made, chain)                                      # identical client + server header
    b = prov(made, chain)
    rep = D.run([("alpha", a.url), ("beta", b.url)], **FAST)
    assert cross(rep)["upstream_indicators"]["status"] == "WARN"


def test_one_provider_cannot_corroborate(made, chain):
    a = prov(made, chain)
    rep = D.run([("alpha", a.url)], **FAST)
    assert cross(rep)["independent_sources"]["status"] == "FAIL"


def test_concurrency_probe_is_off_by_default_and_bounded(made, chain):
    a = prov(made, chain)
    rep = D.run([("alpha", a.url)], **FAST)
    assert checks(rep)["concurrency_probe"]["status"] == "SKIP"
    rep = D.run([("alpha", a.url)], concurrency=3, concurrency_rounds=2, **FAST)
    c = checks(rep)["concurrency_probe"]
    assert c["status"] == "PASS" and c["data"]["total"] == 6


def test_cli_reads_urls_from_the_environment_only_and_blocks_without_them(made, chain, tmp_path,
                                                                          capsys):
    assert D.main(["--duration", "0"], environ={}) == 2
    a = prov(made, chain, server_header="a", client_version="a")
    b = prov(made, chain, server_header="b", client_version="b")
    out, md = tmp_path / "r.json", tmp_path / "r.md"
    env = {"TRADING_EXEC_RPC_URL": a.url, "TRADING_CORROBORATION_RPC_URLS": f"beta={b.url}"}
    code = D.main(["--duration", "0.4", "--interval", "0.2", "--max-rps", "20", "--out", str(out),
                   "--markdown", str(md)], environ=env)
    assert code == 0
    rep = json.loads(out.read_text())
    assert [p["operator"] for p in rep["providers"]] == ["127.0.0.1", "beta"]
    assert "SECRETKEY" not in out.read_text() + md.read_text() + capsys.readouterr().out
    with pytest.raises(SystemExit):
        D.main(["--max-calls", "999999"], environ=env)
    assert D.main(["--duration", "0"], environ={"TRADING_EXEC_RPC_URL": "ftp://x/KEY"}) == 2
