# ruff: noqa: F811
"""F-15 against a real PostgreSQL ledger: what a receipt, a finalization, a fill, a reorg and an
automatic lock release must carry before the ledger accepts them, and what the receipt worker and
the reconciler do under hostile, stale, forked or flapping providers, retries, restarts and
concurrent workers. Nothing is signed, broadcast or funded."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import TEST_URL  # noqa: F401
from sqlalchemy.exc import DBAPIError
from test_trading_ledger import (  # noqa: F401
    OWNER,
    U1,
    Actor,
    LedgerRefused,
    clock,
    ledger,
    live,
    owner_engine,
    raw,
    rw_engine,
)
from test_wallet_receipts import AMT, GAS, PAIR, T, leg_of, mined_buy, results, rr, state
from test_wallet_reconcile import H1, H2, FakeNode, errored, node, rec, submitted  # noqa: F401
from walletkit import (
    TOKEN,
    WALLET,
    Provider,
    block_hash,
    deposit_log,
    fake_evidence,
    transfer_log,
)

from agora_tracker.trading.ledger.legs import FillRecord
from agora_tracker.trading.v2 import ROUTER
from agora_tracker.trading.wallet.corroboration import Corroborator
from agora_tracker.trading.wallet.reconcile import swap_deadline

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")


def ev_rows(live, oid):
    return live.chain_evidence_for(leg_of(live, oid)["id"])


def kinds(live, oid):
    return [e["kind"] for e in ev_rows(live, oid)]


def counts(engine):
    return {t: raw(engine, f"SELECT count(*) FROM trading_private.{t}")[0][0]
            for t in ("order_events", "order_legs", "order_fills", "chain_evidence",
                      "execution_attempts", "orders")}


def good(live, leg, *, state_="included", **kw):
    return fake_evidence(live.clock, leg, state_, tx_hash=H1, **kw)


def verified_fill() -> FillRecord:
    return FillRecord("verified", "buy", TOKEN, AMT, T, GAS, "logs == balances", None,
                      method_agreement=True)


def apply(live, leg, evidence, *, fill=None, finalize=False, **over):
    kw = dict(expected_version=leg["version"], state="included", block_number=1000,
              block_hash=block_hash(1000), receipt_status=1, gas_used=150_000, gas_cost_wei=GAS,
              fill=fill, finalize=finalize, evidence=evidence)
    kw.update(over)
    return live.apply_receipt(leg["id"], **kw)


# ====================================================================== the ledger refuses uncorroborated writes

def test_a_receipt_without_evidence_is_refused_and_changes_nothing(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    before = counts(owner_engine)
    with pytest.raises(LedgerRefused) as e:
        apply(live, row, None, fill=verified_fill())
    assert e.value.code == "evidence_required"
    assert counts(owner_engine) == before and state(live, oid) == "submitted"


def mut(fn):
    def go(ev):
        ev = copy.deepcopy(ev)
        fn(ev)
        return ev
    return go


BAD = {
    "one_source": mut(lambda e: e.__setitem__("sources", e["sources"][:1])),
    "same_operator": mut(lambda e: e["sources"][1].__setitem__("operator", "alpha.test")),
    "same_name": mut(lambda e: e["sources"][1].__setitem__("name", e["sources"][0]["name"])),
    "no_operator": mut(lambda e: e["sources"][1].pop("operator")),
    "no_agreement": mut(lambda e: e.__setitem__("source_agreement", False)),
    "agreement_not_true": mut(lambda e: e.__setitem__("source_agreement", "yes")),
    "problems": mut(lambda e: e.__setitem__("problems", ["stale_head"])),
    "failed_source": mut(lambda e: e.__setitem__("failed", [{"name": "x", "error": "down"}])),
    "stale": mut(lambda e: e.__setitem__("taken_at", e["taken_at"] - 3600)),
    "future": mut(lambda e: e.__setitem__("taken_at", e["taken_at"] + 3600)),
    "wrong_chain": mut(lambda e: e.__setitem__("chain_id", 1)),
    "no_anchor": mut(lambda e: e.__setitem__("anchor", None)),
    "bad_anchor_hash": mut(lambda e: e["anchor"].__setitem__("hash", "0x12")),
    "no_finality": mut(lambda e: e.pop("finality")),
    "wrong_schema": mut(lambda e: e.__setitem__("schema", 2)),
    "other_leg": mut(lambda e: e["facts"].__setitem__("leg_id", 999)),
    "stale_version": mut(lambda e: e["facts"].__setitem__("leg_version", 0)),
    "other_outcome": mut(lambda e: e["facts"].__setitem__("outcome", "reverted")),
    "other_block": mut(lambda e: e["facts"].__setitem__("block_number", 1001)),
    "other_block_hash": mut(lambda e: e["facts"].__setitem__("block_hash", block_hash(1000, 3))),
    "other_status": mut(lambda e: e["facts"].__setitem__("receipt_status", 0)),
    "other_tx": mut(lambda e: e["facts"].__setitem__("tx_hash", "0x" + "ee" * 32)),
    "no_confirmations": mut(lambda e: e["facts"].__setitem__("confirmations", 0)),
    "huge": mut(lambda e: e.__setitem__("reads", ["x" * 100] * 400)),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_every_malformed_or_foreign_evidence_is_refused_and_nothing_is_written(
        live, node, owner_engine, name):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    before = counts(owner_engine)
    with pytest.raises(LedgerRefused) as e:
        apply(live, row, BAD[name](good(live, row)))
    assert e.value.code == "evidence_required"
    assert counts(owner_engine) == before and leg_of(live, oid)["state"] == "hash_reported"


def test_good_evidence_is_accepted_stored_append_only_and_tied_to_the_write(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    assert apply(live, row, good(live, row)) is True
    (e,) = ev_rows(live, oid)
    assert (e["kind"], e["source_count"], e["source_agreement"], e["chain_id"]) == (
        "inclusion", 2, True, 99901)
    assert e["anchor_number"] == 2000 and len(e["evidence_digest"]) == 64
    # a stale replay changes nothing and stores nothing (the evidence row shares the transaction)
    assert apply(live, row, good(live, row)) is False
    assert len(ev_rows(live, oid)) == 1


def test_finalizing_needs_corroborated_confirmations_at_the_stated_finality(live, node):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    with pytest.raises(LedgerRefused, match="finality"):
        apply(live, row, good(live, row, confirmations=11, finality=12), finalize=True)
    assert apply(live, row, good(live, row, confirmations=12, finality=12), finalize=True) is True
    assert leg_of(live, oid)["finalized_at"] is not None and kinds(live, oid) == ["final"]


# ---------------------------------------------------------------------- verified fills need BOTH properties

def test_a_verified_fill_needs_method_agreement_in_the_fill(live, node):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    fill = FillRecord("verified", "buy", TOKEN, AMT, T, GAS, "x", None, method_agreement=False)
    with pytest.raises(LedgerRefused, match="separate properties"):
        apply(live, row, good(live, row, fill=fill), fill=fill)


def test_a_verified_fill_needs_source_agreement_of_its_inputs_in_the_evidence(live, node):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    fill = verified_fill()
    ev = good(live, row, fill=fill)
    ev["fill"]["inputs_source_agreed"] = False
    with pytest.raises(LedgerRefused, match="separate properties"):
        apply(live, row, ev, fill=fill)
    ev2 = good(live, row)                                  # evidence silent about the fill's inputs
    with pytest.raises(LedgerRefused, match="separate properties"):
        apply(live, row, ev2, fill=fill)
    ev3 = good(live, row, fill=fill)
    ev3["fill"]["method_agreement"] = False                # methods disagreed per the evidence
    with pytest.raises(LedgerRefused, match="separate properties"):
        apply(live, row, ev3, fill=fill)
    assert live.get_fills(U1, oid) == []


def test_the_fill_record_itself_must_carry_method_agreement_even_if_the_evidence_claims_it(live, node):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    liar = FillRecord("verified", "buy", TOKEN, AMT, T, GAS, "x", None, method_agreement=False)
    ev = good(live, row, fill=verified_fill())          # evidence says: methods agree, inputs agreed
    with pytest.raises(LedgerRefused, match="separate properties"):
        apply(live, row, ev, fill=liar)
    assert live.get_fills(U1, oid) == []


def test_a_verified_fill_points_at_the_evidence_that_certified_it(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    fill = verified_fill()
    assert apply(live, row, good(live, row, fill=fill), fill=fill) is True
    (f,) = live.get_fills(U1, oid)
    (e,) = ev_rows(live, oid)
    assert f["evidence_id"] == e["id"] and e["method_agreement"] is True
    # the database independently refuses a verified fill that has no evidence at all
    with pytest.raises(DBAPIError, match="ck_fill_verified_evidence"):
        raw(owner_engine, "INSERT INTO trading_private.order_fills (order_id, leg_id, user_id, wallet, "
            "chain_id, token, side, tx_hash, block_number, block_hash, verdict, native_wei, token_wei, "
            "gas_cost_wei, basis, reason) SELECT order_id, leg_id, user_id, wallet, chain_id, token, "
            "side, tx_hash, block_number, '0x" + "77" * 32 + "', 'verified', native_wei, token_wei, "
            "gas_cost_wei, basis, reason FROM trading_private.order_fills")


# ---------------------------------------------------------------------- reorg and lock-release writes

def included(live, node, **kw):
    oid, leg, ch = mined_buy(live, node, **kw)
    r = rr(live, ch)
    assert r.run_once().counts == {"provisional_included": 1}
    return oid, leg_of(live, oid), ch, r


def test_a_reorg_without_corroborated_evidence_is_refused(live, node, owner_engine):
    oid, row, ch, r = included(live, node)
    before = counts(owner_engine)
    for bad in (None, {}, good(live, row, state_="included")):
        with pytest.raises(LedgerRefused) as e:
            live.apply_reorg(row["id"], expected_version=row["version"], reason="gone", evidence=bad)
        assert e.value.code == "evidence_required"
    ev = fake_evidence(live.clock, row, "reorg", basis="absent", block_hash_=block_hash(1000, 5))
    with pytest.raises(LedgerRefused, match="another inclusion"):
        live.apply_reorg(row["id"], expected_version=row["version"], reason="gone", evidence=ev)
    ev = fake_evidence(live.clock, row, "reorg", basis="vibes", block_hash_=row["block_hash"])
    with pytest.raises(LedgerRefused, match="reorg"):
        live.apply_reorg(row["id"], expected_version=row["version"], reason="gone", evidence=ev)
    assert counts(owner_engine) == before and state(live, oid) == "included"


@pytest.mark.parametrize("to_state", ["superseded", "abandoned", "rejected"])
def test_a_terminal_verdict_without_corroborated_evidence_is_refused(live, node, to_state):
    oid, plan, leg, _ = errored(live, node)
    for bad in (None, {}, fake_evidence(live.clock, leg, "hash_reported")):
        with pytest.raises(LedgerRefused) as e:
            live.resolve_leg(leg["id"], expected_version=leg["version"], to_state=to_state,
                             resolution="x", evidence=bad)
        assert e.value.code == "evidence_required"
    assert leg_of(live, oid)["state"] == "ambiguous" and state(live, oid) == "ambiguous"


def test_a_rejection_is_never_decided_from_chain_evidence(live, node):
    oid, plan, leg, _ = errored(live, node)
    ev = fake_evidence(live.clock, leg, "rejected")
    with pytest.raises(LedgerRefused, match="never decided from chain"):
        live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="rejected",
                         resolution="x", evidence=ev)


def test_recording_a_final_tx_hash_needs_evidence_but_tightening_moves_do_not(live, node):
    oid, plan, leg, _ = errored(live, node)
    with pytest.raises(LedgerRefused):
        live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="hash_reported",
                         resolution="x", final_tx_hash=H2, seen=True)
    # moves that only keep or tighten the block need none: a wrong one cannot release anything
    assert live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="hash_reported",
                            resolution="pending", nonce=5, seen=True) is True


SUPERSEDED_FACTS = {"final_tx_hash": H2, "consuming_tx": {
    "hash": H2, "block_number": 1050, "block_hash": block_hash(1050), "nonce": 5,
    "confirmations": 12, "mismatches": ["to"]}}


@pytest.mark.parametrize("name,fix", [
    ("shallow", lambda f: f["consuming_tx"].__setitem__("confirmations", 11)),
    ("no_mismatch", lambda f: f["consuming_tx"].__setitem__("mismatches", [])),
    ("no_consumer", lambda f: f.pop("consuming_tx")),
    ("bad_block_hash", lambda f: f["consuming_tx"].__setitem__("block_hash", "0x1"))])
def test_a_supersession_needs_a_different_transaction_that_is_final(live, node, name, fix):
    oid, plan, leg, _ = errored(live, node)
    facts = copy.deepcopy(SUPERSEDED_FACTS)
    fix(facts)
    ev = fake_evidence(live.clock, leg, "superseded", **facts)
    with pytest.raises(LedgerRefused):
        live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="superseded",
                         resolution="x", nonce=5, final_tx_hash=H2, evidence=ev)
    assert leg_of(live, oid)["state"] == "ambiguous"
    ok = fake_evidence(live.clock, leg, "superseded", **SUPERSEDED_FACTS)
    assert live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="superseded",
                            resolution="x", nonce=5, final_tx_hash=H2, evidence=ok,
                            order_path=("ambiguous", "cancelled"), order_reason="superseded") is True
    assert kinds(live, oid) == ["resolution"] and state(live, oid) == "cancelled"


@pytest.mark.parametrize("name,facts", [
    ("queue_not_empty", {"wallet_queue_empty": False, "anchor_timestamp": 10_000, "swap_deadline": 1,
                         "deadline_margin_s": 90}),
    ("deadline_not_passed", {"wallet_queue_empty": True, "anchor_timestamp": 100, "swap_deadline": 100,
                             "deadline_margin_s": 90}),
    ("margin_too_small", {"wallet_queue_empty": True, "anchor_timestamp": 101, "swap_deadline": 100,
                          "deadline_margin_s": 0}),
    ("no_deadline_no_policy", {"wallet_queue_empty": True, "anchor_timestamp": 10_000}),
    ("approval_too_young", {"wallet_queue_empty": True, "anchor_timestamp": 10_000,
                            "policy": "approval_never_sent", "age_s": 5})])
def test_an_abandonment_needs_a_passed_deadline_or_policy_and_an_empty_queue(live, node, name, facts):
    oid, plan, leg, _ = errored(live, node)
    ev = fake_evidence(live.clock, leg, "abandoned", **facts)
    with pytest.raises(LedgerRefused):
        live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="abandoned",
                         resolution="x", evidence=ev)
    assert leg_of(live, oid)["state"] == "ambiguous"


# ---------------------------------------------------------------------- the evidence table itself

def test_chain_evidence_is_append_only_and_least_privilege(live, node, owner_engine, rw_engine):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    apply(live, row, good(live, row))
    for sql in ("UPDATE trading_private.chain_evidence SET code = 'x'",
                "DELETE FROM trading_private.chain_evidence",
                "TRUNCATE trading_private.chain_evidence"):
        with pytest.raises(DBAPIError):
            raw(owner_engine, sql)
    grants = {r[0] for r in raw(owner_engine, "SELECT privilege_type FROM information_schema."
                                              "role_table_grants WHERE table_name = 'chain_evidence' "
                                              "AND grantee = 'trading_rw'")}
    assert grants == {"SELECT", "INSERT"}
    assert raw(rw_engine, "SELECT count(*) FROM trading_private.chain_evidence")[0][0] == 0   # no scope: RLS
    with pytest.raises(DBAPIError):
        raw(rw_engine, "UPDATE trading_private.chain_evidence SET code = 'x'")


def test_evidence_with_fewer_than_two_agreeing_sources_cannot_be_inserted_by_any_path(
        live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    row = leg_of(live, oid)
    with pytest.raises(DBAPIError, match="ck_evidence_sources"):
        raw(owner_engine, "INSERT INTO trading_private.chain_evidence (leg_id, order_id, user_id, kind, "
            "chain_id, anchor_number, anchor_hash, source_count, source_agreement, evidence, "
            "evidence_digest) SELECT id, order_id, user_id, 'inclusion', 99901, 1, '0x" + "11" * 32 +
            "', 1, true, '{}'::jsonb, repeat('a', 64) FROM trading_private.order_legs WHERE id = :i",
            i=row["id"])


# ====================================================================== the receipt worker under hostile providers

def test_two_hosts_of_one_operator_record_nothing(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    ps = [Provider(ch, "rpc1.shop.test", live.clock), Provider(ch, "rpc2.shop.test", live.clock)]
    r = rr(live, ch)
    r.corro = Corroborator(ps, clock=live.clock)
    rep = r.run_once()
    assert rep.counts == {"unverifiable": 1} and rep.codes == {"sources_not_independent": 1}
    assert state(live, oid) == "submitted" and counts(owner_engine)["chain_evidence"] == 0


def test_a_provider_that_hides_the_receipt_is_a_recorded_conflict_not_a_verdict(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    ch.providers[1].hidden.add(H1)
    assert r.run_once().counts == {"conflict": 1}
    assert state(live, oid) == "submitted" and live.get_fills(U1, oid) == []
    assert kinds(live, oid) == ["conflict"]
    assert r.run_once().counts == {"conflict": 1} and kinds(live, oid) == ["conflict"]   # deduplicated
    ch.providers[1].hidden.clear()
    assert r.run_once().counts == {"provisional_included": 1}
    assert kinds(live, oid) == ["conflict", "inclusion"]


def test_a_fork_between_providers_records_nothing_until_they_agree(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    ch.providers[1].header_override[1005] = block_hash(1005, 4)
    rep = r.run_once()
    assert rep.counts == {"unverifiable": 1} and rep.codes == {"sources_disagree_on_chain": 1}
    assert state(live, oid) == "submitted" and live.get_fills(U1, oid) == []
    ch.providers[1].header_override.clear()
    assert r.run_once().counts == {"provisional_included": 1}


def test_stale_providers_record_nothing_until_fresh(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    ch.providers[0].age = 900
    rep = r.run_once()
    assert rep.counts == {"unverifiable": 1} and rep.codes == {"stale_head": 1}
    assert state(live, oid) == "submitted"
    ch.providers[0].age = 2
    assert r.run_once().counts == {"provisional_included": 1}


def test_a_lagging_provider_delays_the_record_it_does_not_block_or_corrupt_it(live, node):
    oid, leg, ch = mined_buy(live, node, block=1005, head=1005)
    r = rr(live, ch)
    ch.providers[1].lag = 1                              # B has not seen block 1005 yet
    assert r.run_once().counts == {"pending": 1}         # "ahead": retry, NOT a disagreement
    assert state(live, oid) == "submitted" and kinds(live, oid) == []
    ch.providers[1].lag = 0
    assert r.run_once().counts == {"provisional_included": 1}


def test_inconsistent_receipts_across_providers_record_nothing(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    ch.providers[1].rc_override[H1] = dict(ch.receipts[H1], status="0x0")
    assert r.run_once().counts == {"conflict": 1}
    assert state(live, oid) == "submitted" and live.get_fills(U1, oid) == []


def test_a_node_without_history_never_yields_a_verified_fill(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    ch.providers[1].no_history = True
    for _ in range(3):
        assert r.run_once().counts == {"evidence_unavailable": 1}
        assert live.get_fills(U1, oid) == [] and state(live, oid) == "submitted"
    assert r.run_once().counts == {"provisional_included": 1}      # the inclusion is real...
    (f,) = live.get_fills(U1, oid)
    assert f["verdict"] == "unknown" and "historical_state_unavailable" in f["reason"]
    assert f["token_wei"] is None and f["native_wei"] is None      # ...the numbers are not guessed
    (e,) = [x for x in ev_rows(live, oid) if x["kind"] == "inclusion"]
    assert e["method_agreement"] is False


def test_one_provider_fabricating_consistent_balances_never_makes_a_verified_fill(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    ch.providers[1].tok_shift = 11                        # B alone says something else (coherent for B)
    for _ in range(3):
        assert r.run_once().counts == {"evidence_unavailable": 1}
    assert r.run_once().counts == {"provisional_included": 1}
    (f,) = live.get_fills(U1, oid)
    assert f["verdict"] == "unknown" and "sources_disagree_on_balance" in f["reason"]


def test_a_verified_fill_carries_both_properties_in_its_evidence(live, node):
    oid, leg, ch = mined_buy(live, node)
    assert rr(live, ch).run_once().counts == {"provisional_included": 1}
    (f,) = live.get_fills(U1, oid)
    (e,) = ev_rows(live, oid)
    assert f["verdict"] == "verified" and f["evidence_id"] == e["id"]
    assert e["method_agreement"] is True and e["source_agreement"] is True and e["source_count"] == 2
    ev = e["evidence"]
    assert [s["operator"] for s in ev["sources"]] == ["alpha.test", "beta.test"]
    assert ev["fill"]["method_agreement"] is True and ev["fill"]["inputs_source_agreed"] is True
    assert ev["finality"] == 12 and ev["chain_id"] == 99901


# ---------------------------------------------------------------------- finality depth is not changed

def test_finality_uses_the_configured_depth_at_the_shared_anchor(live, node):
    oid, leg, ch = mined_buy(live, node, head=1011)         # 12 confirmations on A
    r = rr(live, ch)
    ch.providers[1].lag = 2                                  # shared anchor 1009: only 10 deep
    assert r.run_once().counts == {"provisional_included": 1}
    assert leg_of(live, oid)["finalized_at"] is None
    ch.providers[1].lag = 0
    assert r.run_once().counts == {"final_included": 1}
    assert kinds(live, oid) == ["inclusion", "final"]
    from agora_tracker.config import TradingConfig
    assert TradingConfig().finality_confirmations == 12


# ---------------------------------------------------------------------- reorgs

def test_a_corroborated_reorg_orphans_and_keeps_every_record(live, node, owner_engine):
    oid, row, ch, r = included(live, node)
    ch.canon[1000] = block_hash(1000, 9)                    # every provider: a different block 1000
    ch.drop(H1)
    assert r.run_once().counts == {"orphaned": 1}
    assert state(live, oid) == "orphaned" and leg_of(live, oid)["state"] == "orphaned"
    fills = live.get_fills(U1, oid, include_rolled_back=True)
    assert [(f["status"], f["verdict"]) for f in fills] == [("rolled_back", "verified")]   # kept
    assert kinds(live, oid) == ["inclusion", "reorg"]
    reorg = ev_rows(live, oid)[1]["evidence"]["facts"]
    assert reorg["basis"] == "absent" and reorg["block_hash"] == row["block_hash"]


def test_a_single_provider_saying_the_receipt_is_gone_orphans_nothing(live, node):
    oid, row, ch, r = included(live, node)
    ch.providers[1].hidden.add(H1)
    assert r.run_once().counts == {"conflict": 1}
    assert state(live, oid) == "included" and leg_of(live, oid)["state"] == "included"
    assert [f["status"] for f in live.get_fills(U1, oid)] == ["provisional"]


def test_a_single_provider_on_a_fork_orphans_nothing(live, node):
    oid, row, ch, r = included(live, node)
    ch.providers[1].header_override[1000] = block_hash(1000, 9)
    rep = r.run_once()
    assert set(rep.counts) <= {"conflict", "unverifiable"} and "orphaned" not in rep.counts
    assert state(live, oid) == "included"


def test_every_provider_losing_the_receipt_while_the_block_is_unchanged_is_a_conflict(live, node):
    oid, row, ch, r = included(live, node)
    ch.drop(H1)                                              # canonical block 1000 unchanged
    assert r.run_once().counts == {"conflict": 1}
    assert state(live, oid) == "included"
    assert [e["code"] for e in ev_rows(live, oid) if e["kind"] == "conflict"] == [
        "receipt_missing_in_canonical_block"]


def test_providers_behind_the_recorded_block_are_not_proof_of_a_reorg(live, node):
    oid, row, ch, r = included(live, node)
    ch.drop(H1)
    ch.head_n = 998
    assert r.run_once().counts == {"pending": 1} and state(live, oid) == "included"


def test_reinclusion_after_a_reorg_adds_a_new_fill_and_keeps_the_old_one(live, node):
    oid, row, ch, r = included(live, node)
    ch.canon[1000] = block_hash(1000, 9)
    ch.drop(H1)
    assert r.run_once().counts == {"orphaned": 1}
    ch.mine(leg_of(live, oid), H1, 1002, fork=3, logs=[deposit_log(ROUTER, AMT),
                                                       transfer_log(TOKEN, PAIR, WALLET, T)])
    ch.buy_balances(1002, T)
    assert r.run_once().counts == {"provisional_included": 1}
    fills = live.get_fills(U1, oid, include_rolled_back=True)
    assert [f["status"] for f in fills] == ["rolled_back", "provisional"]
    assert fills[0]["block_hash"] != fills[1]["block_hash"] and state(live, oid) == "included"
    assert kinds(live, oid) == ["inclusion", "reorg", "inclusion"]


# ---------------------------------------------------------------------- retries, restarts, concurrency

def test_restart_after_a_conflict_neither_duplicates_nor_resubmits(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    r1 = rr(live, ch)
    ch.providers[1].hidden.add(H1)
    r1.run_once()
    before = counts(owner_engine)
    r2 = rr(live, ch)                                        # "process restarted": no memory at all
    ch.providers[1].hidden.clear()
    assert r2.run_once().counts == {"provisional_included": 1}
    ch.head_n = 1011
    assert r2.run_once().counts == {"final_included": 1}
    after = counts(owner_engine)
    assert after["order_fills"] == 1 and after["order_legs"] == before["order_legs"] == 1
    assert after["orders"] == 1 and after["execution_attempts"] == before["execution_attempts"] == 1
    assert results(owner_engine, oid).count(("receipt_included",)) == 2     # provisional + final
    assert len(live.get_legs(U1, oid)) == 1                  # never a second leg: no resubmission
    assert kinds(live, oid) == ["conflict", "inclusion", "final"]


def test_a_provider_flapping_between_passes_never_double_counts(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    outcomes = []
    for i in range(6):
        ch.providers[i % 2].down = i % 3 == 0
        ch.providers[(i + 1) % 2].down = False
        outcomes.append(tuple(r.run_once().counts))
    ch.providers[0].down = ch.providers[1].down = False
    r.run_once()
    assert len(live.get_fills(U1, oid, include_rolled_back=True)) == 1
    assert state(live, oid) == "included" and kinds(live, oid).count("inclusion") == 1


def test_concurrent_workers_record_exactly_one_inclusion_and_one_evidence_row(live, node):
    oid, leg, ch = mined_buy(live, node)
    with ThreadPoolExecutor(6) as ex:
        reps = list(ex.map(lambda _: rr(live, ch).run_once(), range(6)))
    allowed = {"provisional_included", "provisional_unchanged", "stale"}
    assert all(set(x.counts) <= allowed for x in reps)
    assert sum(x.counts.get("provisional_included", 0) for x in reps) == 1
    assert len(live.get_fills(U1, oid, include_rolled_back=True)) == 1
    assert kinds(live, oid) == ["inclusion"] and state(live, oid) == "included"


def test_a_pass_never_spends_more_than_its_call_budget(live, node):
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch, rpc_budget=40)
    rep = r.run_once()
    assert rep.calls <= 40 and sum(len(p.calls) for p in ch.providers) <= 40


# ====================================================================== the reconciler: lock release needs both

def stuck_hash(live, node):
    """A hash-reported swap (nonce known after the first pass) whose hash no provider has."""
    oid, _, leg, _ = submitted(live, node)
    node.add(leg, H1)
    rec(live, node).run_once()
    del node.txs[H1]
    return oid, leg


def test_one_provider_claiming_the_nonce_is_used_releases_nothing(live, node):
    oid, leg = stuck_hash(live, node)
    other = FakeNode()
    node.add(leg, H2, nonce=5, mined=1000, to=leg["wallet"], input="0x", value="0x0")
    node.head_n = other.head_n = 1120
    r = rec(live, node, other)                              # `other` still sees nonce 5 as free
    assert r.run_once().counts == {"conflict": 1}
    assert leg_of(live, oid)["state"] == "hash_reported" and state(live, oid) == "submitted"
    assert "resolution" not in kinds(live, oid)


def test_both_providers_agreeing_on_a_final_other_transaction_supersedes_with_evidence(live, node):
    oid, leg = stuck_hash(live, node)
    node.add(leg, H2, nonce=5, mined=1095, to=leg["wallet"], input="0x", value="0x0")
    node.head_n = 1120
    assert rec(live, node).run_once().counts == {"superseded": 1}
    (e,) = [x for x in ev_rows(live, oid) if x["kind"] == "resolution"]
    ct = e["evidence"]["facts"]["consuming_tx"]
    assert ct["hash"] == H2 and ct["confirmations"] >= 12 and e["source_count"] == 2
    assert leg_of(live, oid)["state"] == "superseded" and state(live, oid) == "cancelled"


def test_abandonment_is_judged_on_the_chains_clock_not_this_hosts(live, node, clock):
    oid, plan, leg, _ = errored(live, node)
    r = rec(live, node)
    clock.t = swap_deadline(leg) + 10_000                   # this host's clock says: long past
    for p in r.providers:
        p.age = 10_000                                      # ...but the chain has not moved for hours
    rep = r.run_once()
    assert rep.counts == {"unknown": 1} and rep.codes == {"stale_head": 1}
    assert leg_of(live, oid)["state"] == "ambiguous"
    for p in r.providers:
        p.age = 2
    assert rec(live, node).run_once().counts == {"abandoned": 1}
    (e,) = [x for x in ev_rows(live, oid) if x["kind"] == "resolution"]
    f = e["evidence"]["facts"]
    assert f["wallet_queue_empty"] is True and f["anchor_timestamp"] > f["swap_deadline"] + 90


def test_a_queued_transaction_on_any_one_provider_blocks_abandonment(live, node, clock):
    oid, plan, leg, _ = errored(live, node)
    clock.t = swap_deadline(leg) + 10_000
    r = rec(live, node)
    r.providers[1].nonce_bias = 0
    node.pending = 6                                        # a pending tx exists (seen by both here)
    assert r.run_once().counts == {"unresolved": 1}
    assert leg_of(live, oid)["state"] == "ambiguous"


def test_the_lock_stays_until_the_evidence_exists_whatever_a_provider_says(live, node, owner_engine):
    oid, plan, leg, _ = errored(live, node)
    held = raw(owner_engine, "SELECT count(*) FROM trading_private.order_legs WHERE state IN "
                             "('prompted','hash_reported','ambiguous')")[0][0]
    r = rec(live, node)
    r.providers[0].nonce_bias = 3                           # one provider lies about the nonce
    rep = r.run_once()
    assert rep.counts == {"conflict": 1} and rep.codes == {"sources_disagree_on_wallet_nonce": 1}
    assert raw(owner_engine, "SELECT count(*) FROM trading_private.order_legs WHERE state IN "
                             "('prompted','hash_reported','ambiguous')")[0][0] == held == 1


# ====================================================================== migration 0016

def test_migration_0016_creates_the_guarded_append_only_table_and_round_trips(pg_engine):
    from agora_tracker.migrate import downgrade, upgrade
    assert raw(pg_engine, "SELECT relrowsecurity FROM pg_class WHERE relname = 'chain_evidence'")[0][0]
    trig = {r[0] for r in raw(pg_engine, "SELECT tgname FROM pg_trigger WHERE tgrelid = "
                                         "'trading_private.chain_evidence'::regclass AND NOT tgisinternal")}
    assert {"chain_evidence_append_only", "chain_evidence_no_truncate"} <= trig
    assert raw(pg_engine, "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_fill_verified_evidence'")
    downgrade(pg_engine, "0015")
    assert raw(pg_engine, "SELECT count(*) FROM information_schema.tables WHERE table_schema = "
                          "'trading_private' AND table_name = 'chain_evidence'")[0][0] == 0
    assert raw(pg_engine, "SELECT count(*) FROM information_schema.columns WHERE table_name = "
                          "'order_fills' AND column_name = 'evidence_id'")[0][0] == 0
    upgrade(pg_engine)
    assert raw(pg_engine, "SELECT count(*) FROM information_schema.tables WHERE table_schema = "
                          "'trading_private' AND table_name = 'chain_evidence'")[0][0] == 1
