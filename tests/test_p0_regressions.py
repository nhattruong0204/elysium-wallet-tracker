"""P0 regression tests. Each test named after an audit finding (docs/AUDIT.md) failed on
v0.1 (ea11ad4) and must pass now."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from test_tracker import E18, FEE, FRESH, POOL, TOKEN, W1, W2, FakeChain, build

from agora_tracker import rpc as rpc_mod
from agora_tracker.__main__ import Tracker
from agora_tracker.config import AlertConfig, ChainConfig, Config, LeaderboardConfig, TelegramConfig
from agora_tracker.db import Store
from agora_tracker.leaderboard import Trader, parse_items
from agora_tracker.notify import Notifier
from agora_tracker.rpc import RpcClient, RpcError, redact

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(rpc_mod.time, "sleep", lambda s: None)


def scan_apply(f, e, wallets, frm=1, to=40):
    ev, ts = f.scan(frm, to, wallets)
    e.apply_range(ev, ts, to)
    return ev


# --- C1: missing tx data must fail the range, not drop the trade ----------------------
def test_C1_tx_lookup_failure_fails_range_and_keeps_cursor(tmp_path):
    c = FakeChain()
    c.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 1000 * E18)],
             bal={W1: (10 * E18, 9 * E18 - FEE)})
    orig = c.call

    def flaky(m, p):
        if m == "eth_getTransactionByHash":
            raise RpcError("upstream timeout")
        return orig(m, p)

    c.call = flaky
    store, f, e = build(tmp_path, c)
    with pytest.raises(RpcError):
        scan_apply(f, e, {W1})
    assert store.get_kv("cursor") is None

    c.call = orig  # provider recovers: the same range now yields the trade
    ev = scan_apply(f, e, {W1})
    assert [(x.kind, x.hype_wei, x.confidence) for x in ev] == [("BUY", E18, "high")]


def test_C1_missing_block_timestamp_fails_range(tmp_path):
    c = FakeChain()
    c.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 5 * E18)],
             bal={W1: (10 * E18, 9 * E18 - FEE)})
    orig = c.call
    c.call = lambda m, p: None if m == "eth_getBlockByNumber" else orig(m, p)
    _, f, _ = build(tmp_path, c)
    with pytest.raises(RpcError):
        f.scan(1, 40, {W1})


# --- C2/C3: HYPE attribution ----------------------------------------------------------
def test_C2_airdrop_next_to_unrelated_hype_payment_is_not_a_buy(tmp_path):
    c = FakeChain()
    c.add_tx("0xa", 10, FRESH, transfers=[(FRESH, W1, 1000 * E18)],
             bal={W1: (10 * E18, 5 * E18)})
    c.plain_sends.append((W1, 10))  # W1 paid 5 HYPE to someone in another tx, same block
    store, f, e = build(tmp_path, c)
    (ev,) = scan_apply(f, e, {W1})
    assert ev.kind == "TRANSFER_IN" and ev.hype_wei is None
    pos = Store.get_position(store.conn, W1, TOKEN)
    assert pos.cost_wei == 0 and pos.basis == "partial_unknown"


def test_C2_own_buy_plus_hidden_plain_send_falls_back_to_estimate(tmp_path):
    c = FakeChain()
    # W1 buys for 1 HYPE AND sends 3 HYPE elsewhere in the same block (no token logs).
    c.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 1000 * E18)],
             bal={W1: (10 * E18, 6 * E18 - 2 * FEE)})
    c.plain_sends.append((W1, 10))
    _, f, _ = build(tmp_path, c)
    (ev,), _ = f.scan(1, 40, {W1})
    # v0.1 would book ~4 HYPE as exact; now: nonce mismatch -> tx.value, flagged estimated.
    assert (ev.kind, ev.hype_wei, ev.confidence, ev.hype_method) == \
        ("BUY", E18, "estimated", "tx_value")


def test_C2_nonce_unavailable_means_not_high_confidence(tmp_path):
    c = FakeChain()
    c.nonce_unavailable = True
    c.add_tx("0xd", 20, W1, transfers=[(W1, POOL, 500 * E18)],
             bal={W1: (5 * E18, 5 * E18 + 8 * E18 // 10 - FEE)})
    _, f, _ = build(tmp_path, c)
    (ev,), _ = f.scan(1, 40, {W1})
    assert (ev.kind, ev.confidence) == ("TRANSFER_OUT", "unknown")


def test_C3_tx_sent_by_someone_else_is_never_a_trade(tmp_path):
    c = FakeChain()
    # A keeper fills W1's order: tokens arrive in the keeper's tx, W1's balance even moves.
    c.add_tx("0xk", 10, FRESH, transfers=[(POOL, W1, 50 * E18)],
             bal={W1: (10 * E18, 11 * E18)})
    _, f, _ = build(tmp_path, c)
    (ev,), _ = f.scan(1, 40, {W1})
    assert ev.kind == "TRANSFER_IN"


def test_wrapped_hype_payment_is_high_confidence_without_balance_history(tmp_path):
    c = FakeChain()
    whype = "0x" + "e0" * 20
    c.add_tx("0xw", 10, W1, transfers=[(POOL, W1, 100 * E18)])
    c.logs.append({"address": whype, "blockNumber": hex(10), "transactionHash": "0xw",
                   "logIndex": "0x9", "data": "0x" + (2 * E18).to_bytes(32, "big").hex(),
                   "topics": [c.logs[0]["topics"][0], "0x" + "0" * 24 + W1[2:],
                              "0x" + "0" * 24 + POOL[2:]]})
    store, f, e = build(tmp_path, c)
    f.cfg.wrapped_native = whype
    (ev,), _ = f.scan(1, 40, {W1})
    assert (ev.kind, ev.hype_wei, ev.confidence, ev.hype_method) == \
        ("BUY", 2 * E18, "high", "wrapped")


# --- C4: metadata cache ---------------------------------------------------------------
def test_C4_transient_metadata_failure_is_not_cached(tmp_path):
    c = FakeChain()
    orig = c.call

    def flaky(m, p):
        if m == "eth_call":
            raise RpcError("rate limited", -32005)
        return orig(m, p)

    c.call = flaky
    store, f, _ = build(tmp_path, c)
    f.tokens.resolve({TOKEN})
    assert store.get_token(TOKEN) is None
    c.call = orig
    f.tokens.resolve({TOKEN})
    assert store.get_token(TOKEN) == ("KLEOS", 18)


def test_C4_reverting_token_is_cached_with_fallback(tmp_path):
    c = FakeChain()
    orig = c.call
    c.call = lambda m, p: (_ for _ in ()).throw(RpcError("execution reverted", 3)) \
        if m == "eth_call" else orig(m, p)
    store, f, _ = build(tmp_path, c)
    f.tokens.resolve({TOKEN})
    assert store.get_token(TOKEN) == (TOKEN[:8], 18)


# --- C6: rate limits must not trigger range splitting ----------------------------------
def test_C6_rate_limited_getlogs_is_not_split(tmp_path):
    c = FakeChain()
    calls = {"n": 0}
    orig = c.call

    def limited(m, p):
        if m == "eth_getLogs":
            calls["n"] += 1
            raise RpcError("rate limit exceeded", -32005)
        return orig(m, p)

    c.call = limited
    _, f, _ = build(tmp_path, c)
    with pytest.raises(RpcError):
        f.fetch_logs(1, 10, [W1])
    assert calls["n"] == 1


# --- T2: one row per leg across replays and v0.1 keys ---------------------------------
def test_T2_replay_with_better_data_does_not_duplicate_leg(tmp_path):
    c = FakeChain()
    c.add_tx("0xd", 20, W1, transfers=[(W1, POOL, 500 * E18)])  # no balance history yet
    store, f, e = build(tmp_path, c)
    scan_apply(f, e, {W1})
    c.balances[(W1, 19)] = 9 * E18
    c.balances[(W1, 20)] = 9 * E18 + 8 * E18 // 10 - FEE
    scan_apply(f, e, {W1})
    rows = store.conn.execute("SELECT kind FROM events WHERE tx='0xd'").fetchall()
    assert [r[0] for r in rows] == ["TRANSFER_OUT"]


# --- P1: estimates never become exact PnL ---------------------------------------------
def test_P1_estimated_buy_books_estimated_pnl_only(tmp_path):
    c = FakeChain()
    c.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 1000 * E18)])  # no balances
    c.add_tx("0xd", 20, W1, transfers=[(W1, POOL, 1000 * E18)],
             bal={W1: (5 * E18, 7 * E18 - FEE)})
    store, f, e = build(tmp_path, c)
    scan_apply(f, e, {W1})
    pos = Store.get_position(store.conn, W1, TOKEN)
    assert pos.realized_wei == 0 and pos.realized_est_wei == E18
    texts = [r["text"] for r in store.pending_alerts(50)]
    assert any("≈1.000 HYPE" in t and "estimated (tx_value)" in t for t in texts)
    assert any("realized ≈+1.000 HYPE" in t for t in texts)


def test_P1_sell_after_unknown_cost_transfer_in_books_no_pnl(tmp_path):
    c = FakeChain()
    c.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 1000 * E18)],
             bal={W1: (10 * E18, 9 * E18 - FEE)})
    c.add_tx("0xb", 12, FRESH, transfers=[(FRESH, W1, 1000 * E18)])  # gift, cost unknown
    c.add_tx("0xd", 20, W1, transfers=[(W1, POOL, 2000 * E18)],
             bal={W1: (9 * E18, 12 * E18 - FEE)})
    store, f, e = build(tmp_path, c)
    scan_apply(f, e, {W1})
    pos = Store.get_position(store.conn, W1, TOKEN)
    assert pos.qty == 0 and pos.realized_wei == 0 and pos.realized_est_wei == 0
    assert any("PnL unknown" in r["text"] for r in store.pending_alerts(50))


def test_P4_cluster_ignores_dust_and_estimated_buys(tmp_path):
    c = FakeChain()
    c.add_tx("0xa", 10, W1, value=E18, transfers=[(POOL, W1, 100 * E18)],
             bal={W1: (10 * E18, 9 * E18 - FEE)})
    c.add_tx("0xb", 11, W2, value=E18 // 100, transfers=[(POOL, W2, 1 * E18)],   # dust
             bal={W2: (10 * E18, 10 * E18 - E18 // 100 - FEE)})
    c.add_tx("0xc", 12, FRESH, value=E18, transfers=[(POOL, FRESH, 100 * E18)])  # estimated
    store, f, e = build(tmp_path, c)
    scan_apply(f, e, {W1, W2, FRESH})
    assert not any("CLUSTER" in r["text"] for r in store.pending_alerts(50))


# --- crash recovery ---------------------------------------------------------------------
def test_crash_mid_range_rolls_back_everything(tmp_path, monkeypatch):
    c = FakeChain()
    for i, w in enumerate((W1, W2)):
        c.add_tx(f"0x{i}", 10 + i, w, value=E18, transfers=[(POOL, w, 10 * E18)],
                 bal={w: (10 * E18, 9 * E18 - FEE)})
    store, f, e = build(tmp_path, c)
    real = Store.enqueue
    n = {"i": 0}

    def boom(conn, key, text):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("process killed")
        real(conn, key, text)

    monkeypatch.setattr(Store, "enqueue", staticmethod(boom))
    with pytest.raises(RuntimeError):
        scan_apply(f, e, {W1, W2})
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert store.get_kv("cursor") is None
    monkeypatch.setattr(Store, "enqueue", staticmethod(real))
    scan_apply(f, e, {W1, W2})
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


# --- D1 + L2: tracker lifecycle ---------------------------------------------------------
def make_tracker(tmp_path, chain=None) -> Tracker:
    cfg = Config(chain=ChainConfig(rpc_urls=["http://fake"], max_block_range=100),
                 leaderboard=LeaderboardConfig(enabled=False), alerts=AlertConfig(),
                 telegram=TelegramConfig(), db_path=str(tmp_path / "t.db"))
    t = Tracker(cfg, dry_run=True)
    fake = chain or FakeChain()
    t.rpc = fake
    t.follower.rpc = fake
    t.follower.tokens.rpc = fake
    return t


def test_D1_cursor_held_while_nothing_is_tracked(tmp_path):
    t = make_tracker(tmp_path)
    t.store.set_kv("cursor", "5")
    t.tracked = set()
    assert t.scan_step() is False
    assert t.store.get_kv("cursor") == "5"
    t.tracked = {W1}
    t.scan_step()
    assert t.store.get_kv("cursor") == "40"


def test_L2_degraded_leaderboard_keeps_previous_set_and_sends_no_exits(tmp_path):
    t = make_tracker(tmp_path)
    t.cfg.exclude_contracts = False
    t.cfg.leaderboard.top_n = 4
    full = [Trader(address="0x" + f"{i:040x}", rank=i + 1, pnl=100.0 - i) for i in range(4)]

    class LB:
        rows = full

        def fetch(self):
            return self.rows

    t.leaderboard = LB()
    t.refresh_leaderboard()
    assert len(t.tracked) == 4
    LB.rows = full[:1]  # endpoint suddenly returns one row
    t.refresh_leaderboard()
    assert len(t.tracked) == 4
    assert not any("dropped out" in r["text"] for r in t.store.pending_alerts(50))


def test_L1_zero_pnl_sorts_above_negative():
    cfg = LeaderboardConfig(items_path="", fields={"address": "a", "pnl": "p"})
    ts = parse_items([{"a": W1, "p": -500}, {"a": W2, "p": 0}, {"a": FRESH}], cfg)
    assert [t.address for t in ts] == [W2, W1, FRESH]


# --- R1/R3/R5: RPC client ---------------------------------------------------------------
class FakeHttp:
    """Per-URL scripted JSON-RPC server for RpcClient._raw_post."""

    def __init__(self, chains: dict[str, int]):
        self.chains = chains
        self.log: list[tuple[str, str]] = []
        self.errors: dict[str, list] = {}

    def __call__(self, url, payload):
        if isinstance(payload, list):
            return [self._one(url, p) for p in payload]
        return self._one(url, payload)

    def _one(self, url, p):
        self.log.append((url, p["method"]))
        if p["method"] == "eth_chainId":
            return {"id": p["id"], "result": hex(self.chains[url])}
        queue = self.errors.get(p["method"])
        if queue:
            return {"id": p["id"], "error": queue.pop(0)}
        return {"id": p["id"], "result": "0x2a"}


def test_R1_wrong_chain_endpoint_is_quarantined_never_used():
    c = RpcClient(["https://evil.example/rpc", "https://good.example/rpc"], max_retries=3,
                  expected_chain_id=99901)
    http = FakeHttp({"https://evil.example/rpc": 1, "https://good.example/rpc": 99901})
    c._raw_post = http
    assert c.block_number() == 42
    used = {u for u, m in http.log if m == "eth_blockNumber"}
    assert used == {"https://good.example/rpc"}
    assert c.live_urls == ["https://good.example/rpc"]


def test_R1_all_endpoints_wrong_chain_raises():
    c = RpcClient(["https://a.example/rpc"], max_retries=2, expected_chain_id=99901)
    c._raw_post = FakeHttp({"https://a.example/rpc": 999})
    with pytest.raises(RpcError, match="quarantined"):
        c.block_number()


def test_rate_limit_error_is_retried_then_succeeds():
    c = RpcClient(["https://a.example/rpc"], max_retries=3, expected_chain_id=99901)
    http = FakeHttp({"https://a.example/rpc": 99901})
    http.errors["eth_blockNumber"] = [{"code": -32005, "message": "rate limit"}] * 2
    c._raw_post = http
    assert c.block_number() == 42


def test_non_retryable_error_raises_immediately():
    c = RpcClient(["https://a.example/rpc"], max_retries=3, expected_chain_id=99901)
    http = FakeHttp({"https://a.example/rpc": 99901})
    http.errors["eth_call"] = [{"code": 3, "message": "execution reverted"}]
    c._raw_post = http
    with pytest.raises(RpcError, match="reverted"):
        c.call("eth_call", [{}, "latest"])
    assert sum(1 for _, m in http.log if m == "eth_call") == 1


def test_batch_matches_ids_not_positions_and_survives_one_bad_response():
    c = RpcClient(["https://a.example/rpc"], expected_chain_id=None)
    calls = {"n": 0}

    def post(url, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"error": {"message": "temporary"}}       # one non-list response
        if isinstance(payload, dict):
            return {"id": payload["id"], "result": payload["params"][0]}
        return list(reversed([{"id": p["id"], "result": p["params"][0]} for p in payload]))

    c._raw_post = post
    assert c.batch([("x", ["a"]), ("x", ["b"])]) == ["a", "b"]   # sequential fallback once
    assert c._batch_ok
    assert c.batch([("x", ["c"]), ("x", ["d"])]) == ["c", "d"]   # reversed list, mapped by id


def test_R5_redact_hides_keys():
    assert redact("https://elysium.g.alchemy.com/v2/AbCdEf1234567890xyz") == \
        "https://elysium.g.alchemy.com/v2/***"
    assert redact("https://rpc.thekleos.xyz/rpc?apikey=s3cret") == "https://rpc.thekleos.xyz/rpc?***"
    assert redact("https://rpc.thekleos.xyz/rpc") == "https://rpc.thekleos.xyz/rpc"


# --- N1: notifier -----------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def make_notifier(tmp_path, responses, max_attempts=3):
    store = Store(str(tmp_path / "n.db"))
    n = Notifier(store, TelegramConfig(bot_token="123:SECRET", chat_id="1", min_interval_s=0,
                                       max_attempts=max_attempts))
    seq = list(responses)

    def post(url, json, timeout):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    n.session.post = post
    return store, n


def test_N1_backoff_then_dead_letter_and_token_not_logged(tmp_path, caplog):
    import requests
    store, n = make_notifier(tmp_path, [requests.ConnectionError(
        "https://api.telegram.org/bot123:SECRET/sendMessage failed")] * 3)
    store.enqueue_now("k1", "hello")
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        row = store.conn.execute("SELECT * FROM outbox").fetchone()
        store.conn.execute("UPDATE outbox SET next_attempt_at=? WHERE id=?", (0, row["id"]))
        n.drain()
    row = store.conn.execute("SELECT * FROM outbox").fetchone()
    assert (row["status"], row["attempts"]) == ("dead", 3)
    assert "SECRET" not in caplog.text and "SECRET" not in (row["last_error"] or "")


def test_N1_failed_alert_waits_for_backoff(tmp_path):
    import requests
    store, n = make_notifier(tmp_path, [requests.Timeout("t"), FakeResp(200)])
    store.enqueue_now("k1", "hello")
    n.drain()
    assert n.drain() == 0                        # not due yet (10s backoff)
    store.conn.execute("UPDATE outbox SET next_attempt_at=0")
    assert n.drain() == 1
    assert store.outbox_counts() == {"sent": 1}


def test_N1_bad_request_is_dead_immediately_and_does_not_block_queue(tmp_path):
    store, n = make_notifier(tmp_path, [FakeResp(400, text="chat not found"), FakeResp(200)])
    store.enqueue_now("k1", "bad")
    store.enqueue_now("k2", "good")
    assert n.drain() == 1
    assert store.outbox_counts() == {"dead": 1, "sent": 1}


# --- upgrade of a v0.1 database ---------------------------------------------------------
def test_v0_1_database_upgrades_in_place(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript((FIXTURES / "schema_v0_1.sql").read_text())
    con.execute("INSERT INTO events(key,block,ts,tx,wallet,kind,token,qty,hype_wei,approx) "
                "VALUES(?,10,1,'0xa',?,'BUY',?,'5','7',1)", (f"0xa:{W1}:{TOKEN}:BUY", W1, TOKEN))
    con.execute("INSERT INTO positions VALUES(?,?,'5','7','0',1)", (W1, TOKEN))
    con.execute("INSERT INTO outbox(key,text,created_at,sent_at,attempts) VALUES('a','x',1,2,0)")
    con.execute("INSERT INTO outbox(key,text,created_at,attempts) VALUES('b','y',1,10)")
    con.execute("INSERT INTO outbox(key,text,created_at,attempts) VALUES('c','z',1,0)")
    con.commit()
    con.close()

    store = Store(str(db))
    assert store.conn.execute("SELECT confidence FROM events").fetchone()[0] == "estimated"
    assert Store.get_position(store.conn, W1, TOKEN).basis == "legacy"
    assert store.outbox_counts() == {"sent": 1, "dead": 1, "pending": 1}
    # a replay of the same leg is recognised despite the v0.1 key format
    from agora_tracker.classify import TradeEvent
    ev = TradeEvent("BUY", "0xa", 10, W1, TOKEN, 5, 7)
    assert Store.insert_event(store.conn, ev, 1) is False
