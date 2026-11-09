"""RPC corroboration (#95 F-15): receipts, canonical block evidence and fill accounting are never
accepted because ONE provider says so.

Unit half (no database): the shared `Session` against scriptable providers that lag, go stale,
fork, hide, fabricate, lack historical state or collude. Database half: what the ledger records,
refuses and keeps (append-only provenance), under reorgs, retries, restarts and concurrent workers.
Nothing is signed, broadcast or funded; every number is built independently of the code under test."""

from __future__ import annotations

import pytest
from test_wallet_fills import AMT, PAIR, H, buy_world, sell_world
from walletkit import (
    E18,
    NOW,
    TOKEN,
    WALLET,
    Provider,
    ReceiptChain,
    block_hash,
    corroborator,
    deposit_log,
    providers,
    session_of,
    transfer_log,
)

from agora_tracker.trading.v2 import ROUTER
from agora_tracker.trading.wallet import fills as F
from agora_tracker.trading.wallet.corroboration import (
    BudgetExhausted,
    Conflict,
    Corroborator,
    EvidenceError,
    Unverifiable,
)

T = 3 * E18


def clk():
    return NOW


def mined_world(*, head=1005, block=1000):
    """A buy mined at `block` on a fresh scriptable chain; returns (leg, tx, rc, world)."""
    leg, tx, rc, ch = buy_world()
    ch.head_n = head
    if block != 1000:
        ch2 = ReceiptChain(head)
        tx, rc = ch2.mine(leg, H, block, logs=rc["logs"])
        ch2.buy_balances(block, T)
        return leg, tx, rc, ch2
    return leg, tx, rc, ch


def code(excinfo) -> str:
    return excinfo.value.code


# ====================================================================== independence preconditions

def test_a_single_source_can_never_corroborate():
    ch = ReceiptChain(1005)
    c = Corroborator(providers(ch, clk, 1), clock=clk)
    snap = c.session().snapshot()
    assert not snap.ok and "needs_two_independent_sources" in snap.problems
    with pytest.raises(Unverifiable) as e:
        c.session().pin()
    assert code(e) == "needs_two_independent_sources"


def test_two_names_for_one_host_are_not_two_sources():
    ch = ReceiptChain(1005)
    a, b = Provider(ch, "node.alpha.test", clk), Provider(ch, "node.alpha.test", clk)
    snap = Corroborator([a, b], clock=clk).session().snapshot()
    assert "sources_not_independent" in snap.problems and not snap.ok


def test_two_hosts_of_one_operator_are_not_two_sources():
    """rpc1.provider.test and rpc2.provider.test are one company: agreement proves nothing."""
    ch = ReceiptChain(1005)
    a, b = Provider(ch, "rpc1.provider.test", clk), Provider(ch, "rpc2.provider.test", clk)
    assert a.operator == b.operator == "provider.test"
    snap = Corroborator([a, b], clock=clk).session().snapshot()
    assert "sources_not_independent" in snap.problems and not snap.ok


@pytest.mark.parametrize("attr,val,problem", [("chain_id", 1, "wrong_chain"),
                                              ("chain_verified", False, "chain_unverified")])
def test_a_source_on_another_or_unverified_chain_is_refused(attr, val, problem):
    ch = ReceiptChain(1005)
    ps = providers(ch, clk, 2)
    setattr(ps[1], attr, val)
    snap = Corroborator(ps, clock=clk).session().snapshot()
    assert problem in snap.problems and not snap.ok


# ====================================================================== one anchor, same-block facts

def test_a_provider_that_is_behind_moves_the_anchor_it_is_not_a_disagreement():
    leg, tx, rc, ch = mined_world(head=1010)
    c, (a, b) = corroborator(ch, clk)
    b.lag = 2                                       # B is two blocks behind A
    s = c.session()
    assert s.anchor().number == 1008
    inc = s.inclusion(H)
    assert inc.status == "mined" and inc.block_number == 1000 and inc.confirmations == 9


def test_a_transaction_mined_above_the_shared_anchor_is_ahead_not_a_conflict():
    leg, tx, rc, ch = mined_world(head=1002, block=1002)
    c, (a, b) = corroborator(ch, clk)
    b.lag = 1                                       # A has block 1002, B only 1001
    inc = c.session().inclusion(H)
    assert inc.status == "ahead" and inc.confirmations is None
    assert "retry" in (inc.detail or "")


def test_heads_too_far_apart_fail_closed():
    ch = ReceiptChain(1100)
    c, (a, b) = corroborator(ch, clk)
    b.lag = 65                                      # > MAX_HEAD_SKEW (64 blocks ~ 6-13 s)
    with pytest.raises(Unverifiable) as e:
        c.session().pin()
    assert code(e) == "heads_diverge"


def test_a_few_seconds_of_head_difference_is_normal_on_a_fast_chain():
    # Elysium makes 5-10 blocks/s: providers polled a moment apart differ by several blocks. The
    # lowest head is the anchor, so this is never a disagreement (F-07).
    ch = ReceiptChain(1100)
    c, (a, b) = corroborator(ch, clk)
    b.lag = 30
    snap = c.session().pin()
    assert snap.ok and snap.anchor.number == 1100 - 30


def test_a_stale_head_fails_closed():
    ch = ReceiptChain(1100)
    c, (a, b) = corroborator(ch, clk)
    b.age = 600                                     # B's newest block is ten minutes old
    with pytest.raises(Unverifiable) as e:
        c.session().pin()
    assert code(e) == "stale_head"


def test_providers_on_different_forks_at_the_anchor_do_not_corroborate():
    ch = ReceiptChain(1100)
    c, (a, b) = corroborator(ch, clk)
    b.header_override[1100] = block_hash(1100, 7)
    with pytest.raises(Conflict) as e:
        c.session().pin()
    assert code(e) == "sources_disagree_on_chain"


def test_a_fork_below_the_anchor_is_found_when_the_inclusion_block_is_read():
    leg, tx, rc, ch = mined_world(head=1010)
    c, (a, b) = corroborator(ch, clk)
    b.header_override[1000] = block_hash(1000, 7)   # B's canonical block 1000 is another block
    with pytest.raises(Conflict) as e:
        c.session().inclusion(H)
    assert code(e) == "sources_disagree_on_block"


def test_a_source_that_goes_down_fails_the_whole_read():
    leg, tx, rc, ch = mined_world()
    c, (a, b) = corroborator(ch, clk)
    b.down = True
    s = c.session()
    snap = s.snapshot()
    assert "source_unavailable" in snap.problems and [f["name"] for f in snap.failed] == [b.host]
    with pytest.raises(Unverifiable):
        s.inclusion(H)


# ====================================================================== inconsistent transaction facts

def test_all_sources_agreeing_gives_a_mined_inclusion_with_confirmations():
    leg, tx, rc, ch = mined_world(head=1005)
    inc = session_of(ch).inclusion(H)
    assert (inc.status, inc.block_number, inc.block_hash, inc.confirmations) == (
        "mined", 1000, block_hash(1000), 6)


def test_a_source_that_hides_a_mined_transaction_is_a_conflict_not_absence():
    leg, tx, rc, ch = mined_world()
    c, (a, b) = corroborator(ch, clk)
    b.hidden.add(H)
    with pytest.raises(Conflict) as e:
        c.session().inclusion(H)
    assert code(e) == "sources_disagree_on_transaction"


def test_both_sources_missing_it_is_absence():
    leg, tx, rc, ch = mined_world()
    ch.drop(H)
    assert session_of(ch).inclusion(H).status == "absent"


def test_a_receipt_without_a_transaction_contradicts_itself():
    leg, tx, rc, ch = mined_world()
    c, (a, b) = corroborator(ch, clk)
    a.hidden.discard(H)
    del ch.txs[H]                                   # receipts still served, transaction gone
    with pytest.raises(Conflict) as e:
        c.session().inclusion(H)
    assert code(e) == "source_contradicts_itself"


def test_a_receipt_for_a_different_block_than_its_transaction_is_self_contradictory():
    leg, tx, rc, ch = mined_world()
    ch.receipts[H]["blockHash"] = block_hash(1000, 5)
    with pytest.raises(Conflict) as e:
        session_of(ch).inclusion(H)
    assert code(e) == "source_contradicts_itself"


def test_sources_that_report_different_receipts_conflict():
    leg, tx, rc, ch = mined_world()
    c, (a, b) = corroborator(ch, clk)
    forged = dict(ch.receipts[H], status="0x0")
    b.rc_override[H] = forged
    with pytest.raises(Conflict) as e:
        c.session().inclusion(H)
    assert code(e) == "sources_disagree_on_receipt"


def test_sources_that_report_different_logs_conflict():
    leg, tx, rc, ch = mined_world()
    c, (a, b) = corroborator(ch, clk)
    b.rc_override[H] = dict(ch.receipts[H], logs=[transfer_log(TOKEN, PAIR, WALLET, 10 * T)])
    with pytest.raises(Conflict) as e:
        c.session().inclusion(H)
    assert code(e) == "sources_disagree_on_receipt"


def test_sources_that_report_different_transactions_under_one_hash_conflict():
    leg, tx, rc, ch = mined_world()
    c, (a, b) = corroborator(ch, clk)
    b.tx_override[H] = dict(ch.txs[H], input="0x00")
    with pytest.raises(Conflict) as e:
        c.session().inclusion(H)
    assert code(e) == "sources_disagree_on_transaction"


def test_a_malformed_response_fails_closed():
    leg, tx, rc, ch = mined_world()
    c, (a, b) = corroborator(ch, clk)
    b.rc_override[H] = dict(ch.receipts[H], status="0x2")
    with pytest.raises(Unverifiable) as e:
        c.session().inclusion(H)
    assert code(e) == "malformed_response"


def test_a_receipt_above_the_providers_own_head_is_self_contradictory():
    leg, tx, rc, ch = mined_world(head=1005)
    c, (a, b) = corroborator(ch, clk)
    b.rc_override[H] = dict(ch.receipts[H], blockNumber=hex(1900))
    b.tx_override[H] = dict(ch.txs[H], blockNumber=hex(1900))
    with pytest.raises(EvidenceError):
        c.session().inclusion(H)


def test_a_receipt_from_a_block_every_source_agrees_is_no_longer_canonical():
    leg, tx, rc, ch = mined_world(head=1010)
    c, (a, b) = corroborator(ch, clk)
    for p in (a, b):
        p.header_override[1000] = block_hash(1000, 9)
    assert c.session().inclusion(H).status == "not_canonical"


# ====================================================================== colluding-looking providers

def _fabricate(ch, leg, ps):
    """Make `ps` all claim a (fake) receipt that the honest chain does not have."""
    ch.drop(H)
    fake_tx, fake_rc = ReceiptChain(ch.head_n).mine(leg, H, 1000, logs=[
        deposit_log(ROUTER, AMT), transfer_log(TOKEN, PAIR, WALLET, T)])
    for p in ps:
        p.tx_override[H], p.rc_override[H] = fake_tx, fake_rc


def test_two_hosts_of_one_operator_cannot_lend_each_other_credibility():
    leg, tx, rc, ch = mined_world()
    ps = [Provider(ch, "rpc1.shop.test", clk), Provider(ch, "rpc2.shop.test", clk)]
    _fabricate(ch, leg, ps)
    with pytest.raises(Unverifiable) as e:
        Corroborator(ps, clock=clk).session().inclusion(H)
    assert code(e) == "sources_not_independent"


def test_an_identical_fabrication_is_broken_by_a_third_honest_operator():
    leg, tx, rc, ch = mined_world()
    c, (a, b, honest) = corroborator(ch, clk, n=3)
    _fabricate(ch, leg, [a, b])                      # two operators tell the same lie
    with pytest.raises(Conflict) as e:
        c.session().inclusion(H)                     # the third one does not know that transaction
    assert code(e) == "sources_disagree_on_transaction"


def test_colluding_fabrication_of_balances_is_broken_by_a_third_operator():
    leg, tx, rc, ch = mined_world()
    c, (a, b, honest) = corroborator(ch, clk, n=3)
    a.tok_shift = b.tok_shift = 5 * E18              # a and b agree on a false balance
    s = c.session()
    with pytest.raises(Conflict) as e:
        s.token_balance(TOKEN, WALLET, 1000)
    assert code(e) == "sources_disagree_on_balance"


def test_two_colluding_operators_are_beyond_what_code_can_detect_and_the_docs_say_so():
    """The honest limit, pinned: with exactly two operators that tell the SAME lie, nothing the code
    can read distinguishes it from the truth. What stops this in practice is operator
    independence (a deployment attestation), which docs/TRADING_RPC_CORROBORATION.md states."""
    leg, tx, rc, ch = mined_world()
    c, (a, b) = corroborator(ch, clk)
    ch.drop(H)
    fake_tx, fake_rc = ReceiptChain(ch.head_n).mine(leg, H, 1000, logs=[])
    for p in (a, b):
        p.tx_override[H], p.rc_override[H] = fake_tx, fake_rc
    assert c.session().inclusion(H).status == "mined"
    import pathlib
    doc = (pathlib.Path(__file__).parent.parent / "docs" / "TRADING_RPC_CORROBORATION.md").read_text()
    assert "colluding" in doc and "cannot be detected" in doc


# ====================================================================== fills: two SEPARATE properties

def test_balance_evidence_is_read_at_the_same_block_from_every_source():
    leg, tx, rc, ch = mined_world()
    s = session_of(ch)
    assert s.token_balance(TOKEN, WALLET, 999) == 0 and s.token_balance(TOKEN, WALLET, 1000) == T
    reads = {(r["read"], r["block"]) for r in s.provenance()["reads"]}
    assert ("token_balance", 999) in reads and ("token_balance", 1000) in reads


def test_a_balance_above_the_anchor_is_refused():
    leg, tx, rc, ch = mined_world(head=1005)
    ch.tok[(TOKEN, WALLET, 1500)] = 1
    with pytest.raises(Unverifiable) as e:
        session_of(ch).token_balance(TOKEN, WALLET, 1500)
    assert code(e) == "block_beyond_anchor"


def test_methods_agree_but_sources_disagree_is_never_a_fill():
    """One provider returns perfectly consistent but different balances: method agreement holds on
    each provider alone, source agreement does not."""
    leg, tx, rc, ch = buy_world()
    c, (a, b) = corroborator(ch, clk)
    b.tok_shift = 7                                  # B is internally coherent only after the shift
    with pytest.raises(F.EvidenceUnavailable, match="sources_disagree_on_balance"):
        F.compute_fill(leg, tx, rc, H, c.session(), strict=True)       # retry: never a verdict
    f = F.compute_fill(leg, tx, rc, H, c.session(), strict=False)      # after bounded retries
    assert f.verdict == "unknown" and "sources_disagree_on_balance" in f.reason
    assert f.method_agreement is False and f.token_wei is None


def test_sources_agree_but_methods_disagree_is_unknown_with_no_method_agreement():
    leg, tx, rc, ch = buy_world(balance_gain=T - 1)  # BOTH providers report the same, off-by-one
    f = F.compute_fill(leg, tx, rc, H, session_of(ch))
    assert f.verdict == "unknown" and f.method_agreement is False and f.token_wei is None


def test_both_properties_together_give_a_verified_buy_and_sell():
    leg, tx, rc, ch = buy_world()
    f = F.compute_fill(leg, tx, rc, H, session_of(ch))
    assert (f.verdict, f.method_agreement, f.token_wei, f.native_wei) == ("verified", True, T, AMT)
    leg, tx, rc, ch = sell_world()
    f = F.compute_fill(leg, tx, rc, H, session_of(ch))
    assert f.verdict == "verified" and f.method_agreement is True


def test_a_node_without_historical_state_fails_closed_with_a_code():
    leg, tx, rc, ch = buy_world()
    c, (a, b) = corroborator(ch, clk)
    b.no_history = True
    with pytest.raises(F.EvidenceUnavailable, match="historical_state_unavailable"):
        F.compute_fill(leg, tx, rc, H, c.session(), strict=True)
    f = F.compute_fill(leg, tx, rc, H, c.session(), strict=False)
    assert f.verdict == "unknown" and "historical_state_unavailable" in f.reason
    assert f.token_wei is None and f.method_agreement is False


def test_a_provider_that_ignores_the_block_parameter_cannot_make_a_fill():
    """A node answering `latest` for every historical query: logs say +3e18, 'before' == 'after'."""
    leg, tx, rc, ch = buy_world()
    c, (a, b) = corroborator(ch, clk)
    for p in (a, b):
        p.w_token_balance = p.token_balance
        p.token_balance = lambda token, address, block, _p=p: _p.w.token_balance(token, address, 1000)
    f = F.compute_fill(leg, tx, rc, H, c.session())
    assert f.verdict == "unknown" and f.method_agreement is False


def test_the_fill_code_refuses_an_uncorroborated_source_object_outright():
    leg, tx, rc, ch = buy_world()
    assert F.compute_fill(leg, tx, rc, H, ch).verdict == "unknown"


def test_the_call_budget_is_enforced_and_is_not_mistaken_for_a_provider_outage():
    leg, tx, rc, ch = mined_world()
    c, _ = corroborator(ch, clk)
    s = c.session(calls=5)
    with pytest.raises(BudgetExhausted):
        s.inclusion(H)                               # pin (4) + the first tx read, then out
    assert s.spent <= 5
    s2 = c.session(calls=1)
    with pytest.raises(BudgetExhausted):             # not "source_unavailable"
        s2.pin()


def test_the_wall_time_budget_is_enforced():
    leg, tx, rc, ch = mined_world()
    t = [0.0]
    c = Corroborator(providers(ch, clk, 2), clock=clk, mono=lambda: t[0])
    s = c.session(seconds=1.0)
    s.pin()
    t[0] = 5.0
    with pytest.raises(BudgetExhausted):
        s.inclusion(H)


def test_a_session_never_exceeds_its_call_budget_whatever_it_is_asked():
    leg, tx, rc, ch = mined_world()
    c, ps = corroborator(ch, clk)
    s = c.session(calls=40)
    for _ in range(100):
        try:
            s.inclusion(H)
            s.token_balance(TOKEN, WALLET, 1000)
        except BudgetExhausted:
            break
    assert s.spent <= 40 and sum(len(p.calls) for p in ps) <= 40


def test_providers_are_only_ever_asked_to_read():
    leg, tx, rc, ch = buy_world()
    c, ps = corroborator(ch, clk)
    s = c.session()
    s.inclusion(H)
    F.compute_fill(leg, tx, rc, H, s)
    allowed = {"head", "block_header", "transaction", "receipt", "block_transactions", "tx_count",
               "native_balance", "token_balance"}
    assert {x for p in ps for x in p.calls} <= allowed


def test_the_provenance_names_operators_anchor_finality_and_the_reads():
    leg, tx, rc, ch = mined_world()
    c, _ = corroborator(ch, clk, finality=7)
    s = c.session()
    s.inclusion(H)
    p = s.provenance()
    assert p["chain_id"] == 99901 and p["finality"] == 7 and p["source_agreement"] is True
    assert [x["operator"] for x in p["sources"]] == ["alpha.test", "beta.test"]
    assert p["anchor"]["number"] == 1005 and p["anchor"]["hash"] == block_hash(1005)
    assert p["problems"] == [] and p["failed"] == [] and p["calls"] > 0
    assert any(r["read"] == "inclusion" for r in p["reads"])


# ====================================================================== finality (F-04)

def test_depth_alone_is_not_final_when_the_finalized_tag_is_behind_the_block():
    leg, tx, rc, ch = mined_world(head=1100)                 # 101 confirmations, ~10-20 s
    c, (a, b) = corroborator(ch, clk)
    a.finalized_lag = b.finalized_lag = 150                  # parent chain has not finalized it
    s = c.session()
    inc = s.inclusion(H)
    assert inc.confirmations >= 12
    final, why = s.finality(inc.block_number, inc.confirmations)
    assert final is False and "finalized height" in why


def test_finalized_height_is_the_lowest_over_every_source_and_capped_at_the_anchor():
    ch = ReceiptChain(1100)
    c, (a, b) = corroborator(ch, clk)
    a.finalized_lag, b.finalized_lag = 10, 40
    s = c.session()
    assert s.finalized() == 1060
    assert s.is_final(1060, 41) and not s.is_final(1061, 40)
    assert s.provenance()["finalized"] == {"number": 1060}
    assert s.provenance()["finality_policy"] == "finalized_tag+depth"


def test_one_source_without_the_finalized_tag_means_nothing_is_final():
    leg, tx, rc, ch = mined_world(head=1100)
    c, (a, b) = corroborator(ch, clk)
    b.finalized_lag = None                                   # node does not serve the tag
    s = c.session()
    assert s.finalized() is None
    final, why = s.finality(1000, 101)
    assert final is False and "finalized_tag_unsupported" in why
    assert s.provenance()["finalized"] is None


def test_a_finalized_tag_above_the_sources_own_head_is_malformed_not_final():
    ch = ReceiptChain(1100)
    c, (a, b) = corroborator(ch, clk)
    b.finalized_lag = -5                                     # "finalized" beyond its own head
    s = c.session()
    assert s.finalized() is None and not s.is_final(1000, 101)


def test_depth_is_still_required_even_when_the_tag_says_final():
    ch = ReceiptChain(1100)
    c, _ = corroborator(ch, clk)
    s = c.session()
    assert s.finalized() == 1100
    assert not s.is_final(1095, 6) and s.is_final(1089, 12)


def test_the_depth_only_policy_is_an_explicit_choice():
    leg, tx, rc, ch = mined_world(head=1100)
    c, (a, b) = corroborator(ch, clk, require_finalized_tag=False)
    a.finalized_lag = b.finalized_lag = None
    s = c.session()
    assert s.is_final(1000, 101) and s.provenance()["finality_policy"] == "depth"
    with pytest.raises(ValueError):
        Corroborator([a, b], require_finalized_tag="yes")
