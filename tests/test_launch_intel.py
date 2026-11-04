"""Launch intelligence: bundle-aware scoring, due diligence, new-venue detection, paper log.

Pure logic needs no database; store/engine tests use the PostgreSQL test database and fake
JSON-RPC nodes. No network.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from test_tracker import E18, FEE, POOL, TOKEN, W1, W2, W3, FakeChain, build

from agora_tracker import cards
from agora_tracker import launchintel as li
from agora_tracker import schema as t
from agora_tracker.cards import Links
from agora_tracker.config import IntelConfig, LinksConfig
from agora_tracker.market import TokenInfo

L = Links(LinksConfig())
A = lambda n: "0x" + f"{n:02x}" * 20   # noqa: E731


# --- scoring --------------------------------------------------------------------------
def test_bundle_detection_needs_repeated_same_block_buying():
    rows = []
    for i in range(5):                      # five tokens, same four wallets, same block each
        for w in (A(1), A(2), A(3), A(4)):
            rows.append((100 + i, A(0xe0 + i), w))
    rows += [(300, A(0xf0), A(5)), (300, A(0xf0), A(6))]          # a pair once: not a bundle
    rows += [(400, A(0xf1), A(5)), (401, A(0xf1), A(7))]
    assert li.find_bundle_wallets(rows) == {A(1), A(2), A(3), A(4)}
    assert li.find_bundle_wallets(rows[:12]) == set()                # only 3 blocks: below min_hits


def test_independent_count_collapses_bundles_and_same_block_pairs():
    bundles = {A(1), A(2), A(3)}
    assert li.count_independent([(A(1), 10), (A(2), 11), (A(3), 12)], bundles) == 1
    assert li.count_independent([(A(1), 10), (A(2), 11), (A(9), 12)], bundles) == 2
    # unknown wallets that bought in the very same block are one operator
    assert li.count_independent([(A(7), 10), (A(8), 10), (A(9), 11)], set()) == 2
    assert li.count_independent([(A(7), 10), (A(8), 11), (A(9), 12)], set()) == 3


def test_tier_labels():
    assert li.tier_label(3, 3, 1800) == "3 independent in 30 min"
    assert li.tier_label(1, 4, 1800) == "bundle entry (one operator)"
    assert li.tier_label(2, 3, 1800) == "2 independent, 3 addresses"


# --- due diligence against a fake node ------------------------------------------------
WHYPE, FACTORY, ROUTER = A(0x77), A(0xfa), A(0x2d)
POOL_A, CREATOR, BUYER = A(0xb1), A(0xc1), A(0xb2)
TX = "0x" + "ab" * 32
CODE = "0x63" + li.selector("mint(address,uint256)")[2:] + "63" + li.selector("owner()")[2:]


def word(n: int) -> str:
    return "0x" + f"{n:064x}"


def padt(a: str) -> str:
    return "0x" + "0" * 24 + a[2:]


def xfer(frm, to, v, blk):
    return {"topics": [li.TOPIC_TRANSFER, padt(frm), padt(to)], "data": word(v),
            "blockNumber": hex(blk)}


class FakeNode:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.logs = [xfer(li.ZERO, CREATOR, 1000 * E18, 90), xfer(CREATOR, POOL_A, 700 * E18, 91),
                     xfer(POOL_A, BUYER, 10 * E18, 95), xfer(BUYER, POOL_A, 4 * E18, 96)]

    def call(self, method, params):
        self.calls += 1
        if self.fail:
            raise RuntimeError("node down")
        if method == "eth_getCode":
            return CODE if params[0] == TOKEN else "0x6000"
        if method == "eth_blockNumber":
            return hex(100)
        if method == "eth_getTransactionByHash":
            return {"to": ROUTER, "from": BUYER}
        if method == "eth_getLogs":
            return self.logs
        if method == "eth_getBalance":
            return hex(0)
        if method == "eth_call":
            to, data = params[0]["to"], params[0]["data"]
            sel = data[:10]
            if sel == li.selector("decimals()"):
                return word(18)
            if sel == li.selector("totalSupply()"):
                return word(1000 * E18)
            if sel == li.selector("owner()"):
                return padt(CREATOR)
            if sel == li.selector("getPool(address,address,uint24)"):
                fee = int(data[-64:], 16)
                return padt(POOL_A) if (to == FACTORY and fee == 10000) else padt(li.ZERO)
            if sel == li.selector("balanceOf(address)"):
                who = "0x" + data[-40:]
                if to == WHYPE and who == POOL_A:
                    return word(500 * E18)
                if to == TOKEN and who == CREATOR:
                    return word(300 * E18)
                return word(0)
        raise RuntimeError(f"unsupported {method}")


def dd_for(node, **kw):
    return li.DueDiligence(node, wrapped=WHYPE, factories=[FACTORY], **kw)


def test_due_diligence_reads_pool_owner_risks_and_sellers():
    dd = dd_for(FakeNode()).probe(TOKEN, TX)
    assert dd.pool == POOL_A and dd.pool_kind == "pool" and dd.liquidity_hype == 500
    assert dd.router == ROUTER
    assert dd.risks == ["owner can mint more supply"]
    assert dd.owner == CREATOR and dd.owner_pct == pytest.approx(30)
    assert dd.sellers == 2 and dd.sold_pct == pytest.approx(70.4)
    assert dd.top_holder == CREATOR and dd.top_is_owner and dd.top_pct == pytest.approx(30)
    assert dd.complete and not dd.partial


def test_due_diligence_curve_when_no_pool():
    node = FakeNode()
    node.call_orig = node.call

    def call(method, params):
        if method == "eth_call" and params[0]["data"][:10] == li.selector("balanceOf(address)") \
                and params[0]["to"] == TOKEN and params[0]["data"].endswith(ROUTER[2:]):
            return word(900 * E18)
        if method == "eth_getBalance":
            return hex(975 * E18)
        return node.call_orig(method, params)
    node.call = call
    dd = li.DueDiligence(node, wrapped=WHYPE, factories=[]).probe(TOKEN, TX)
    assert dd.pool == ROUTER and dd.pool_kind == "curve" and dd.liquidity_hype == 975
    assert dd.router is None


def test_due_diligence_cached_and_never_raises_when_node_is_down():
    node = FakeNode()
    clock = [0.0]
    d = dd_for(node, clock=lambda: clock[0], cooldown_s=60)
    first = d.probe(TOKEN, TX)
    n = node.calls
    assert d.probe(TOKEN, TX) is first and node.calls == n          # cached
    node.fail = True
    other = A(0x55)
    bad = d.probe(other, TX)
    assert bad is not None and bad.partial and bad.pool is None     # fields missing, no raise
    calls = node.calls
    assert d.probe(A(0x56), TX) is None and node.calls == calls     # cooling down: no RPC at all
    clock[0] += 61
    node.fail = False
    assert d.probe(A(0x56), TX).pool == POOL_A                      # recovered


def test_due_diligence_respects_time_budget():
    node = FakeNode()
    t_ = [0.0]

    def clock():
        t_[0] += 2.0                       # every call "takes" 2 s
        return t_[0]
    dd = dd_for(node, budget_s=5.0, clock=clock).probe(TOKEN, TX)
    assert dd.partial and node.calls < 8


# --- rendering ------------------------------------------------------------------------
def buyers(n):
    return [{"wallet": A(0x10 + i), "label": None, "rank": i + 1, "pnl": 1000.0,
             "pnl_unit": "USD"} for i in range(n)]


def test_strong_cluster_card_shows_tier_and_dd_with_venue_buttons():
    info = TokenInfo(address=TOKEN, symbol="VOLTA", launchpad="volta", mcap_hype=1000.0)
    dd = li.DD(token=TOKEN, pool=ROUTER, pool_kind="curve", liquidity_hype=975.0, router=A(0x1b),
               owner=CREATOR, owner_pct=0.0, risks=["owner can mint more supply"], sellers=0,
               decimals=18)
    p = {"type": "cluster", "strong": True, "token": TOKEN, "symbol": "VOLTA", "min_hype": 0.5,
         "window_min": 30, "buyers": buyers(3), "tx": TX, "independent": 3, "n_raw": 3,
         "tier": "3 independent in 30 min"}
    text, kb = cards.render(p, L, info, 40.0, dd=dd)
    assert "CLUSTER BUY" in text and "✅ independent" in text
    assert "3 independent in 30 min" in text
    assert "Bonding curve" in text and "975.00 HYPE" in text
    assert "owner can mint more supply" in text and "Nobody has sold" in text
    urls = [b["url"] for row in kb["inline_keyboard"] for b in row]
    assert not any("rabby" in u or "okx" in u or "kleos" in u or "boxa" in u for u in urls)
    assert L.address(ROUTER) in urls and L.address(A(0x1b)) in urls and L.tx(TX) in urls
    assert "Kleos" not in text.split("🛡")[0]


def test_known_launchpads_keep_the_existing_buttons():
    info = TokenInfo(address=TOKEN, symbol="X", launchpad="kleos")
    p = {"type": "cluster", "token": TOKEN, "symbol": "X", "min_hype": 0.5, "window_min": 60,
         "buyers": buyers(3)}
    _, kb = cards.render(p, L, info, 40.0)
    urls = [b["url"] for row in kb["inline_keyboard"] for b in row]
    assert any("rabby" in u for u in urls) and any("kleos" in u for u in urls)
    text, _ = cards.render(p, L, None, None)           # no market info at all: still renders
    assert "CLUSTER BUY" in text


def test_launch_cards():
    p = {"type": "launch", "kind": "new_launchpad", "launchpad": "zeta",
         "tokens": [{"token": TOKEN, "symbol": "ZZ"}]}
    text, kb = cards.render(p, L)
    assert "NEW LAUNCHPAD" in text and "zeta" in text and TOKEN in text
    p = {"type": "launch", "kind": "new_pool", "token": TOKEN, "symbol": "PERPME",
         "pool": POOL_A, "factory": FACTORY, "fee": 10000, "liquidity_hype": 812.0, "age_s": 180}
    text, kb = cards.render(p, L, None, 40.0)
    assert "NEW DEX POOL" in text and "812.00 HYPE" in text and "fee 1%" in text
    urls = [b["url"] for row in kb["inline_keyboard"] for b in row]
    assert L.address(POOL_A) in urls
    assert cards.wants_dd(p) and not cards.wants_dd({"type": "trade", "kind": "SELL"})


# --- store: launch venues, paper log ---------------------------------------------------
def test_register_launchpads_seeds_then_alerts_once(store):
    with store.tx() as c:
        c.execute(pg_insert(t.token_profiles).values(
            chain_id=99901, address=A(0x31), symbol="OLD", launchpad="kleos"))
    with store.tx() as c:
        assert store.register_launchpads(c, {"boxa": [{"token": A(0x32), "symbol": "B"}]}) == []
    with store.tx() as c:
        assert store.register_launchpads(c, {"kleos": [], "boxa": []}) == []
    with store.tx() as c:
        new = store.register_launchpads(c, {"boxa": [], "zeta": [{"token": A(0x33), "symbol": "Z"}]})
    assert [n for n, _ in new] == ["zeta"]
    with store.tx() as c:
        assert store.register_launchpads(c, {"zeta": []}) == []


class PoolNode:
    def __init__(self):
        self.head = 100
        self.logs = []
        self.liq = 500 * E18

    def call(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            return [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi]
        if method == "eth_call":
            sel = params[0]["data"][:10]
            if sel == li.selector("balanceOf(address)"):
                return word(self.liq)
            if sel == li.selector("symbol()"):
                return "0x" + "20".rjust(64, "0") + "3".rjust(64, "0") + b"NEW".hex().ljust(64, "0")
        raise RuntimeError(method)


def pool_log(token, pool, blk, wrapped_first=True):
    a, b = (WHYPE, token) if wrapped_first else (token, WHYPE)
    return {"address": FACTORY, "blockNumber": hex(blk),
            "topics": [li.TOPIC_POOL_CREATED, padt(a), padt(b), word(10000)],
            "data": word(200) + "0" * 24 + pool[2:]}


def test_launch_watcher_alerts_once_for_funded_new_token_pools(store):
    node, clock = PoolNode(), [1000.0]
    w = li.LaunchWatcher(store, node, wrapped=WHYPE, factories=[], min_liquidity_hype=300,
                         delay_s=120, now=lambda: clock[0])
    assert w.scan() == 0                              # first start: remember head, no backfill
    node.head = 110
    node.logs = [pool_log(TOKEN, POOL_A, 105), pool_log(A(0x44), A(0x45), 106),
                 pool_log(A(0x46), A(0x47), 107, wrapped_first=False)]
    node.logs.append({**pool_log(A(0x48), A(0x49), 108), "topics": [
        li.TOPIC_POOL_CREATED, padt(A(0x48)), padt(A(0x4a)), word(500)]})    # no WHYPE side
    assert w.scan() == 3
    assert w.scan() == 0                              # cursor advanced: nothing twice
    assert w.evaluate() == 0                          # too young
    clock[0] += 200
    node.liq = 10 * E18
    assert w.evaluate() == 0                          # unfunded: keep waiting
    node.liq = 500 * E18
    assert w.evaluate() == 3
    assert w.evaluate() == 0                          # alerted once
    alerts = [r for r in store.pending_alerts(50) if (r["payload"] or {}).get("type") == "launch"]
    assert len(alerts) == 3
    p = next(r["payload"] for r in alerts if r["payload"]["pool"] == POOL_A)
    assert p["token"] == TOKEN and p["liquidity_hype"] == 500 and p["symbol"] == "NEW"
    assert store.launch_factories() == [FACTORY]


def test_launch_watcher_skips_old_tokens_and_expires_unfunded_pools(store):
    node, clock = PoolNode(), [1000.0]
    w = li.LaunchWatcher(store, node, wrapped=WHYPE, factories=[], min_liquidity_hype=300,
                         delay_s=60, max_age_s=3600, now=lambda: clock[0])
    w.scan()
    node.head = 105
    node.logs = [pool_log(TOKEN, POOL_A, 103), pool_log(A(0x44), A(0x45), 104)]
    with store.tx() as c:
        c.execute(pg_insert(t.token_profiles).values(
            chain_id=99901, address=TOKEN, symbol="OLD", launchpad="kleos",
            launched_at=datetime.now(timezone.utc) - timedelta(days=10)))
    assert w.scan() == 2
    clock[0] += 100
    assert w.evaluate() == 1                          # the old token's new pool is not news
    node.liq = 1 * E18
    clock[0] += 5000
    assert w.evaluate() == 0
    assert store.launch_pending_pools() == []         # expired, no longer polled


# --- engine: strong signal, bundles, paper trade --------------------------------------
def three_buys(same_block: bool):
    c = FakeChain()
    for i, w in enumerate((W1, W2, W3)):
        blk = 10 if same_block else 10 + i
        c.add_tx(f"0x{i:02x}", blk, w, value=E18, transfers=[(POOL, w, 600 * E18)],
                 bal={w: (5 * E18, 4 * E18 - FEE)})
    return c


def signals(store, kind):
    with store.engine.connect() as c:
        return c.execute(sa.text("SELECT id, details FROM signals WHERE kind = :k"),
                         {"k": kind}).all()


def alerts(store):
    return [r["payload"] for r in store.pending_alerts(100) if r["payload"]]


def run(store, chain, bundles=None):
    store, f, e = build(store, chain)
    e.intel = IntelConfig()
    if bundles is not None:
        e._bundles, e._bundles_at = set(bundles), time.monotonic()
    e.apply_range(f.scan(1, 40, {W1, W2, W3}), 40)
    return e


def test_three_unrelated_wallets_fire_strong_signal_and_open_one_paper_trade(store):
    run(store, three_buys(same_block=False))
    assert len(signals(store, "independent_buy")) == 1
    cl = [p for p in alerts(store) if p["type"] == "cluster"]
    strong = [p for p in cl if p.get("strong")]
    assert len(cl) == 1 and len(strong) == 1 and strong[0]["independent"] == 3   # one card, not two
    assert strong[0]["tier"] == "3 independent in 30 min" and strong[0]["tx"]
    with store.engine.connect() as c:
        row = c.execute(sa.text("SELECT token, n_independent, n_raw, entry_price_hype, signal_id "
                                "FROM paper_trades")).one()
    assert row.token == TOKEN and row.n_independent == 3 and row.signal_id is not None
    assert Decimal(row.entry_price_hype) == pytest.approx(Decimal(1) / Decimal(600), rel=1e-9)


def test_same_block_buyers_are_one_operator_no_strong_signal(store):
    run(store, three_buys(same_block=True))
    assert signals(store, "independent_buy") == []
    cl = [p for p in alerts(store) if p["type"] == "cluster"]
    assert len(cl) == 1 and cl[0]["tier"] == "bundle entry (one operator)"
    assert not cl[0].get("strong")
    with store.engine.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM paper_trades")).scalar() == 0


def test_known_bundle_wallets_do_not_count_as_independent(store):
    run(store, three_buys(same_block=False), bundles={W1, W2})
    assert signals(store, "independent_buy") == []
    cl = [p for p in alerts(store) if p["type"] == "cluster"]
    assert cl[0]["independent"] == 2 and cl[0]["tier"] == "2 independent, 3 addresses"


def test_replaying_the_range_does_not_duplicate_signal_or_paper_trade(store):
    chain = three_buys(same_block=False)
    st, f, e = build(store, chain)
    res = f.scan(1, 40, {W1, W2, W3})
    e.apply_range(res, 40)
    e.apply_range(res, 40)
    assert len(signals(store, "independent_buy")) == 1
    with store.engine.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM paper_trades")).scalar() == 1


def test_engine_bundle_refresh_from_database(store):
    c = FakeChain()
    for i in range(5):                     # three wallets, same block, five times
        for j, w in enumerate((W1, W2, W3)):
            c.add_tx(f"0x{i}{j}", 4 + 5 * i, w, value=E18, transfers=[(POOL, w, 100 * E18)],
                     bal={w: (5 * E18, 4 * E18 - FEE)})
    st, f, e = build(store, c)
    e.apply_range(f.scan(1, 40, {W1, W2, W3}), 40)
    assert st.bundle_wallets(lookback_days=14000, min_wallets=3, min_hits=5) == {W1, W2, W3}


# --- paper report -----------------------------------------------------------------------
def test_paper_outcome_and_summary():
    base = 1_000_000.0
    tr = {"token": A(1), "opened_ts": base, "n_independent": 3, "entry": 1.0,
          "snapshots": [(base + 600, 1.5), (base + 3000, 4.4), (base + 4000, 0.5),
                        (base + 3600 * 7, 0.5)]}
    o = li.paper_outcome(tr, now=base + 3600 * 8, slippage_pct=10)
    assert o["peak"] == pytest.approx(4.0)                    # 4.4 / 1.1 fill
    assert o["h1"] == pytest.approx(4.0)                      # last snapshot at or before +1h
    assert o["h6"] == pytest.approx(0.5 / 1.1) and "h24" not in o
    unpriced = {"token": A(2), "opened_ts": base, "n_independent": 3, "entry": None, "snapshots": []}
    s = li.summarize_paper([tr, unpriced], now=base + 3600 * 8)
    assert s["signals"] == 2 and s["priced"] == 1 and s["pct_ge_2x"] == 100
    assert s["pct_below_entry_now"] == 100
    assert "anecdote" in li.format_paper_report(s)


def test_store_paper_trades_returns_snapshots_after_entry(store):
    opened = datetime.now(timezone.utc) - timedelta(hours=3)
    with store.tx() as c:
        store.record_paper_trade(c, token=TOKEN, signal_id=None, opened_at=int(opened.timestamp()),
                                 block=1, entry_price_hype=Decimal("0.0016"), n_independent=3,
                                 n_raw=3, details={})
        for k, px in ((-1, 9), (1, 0.002), (2, 0.004)):
            c.execute(pg_insert(t.token_market_snapshots).values(
                chain_id=99901, token=TOKEN, captured_at=opened + timedelta(hours=k),
                source="agora", price_hype=Decimal(str(px))))
        assert store.record_paper_trade(c, token=TOKEN, signal_id=None, opened_at=1, block=1,
                                        entry_price_hype=None, n_independent=3, n_raw=3,
                                        details={}) is False
    rows = store.paper_trades()
    assert len(rows) == 1 and [p for _, p in rows[0]["snapshots"]] == [0.002, 0.004]
    s = li.summarize_paper(rows, now=time.time(), slippage_pct=0)
    assert s["median_peak"] == pytest.approx(2.5)
