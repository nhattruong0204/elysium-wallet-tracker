# ruff: noqa: F811
"""Exactly-once logical handling under duplicates, restarts, replacement and bad evidence (#93).

Real PostgreSQL ledger; a scripted FakeNode plays the chain. Nothing is signed or broadcast."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import TEST_URL  # noqa: F401
from test_trading_ledger import (  # noqa: F401
    NOW0,
    U1,
    clock,
    ledger,
    live,
    owner_engine,
    raw,
    rw_engine,
    step,
)
from test_wallet_flow import SESSION, buy_order, mkflow, ok, state
from walletkit import E18

from agora_tracker.trading.ledger import OrderState
from agora_tracker.trading.paper import TradeRefused
from agora_tracker.trading.wallet.approvals import Head
from agora_tracker.trading.wallet.flow import FlowRefused, WalletFlow
from agora_tracker.trading.wallet.reconcile import LegReconciler, swap_deadline, timeline

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")
H1, H2, H3 = "0x" + "a1" * 32, "0x" + "b2" * 32, "0x" + "c3" * 32
OTHER_TO = "0x" + "77" * 20


class FakeNode:
    def __init__(self):
        self.txs: dict[str, dict] = {}
        self.blocks: dict[int, list[dict]] = {}
        self.latest = self.pending = 5
        self.head_n = 1100
        self.down = False

    # TxRead + NonceReader
    def head(self):
        if self.down:
            raise TradeRefused("down")
        return Head(self.head_n, "0x" + "ab" * 32, 0)

    def transaction(self, h):
        if self.down:
            raise TradeRefused("down")
        return self.txs.get(h)

    def tx_count(self, address, tag):
        if self.down:
            raise TradeRefused("down")
        return self.latest if tag == "latest" else self.pending

    def pending_nonce(self, address):
        return self.tx_count(address, "pending")

    def block_transactions(self, n):
        if self.down:
            raise TradeRefused("down")
        return self.blocks.get(n, [])

    def add(self, leg, h, *, nonce=5, mined=None, **changes):
        tx = {"hash": h, "from": leg["wallet"], "to": leg["expected_to"],
              "value": hex(int(leg["expected_value_wei"])), "input": leg["expected_data"],
              "gas": hex(int(leg["gas_limit"])), "gasPrice": hex(10**9), "chainId": "0x1863d",
              "nonce": hex(nonce), "blockNumber": hex(mined) if mined else None}
        tx.update(changes)
        self.txs[h] = tx
        if mined:
            self.blocks.setdefault(mined, []).append(tx)
            self.latest = max(self.latest, nonce + 1)
        else:
            self.pending = max(self.pending, nonce + 1)
        return tx


@pytest.fixture
def node():
    return FakeNode()


def submitted(live, node, key="buy-order-1", **kw):
    """A buy order whose wallet reported hash H1. Returns (order id, plan, leg, flow)."""
    oid, plan = buy_order(live, key, **kw)
    flow = WalletFlow(live, mkflow(live).planner, clock=live.clock, nonce_reader=node)
    rid = flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                           revalidate=ok)["request"]["id"]
    flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="hash", tx_hash=H1)
    return oid, plan, live.get_legs(U1, oid)[0], flow


def errored(live, node, key="buy-order-1"):
    oid, plan = buy_order(live, key)
    flow = WalletFlow(live, mkflow(live).planner, clock=live.clock, nonce_reader=node)
    rid = flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan,
                           revalidate=ok)["request"]["id"]
    flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="error", reason="timeout")
    return oid, plan, live.get_legs(U1, oid)[0], flow


def rec(live, *nodes, **kw):
    return LegReconciler(live, list(nodes), clock=live.clock, **kw)


def leg_of(live, oid):
    return live.get_legs(U1, oid)[0]


# ------------------------------------------------------------------ durable legs

def test_leg_records_the_exact_prompt_and_the_nonce_hint(live, node):
    oid, plan, leg, _ = submitted(live, node)
    assert leg["kind"] == "swap" and leg["state"] == "hash_reported" and leg["tx_hash"] == H1
    assert leg["nonce_hint"] == "5" and leg["expected_data"] == plan.transaction.calldata
    assert leg["expected_to"] == plan.transaction.target and leg["prompt_block"] == 1000
    assert leg["answered_at"] is not None and leg["nonce"] is None


def test_without_a_readable_nonce_the_prompt_fails_closed(live, node):
    oid, plan = buy_order(live)
    node.down = True
    flow = WalletFlow(live, mkflow(live).planner, clock=live.clock, nonce_reader=node)
    with pytest.raises(FlowRefused) as e:
        flow.swap_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan, revalidate=ok)
    assert e.value.code == "nonce_unavailable" and live.get_legs(U1, oid) == []


# ------------------------------------------------------------------ evidence -> state

def test_pending_then_mined_matching_tx_records_nonce_without_claiming_settlement(live, node):
    oid, _, leg, _ = submitted(live, node)
    node.add(leg, H1)
    r = rec(live, node)
    assert r.run_once().counts == {"pending": 1}
    got = leg_of(live, oid)
    assert got["state"] == "hash_reported" and got["nonce"] == "5" and got["seen_at"] is not None
    assert state(live, oid) == "submitted"
    v = got["version"]
    r.run_once()                                                 # idempotent: nothing to change
    assert leg_of(live, oid)["version"] == v
    node.txs[H1]["blockNumber"] = hex(1090)
    assert rec(live, node).run_once().counts == {"mined": 1}
    assert state(live, oid) == "submitted"                       # inclusion/finality belong to #94


def test_wallet_fee_changes_are_advisory_not_a_mismatch(live, node):
    oid, _, leg, _ = submitted(live, node)
    node.add(leg, H1, gasPrice=hex(10**11), gas=hex(int(leg["gas_limit"]) + 1))
    assert rec(live, node).run_once().counts == {"pending": 1}


@pytest.mark.parametrize("change", [{"to": OTHER_TO}, {"input": "0x00"}, {"value": "0x1"},
                                    {"from": "0x" + "99" * 20}, {"chainId": "0x1"}])
def test_a_different_transaction_under_our_hash_is_never_the_reviewed_trade(live, node, change):
    oid, _, leg, _ = submitted(live, node)
    node.add(leg, H1, **change)
    assert rec(live, node).run_once().counts == {"mismatch": 1}
    assert leg_of(live, oid)["state"] == "ambiguous" and state(live, oid) == "ambiguous"
    # the wallet stays blocked: no new prompt can race a transaction we do not understand
    oid2, plan2 = buy_order(live, "buy-order-2", amount_in=E18 // 4)
    with pytest.raises(FlowRefused) as e:
        mkflow(live).swap_prompt(user_id=U1, session=SESSION, order_id=oid2, plan=plan2, revalidate=ok)
    assert e.value.code == "wallet_busy"


def test_provider_lag_is_not_failure_then_the_tx_appears(live, node):
    oid, _, leg, _ = submitted(live, node)
    r = rec(live, node)
    assert r.run_once().counts == {"unresolved": 1}              # hash unknown to our RPC yet
    assert leg_of(live, oid)["state"] == "hash_reported" and state(live, oid) == "submitted"
    node.add(leg, H1)
    assert r.run_once().counts == {"pending": 1}


def test_an_unreachable_source_changes_nothing(live, node):
    oid, _, leg, _ = submitted(live, node)
    node.down = True
    assert rec(live, node).run_once().counts == {"unknown": 1}
    assert leg_of(live, oid)["version"] == leg["version"]


def test_presence_in_any_source_counts_but_absence_needs_every_source(live, node):
    oid, _, leg, _ = submitted(live, node)
    other = FakeNode()
    assert rec(live, node, other).run_once().counts == {"unresolved": 1}     # both healthy, both absent
    other.down = True
    assert rec(live, node, other).run_once().counts == {"unknown": 1}        # one blind: no belief
    node.add(leg, H1)
    assert rec(live, other, node).run_once().counts == {"pending": 1}        # one source saw it


def test_sources_that_disagree_on_what_a_hash_is_are_ambiguous(live, node):
    oid, _, leg, _ = submitted(live, node)
    other = FakeNode()
    node.add(leg, H1)
    other.add(leg, H1, to=OTHER_TO)
    assert rec(live, node, other).run_once().counts == {"mismatch": 1}
    assert state(live, oid) == "ambiguous"


# ------------------------------------------------------------------ replacement / nonce reuse

def test_speed_up_replacement_with_the_same_payload_is_the_same_logical_order(live, node):
    oid, _, leg, _ = submitted(live, node)
    node.add(leg, H1)
    rec(live, node).run_once()                                   # nonce 5 recorded
    del node.txs[H1]                                             # the wallet replaced it
    node.add(leg, H2, nonce=5, mined=1095, gasPrice=hex(3 * 10**9))
    assert rec(live, node).run_once().counts == {"replaced_same": 1}
    got = leg_of(live, oid)
    assert got["final_tx_hash"] == H2 and got["tx_hash"] == H1 and got["state"] == "hash_reported"
    assert state(live, oid) == "submitted"
    assert len(live.get_legs(U1, oid)) == 1                      # never a second logical leg


def test_cancel_replacement_proves_the_original_cannot_execute(live, node):
    oid, _, leg, _ = submitted(live, node)
    node.add(leg, H1)
    rec(live, node).run_once()
    del node.txs[H1]
    node.add(leg, H2, nonce=5, mined=1095, to=leg["wallet"], input="0x", value="0x0")
    assert rec(live, node).run_once().counts == {"superseded": 1}
    assert leg_of(live, oid)["state"] == "superseded"
    assert state(live, oid) == "cancelled"
    # the nonce is spent by another transaction: the wallet is free again
    oid2, plan2 = buy_order(live, "buy-order-2", amount_in=E18 // 4)
    assert mkflow(live).swap_prompt(user_id=U1, session=SESSION, order_id=oid2, plan=plan2,
                                    revalidate=ok)["request"]


def test_externally_consumed_nonce_with_unknown_hash_stays_ambiguous_and_blocked(live, node):
    oid, _, leg, _ = errored(live, node)
    node.latest = node.pending = 7
    node.blocks[1095] = [{"hash": H3, "from": leg["wallet"], "nonce": "0x5", "to": OTHER_TO,
                          "value": "0x0", "input": "0x", "chainId": "0x1863d"}]
    assert rec(live, node).run_once().counts == {"unresolved": 1}
    assert leg_of(live, oid)["state"] == "ambiguous" and state(live, oid) == "ambiguous"


def test_lost_answer_is_found_by_scanning_for_an_identical_request(live, node):
    oid, _, leg, _ = errored(live, node)
    node.add(leg, H2, nonce=5, mined=1095)
    assert rec(live, node).run_once().counts == {"replaced_same": 1}
    got = leg_of(live, oid)
    assert got["final_tx_hash"] == H2 and got["state"] == "hash_reported"
    assert state(live, oid) == "submitted"


def test_no_scan_window_means_unresolved_not_guessed(live, node):
    oid, _, leg, _ = errored(live, node)
    node.latest = node.pending = 9
    node.head_n = 99_999                                         # prompt block far outside the window
    assert rec(live, node, scan_blocks=10).run_once().counts == {"unresolved": 1}
    assert state(live, oid) == "ambiguous"


# ------------------------------------------------------------------ abandonment is evidence-only

def test_abandoned_only_after_the_swap_deadline_and_with_no_nonce_movement(live, node, clock):
    oid, plan, leg, _ = errored(live, node)
    r = rec(live, node)
    assert r.run_once().counts == {"unresolved": 1}              # before the deadline: wait
    assert state(live, oid) == "ambiguous"
    clock.t = swap_deadline(leg) + 200
    node.pending = 6                                             # something is pending: not safe
    assert r.run_once().counts == {"unresolved": 1}
    node.pending = 5
    assert r.run_once().counts == {"abandoned": 1}
    assert leg_of(live, oid)["state"] == "abandoned" and state(live, oid) == "cancelled"


def test_hash_reported_but_never_seen_is_abandoned_only_past_the_deadline(live, node, clock):
    oid, _, leg, _ = submitted(live, node)
    assert rec(live, node).run_once().counts == {"unresolved": 1}
    clock.t = swap_deadline(leg) + 200
    assert rec(live, node).run_once().counts == {"abandoned": 1}
    assert state(live, oid) == "cancelled"


# ------------------------------------------------------------------ concurrency / restarts

def test_two_reconcilers_applying_the_same_evidence_change_state_once(live, node, owner_engine):
    oid, _, leg, _ = submitted(live, node)
    node.add(leg, H1)
    gate = threading.Barrier(4)

    def go(_):
        gate.wait()
        return rec(live, node).run_once().counts

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(go, range(4)))
    events = raw(owner_engine, "SELECT count(*) FROM trading_private.order_events "
                               "WHERE order_id = :o AND request_key LIKE 'recon-%'", o=oid)
    assert events[0][0] == 1
    assert leg_of(live, oid)["version"] == leg["version"] + 1


def test_stale_evidence_is_not_applied_over_a_newer_leg_version(live, node):
    oid, _, leg, _ = submitted(live, node)
    assert live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="hash_reported",
                            resolution="first", nonce=5, seen=True) is True
    assert live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="ambiguous",
                            resolution="stale") is False
    assert leg_of(live, oid)["state"] == "hash_reported"


def test_restart_resumes_from_durable_state_and_duplicate_reports_do_nothing(live, node):
    oid, _, leg, flow = submitted(live, node)
    node.add(leg, H1)
    again = flow.report(user_id=U1, session=SESSION, request_id=leg["request_id"],
                        outcome="hash", tx_hash=H1)
    assert again["status"] == "duplicate" and len(live.list_orders(U1)) == 1
    fresh = LegReconciler(live, [node], clock=live.clock)         # "new process"
    assert fresh.run_once().counts == {"pending": 1}
    assert len(live.get_legs(U1, oid)) == 1 and len(live.get_order(U1, oid).attempts) == 1


def test_leg_columns_are_set_once_and_edges_are_enforced(live, node, owner_engine):
    oid, _, leg, _ = submitted(live, node)
    from sqlalchemy.exc import DBAPIError
    for sql in ("UPDATE trading_private.order_legs SET tx_hash = '0x" + "dd" * 32 + "', version = version + 1",
                "UPDATE trading_private.order_legs SET state = 'prompted', version = version + 1",
                "UPDATE trading_private.order_legs SET version = version + 2",
                "DELETE FROM trading_private.order_legs"):
        with pytest.raises(DBAPIError):
            raw(owner_engine, sql)


def test_the_database_allows_one_unsettled_leg_per_wallet(live, node, owner_engine):
    submitted(live, node)
    rows = raw(owner_engine, "SELECT count(*) FROM trading_private.order_legs WHERE state IN "
                             "('prompted','hash_reported','ambiguous')")
    assert rows[0][0] == 1
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        raw(owner_engine, "INSERT INTO trading_private.order_legs (order_id, user_id, leg_no, kind, "
            "wallet, chain_id, idempotency_key, request_digest, expected_to, expected_value, "
            "expected_data) SELECT order_id, user_id, 2, 'approve', wallet, chain_id, 'dup-key-0001', "
            "request_digest, expected_to, 0, expected_data FROM trading_private.order_legs")


def test_order_ledger_chain_stays_valid_after_reconciliation(live, node):
    oid, _, leg, _ = submitted(live, node)
    node.add(leg, H1, to=OTHER_TO)
    rec(live, node).run_once()
    assert live.verify_chain().ok
    assert OrderState(state(live, oid)) == OrderState.AMBIGUOUS


def test_timeline_measures_each_stage_separately(live, node, clock):
    oid, plan, leg, _ = submitted(live, node)
    clock.t = NOW0 + 12
    node.add(leg, H1)
    rec(live, node).run_once()
    t = timeline(leg_of(live, oid), quote_ready_at=plan.created_at)
    assert set(t) == {"quote_to_prompt_s", "prompt_to_answer_s", "answer_to_seen_s",
                      "seen_to_included_s", "included_to_settled_s"}
    assert t["quote_to_prompt_s"] == pytest.approx(0, abs=1) and t["answer_to_seen_s"] >= 11
    assert t["seen_to_included_s"] is None and t["included_to_settled_s"] is None  # #94 fills these
