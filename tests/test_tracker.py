"""Offline tests: a fake Elysium RPC replays buys, sells, transfers and a cluster."""

from __future__ import annotations

import contextlib
import json

import pytest
import sqlalchemy as sa

from agora_tracker.chain import TRANSFER_TOPIC, ChainFollower, pad_topic
from agora_tracker.classify import classify
from agora_tracker.config import AlertConfig, ChainConfig, LeaderboardConfig
from agora_tracker.engine import WEI, Engine
from agora_tracker.leaderboard import diff_leaderboard, guess_mapping, parse_curl, parse_items
from agora_tracker.rpc import RpcError

W1 = "0x" + "a1" * 20
W2 = "0x" + "a2" * 20
W3 = "0x" + "a3" * 20
FRESH = "0x" + "f0" * 20
POOL = "0x" + "b0" * 20
TOKEN = "0x" + "c0" * 20
E18 = 10 ** 18
FEE = 10 ** 15  # 0.001 HYPE


def abi_string(s: str) -> str:
    b = s.encode()
    return "0x" + (32).to_bytes(32, "big").hex() + len(b).to_bytes(32, "big").hex() + \
        b.ljust(32, b"\0").hex()


class FakeChain:
    """Minimal JSON-RPC: logs, txs, receipts, historical balances, blocks, token metadata."""

    def __init__(self):
        self.head = 40
        self.logs, self.txs, self.balances = [], {}, {}
        self.plain_sends: list[tuple[str, int]] = []   # (sender, block) txs without token logs
        self.nonce_unavailable = False
        self.calls = 0

    def nonce_at(self, addr, block):
        own = sum(1 for t in self.txs.values() if t["from"] == addr and t["block"] <= block)
        return own + sum(1 for s, b in self.plain_sends if s == addr and b <= block)

    def add_tx(self, h, block, sender, value=0, transfers=(), bal=None):
        self.txs[h] = {"hash": h, "from": sender, "value": hex(value), "block": block}
        for i, (src, dst, amt) in enumerate(transfers):
            self.logs.append({"address": TOKEN, "blockNumber": hex(block), "transactionHash": h,
                              "logIndex": hex(i), "data": "0x" + amt.to_bytes(32, "big").hex(),
                              "topics": [TRANSFER_TOPIC, pad_topic(src), pad_topic(dst)]})
        for w, (before, after) in (bal or {}).items():
            self.balances[(w, block - 1)] = before
            self.balances[(w, block)] = after

    @staticmethod
    def _match(topic, want):
        return want is None or topic in (want if isinstance(want, list) else [want])

    def call(self, method, params):
        self.calls += 1
        if method == "eth_chainId":
            return hex(99901)
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            f = params[0]
            frm, to = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            if to - frm > 15:
                raise RpcError("block range too large")
            out = []
            for lg in self.logs:
                b = int(lg["blockNumber"], 16)
                if frm <= b <= to and all(self._match(lg["topics"][i], w)
                                          for i, w in enumerate(f["topics"])):
                    out.append(lg)
            return out
        if method == "eth_getTransactionByHash":
            t = self.txs[params[0]]
            return {"from": t["from"], "value": t["value"], "gasPrice": hex(1)}
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1", "gasUsed": hex(FEE), "effectiveGasPrice": hex(1)}
        if method == "eth_getBalance":
            key = (params[0], int(params[1], 16))
            if key not in self.balances:
                raise RpcError("missing trie node")
            return hex(self.balances[key])
        if method == "eth_getTransactionCount":
            if self.nonce_unavailable:
                raise RpcError("missing trie node")
            return hex(self.nonce_at(params[0], int(params[1], 16)))
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(1_800_000_000 + int(params[0], 16) * 60)}
        if method == "eth_call":
            data = params[0]["data"]
            return abi_string("KLEOS") if data == "0x95d89b41" else hex(18)
        if method == "eth_getCode":
            return "0x60806040" if params[0] == POOL else "0x"
        raise RpcError(f"unsupported {method}")

    def batch(self, calls):
        out = []
        for m, p in calls:
            try:
                out.append(self.call(m, p))
            except RpcError as e:
                out.append(e)
        return out

    def block_number(self):
        return self.head

    urls = ["fake"]

    @contextlib.contextmanager
    def pinned(self):
        yield "fake"


@pytest.fixture
def chain():
    c = FakeChain()
    b0 = 10 * E18
    # W1 buys 1000 KLEOS for 1 HYPE
    c.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 1000 * E18)],
             bal={W1: (b0, b0 - E18 - FEE)})
    b1 = b0 - E18 - FEE
    # W2, W3 also buy -> cluster
    c.add_tx("0xb", 12, W2, value=2 * E18, transfers=[(POOL, W2, 1500 * E18)],
             bal={W2: (5 * E18, 3 * E18 - FEE)})
    c.add_tx("0xc", 13, W3, value=E18, transfers=[(POOL, W3, 600 * E18)],
             bal={W3: (5 * E18, 4 * E18 - FEE)})
    # W1 sells half for 0.8 HYPE (native HYPE comes back through an internal call)
    c.add_tx("0xd", 20, W1, transfers=[(W1, POOL, 500 * E18)],
             bal={W1: (b1, b1 + 8 * E18 // 10 - FEE)})
    # W1 moves 100 to a fresh wallet; balance history unavailable -> approx path
    c.add_tx("0xe", 30, W1, transfers=[(W1, FRESH, 100 * E18)])
    # untracked noise
    c.add_tx("0xf", 31, FRESH, transfers=[(FRESH, POOL, 50 * E18)])
    return c


def build(store, chain):
    ccfg = ChainConfig(rpc_urls=["fake"], wallet_chunk=2)
    acfg = AlertConfig(min_trade_hype=0.1, cluster_min_wallets=3, cluster_window_s=3600)
    follower = ChainFollower(chain, store, ccfg, int(acfg.hype_dust * WEI), set())
    engine = Engine(store, follower.tokens, ccfg, acfg, {W1: "whale-1"})
    return store, follower, engine


def get_pos(store, wallet, token):
    with store.tx() as c:
        return store.get_position(c, wallet, token)


def run_all(follower, engine, wallets, frm=1, to=40):
    res = follower.scan(frm, to, wallets)
    engine.apply_range(res, to)
    events = res.events
    return events


def test_end_to_end_buy_sell_transfer_cluster(store, chain):
    store, follower, engine = build(store, chain)
    events = run_all(follower, engine, {W1, W2, W3})
    kinds = [(e.wallet, e.kind) for e in events]
    assert kinds == [(W1, "BUY"), (W2, "BUY"), (W3, "BUY"), (W1, "SELL"), (W1, "TRANSFER_OUT")]

    buy = events[0]
    assert buy.qty == 1000 * E18 and buy.hype_wei == E18 and not buy.approx
    sell = events[3]
    assert sell.hype_wei == 8 * E18 // 10

    pos = get_pos(store, W1, TOKEN)
    # bought 1000 for 1.0, sold 500 for 0.8, then moved 100 out (cost leaves with it)
    assert pos.qty == 400 * E18
    assert pos.cost_wei == 4 * E18 // 10
    assert pos.realized_wei == 3 * E18 // 10        # 0.8 received - 0.5 cost basis
    assert pos.basis == "known" and pos.realized_est_wei == 0

    texts = [r["text"] for r in store.pending_alerts(100)]
    assert any("CLUSTER BUY" in t and "KLEOS" in t for t in texts)
    assert any("new position" in t and "whale-1" in t for t in texts)
    assert any("realized +0.300 HYPE (+60%)" in t for t in texts)
    assert any("OUT (sell?)" in t and "HYPE leg unknown" in t for t in texts)
    assert store.get_cursor() == 40


def test_replay_is_idempotent(store, chain):
    store, follower, engine = build(store, chain)
    run_all(follower, engine, {W1, W2, W3})
    n_alerts = len(store.pending_alerts(100))
    pos_before = get_pos(store, W1, TOKEN)
    run_all(follower, engine, {W1, W2, W3})          # same range again
    assert len(store.pending_alerts(100)) == n_alerts
    assert get_pos(store, W1, TOKEN) == pos_before


def test_getlogs_range_split_and_wallet_chunks(store, chain):
    # range 1-40 exceeds fake node limit (15) and 3 wallets exceed chunk size (2)
    _, follower, _ = build(store, chain)
    logs = follower.fetch_logs(1, 40, sorted({W1, W2, W3}))
    assert len(logs) == 5


def test_untracked_ignored_and_contract_filter(store, chain):
    store, follower, engine = build(store, chain)
    assert run_all(follower, engine, {W2}) and all(
        e.wallet == W2 for e in follower.scan(1, 40, {W2}).events)
    assert follower.filter_eoas({W1, POOL}) == {W1}


def test_classify_edge_cases():
    kw = dict(tx="0x1", block=1, wallet=W1, dust_wei=10 ** 14)
    dust = classify(token_deltas={TOKEN: 5}, trade_hype_wei=-10 ** 13, **kw)
    assert dust[0].kind == "TRANSFER_IN"
    sw = classify(token_deltas={TOKEN: 5, POOL: -7}, trade_hype_wei=0, **kw)[0]
    assert (sw.kind, sw.counter_token, sw.counter_qty) == ("SWAP", POOL, 7)
    multi = classify(token_deltas={TOKEN: 5, POOL: 3}, trade_hype_wei=-E18, **kw)
    assert {e.kind for e in multi} == {"MULTI"}
    assert classify(token_deltas={TOKEN: 0}, trade_hype_wei=-E18, **kw) == []


def test_leaderboard_parse_diff_and_curl():
    cfg = LeaderboardConfig(items_path="data.rows", top_n=2,
                            fields={"address": "wallet", "pnl": "stats.pnl", "trades": "n"})
    payload = {"data": {"rows": [
        {"wallet": W1.upper().replace("0X", "0x"), "stats": {"pnl": "100.5"}, "n": "3"},
        {"wallet": W2, "stats": {"pnl": 900}, "n": 9},
        {"wallet": W3, "stats": {"pnl": 50}, "n": 1},
        {"wallet": "not-an-address", "stats": {"pnl": 1e9}},
    ]}}
    ts = parse_items(payload, cfg)
    assert [(t.address, t.rank, t.pnl) for t in ts] == [(W2, 1, 900.0), (W1, 2, 100.5)]

    prev = {W1: {"rank": 1, "pnl": 100.5}, W3: {"rank": 2, "pnl": 50}}
    changes = diff_leaderboard(prev, ts, rank_change=1, pnl_change=10)
    kinds = sorted((k, t.address) for k, _, t in changes)
    assert kinds == sorted([("ENTERED", W2), ("RANK", W1), ("EXITED", W3)])

    spec = parse_curl("curl 'https://api.example.xyz/v1/traders?sort=pnl&limit=100' "
                      "-H 'accept: application/json' -H 'content-length: 0' "
                      "-H 'x-api-key: abc' -b 'cf=1'")
    assert spec["url"] == "https://api.example.xyz/v1/traders"
    assert spec["params"] == {"sort": "pnl", "limit": "100"}
    assert spec["headers"] == {"accept": "application/json", "x-api-key": "abc", "cookie": "cf=1"}
    assert spec["method"] == "GET"

    g = guess_mapping({"ok": True, "result": {"traders": [
        {"address": W1, "realizedPnl": 1, "totalWorth": 2, "volumeUsd": 3, "tradeCount": 4}]}})
    assert g["items_path"] == "result.traders"
    assert g["fields"] == {"address": "address", "pnl": "realizedPnl", "worth": "totalWorth",
                           "volume": "volumeUsd", "trades": "tradeCount"}
    json.dumps(g)


def test_discover_candidates(monkeypatch):
    from agora_tracker import discover as d
    pages = {
        "https://www.agoradex.app/traders":
            '<script src="/_next/static/chunks/app.js"></script>',
        "https://www.agoradex.app/_next/static/chunks/app.js":
            'const B="https://api.agoradex.app/v1";fetch(B+"/traders?sort="+s);'
            '"static/chunks/lazy.js";"https://x.com/agora_elysium"',
        "https://www.agoradex.app/_next/static/chunks/lazy.js":
            'fetch("/api/leaderboard/traders",{})',
    }
    monkeypatch.setattr(d, "_fetch", lambda s, url, timeout: pages.get(url))
    abs_urls, paths = d.collect_candidates("https://www.agoradex.app/traders")
    assert abs_urls == ["https://api.agoradex.app/v1"]
    assert set(paths) == {"/traders", "/api/leaderboard/traders"}
    urls = d.candidate_requests("https://www.agoradex.app/traders", abs_urls, paths)
    assert "https://api.agoradex.app/v1/traders" in urls
    assert "https://www.agoradex.app/api/leaderboard/traders" in urls
    assert "https://www.agoradex.app/traders" not in urls


def test_quiet_mode_keeps_trade_alerts_and_records_signals_silently(store, chain):
    """cluster_alerts / leaderboard_alerts off: buys, sells and transfers still alert; the
    cluster signal and leaderboard history are still recorded, but nothing else is sent."""
    store, follower, engine = build(store, chain)
    engine.cfg.cluster_alerts = False
    run_all(follower, engine, {W1, W2, W3})
    texts = [r["text"] for r in store.pending_alerts(100)]
    assert not any("CLUSTER BUY" in t for t in texts)
    assert any("new position" in t for t in texts) and any("OUT (sell?)" in t for t in texts)
    with store.tx() as c:
        assert c.execute(sa.text("SELECT count(*) FROM signals")).scalar() == 1


def test_leaderboard_changes_recorded_without_alerts(store, monkeypatch):
    from agora_tracker.leaderboard import Trader
    from tests.test_p0_regressions import make_tracker
    t = make_tracker(store, monkeypatch)
    t.cfg.exclude_contracts = False
    t.cfg.alerts.leaderboard_alerts = False
    t.cfg.leaderboard.top_n = 2

    class LB:
        rows = [Trader(address=W1, rank=1, pnl=100.0), Trader(address=W2, rank=2, pnl=50.0)]
        last_payload = None
        last_meta: dict = {}

        def fetch(self):
            return self.rows
    t.leaderboard = LB()
    t.refresh_leaderboard()
    LB.rows = [Trader(address=W3, rank=1, pnl=900.0), Trader(address=W1, rank=2, pnl=10.0)]
    t.refresh_leaderboard()
    assert store.pending_alerts(100) == []
    with store.tx() as c:
        assert c.execute(sa.text("SELECT count(*) FROM leaderboard_snapshots")).scalar() == 2
    assert W3 in t.tracked
