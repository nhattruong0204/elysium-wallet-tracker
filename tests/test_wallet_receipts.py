# ruff: noqa: F811
"""Receipt reconciliation against a real PostgreSQL ledger (#94).

A scripted ReceiptChain plays the node. Nothing is signed, broadcast or funded: the point is what
the ledger records as fact, what it refuses to, and that reorgs/restarts/duplicates never double
count or invent a fill."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import TEST_URL  # noqa: F401
from test_trading_ledger import (  # noqa: F401
    OWNER,
    U1,
    U2,
    Actor,
    clock,
    ledger,
    live,
    owner_engine,
    raw,
    rw_engine,
)
from test_wallet_flow import HASH, SESSION, mkflow, sell_order
from test_wallet_reconcile import H1, FakeNode, node, submitted  # noqa: F401
from walletkit import (
    E18,
    TOKEN,
    WALLET,
    ReceiptChain,
    block_hash,
    deposit_log,
    transfer_log,
)

from agora_tracker.migrate import downgrade, upgrade
from agora_tracker.trading.v2 import ROUTER
from agora_tracker.trading.wallet.receipts import ReceiptReconciler
from agora_tracker.trading.wallet.status import order_status, positions

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")
AMT = E18 // 2
T = 3 * E18
PAIR = "0x" + "03" * 20
GAS = 150_000 * 10**9


def mined_buy(live, node, ch=None, *, block=1000, head=1005, fork=0, **kw):
    """A submitted buy order whose transaction is mined at `block` on `ch`."""
    ch = ch or ReceiptChain(head)
    oid, plan, leg, flow = submitted(live, node)
    ch.mine(leg, H1, block, fork=fork, logs=[deposit_log(ROUTER, AMT),
                                              transfer_log(TOKEN, PAIR, WALLET, T)], **kw)
    ch.buy_balances(block, T)
    return oid, leg, ch


def rr(live, ch, **kw):
    return ReceiptReconciler(live, ch, clock=live.clock, **kw)


def state(live, oid):
    return live.get_order(U1, oid).state


def leg_of(live, oid, n=0):
    return live.get_legs(U1, oid)[n]


def results(owner_engine, oid):
    return raw(owner_engine, "SELECT detail->>'outcome' FROM trading_private.order_events WHERE "
                             "order_id = :o AND kind = 'WALLET_RESULT' ORDER BY chain_no", o=oid)


# ------------------------------------------------------------------ the happy path

def test_swap_receipt_is_provisional_then_final_with_one_verified_fill(live, node):
    oid, leg, ch = mined_buy(live, node)
    assert rr(live, ch).run_once().counts == {"provisional_included": 1}
    assert state(live, oid) == "included"
    row = leg_of(live, oid)
    assert (row["state"], row["block_number"], row["receipt_status"], row["finalized_at"]) == (
        "included", 1000, 1, None)
    assert row["gas_cost_wei"] == str(GAS) and row["block_hash"] == block_hash(1000)
    f, = live.get_fills(U1, oid)
    assert (f["verdict"], f["status"], f["native_wei"], f["token_wei"]) == (
        "verified", "provisional", str(AMT), str(T))

    ch.head_n = 1011                                   # 12 confirmations
    assert rr(live, ch).run_once().counts == {"final_included": 1}
    assert state(live, oid) == "finalized"
    f, = live.get_fills(U1, oid)
    assert f["status"] == "final" and leg_of(live, oid)["finalized_at"] is not None
    assert rr(live, ch).run_once().counts == {}        # nothing left to watch


def test_not_enough_confirmations_stays_provisional_and_changes_nothing(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    r.run_once()
    n = len(results(owner_engine, oid))
    assert r.run_once().counts == {"provisional_unchanged": 1}
    assert len(results(owner_engine, oid)) == n and state(live, oid) == "included"


def test_finality_depth_is_configurable(live, node):
    oid, leg, ch = mined_buy(live, node, head=1001)
    assert rr(live, ch, finality_confirmations=2).run_once().counts == {"final_included": 1}
    assert state(live, oid) == "finalized"


def test_a_reported_hash_without_a_receipt_stays_submitted(live, node):
    oid, plan, leg, flow = submitted(live, node)
    ch = ReceiptChain()
    assert rr(live, ch).run_once().counts == {"pending": 1}
    assert state(live, oid) == "submitted" and live.get_fills(U1, oid) == []


def test_node_that_has_the_receipt_but_not_the_header_yet_is_pending(live, node):
    oid, leg, ch = mined_buy(live, node)
    del ch.canon[1000]
    assert rr(live, ch).run_once().counts == {"pending": 1}
    assert state(live, oid) == "submitted"


def test_a_receipt_from_a_non_canonical_block_is_not_recorded(live, node):
    oid, leg, ch = mined_buy(live, node)
    ch.canon[1000] = block_hash(1000, 7)
    assert rr(live, ch).run_once().counts == {"not_canonical": 1}
    assert state(live, oid) == "submitted" and live.get_fills(U1, oid) == []


# ------------------------------------------------------------------ never trust a bad pair

@pytest.mark.parametrize("tx,rc", [
    ({"input": "0x00"}, None), ({"to": "0x" + "77" * 20}, None), ({"value": "0x1"}, None),
    ({"from": "0x" + "77" * 20}, None), (None, {"from": "0x" + "77" * 20}),
    (None, {"to": "0x" + "77" * 20}), (None, {"contractAddress": "0x" + "88" * 20}),
    (None, {"status": "0x2"}), (None, {"blockHash": block_hash(1000, 5)})])
def test_a_receipt_that_is_not_the_reviewed_transaction_records_nothing(live, node, tx, rc):
    oid, plan, leg, flow = submitted(live, node)
    ch = ReceiptChain(1005)
    ch.mine(leg, H1, 1000, logs=[], tx=tx, rc=rc)
    assert rr(live, ch).run_once().counts == {"invalid": 1}
    assert state(live, oid) == "submitted" and live.get_fills(U1, oid) == []
    assert leg_of(live, oid)["state"] == "hash_reported"


# ------------------------------------------------------------------ outcomes

def test_a_reverted_swap_records_gas_only_and_reaches_reverted_then_finalized(live, node):
    oid, plan, leg, flow = submitted(live, node)
    ch = ReceiptChain(1011)
    ch.mine(leg, H1, 1000, status=0)
    assert rr(live, ch).run_once().counts == {"final_reverted": 1}
    f, = live.get_fills(U1, oid)
    assert (f["verdict"], f["native_wei"], f["token_wei"], f["gas_cost_wei"], f["status"]) == (
        "reverted", None, None, str(GAS), "final")
    assert state(live, oid) == "finalized" and leg_of(live, oid)["receipt_status"] == 0
    st = order_status(live.get_order(U1, oid).public(), live.get_legs(U1, oid),
                      live.get_fills(U1, oid))
    assert st["fill"] is None and st["gas_cost_wei"] == str(GAS)
    assert positions(live.get_fills(U1)) == []


def test_an_approval_receipt_never_creates_a_fill_or_touches_the_order(live):
    oid, plan = sell_order(live)
    flow = mkflow(live)
    req = flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)["request"]
    flow.report(user_id=U1, session=SESSION, request_id=req["id"], outcome="hash", tx_hash=HASH)
    leg = leg_of(live, oid)
    ch = ReceiptChain(1005)
    ch.mine(leg, HASH, 1000, logs=[])
    assert rr(live, ch).run_once().counts == {"provisional_included": 1}
    assert leg_of(live, oid)["state"] == "included" and state(live, oid) == "requires_approval"
    assert live.get_fills(U1, oid) == []
    st = order_status(live.get_order(U1, oid).public(), live.get_legs(U1, oid), [])
    assert "refresh quote required" in st["label"]
    ch.head_n = 1011
    assert rr(live, ch).run_once().counts == {"final_included": 1}
    assert state(live, oid) == "requires_approval" and live.get_fills(U1) == []


def test_unreadable_balances_retry_then_record_unknown_never_a_guess(live, node):
    oid, leg, ch = mined_buy(live, node)
    del ch.tok[(TOKEN, WALLET, 999)]
    r = rr(live, ch)
    for _ in range(3):
        assert r.run_once().counts == {"evidence_unavailable": 1}
        assert state(live, oid) == "submitted" and live.get_fills(U1, oid) == []
    assert r.run_once().counts == {"provisional_included": 1}
    f, = live.get_fills(U1, oid)
    assert f["verdict"] == "unknown" and f["token_wei"] is None and "unavailable" in f["reason"]
    assert state(live, oid) == "included"
    p, = positions(live.get_fills(U1))
    assert p["quantity_wei"] is None and p["realized_pnl_wei"] is None


def test_a_same_block_neighbour_makes_the_fill_unknown_but_the_receipt_is_still_recorded(live, node):
    oid, plan, leg, flow = submitted(live, node)
    ch = ReceiptChain(1005)
    ch.mine(leg, H1, 1000, logs=[deposit_log(ROUTER, AMT), transfer_log(TOKEN, PAIR, WALLET, T)],
            others=[{"hash": "0x" + "99" * 32, "from": WALLET, "to": "0x" + "66" * 20}])
    ch.buy_balances(1000, T)
    rr(live, ch).run_once()
    assert live.get_fills(U1, oid)[0]["verdict"] == "unknown" and state(live, oid) == "included"


# ------------------------------------------------------------------ reorgs

def test_reorg_rolls_back_the_provisional_fill_then_a_new_inclusion_is_a_new_row(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    r.run_once()
    ch.canon[1000] = block_hash(1000, 9)               # block replaced...
    ch.drop(H1)                                        # ...and the tx is not in the new chain yet
    assert r.run_once().counts == {"orphaned": 1}
    assert state(live, oid) == "orphaned" and leg_of(live, oid)["state"] == "orphaned"
    assert live.get_fills(U1, oid) == []                                  # hidden by default
    rolled, = live.get_fills(U1, oid, include_rolled_back=True)
    assert rolled["status"] == "rolled_back" and positions(live.get_fills(U1)) == []
    st = order_status(live.get_order(U1, oid).public(), live.get_legs(U1, oid),
                      live.get_fills(U1, oid, include_rolled_back=True))
    assert "reorganized" in st["label"] and st["fill"] is None

    ch.mine(leg, H1, 1002, fork=9, logs=[deposit_log(ROUTER, AMT),
                                         transfer_log(TOKEN, PAIR, WALLET, T)])
    ch.buy_balances(1002, T)
    ch.head_n = 1008
    assert r.run_once().counts == {"provisional_included": 1}
    assert state(live, oid) == "included"
    rows = live.get_fills(U1, oid, include_rolled_back=True)
    assert [(f["status"], f["block_hash"]) for f in rows] == [
        ("rolled_back", block_hash(1000)), ("provisional", block_hash(1002, 9))]
    ch.head_n = 1020
    assert r.run_once().counts == {"final_included": 1} and state(live, oid) == "finalized"
    assert [f["status"] for f in live.get_fills(U1, oid, include_rolled_back=True)] == [
        "rolled_back", "final"]


def test_reinclusion_in_a_different_block_at_the_same_height_replaces_the_fill(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    r.run_once()
    ch.mine(leg, H1, 1000, fork=4, logs=[deposit_log(ROUTER, AMT),
                                         transfer_log(TOKEN, PAIR, WALLET, T)])
    assert r.run_once().counts == {"provisional_included": 1}
    assert state(live, oid) == "included"
    assert [f["status"] for f in live.get_fills(U1, oid, include_rolled_back=True)] == [
        "rolled_back", "provisional"]


def test_a_finalized_inclusion_can_never_be_orphaned(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node, head=1011)
    rr(live, ch).run_once()
    row = leg_of(live, oid)
    assert row["finalized_at"] is not None
    assert live.apply_reorg(row["id"], expected_version=row["version"], reason="late") is False
    with pytest.raises(Exception, match="finalized leg is immutable"):
        raw(owner_engine, "UPDATE trading_private.order_legs SET state = 'orphaned', "
                          "version = version + 1 WHERE id = :i", i=row["id"])
    with pytest.raises(Exception, match="finalized leg is immutable"):
        raw(owner_engine, "UPDATE trading_private.order_legs SET block_hash = :h, "
                          "version = version + 1 WHERE id = :i", i=row["id"], h=block_hash(1, 1))


def test_node_is_behind_with_no_header_and_no_receipt_is_not_proof_of_a_reorg(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    r.run_once()
    del ch.canon[1000]
    ch.drop(H1)
    assert r.run_once().counts == {"pending": 1} and state(live, oid) == "included"


# ------------------------------------------------------------------ exactly once

def test_restart_continues_without_duplicating_anything(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    rr(live, ch).run_once()
    fresh = rr(live, ch)                                 # "restart": a new worker, no memory
    assert fresh.run_once().counts == {"provisional_unchanged": 1}
    ch.head_n = 1011
    assert fresh.run_once().counts == {"final_included": 1}
    assert len(live.get_fills(U1, oid, include_rolled_back=True)) == 1
    assert results(owner_engine, oid).count(("receipt_included",)) == 2   # provisional + final


def test_applying_the_same_receipt_twice_is_a_noop(live, node):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    from agora_tracker.trading.wallet import fills as F
    fill = F.compute_fill(row, ch.txs[H1], ch.receipts[H1], H1, ch)
    kw = dict(expected_version=row["version"], state="included", block_number=1000,
              block_hash=block_hash(1000), receipt_status=1, gas_used=150_000, gas_cost_wei=GAS,
              fill=fill, finalize=False)
    assert live.apply_receipt(row["id"], **kw) is True
    assert live.apply_receipt(row["id"], **kw) is False
    assert len(live.get_fills(U1, oid)) == 1


def test_two_workers_racing_record_exactly_one_inclusion(live, node):
    oid, leg, ch = mined_buy(live, node)
    with ThreadPoolExecutor(2) as ex:
        reps = list(ex.map(lambda _: rr(live, ch).run_once(), range(2)))
    allowed = {"provisional_included", "provisional_unchanged", "stale"}
    assert all(set(r.counts) <= allowed for r in reps)
    assert sum(r.counts.get("provisional_included", 0) for r in reps) == 1
    assert len(live.get_fills(U1, oid, include_rolled_back=True)) == 1
    assert state(live, oid) == "included"


# ------------------------------------------------------------------ operations

def test_receipts_are_recorded_while_trading_is_paused(live, node):
    oid, leg, ch = mined_buy(live, node)
    live.pause(Actor.owner(OWNER), "incident")
    assert rr(live, ch).run_once().counts == {"provisional_included": 1}
    assert state(live, oid) == "included"


def test_an_unreachable_node_changes_nothing(live, node):
    oid, leg, ch = mined_buy(live, node)
    ch.down = True
    assert rr(live, ch).run_once().counts == {"error": 1}
    assert state(live, oid) == "submitted" and live.get_fills(U1, oid) == []


def test_the_rpc_budget_bounds_a_pass_and_leaves_state_untouched(live, node):
    oid, leg, ch = mined_buy(live, node)
    rep = rr(live, ch, rpc_budget=3).run_once()
    assert rep.budget_exhausted and rep.counts == {"budget_exhausted": 1}
    assert state(live, oid) == "submitted" and live.get_fills(U1, oid) == []
    assert rr(live, ch).run_once().counts == {"provisional_included": 1}   # next pass completes it


def test_bad_finality_depth_is_rejected(live):
    for bad in (0, 257):
        with pytest.raises(ValueError):
            ReceiptReconciler(live, ReceiptChain(), finality_confirmations=bad)


# ------------------------------------------------------------------ privacy and the database

def test_fills_and_legs_are_private_per_user(live, node):
    oid, leg, ch = mined_buy(live, node)
    rr(live, ch).run_once()
    assert live.get_fills(U2) == [] and live.get_fills(U2, oid) == []
    assert live.get_legs(U2, oid) == []
    assert len(live.get_fills(U1)) == 1


def test_recorded_fills_are_immutable_and_never_deleted(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    rr(live, ch).run_once()
    for sql, msg in (
            ("UPDATE trading_private.order_fills SET native_wei = 1", "changes only its status"),
            ("UPDATE trading_private.order_fills SET verdict = 'unknown', native_wei = NULL, "
             "token_wei = NULL", "changes only its status"),
            ("DELETE FROM trading_private.order_fills", "never deleted"),
            ("TRUNCATE trading_private.order_fills", "append-only")):
        with pytest.raises(Exception, match=msg):
            raw(owner_engine, sql)
    raw(owner_engine, "UPDATE trading_private.order_fills SET status = 'final'")
    with pytest.raises(Exception, match="is not allowed"):
        raw(owner_engine, "UPDATE trading_private.order_fills SET status = 'provisional'")
    with pytest.raises(Exception, match="is not allowed"):
        raw(owner_engine, "UPDATE trading_private.order_fills SET status = 'rolled_back'")


def test_a_fill_row_must_be_internally_consistent(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    rr(live, ch).run_once()
    f = live.get_fills(U1, oid)[0]
    base = ("INSERT INTO trading_private.order_fills (order_id, leg_id, user_id, wallet, chain_id, "
            "token, side, tx_hash, block_number, block_hash, verdict, native_wei, token_wei, "
            "gas_cost_wei) VALUES (:o, :l, :u, :w, 99901, :t, 'buy', :h, 5, :bh, :v, :n, :k, 1)")
    args = dict(o=oid, l=f["leg_id"], u=U1, w=WALLET, t=TOKEN, h=H1, bh=block_hash(5, 3))
    with pytest.raises(Exception, match="ck_fill_verified"):
        raw(owner_engine, base, v="verified", n=None, k=None, **args)
    with pytest.raises(Exception, match="ck_fill_empty"):
        raw(owner_engine, base, v="unknown", n=1, k=1, **args)
    with pytest.raises(Exception, match="uq_fill_inclusion"):
        raw(owner_engine, base, v="unknown", n=None, k=None, **{**args, "bh": f["block_hash"]})


def test_migration_0014_round_trips_and_keeps_the_guards(pg_engine):
    downgrade(pg_engine, "0013")
    assert raw(pg_engine, "SELECT count(*) FROM information_schema.tables WHERE table_schema = "
                          "'trading_private' AND table_name = 'order_fills'")[0][0] == 0
    cols = {r[0] for r in raw(pg_engine, "SELECT column_name FROM information_schema.columns "
                                         "WHERE table_name = 'order_legs'")}
    assert "block_hash" not in cols
    upgrade(pg_engine)
    cols = {r[0] for r in raw(pg_engine, "SELECT column_name FROM information_schema.columns "
                                         "WHERE table_name = 'order_legs'")}
    assert {"block_number", "block_hash", "receipt_status", "gas_used", "gas_cost_wei",
            "included_at", "finalized_at"} <= cols
    assert raw(pg_engine, "SELECT relrowsecurity FROM pg_class WHERE relname = 'order_fills'")[0][0]
    assert "orphaned" in raw(pg_engine, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                                        "conname = 'order_legs_state_check'")[0][0]


# ------------------------------------------------------------------ Telegram view

def test_telegram_sections_show_verified_facts_and_are_private(live, node):
    from types import SimpleNamespace

    from agora_tracker.tgui import IntelBot as TgBot
    oid, leg, ch = mined_buy(live, node, head=1011)
    rr(live, ch).run_once()
    links = SimpleNamespace(tx=lambda h: f"https://explorer.example/tx/{h}")
    bot = SimpleNamespace(ledger=live, L=links, _wallet_section=None)
    text = TgBot._wallet_section(bot, U1, positions=True)
    assert "Final" in text and "explorer.example/tx/" in text and f"held {T} wei" in text
    assert "realized PnL 0 wei" in text
    assert TgBot._wallet_section(bot, U2, positions=True) == ""
    assert TgBot._wallet_section(SimpleNamespace(ledger=None), U1, positions=True) == ""


def test_telegram_section_never_raises_into_the_command_handler(live):
    from types import SimpleNamespace

    from agora_tracker.tgui import IntelBot as TgBot

    class Broken:
        def list_orders(self, *a, **k):
            raise RuntimeError("db down")
    assert TgBot._wallet_section(SimpleNamespace(ledger=Broken(), L=None), U1, positions=False) == ""


# ------------------------------------------------------------------ worker loop

def test_serve_survives_failures_and_stops_when_asked():
    import threading

    from agora_tracker.trading.wallet.receipts import serve

    class Flaky:
        def __init__(self):
            self.n = 0

        def run_once(self):
            self.n += 1
            if self.n < 3:
                raise RuntimeError("boom")
            stop.set()

    stop = threading.Event()
    flaky = Flaky()
    assert serve(flaky, stop, interval=0.001, max_backoff=0.01) == 3 and flaky.n == 3
