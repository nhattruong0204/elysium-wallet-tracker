"""Scan strategy: filtered (topic OR-lists) vs full (one unfiltered query, local filter),
learned topic-list and block-range limits, and the RPC cost of each at growing wallet counts."""

from __future__ import annotations

from collections import defaultdict

import pytest
from test_tracker import E18, FEE, POOL, TOKEN, W1, W2, W3, FakeChain

from agora_tracker import rpc as rpc_mod
from agora_tracker.chain import TRANSFER_TOPIC, ChainFollower, pad_topic
from agora_tracker.config import ChainConfig
from agora_tracker.engine import WEI
from agora_tracker.rpc import RpcError


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(rpc_mod.time, "sleep", lambda s: None)


class Chain(FakeChain):
    """FakeChain that counts calls per method and can impose node limits."""

    def __init__(self, *, max_alternatives: int | None = None, max_span: int = 15,
                 reject_unfiltered: bool = False):
        super().__init__()
        self.by_method: dict[str, int] = defaultdict(int)
        self.max_alternatives, self.max_span = max_alternatives, max_span
        self.reject_unfiltered = reject_unfiltered
        self.log_filters: list[list] = []

    def call(self, method, params):
        self.by_method[method] += 1
        if method == "eth_getLogs":
            f = params[0]
            self.log_filters.append(f["topics"])
            frm, to = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            if to - frm + 1 > self.max_span:
                raise RpcError("block range too large", -32602)
            if self.reject_unfiltered and len(f["topics"]) == 1:
                raise RpcError("query returned more than 10000 results", -32005)
            for w in f["topics"][1:]:
                if self.max_alternatives and isinstance(w, list) and len(w) > self.max_alternatives:
                    raise RpcError("Too many topic alternatives", -32602)
            # the parent class enforces its own 15-block cap; widen it for this fake
            f2 = dict(f)
            out = []
            for lg in self.logs:
                b = int(lg["blockNumber"], 16)
                if frm <= b <= to and all(self._match(lg["topics"][i], w)
                                          for i, w in enumerate(f2["topics"])):
                    out.append(lg)
            return out
        return super().call(method, params)


def populate(chain: Chain):
    chain.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 1000 * E18)],
                 bal={W1: (10 * E18, 9 * E18 - FEE)})
    chain.add_tx("0xb", 12, W2, value=E18, transfers=[(POOL, W2, 500 * E18)],
                 bal={W2: (10 * E18, 9 * E18 - FEE)})
    # unrelated traffic by wallets we do not track: must never be stored in full mode
    other = "0x" + "e1" * 20
    for i in range(30):
        chain.add_tx(f"0xf{i:02x}", 20 + i % 10, other, value=E18,
                     transfers=[(POOL, other, (i + 1) * E18)], bal={other: (10 * E18, 9 * E18)})


def follower(store, chain, **kw) -> ChainFollower:
    cfg = ChainConfig(rpc_urls=["fake"], wallet_chunk=kw.pop("wallet_chunk", 2), **kw)
    return ChainFollower(chain, store, cfg, int(0.001 * WEI), set())


def synthetic(n: int) -> list[str]:
    return ["0x" + f"{0x5000 + i:040x}" for i in range(n)]


def key(res):
    return [(e.kind, e.wallet, e.token, e.block, e.tx, e.qty, e.hype_wei) for e in res.events]


def test_full_and_filtered_scans_produce_identical_results(store):
    c1, c2 = Chain(), Chain()
    populate(c1), populate(c2)
    wallets = {W1, W2, W3, *synthetic(7)}
    a = follower(store, c1, scan_mode="filtered").scan(1, 40, wallets)
    b = follower(store, c2, scan_mode="full").scan(1, 40, wallets)
    assert key(a) == key(b) and key(a)
    ev = lambda r: sorted((t["tx_hash"], t["log_index"], t["from_address"], t["to_address"]) for t in r.transfers)  # noqa: E731
    assert ev(a) == ev(b)
    assert {t["tx_hash"] for t in b.transfers} == {"0xa", "0xb"}     # nothing chain-wide stored


def test_full_scan_costs_one_get_logs_per_range_whatever_the_wallet_count(store):
    for n in (10, 100, 1000, 5000):
        chain = Chain(max_span=10_000)
        populate(chain)
        f = follower(store, chain, scan_mode="full", wallet_chunk=50)
        f.scan(1, 40, {W1, W2, *synthetic(n)})
        assert chain.by_method["eth_getLogs"] == 1, n


def test_filtered_scan_cost_grows_linearly_with_wallets(store):
    costs = {}
    for n in (50, 331, 1000, 5000):
        chain = Chain(max_span=10_000)
        populate(chain)
        f = follower(store, chain, scan_mode="filtered", wallet_chunk=50)
        f.scan(1, 40, {W1, W2, *synthetic(n - 2)})
        costs[n] = chain.by_method["eth_getLogs"]
        assert costs[n] == f.filtered_calls(n)
    assert costs == {50: 2, 331: 14, 1000: 40, 5000: 200}


def test_full_scan_span_is_capped_below_the_unfiltered_node_limit(store):
    f = follower(store, Chain(), scan_mode="full", max_block_range=2000, full_scan_max_span=1000)
    assert f.next_span(500) == 1000                     # Kleos: unfiltered queries <= 1,000 blocks
    g = follower(store, Chain(), scan_mode="filtered", max_block_range=2000)
    assert g.next_span(500) == 2000


def test_auto_mode_falls_back_to_filters_on_a_busy_chain_and_tests_again(store, monkeypatch):
    chain = Chain(max_span=10_000)
    for i in range(60):                                  # 60 logs in 20 blocks = 3/block
        chain.add_tx(f"0xb{i:02x}", 1 + i % 20, W1, value=E18,
                     transfers=[(POOL, W1, (i + 1) * E18)], bal={W1: (10 * E18, 9 * E18)})
    f = follower(store, chain, scan_mode="auto", wallet_chunk=2, full_scan_above_calls=2,
                 full_scan_max_logs_per_block=2.0)
    wallets = {W1, W2, W3}
    assert f.uses_full_scan(len(wallets))
    f._fetch_full(1, 20, wallets)
    assert f.last_density == 3.0 and not f.uses_full_scan(len(wallets))   # too busy: filters
    f._full_blocked_until = 0                            # ten minutes later
    assert f.uses_full_scan(len(wallets))                # tested again
    quiet = follower(store, chain, scan_mode="auto", wallet_chunk=2, full_scan_above_calls=2,
                     full_scan_max_logs_per_block=5.0)
    quiet._fetch_full(1, 20, wallets)
    assert quiet.uses_full_scan(len(wallets))            # under the cap: stays full


def test_auto_mode_switches_to_full_above_the_call_threshold(store):
    f = follower(store, Chain(), scan_mode="auto", wallet_chunk=50, full_scan_above_calls=6)
    assert not f.uses_full_scan(150)       # 2 * 3 = 6 calls: still filtered
    assert f.uses_full_scan(151)           # 2 * 4 = 8 calls: full
    assert follower(store, Chain(), scan_mode="filtered").uses_full_scan(10_000) is False
    assert follower(store, Chain(), scan_mode="full").uses_full_scan(1) is True


def test_a_node_topic_limit_is_learned_and_the_result_is_unchanged(store):
    ref, lim = Chain(max_span=10_000), Chain(max_alternatives=3, max_span=10_000)
    populate(ref), populate(lim)
    wallets = {W1, W2, W3, *synthetic(9)}
    want = follower(store, ref, scan_mode="filtered", wallet_chunk=8).scan(1, 40, wallets)
    f = follower(store, lim, scan_mode="filtered", wallet_chunk=8)
    got = f.scan(1, 40, wallets)
    assert key(got) == key(want) and key(got)
    assert f.wallet_chunk() <= 3                          # 8 -> 4 -> 2: learned, kept
    calls = lim.by_method["eth_getLogs"]
    f.scan(1, 40, wallets)
    assert lim.by_method["eth_getLogs"] - calls == f.filtered_calls(len(wallets))   # no more errors


def test_a_range_limit_is_learned_then_recovers(store):
    chain = Chain(max_span=8)
    f = follower(store, chain, scan_mode="filtered", max_block_range=64, min_block_range=4)
    populate(chain)
    res = f.scan(1, 40, {W1, W2})
    assert {e.tx for e in res.events} == {"0xa", "0xb"}   # split down to 8-block slices
    assert f.span_cap is not None and f.span_cap < 64
    first = f.next_span()
    assert first <= f.span_cap
    for _ in range(20):
        f.next_span()
    assert f.next_span() > first                          # doubled back after clean ranges
    for _ in range(200):
        f.next_span()
    assert f.next_span() == 64 and f.span_cap is None     # fully recovered


def test_span_never_shrinks_below_the_floor(store):
    f = follower(store, Chain(), max_block_range=100, min_block_range=30)
    f._shrink_span(40)
    assert f.span_cap == 30
    f._shrink_span(10)
    assert f.span_cap == 30


def test_full_scan_falls_back_to_filters_when_the_node_refuses_it(store):
    chain = Chain(max_span=10_000, reject_unfiltered=True)
    populate(chain)
    f = follower(store, chain, scan_mode="full", wallet_chunk=2)
    res = f.scan(1, 40, {W1, W2, W3})
    assert {e.tx for e in res.events} == {"0xa", "0xb"}   # served by filtered queries
    assert any(len(t) == 2 for t in chain.log_filters)    # topic-filtered queries were used
    n = len(chain.log_filters)
    f.scan(1, 40, {W1, W2, W3})
    assert all(len(t) >= 2 for t in chain.log_filters[n:])     # only wallet-filtered queries
    assert len(chain.log_filters) - n == f.filtered_calls(3)   # no retry of the refused mode


def test_full_scan_ignores_erc721_and_removed_logs(store):
    chain = Chain(max_span=10_000)
    populate(chain)
    chain.logs.append({"address": TOKEN, "blockNumber": hex(15), "transactionHash": "0xnft",
                       "logIndex": hex(0), "data": "0x",
                       "topics": [TRANSFER_TOPIC, pad_topic(W1), pad_topic(POOL),
                                  "0x" + "00" * 31 + "01"]})
    chain.logs.append({**chain.logs[0], "transactionHash": "0xgone", "removed": True})
    f = follower(store, chain, scan_mode="full")
    got = f._fetch_full(1, 40, {W1, W2})
    hashes = {lg["transactionHash"] for lg in got}
    assert "0xgone" not in hashes and hashes >= {"0xa", "0xb"}
    assert f.stats["full_scan_logs"] > len(got)           # density is tracked, evidence is not


@pytest.mark.parametrize("n_wallets", [100, 500, 1000, 5000])
def test_local_filtering_cost_is_independent_of_the_wallet_count(store, n_wallets):
    """50k chain-wide logs in one range, N tracked wallets: the local filter is a set lookup
    per log, so the time must stay flat from 100 to 5,000 wallets (and far below any RPC call)."""
    import time

    wallets = ["0x" + f"{0x7000 + i:040x}" for i in range(n_wallets)]
    mine = set(wallets[:50])
    logs = []
    for i in range(50_000):
        src = wallets[i % 50] if i % 1000 == 0 else "0x" + f"{0xA0000 + i:040x}"
        logs.append({"address": TOKEN, "blockNumber": hex(1 + i % 900),
                     "transactionHash": f"0x{i:064x}", "logIndex": hex(0), "data": "0x01",
                     "topics": [TRANSFER_TOPIC, pad_topic(src), pad_topic(POOL)]})

    class Fixed:
        def call(self, method, params):
            return logs

    f = follower(None, Fixed(), scan_mode="full")
    t0 = time.perf_counter()
    got = f._fetch_full(1, 900, set(wallets))
    dt = time.perf_counter() - t0
    assert len(got) == 50 and {topic[-40:] for lg in got for topic in lg["topics"][1:2]} <= \
        {w[2:] for w in mine}
    assert dt < 1.0, f"{n_wallets} wallets: {dt:.2f}s"
