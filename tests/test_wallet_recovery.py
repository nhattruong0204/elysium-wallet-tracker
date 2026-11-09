# ruff: noqa: F811
"""Owner-audited recovery of stuck wallet legs (#95 F-02), against a real PostgreSQL ledger.

A scriptable multi-provider chain plays the nodes (per-provider head, lag, staleness, outages,
disagreement, hidden transactions). Nothing is signed, broadcast or funded; the point is that a
correction is recorded ONLY with independently corroborated, final evidence plus an owner's
confirmation and reason, that everything else stays blocked, and that nothing else is touched."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from conftest import TEST_URL  # noqa: F401
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_trading_ledger import (  # noqa: F401
    OWNER,
    U1,
    U2,
    Actor,
    LedgerRefused,
    clock,
    ledger,
    live,
    owner_engine,
    raw,
    rw_engine,
)
from test_wallet_flow import SESSION, sell_order
from test_wallet_flow import state as order_state
from test_wallet_reconcile import (  # noqa: F401
    H1,
    H2,
    H3,
    OTHER_TO,
    FakeNode,
    errored,
    leg_of,
    node,
    submitted,
)
from walletkit import WALLET, block_hash

from agora_tracker.migrate import downgrade, upgrade
from agora_tracker.trading.paper import TradeRefused
from agora_tracker.trading.wallet.approvals import Head
from agora_tracker.trading.wallet.corroboration import Corroborator
from agora_tracker.trading.wallet.flow import WalletFlow
from agora_tracker.trading.wallet.reconcile import swap_deadline
from agora_tracker.trading.wallet.recovery import (
    CODE_TTL_S,
    LegRecovery,
    RecoveryRefused,
    render_diagnosis,
    render_stuck,
)

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")

HEAD = 1100
REASON = "verified on explorer and wallet: nothing pending"
OWNER_ACTOR = Actor.owner(OWNER)


# ------------------------------------------------------------------ a scriptable multi-provider chain
class World:
    """The canonical chain. Providers are views of it that can lag, go stale, fail or disagree."""

    def __init__(self, base: int = 5, head: int = HEAD) -> None:
        self.base, self.head = base, head
        self.blocks: dict[int, list[dict]] = {}
        self.txs: dict[str, dict] = {}
        self.status: dict[str, int] = {}
        self.pending_extra = 0

    @staticmethod
    def bh(n: int) -> str:
        return block_hash(n)

    def count_at(self, addr: str, b: int) -> int:
        return self.base + sum(1 for n, txs in self.blocks.items() if n <= b
                               for t in txs if t["from"] == addr)

    def mine(self, tx: dict, block: int, status: int = 1) -> dict:
        t = dict(tx)
        t["blockNumber"], t["blockHash"] = hex(block), self.bh(block)
        self.blocks.setdefault(block, []).append(t)
        self.txs[t["hash"]] = t
        self.status[t["hash"]] = status
        return t

    def mine_other(self, h: str, block: int, *, sender: str = WALLET) -> dict:
        """A DIFFERENT transaction of the wallet (not the reviewed request) at the next nonce."""
        return self.mine({"hash": h, "from": sender, "to": OTHER_TO, "value": "0x0", "input": "0x",
                          "gas": "0x5208", "gasPrice": hex(10**9), "chainId": "0x1863d",
                          "nonce": hex(self.count_at(sender, block - 1))}, block)

    def mine_leg(self, leg: dict, h: str, block: int, *, status: int = 1, **changes) -> dict:
        """The reviewed request itself, exactly as asked."""
        tx = {"hash": h, "from": leg["wallet"], "to": leg["expected_to"],
              "value": hex(int(leg["expected_value_wei"])), "input": leg["expected_data"],
              "gas": hex(int(leg["gas_limit"])), "gasPrice": hex(10**9), "chainId": "0x1863d",
              "nonce": hex(self.count_at(leg["wallet"], block - 1))}
        tx.update(changes)
        return self.mine(tx, block, status)


class Src:
    """One RPC provider's view. `host` is distinct per provider, like ExecutionRpc.host."""

    READS = {"head", "transaction", "receipt", "block_header", "block_transactions", "tx_count",
             "finalized_head"}
    chain_id = 99901
    chain_verified = True

    def __init__(self, world: World, host: str, clock, *, lag: int = 0) -> None:
        from agora_tracker.trading.wallet.rpc import operator_of
        self.w, self.host, self.clock, self.lag = world, host, clock, lag
        self.operator = operator_of(host)
        self.down, self.age = False, 0
        self.override: dict[int, str] = {}
        self.hidden: set[str] = set()
        self.nonce_bias = 0
        self.calls: list[str] = []

    def _go(self, name: str) -> None:
        self.calls.append(name)
        if self.down:
            raise TradeRefused("rpc down")

    @property
    def n(self) -> int:
        return self.w.head - self.lag

    def _bh(self, n: int) -> str:
        return self.override.get(n, self.w.bh(n))

    def head(self) -> Head:
        self._go("head")
        return Head(self.n, self._bh(self.n), int(self.clock()) - 2 - self.age)

    def finalized_head(self):
        self._go("finalized_head")
        if getattr(self.w, "no_finalized_tag", False):
            return None
        n = max(0, self.n - getattr(self.w, "finalized_lag", 0))
        return Head(n, self._bh(n), int(self.clock()) - 2 - self.age)

    def block_header(self, n: int):
        self._go("block_header")
        return None if n > self.n else {"hash": self._bh(n), "number": hex(n)}

    def block_transactions(self, n: int):
        self._go("block_transactions")
        return [dict(t, blockHash=self._bh(n)) for t in self.w.blocks.get(n, [])
                if t["hash"] not in self.hidden]

    def transaction(self, h: str):
        self._go("transaction")
        t = self.w.txs.get(h)
        if t is None or h in self.hidden:
            return None
        t = dict(t)
        if t.get("blockNumber") and int(t["blockNumber"], 16) > self.n:
            t["blockNumber"] = None
        return t

    def receipt(self, h: str):
        self._go("receipt")
        t = self.w.txs.get(h)
        if t is None or h in self.hidden or not t.get("blockNumber"):
            return None
        b = int(t["blockNumber"], 16)
        if b > self.n:
            return None
        t = self.w.txs[h]
        return {"transactionHash": h, "from": t["from"], "to": t["to"], "blockNumber": hex(b),
                "blockHash": self._bh(b), "status": hex(self.w.status[h]), "gasUsed": "0x5208",
                "effectiveGasPrice": hex(10**9), "contractAddress": None, "logs": []}

    def tx_count(self, address: str, tag: str) -> int:
        self._go("tx_count")
        if tag == "latest":
            return self.w.count_at(address, self.n) + self.nonce_bias
        if tag == "pending":
            return self.w.count_at(address, self.n) + self.w.pending_extra + self.nonce_bias
        return self.w.count_at(address, int(tag, 16)) + self.nonce_bias


@pytest.fixture
def world():
    return World()


@pytest.fixture
def srcs(world, clock):
    return Src(world, "rpc-a.example", clock), Src(world, "rpc-b.example", clock)


def recovery(live, sources, clock, **kw):
    """The F-02 owner tool over the SHARED corroboration layer (F-15)."""
    return LegRecovery(live, Corroborator(sources, finality=12, clock=clock), clock=clock, **kw)


@pytest.fixture
def svc(live, srcs, clock):
    return recovery(live, list(srcs), clock)


# ------------------------------------------------------------------ stuck-leg builders
def stuck_swap(live, node, *, key="buy-order-1"):
    """A swap leg that reconciliation could not settle: wallet answered with an error, so there is
    no hash and no nonce, only the hint recorded at the prompt (5) and the prompt block (1000)."""
    oid, plan, leg, flow = errored(live, node, key)
    assert leg["state"] == "ambiguous" and order_state(live, oid) == "ambiguous"
    return oid, leg_of(live, oid), flow


def stuck_hashed(live, node, *, nonce=5):
    """A swap whose wallet reported H1 and whose nonce is known, but which stays ambiguous."""
    oid, plan, leg, flow = submitted(live, node)
    assert live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="ambiguous",
                            resolution="test_unresolved", nonce=nonce,
                            order_path=("ambiguous",)) is True
    return oid, leg_of(live, oid), flow


def stuck_approval(live, node):
    oid, plan = sell_order(live)
    flow = WalletFlow(live, __import__("test_wallet_flow").mkflow(live).planner, clock=live.clock,
                      nonce_reader=node)
    rid = flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)["request"]["id"]
    flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="error", reason="timeout")
    leg = leg_of(live, oid)
    assert leg["kind"] != "swap" and leg["state"] == "ambiguous"
    return oid, leg


def counts(engine):
    return {
        "events": raw(engine, "SELECT count(*) FROM trading_private.order_events")[0][0],
        "legs": raw(engine, "SELECT count(*) FROM trading_private.order_legs")[0][0],
        "orders": raw(engine, "SELECT count(*) FROM trading_private.orders")[0][0],
        "recoveries": raw(engine, "SELECT count(*) FROM trading_private.leg_recoveries")[0][0],
        "requests": raw(engine, "SELECT count(*) FROM trading_private.order_events WHERE kind IN "
                                "('WALLET_REQUEST')")[0][0],
        "attempts": raw(engine, "SELECT count(*) FROM trading_private.execution_attempts")[0][0],
        "fills": raw(engine, "SELECT count(*) FROM trading_private.order_fills")[0][0],
    }


def locked_legs(engine):
    return raw(engine, "SELECT count(*) FROM trading_private.order_legs "
                       "WHERE state IN ('prompted','hash_reported','ambiguous')")[0][0]


def provable_superseded(live, node, world):
    """Hashed leg (nonce 5) whose nonce was consumed by a DIFFERENT transaction, 51 blocks deep."""
    oid, leg, _ = stuck_hashed(live, node)
    world.mine_other(H3, 1050)
    return oid, leg


def provable_abandoned(live, node, clock):
    oid, leg, _ = stuck_swap(live, node)
    clock.t = swap_deadline(leg) + 700
    return oid, leg


# ------------------------------------------------------------------ 1. read-only diagnosis
def test_diagnosis_shows_the_required_facts_and_writes_nothing(live, node, svc, owner_engine):
    oid, leg, _ = stuck_swap(live, node)
    before = counts(owner_engine)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert counts(owner_engine) == before                       # strictly read-only
    assert (d.leg_id, d.leg_state, d.order_state, d.kind) == (leg["id"], "ambiguous", "ambiguous", "swap")
    assert d.wallet == WALLET and d.chain_id == 99901 and d.tx_hash is None
    assert d.nonce is None and d.nonce_hint == 5 and d.prompt_block == 1000
    assert d.last_evidence == leg["resolution"] and d.corroborated is True
    assert d.category == "unknown" and d.reason == "no_transaction_evidence"
    assert d.proposal is None and d.decision_key is None and d.blocked_reason
    assert d.escalation and "STAYS BLOCKED" in d.escalation[0]
    ev = d.evidence
    assert ev["schema"] == 1 and ev["anchor"]["number"] == HEAD
    assert {s["name"] for s in ev["sources"]} == {"rpc-a.example", "rpc-b.example"}
    assert d.evidence_digest == hashlib.sha256(json.dumps(
        ev, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    text = render_diagnosis(d)
    assert str(leg["id"]) in text and "STAYS BLOCKED" in text and WALLET in text
    assert "/wallet_resolve" not in text


def test_stuck_listing_is_owner_only_and_read_only(live, node, svc, owner_engine):
    stuck_swap(live, node)
    before = counts(owner_engine)
    rows = svc.stuck(OWNER_ACTOR)
    assert [r["state"] for r in rows] == ["ambiguous"] and counts(owner_engine) == before
    assert "ambiguous" in render_stuck(rows) and render_stuck([]) == "No wallet legs are stuck."


def test_prepare_on_a_blocked_leg_issues_no_code(live, node, svc):
    _, leg, _ = stuck_swap(live, node)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    assert p.code is None and p.expires_at is None and p.diagnosis.proposal is None


# ------------------------------------------------------------------ 2. the categories
def test_confirmed_on_the_canonical_chain_is_left_to_the_reconciler(live, node, svc, world):
    _, leg, _ = stuck_hashed(live, node)
    world.mine_leg(leg, H1, 1050)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.category, d.auto, d.proposal) == ("confirmed", True, None)
    assert d.evidence["facts"]["canonical_in_all_sources"] is True
    assert svc.prepare(OWNER_ACTOR, leg["id"]).code is None
    assert "reconciliation workers" in render_diagnosis(d)


def test_reverted_on_the_canonical_chain_is_left_to_the_reconciler(live, node, svc, world):
    _, leg, _ = stuck_hashed(live, node)
    world.mine_leg(leg, H1, 1050, status=0)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.category, d.auto, d.proposal) == ("reverted", True, None)


def test_replaced_by_an_identical_request_is_not_a_correction(live, node, svc, world):
    _, leg, _ = stuck_hashed(live, node)
    world.mine_leg(leg, H2, 1050)                               # speed-up: same payload, new hash
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.category, d.reason, d.auto, d.proposal) == (
        "replaced", "replaced_by_identical_request", True, None)


def test_replaced_by_a_different_final_transaction_is_provable(live, node, svc, world):
    oid, leg = provable_superseded(live, node, world)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.category, d.proposal, d.auto) == ("replaced", "superseded", False)
    ct = d.evidence["facts"]["consuming_tx"]
    assert ct["hash"] == H3 and ct["confirmations"] == HEAD - 1050 + 1 and ct["mismatches"]
    assert d.decision_key and d.corroborated


def test_a_replacement_that_is_not_final_yet_stays_blocked(live, node, svc, world):
    _, leg, _ = stuck_hashed(live, node)
    world.mine_other(H3, 1095)                                  # only 6 deep (finality 12)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.reason, d.proposal) == ("replacement_not_final_yet", None) and d.blocked_reason


def test_a_free_nonce_means_the_transaction_could_still_be_mined(live, node, svc, world):
    _, leg, _ = stuck_hashed(live, node)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.category, d.reason, d.proposal) == ("unknown", "nonce_not_consumed", None)


def test_reported_hash_that_is_a_different_final_transaction_is_superseded(live, node, svc, world):
    _, leg, _ = stuck_hashed(live, node)
    world.mine_leg(leg, H1, 1050, to=OTHER_TO)                  # wallet sent something else under H1
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.category, d.proposal) == ("nonce_consumed_externally", "superseded")
    assert "to" in d.evidence["facts"]["consuming_tx"]["mismatches"]


def test_reported_hash_with_a_different_payload_not_final_yet_stays_blocked(live, node, svc, world):
    _, leg, _ = stuck_hashed(live, node)
    world.mine_leg(leg, H1, 1095, to=OTHER_TO)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.reason, d.proposal) == ("different_transaction_not_final_yet", None)


def test_a_pending_transaction_is_never_resolved(live, node, svc, world):
    _, leg, _ = stuck_hashed(live, node)
    world.txs[H1] = {"hash": H1, "from": WALLET, "to": leg["expected_to"], "value": "0x0",
                     "input": leg["expected_data"], "nonce": "0x5", "blockNumber": None,
                     "blockHash": None}
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.reason, d.proposal) == ("transaction_pending", None) and d.blocked_reason


def test_external_nonce_consumption_before_the_deadline_stays_blocked(live, node, svc, world):
    _, leg, _ = stuck_swap(live, node)
    world.mine_other(H3, 1050)                                  # the hinted nonce went elsewhere
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.category == "nonce_consumed_externally" and d.proposal is None
    assert "swap deadline has not passed" in d.detail


def test_abandoned_is_provable_only_after_the_deadline_and_margin(live, node, svc, clock):
    _, leg, _ = stuck_swap(live, node)
    dl = swap_deadline(leg)
    clock.t = dl + 100                                          # past the deadline, inside the margin
    assert svc.diagnose(OWNER_ACTOR, leg["id"]).proposal is None
    clock.t = dl + 700
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.category, d.proposal) == ("expired", "abandoned")
    f = d.evidence["facts"]
    assert f["swap_deadline"] == dl and f["anchor_timestamp"] > dl + f["deadline_margin_s"]


def test_a_wallet_with_queued_transactions_is_never_abandoned(live, node, svc, world, clock):
    _, leg, _ = stuck_swap(live, node)
    clock.t = swap_deadline(leg) + 700
    world.pending_extra = 1
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.reason, d.proposal) == ("wallet_has_pending_transactions", None)


def test_an_identical_request_found_after_the_deadline_is_confirmed_not_abandoned(
        live, node, svc, world, clock):
    _, leg, _ = stuck_swap(live, node)
    clock.t = swap_deadline(leg) + 700
    world.mine_leg(leg, H2, 1060)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert (d.category, d.auto, d.proposal) == ("confirmed", True, None)


def test_outcome_genuinely_unknown_is_blocked_with_an_escalation_procedure(live, node, svc):
    _, leg, _ = stuck_swap(live, node)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and len(d.escalation) >= 4
    joined = " ".join(d.escalation)
    assert "lock is not released" in joined and "explorer" in joined
    assert "no command for it" in joined                         # no auto/ad-hoc unlock exists


def test_approvals_never_expire_but_can_be_superseded_by_a_consumed_nonce(live, node, svc, world, clock):
    oid, leg = stuck_approval(live, node)
    clock.t += 10 * 86400                                       # any amount of time
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and "no on-chain deadline" in d.detail
    assert any("ANOTHER wallet" in s for s in d.escalation)
    # the same leg is provable only if its (hinted) nonce was consumed by something final: it is
    # not enough for an approval (no nonce of its own is known), so it stays blocked.
    world.mine_other(H3, 1050)
    assert svc.diagnose(OWNER_ACTOR, leg["id"]).proposal is None


def test_approval_with_a_known_consumed_nonce_is_superseded_and_the_order_is_untouched(
        live, node, svc, world, owner_engine):
    oid, plan = sell_order(live)
    flow = WalletFlow(live, __import__("test_wallet_flow").mkflow(live).planner, clock=live.clock,
                      nonce_reader=node)
    rid = flow.approval_prompt(user_id=U1, session=SESSION, order_id=oid, plan=plan)["request"]["id"]
    flow.report(user_id=U1, session=SESSION, request_id=rid, outcome="hash", tx_hash=H1)
    leg = leg_of(live, oid)
    assert live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="ambiguous",
                            resolution="test_unresolved", nonce=5) is True
    world.mine_other(H3, 1050)                                  # its nonce went to another tx, final
    order_before = order_state(live, oid)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.kind != "swap" and d.proposal == "superseded"
    p, res = apply(svc, leg["id"])
    assert res.leg_state == "superseded" and res.order_state == order_before
    assert order_state(live, oid) == order_before               # an approval never cancels the order
    assert locked_legs(owner_engine) == 0


# ------------------------------------------------------------------ 3. adversarial: sources
def test_stale_provider_head_blocks_every_correction(live, node, svc, srcs, world):
    oid, leg = provable_superseded(live, node, world)
    srcs[1].age = 3600                                          # one provider serves an old head
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and "stale_head" in d.evidence["problems"] and not d.corroborated
    assert svc.prepare(OWNER_ACTOR, leg["id"]).code is None


def test_a_provider_lagging_beyond_the_skew_blocks_but_within_it_anchors_on_the_lowest(
        live, node, svc, srcs, world):
    oid, leg = provable_superseded(live, node, world)
    srcs[1].lag = 65                                 # beyond MAX_HEAD_SKEW (64 blocks)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and "heads_diverge" in d.evidence["problems"]
    srcs[1].lag = 2
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal == "superseded" and d.evidence["anchor"]["number"] == HEAD - 2


def test_providers_on_different_forks_at_the_anchor_block_do_not_corroborate(
        live, node, svc, srcs, world):
    oid, leg = provable_superseded(live, node, world)
    srcs[1].override[HEAD] = block_hash(HEAD, 7)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and "sources_disagree_on_chain" in d.evidence["problems"]


def test_one_provider_down_means_no_correction_even_if_the_other_has_proof(
        live, node, svc, srcs, world):
    oid, leg = provable_superseded(live, node, world)
    srcs[1].down = True
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and "source_unavailable" in d.evidence["problems"]
    assert not d.corroborated and [f["error"] for f in d.evidence["failed"]] == ["source_unavailable"]


def test_all_providers_down_is_reported_as_rpc_unavailable(live, node, svc, srcs):
    _, leg, _ = stuck_swap(live, node)
    for s in srcs:
        s.down = True
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.category == "rpc_unavailable" and d.proposal is None and not d.corroborated


def test_a_provider_dying_mid_diagnosis_is_unavailable_not_a_guess(live, node, svc, srcs, world):
    oid, leg = provable_superseded(live, node, world)
    orig = srcs[1].block_transactions

    def boom(n):
        raise TradeRefused("rpc down")
    srcs[1].block_transactions = boom
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.category == "rpc_unavailable" and d.proposal is None
    srcs[1].block_transactions = orig
    assert svc.diagnose(OWNER_ACTOR, leg["id"]).proposal == "superseded"


def test_provider_disagreement_on_the_wallet_nonce_blocks(live, node, svc, srcs, world):
    oid, leg = provable_superseded(live, node, world)
    srcs[1].nonce_bias = 1
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and d.category == "unknown"
    assert d.reason in ("sources_disagree_on_wallet_nonce", "sources_disagree_on_consuming_transaction",
                        "nonce_consumed_before_the_request", "nonce_not_consumed")


def test_a_transaction_known_to_one_provider_only_never_supports_a_correction(
        live, node, svc, srcs, world):
    _, leg, _ = stuck_hashed(live, node)
    world.mine_leg(leg, H1, 1050, to=OTHER_TO)
    srcs[1].hidden.add(H1)                                      # B has not indexed it / hides it
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and d.reason == "sources_disagree_on_transaction"


def test_a_receipt_that_differs_between_providers_blocks(live, node, svc, srcs, world):
    _, leg, _ = stuck_hashed(live, node)
    world.mine_leg(leg, H1, 1050, to=OTHER_TO)
    srcs[1].override[1050] = block_hash(1050, 3)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None


def test_a_single_source_can_diagnose_but_never_resolve(live, node, srcs, world, clock):
    oid, leg = provable_superseded(live, node, world)
    one = recovery(live, [srcs[0]], clock)
    d = one.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and "needs_two_independent_sources" in d.evidence["problems"]
    assert not d.corroborated and one.prepare(OWNER_ACTOR, leg["id"]).code is None


def test_two_names_for_one_provider_are_not_two_sources(live, node, srcs, world, clock):
    oid, leg = provable_superseded(live, node, world)
    twin = Src(world, srcs[0].host, clock)
    d = recovery(live, [srcs[0], twin], clock).diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and "sources_not_independent" in d.evidence["problems"]


def test_diagnosis_only_uses_read_methods(live, node, svc, srcs, world):
    oid, leg = provable_superseded(live, node, world)
    svc.prepare(OWNER_ACTOR, leg["id"])
    assert {c for s in srcs for c in s.calls} <= Src.READS


# ------------------------------------------------------------------ 4. owner confirmation + audit
def apply(svc, leg_id, reason=REASON):
    p = svc.prepare(OWNER_ACTOR, leg_id)
    assert p.code, p.diagnosis.blocked_reason
    return p, svc.confirm(OWNER_ACTOR, leg_id, p.code, reason)


def test_superseded_end_to_end_is_audited_append_only_and_releases_only_on_proof(
        live, node, svc, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    assert locked_legs(owner_engine) == 1                       # the wallet lock is held
    before, leg_before = counts(owner_engine), leg_of(live, oid)
    p, res = apply(svc, leg["id"])
    assert (res.applied, res.leg_state, res.order_state, res.action) == (
        True, "superseded", "cancelled", "superseded")
    after = leg_of(live, oid)
    changed = {k for k in after if after[k] != leg_before[k]}
    assert changed <= {"state", "version", "resolution", "resolved_at"}      # no hash/nonce/receipt
    assert after["resolution"] == "owner_recovery:superseded"
    assert after["tx_hash"] == leg_before["tx_hash"] and after["nonce"] == leg_before["nonce"]
    assert locked_legs(owner_engine) == 0                       # released, because it was PROVEN
    c = counts(owner_engine)
    assert c["recoveries"] == 1 and c["legs"] == before["legs"] and c["orders"] == before["orders"]
    assert (c["requests"], c["attempts"], c["fills"]) == (
        before["requests"], before["attempts"], before["fills"])               # nothing was sent
    (rec,) = live.list_recoveries(OWNER_ACTOR, leg_id=leg["id"])
    assert rec["owner_id"] == OWNER and rec["reason"] == REASON and rec["action"] == "superseded"
    assert rec["from_state"] == "ambiguous" and rec["to_state"] == "superseded"
    assert rec["evidence"]["facts"]["consuming_tx"]["hash"] == H3
    assert rec["evidence_digest"] == res.evidence_digest == live.evidence_digest(rec["evidence"])
    ev = raw(owner_engine, "SELECT kind, actor_kind, actor_id, detail FROM trading_private.order_events "
                           "WHERE order_id = :o AND kind IN ('LEG_RECOVERY') ", o=oid)
    assert len(ev) == 1 and ev[0][1] == "owner" and ev[0][2] == OWNER
    assert ev[0][3]["evidence_digest"] == res.evidence_digest and ev[0][3]["reason"] == REASON
    cancelled = raw(owner_engine, "SELECT actor_kind, detail FROM trading_private.order_events "
                                  "WHERE order_id = :o AND kind = 'TRANSITION' ORDER BY chain_no DESC "
                                  "LIMIT 1", o=oid)
    assert cancelled[0][0] == "owner" and REASON in json.dumps(cancelled[0][1])


def test_abandoned_end_to_end_after_the_deadline(live, node, svc, clock, owner_engine):
    oid, leg = provable_abandoned(live, node, clock)
    p, res = apply(svc, leg["id"])
    assert (res.leg_state, res.order_state, res.action) == ("abandoned", "cancelled", "abandoned")
    assert locked_legs(owner_engine) == 0
    (rec,) = live.list_recoveries(OWNER_ACTOR)
    assert rec["evidence"]["facts"]["swap_deadline"] == swap_deadline(leg)


def test_hash_chain_of_the_audit_log_stays_intact(live, node, svc, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    apply(svc, leg["id"])
    rows = raw(owner_engine, "SELECT chain_no, prev_hash, hash FROM trading_private.order_events "
                             "WHERE order_id = :o ORDER BY chain_no", o=oid)
    assert all(rows[i][1] == rows[i - 1][2] for i in range(1, len(rows)))


def test_confirm_needs_a_reason_and_a_bad_reason_does_not_burn_the_code(live, node, svc, world):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    for bad in ("", "short", " " * 20, "x" * 191, "line\nbreak is not printable", None):
        with pytest.raises(RecoveryRefused) as e:
            svc.confirm(OWNER_ACTOR, leg["id"], p.code, bad)
        assert e.value.code == "reason_required"
    assert leg_of(live, oid)["state"] == "ambiguous"
    assert svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON).applied is True


def test_the_code_is_single_use(live, node, svc, world, clock):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    with pytest.raises(RecoveryRefused) as e:
        svc.confirm(OWNER_ACTOR, leg["id"], "0" * 10, REASON)
    assert e.value.code == "bad_code"
    with pytest.raises(LedgerRefused) as e2:                    # a code is for one leg only
        svc.confirm(OWNER_ACTOR, leg["id"] + 1000, p.code, REASON)
    assert e2.value.code == "not_found"
    assert svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON).applied is True
    with pytest.raises(RecoveryRefused) as e:                   # replay of the used code
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code == "bad_code"
    assert len(live.list_recoveries(OWNER_ACTOR)) == 1


def test_a_code_is_bound_to_its_owner_and_its_leg():
    from agora_tracker.trading.wallet.recovery import _Codes
    now = [1000.0]
    codes = _Codes(lambda: now[0])
    code, _ = codes.issue(1, 10, "dk")
    with pytest.raises(RecoveryRefused):
        codes.take(2, 10, code)                                 # another owner
    code, _ = codes.issue(1, 10, "dk")
    with pytest.raises(RecoveryRefused):
        codes.take(1, 11, code)                                 # another leg
    code, _ = codes.issue(1, 10, "dk")
    assert codes.take(1, 10, code).decision_key == "dk"


def test_a_code_expires(live, node, svc, world, clock):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    clock.t += CODE_TTL_S + 1
    with pytest.raises(RecoveryRefused) as e:
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code == "bad_code" and leg_of(live, oid)["state"] == "ambiguous"


def test_a_newer_prepare_voids_the_older_code(live, node, svc, world):
    oid, leg = provable_superseded(live, node, world)
    first = svc.prepare(OWNER_ACTOR, leg["id"])
    second = svc.prepare(OWNER_ACTOR, leg["id"])
    assert first.code != second.code
    with pytest.raises(RecoveryRefused):
        svc.confirm(OWNER_ACTOR, leg["id"], first.code, REASON)
    assert svc.confirm(OWNER_ACTOR, leg["id"], second.code, REASON).applied is True


def test_guessing_codes_is_rate_limited(live, node, svc, world):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    codes = []
    for i in range(6):
        with pytest.raises(RecoveryRefused) as e:
            svc.confirm(OWNER_ACTOR, leg["id"], f"{i:010x}", REASON)
        codes.append(e.value.code)
    assert codes[:5] == ["bad_code"] * 5 and codes[5] == "too_many_attempts"
    with pytest.raises(RecoveryRefused) as e:                   # even the right code is refused now
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code == "too_many_attempts" and leg_of(live, oid)["state"] == "ambiguous"


def test_refusals_are_audited_without_changing_the_leg(live, node, svc, srcs, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    srcs[1].down = True
    with pytest.raises(RecoveryRefused):
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    with pytest.raises(RecoveryRefused):                        # the code was consumed by the attempt
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    ref = raw(owner_engine, "SELECT actor_kind, actor_id, detail FROM trading_private.order_events "
                            "WHERE order_id = :o AND kind = 'LEG_RECOVERY_REFUSED' ORDER BY chain_no", o=oid)
    assert len(ref) == 2 and all(r[0] == "owner" and r[1] == OWNER for r in ref)
    assert {r[2]["code"] for r in ref} == {"source_unavailable", "bad_code"}
    assert leg_of(live, oid)["state"] == "ambiguous" and counts(owner_engine)["recoveries"] == 0


# ------------------------------------------------------------------ 5. adversarial: changing facts
def test_delayed_inclusion_between_prepare_and_confirm_refuses(live, node, svc, world, clock, owner_engine):
    oid, leg = provable_abandoned(live, node, clock)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    assert p.diagnosis.proposal == "abandoned"
    world.mine_leg(leg, H2, 1090, status=0)                     # the "lost" request lands late
    with pytest.raises(RecoveryRefused) as e:
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code in ("identical_request_found_on_chain", "evidence_changed", "no_safe_resolution")
    assert leg_of(live, oid)["state"] == "ambiguous" and order_state(live, oid) == "ambiguous"
    assert counts(owner_engine)["recoveries"] == 0
    ref = raw(owner_engine, "SELECT count(*) FROM trading_private.order_events WHERE order_id = :o "
                            "AND kind = 'LEG_RECOVERY_REFUSED'", o=oid)
    assert ref[0][0] == 1                                       # the refusal itself is on the record


def test_a_transaction_queued_after_prepare_blocks_the_confirmation(live, node, svc, world, clock):
    oid, leg = provable_abandoned(live, node, clock)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    world.pending_extra = 1
    with pytest.raises(RecoveryRefused) as e:
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code == "wallet_has_pending_transactions"
    assert leg_of(live, oid)["state"] == "ambiguous"


def test_an_external_nonce_change_after_prepare_invalidates_the_decision(live, node, svc, world, clock):
    oid, leg = provable_abandoned(live, node, clock)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    world.mine_other(H3, 1090)                                  # an unrelated tx moved the nonce
    with pytest.raises(RecoveryRefused) as e:
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code == "evidence_changed"
    assert leg_of(live, oid)["state"] == "ambiguous"
    p2 = svc.prepare(OWNER_ACTOR, leg["id"])                    # a fresh, human-reviewed decision
    assert p2.diagnosis.proposal == "abandoned"
    assert p2.diagnosis.decision_key != p.diagnosis.decision_key
    assert svc.confirm(OWNER_ACTOR, leg["id"], p2.code, REASON).applied is True


def test_a_source_going_stale_between_prepare_and_confirm_refuses(live, node, svc, srcs, world):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    srcs[0].age = 3600
    with pytest.raises(RecoveryRefused) as e:
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code == "stale_head" and leg_of(live, oid)["state"] == "ambiguous"


def test_a_reorg_after_prepare_refuses(live, node, svc, srcs, world):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    for s in srcs:
        s.override[1050] = block_hash(1050, 9)                  # the consuming block was replaced
    with pytest.raises(RecoveryRefused):
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert leg_of(live, oid)["state"] == "ambiguous"


def test_the_leg_changing_after_prepare_is_refused(live, node, svc, world):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    assert live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="hash_reported",
                            resolution="late wallet answer", seen=True) is True
    with pytest.raises(RecoveryRefused) as e:
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code in ("leg_not_stuck:hash_reported", "not_recoverable", "stale_version",
                            "evidence_changed")
    assert leg_of(live, oid)["state"] == "hash_reported"


# ------------------------------------------------------------------ 6. duplicates, races, restarts
def test_concurrent_confirmations_of_one_code_apply_exactly_once(live, node, srcs, world, clock, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    svc = recovery(live, list(srcs), clock)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    gate = threading.Barrier(4)

    def go(_):
        gate.wait()
        try:
            return svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON).applied
        except (RecoveryRefused, LedgerRefused) as exc:
            return exc.code

    with ThreadPoolExecutor(4) as ex:
        out = list(ex.map(go, range(4)))
    # EXACTLY once. (CI once saw the winner refused `busy`: the losers' refusal audits took the
    # audit-chain lock before the order row, inverting the ledger's lock order -> deadlock. Fixed
    # in record_recovery_refusal; test_refusal_audit_and_recovery_never_deadlock pins it.)
    assert sorted(map(str, out)) == ["True", "bad_code", "bad_code", "bad_code"]
    assert counts(owner_engine)["recoveries"] == 1
    assert leg_of(live, oid)["state"] == "superseded"
    assert raw(owner_engine, "SELECT count(*) FROM trading_private.order_events WHERE order_id = :o "
                             "AND kind = 'LEG_RECOVERY'", o=oid)[0][0] == 1


def test_concurrent_distinct_services_cannot_double_apply(live, node, srcs, world, clock, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    a, b = recovery(live, list(srcs), clock), recovery(live, list(srcs), clock)
    pa, pb = a.prepare(OWNER_ACTOR, leg["id"]), b.prepare(OWNER_ACTOR, leg["id"])
    gate = threading.Barrier(2)

    def go(args):
        s, p = args
        gate.wait()
        try:
            return s.confirm(OWNER_ACTOR, leg["id"], p.code, REASON).applied
        except (RecoveryRefused, LedgerRefused) as exc:
            return exc.code

    with ThreadPoolExecutor(2) as ex:
        out = list(ex.map(go, [(a, pa), (b, pb)]))
    assert out.count(True) == 1 and counts(owner_engine)["recoveries"] == 1


def test_ledger_level_replay_of_a_request_key_is_a_noop(live, node, svc, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    kw = dict(expected_version=d.leg_version, action=d.proposal, category=d.category,
              reason=REASON, evidence=d.evidence, request_key="replay-key-0001")
    first = live.apply_leg_recovery(OWNER_ACTOR, leg["id"], **kw)
    again = live.apply_leg_recovery(OWNER_ACTOR, leg["id"], **kw)
    assert first.applied is True and again.applied is False
    assert again.recovery_id == first.recovery_id and counts(owner_engine)["recoveries"] == 1
    with pytest.raises(LedgerRefused):                          # a different key cannot apply twice
        live.apply_leg_recovery(OWNER_ACTOR, leg["id"], **{**kw, "request_key": "replay-key-0002"})


def test_a_request_key_cannot_be_reused_for_another_leg(live, node, svc, world):
    oid, leg = provable_superseded(live, node, world)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    live.apply_leg_recovery(OWNER_ACTOR, leg["id"], expected_version=d.leg_version,
                            action=d.proposal, category=d.category, reason=REASON,
                            evidence=d.evidence, request_key="shared-key-0001")
    with pytest.raises(LedgerRefused) as e:
        live.apply_leg_recovery(OWNER_ACTOR, leg["id"] + 1, expected_version=1, action="superseded",
                                category="x", reason=REASON, evidence=d.evidence,
                                request_key="shared-key-0001")
    assert e.value.code in ("idempotency_conflict", "evidence_required", "not_found")


def test_restart_voids_codes_but_keeps_the_recovery(live, node, srcs, world, clock):
    oid, leg = provable_superseded(live, node, world)
    old = recovery(live, list(srcs), clock)
    p = old.prepare(OWNER_ACTOR, leg["id"])
    new = recovery(live, list(srcs), clock)            # "process restarted"
    with pytest.raises(RecoveryRefused) as e:
        new.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code == "bad_code" and leg_of(live, oid)["state"] == "ambiguous"
    p2 = new.prepare(OWNER_ACTOR, leg["id"])
    assert new.confirm(OWNER_ACTOR, leg["id"], p2.code, REASON).applied is True
    third = recovery(live, list(srcs), clock)          # restart again: state is durable
    d = third.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and d.blocked_reason in ("already_recovered", "leg_not_stuck:superseded")
    assert third.prepare(OWNER_ACTOR, leg["id"]).code is None
    assert len(live.list_recoveries(OWNER_ACTOR)) == 1


def test_a_second_recovery_of_a_recovered_leg_is_impossible(live, node, svc, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    apply(svc, leg["id"])
    with pytest.raises(LedgerRefused) as e:
        live.apply_leg_recovery(OWNER_ACTOR, leg["id"], expected_version=d.leg_version + 1,
                                action="superseded", category="x", reason=REASON,
                                evidence=d.evidence, request_key="second-try-0001")
    assert e.value.code in ("not_recoverable", "already_recovered", "stale_version", "evidence_required")
    assert counts(owner_engine)["recoveries"] == 1


# ------------------------------------------------------------------ 7. authorization
@pytest.mark.parametrize("who", [Actor.user(U1), Actor.owner(U1), Actor.owner(12345),
                                 Actor.system(), Actor("owner", None)])
def test_only_a_configured_owner_can_use_any_recovery_entry_point(live, node, svc, world, who, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    before = counts(owner_engine)
    for call in (lambda: svc.stuck(who), lambda: svc.diagnose(who, leg["id"]),
                 lambda: svc.prepare(who, leg["id"]),
                 lambda: svc.confirm(who, leg["id"], "0" * 10, REASON),
                 lambda: live.list_recoveries(who),
                 lambda: live.apply_leg_recovery(who, leg["id"], expected_version=1,
                                                 action="superseded", category="x", reason=REASON,
                                                 evidence={}, request_key="intruder-0001")):
        with pytest.raises(LedgerRefused) as e:
            call()
        assert e.value.code == "actor_not_permitted"
    assert counts(owner_engine) == before and leg_of(live, oid)["state"] == "ambiguous"


def test_the_database_role_can_neither_update_nor_delete_a_recovery(live, node, svc, world, owner_engine, rw_engine):
    oid, leg = provable_superseded(live, node, world)
    apply(svc, leg["id"])
    for sql in ("UPDATE trading_private.leg_recoveries SET reason = 'tampered tampered'",
                "DELETE FROM trading_private.leg_recoveries",
                "TRUNCATE trading_private.leg_recoveries"):
        for eng in (rw_engine, owner_engine):                   # the schema owner is stopped by trigger
            with pytest.raises(DBAPIError):
                raw(eng, sql)
    assert counts(owner_engine)["recoveries"] == 1


def test_a_second_row_for_one_leg_is_refused_by_the_database(live, node, svc, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    apply(svc, leg["id"])
    with pytest.raises(IntegrityError):
        raw(owner_engine, "INSERT INTO trading_private.leg_recoveries (leg_id, order_id, user_id, "
                          "owner_id, request_key, action, category, from_state, to_state, leg_version, "
                          "reason, evidence, evidence_digest) SELECT leg_id, order_id, user_id, owner_id, "
                          "'another-key-0001', action, category, from_state, to_state, leg_version, "
                          "reason, evidence, evidence_digest FROM trading_private.leg_recoveries")


# ------------------------------------------------------------------ 8. ledger-level structural gate
def forged(d, **changes):
    ev = json.loads(json.dumps(d.evidence))
    ev.update(changes)
    return ev


def test_the_ledger_refuses_evidence_that_is_not_two_source_and_specific(live, node, svc, world):
    oid, leg = provable_superseded(live, node, world)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])

    def go(ev, **kw):
        base = dict(expected_version=d.leg_version, action="superseded", category="x",
                    reason=REASON, evidence=ev, request_key="gate-key-000001")
        base.update(kw)
        return live.apply_leg_recovery(OWNER_ACTOR, leg["id"], **base)

    one = forged(d)
    one["sources"] = one["sources"][:1]
    for ev in ({}, None, one, forged(d, schema=2), forged(d, action="abandoned"),
               forged(d, problems=["stale_head"]),
               forged(d, leg={**d.evidence["leg"], "version": 99}),
               forged(d, facts={})):
        with pytest.raises(LedgerRefused) as e:
            go(ev)
        assert e.value.code == "evidence_required"
    for bad in ({"reason": "short"}, {"reason": ""}, {"request_key": "x"}, {"action": "cancelled"},
                {"expected_version": d.leg_version + 5}):
        with pytest.raises(LedgerRefused):
            go(d.evidence, **bad)
    assert leg_of(live, oid)["state"] == "ambiguous"


def test_the_ledger_will_not_abandon_before_the_deadline_even_with_forged_claims(live, node, svc, world, clock):
    oid, leg, _ = stuck_swap(live, node)                        # deadline NOT passed
    clock.t = swap_deadline(leg) + 700
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal == "abandoned"
    clock.t = swap_deadline(leg) - 100
    early = forged(d)
    early["facts"]["anchor_timestamp"] = swap_deadline(leg) - 100
    with pytest.raises(LedgerRefused) as e:
        live.apply_leg_recovery(OWNER_ACTOR, leg["id"], expected_version=d.leg_version,
                                action="abandoned", category="expired", reason=REASON,
                                evidence=early, request_key="early-abandon-1")
    assert e.value.code == "evidence_required" and leg_of(live, oid)["state"] == "ambiguous"
    small = forged(d)
    small["facts"]["deadline_margin_s"] = 0
    with pytest.raises(LedgerRefused):
        live.apply_leg_recovery(OWNER_ACTOR, leg["id"], expected_version=d.leg_version,
                                action="abandoned", category="expired", reason=REASON,
                                evidence=small, request_key="early-abandon-2")


def test_the_ledger_only_recovers_ambiguous_or_orphaned_legs(live, node, svc, world):
    oid, leg, _ = stuck_hashed(live, node)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    live.resolve_leg(leg["id"], expected_version=leg["version"], to_state="hash_reported",
                     resolution="late wallet answer", seen=True)
    world.mine_other(H3, 1050)
    ev = svc.diagnose(OWNER_ACTOR, leg["id"]).evidence
    ev["action"] = "superseded"
    with pytest.raises(LedgerRefused) as e:
        live.apply_leg_recovery(OWNER_ACTOR, leg["id"], expected_version=d.leg_version + 1,
                                action="superseded", category="x", reason=REASON, evidence=ev,
                                request_key="notstuck-0001")
    assert e.value.code in ("not_recoverable", "evidence_required", "stale_version")
    assert svc.diagnose(OWNER_ACTOR, leg["id"]).blocked_reason == "leg_not_stuck:hash_reported"


def test_an_order_that_already_has_a_receipt_is_never_cancelled(live, node, svc, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    with owner_engine.begin() as c:                              # force a receipt-bearing order state
        import sqlalchemy as sa
        c.execute(sa.text("ALTER TABLE trading_private.orders DISABLE TRIGGER USER"))
        c.execute(sa.text("UPDATE trading_private.orders SET state = 'included' WHERE id = :o"), {"o": oid})
        c.execute(sa.text("ALTER TABLE trading_private.orders ENABLE TRIGGER USER"))
    with pytest.raises(LedgerRefused):
        live.apply_leg_recovery(OWNER_ACTOR, leg["id"], expected_version=d.leg_version,
                                action="superseded", category=d.category, reason=REASON,
                                evidence=d.evidence, request_key="receipt-order-01")
    assert leg_of(live, oid)["state"] == "ambiguous" and counts(owner_engine)["recoveries"] == 0
    assert svc.diagnose(OWNER_ACTOR, leg["id"]).proposal is None


# ------------------------------------------------------------------ 9. orphaned legs
def orphaned_leg(live, node):
    """A real reorg: the receipt worker records an inclusion, then the block is replaced."""
    from test_wallet_receipts import mined_buy, rr
    oid, leg, ch = mined_buy(live, node)
    r = rr(live, ch)
    r.run_once()
    ch.canon[1000] = block_hash(1000, 9)
    ch.drop(H1)
    assert r.run_once().counts == {"orphaned": 1}
    return oid, leg_of(live, oid)


def test_an_orphaned_leg_is_diagnosed_and_is_not_auto_released(live, node, svc, world):
    oid, leg = orphaned_leg(live, node)
    assert leg["state"] == "orphaned" and order_state(live, oid) == "orphaned"
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.leg_state == "orphaned" and d.proposal is None        # nothing on chain, deadline not passed
    assert d.escalation


def test_an_orphaned_leg_without_a_proven_nonce_stays_blocked_when_another_tx_takes_the_hint(
        live, node, svc, world):
    """The reorged transaction could be re-included; only a PROVEN nonce (or a passed deadline)
    can exclude that. A hinted nonce used by someone else's transaction proves nothing."""
    oid, leg = orphaned_leg(live, node)
    assert leg["nonce"] is None and leg["tx_hash"] == H1
    world.mine_other(H3, 1050)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.category == "nonce_consumed_externally" and d.proposal is None
    assert "swap deadline has not passed" in d.detail and d.escalation
    assert svc.prepare(OWNER_ACTOR, leg["id"]).code is None


def test_an_orphaned_leg_with_a_proven_nonce_consumed_by_another_final_tx_is_superseded(
        live, node, svc, world, owner_engine):
    oid, leg = orphaned_leg(live, node)
    fills_before = live.get_fills(U1, oid, include_rolled_back=True)
    world.mine_other(H3, 1050)
    # a nonce is only attached to a leg by evidence; give this leg the nonce its tx was mined with
    with owner_engine.begin() as c:
        import sqlalchemy as sa
        c.execute(sa.text("ALTER TABLE trading_private.order_legs DISABLE TRIGGER USER"))
        c.execute(sa.text("UPDATE trading_private.order_legs SET nonce = 5 WHERE id = :i"), {"i": leg["id"]})
        c.execute(sa.text("ALTER TABLE trading_private.order_legs ENABLE TRIGGER USER"))
    leg = leg_of(live, oid)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal == "superseded"
    p, res = apply(svc, leg["id"])
    after = leg_of(live, oid)
    for k in ("block_number", "block_hash", "receipt_status", "gas_used", "gas_cost_wei",
              "included_at", "final_tx_hash", "tx_hash", "nonce"):
        assert after[k] == leg[k]                               # a real receipt is never rewritten
    assert live.get_fills(U1, oid, include_rolled_back=True) == fills_before
    assert res.order_state == "cancelled" and counts(owner_engine)["recoveries"] == 1


def test_an_orphaned_leg_past_its_deadline_with_no_replacement_is_abandoned_keeping_history(
        live, node, svc, world, clock, owner_engine):
    oid, leg = orphaned_leg(live, node)
    fills_before = live.get_fills(U1, oid, include_rolled_back=True)
    clock.t = swap_deadline(leg) + 700
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal == "abandoned"
    p, res = apply(svc, leg["id"])
    after = leg_of(live, oid)
    assert after["state"] == "abandoned"
    for k in ("block_number", "block_hash", "receipt_status", "gas_used", "included_at",
              "final_tx_hash", "tx_hash"):
        assert after[k] == leg[k]                               # a real receipt is never rewritten
    assert live.get_fills(U1, oid, include_rolled_back=True) == fills_before
    assert res.order_state == "cancelled"


# ------------------------------------------------------------------ 10. the owner commands
class Bot:
    def __init__(self, live, rec, owners=None):
        self.ledger, self.recovery, self.cfg = live, rec, SimpleNamespace()
        self.sent: list[str] = []
        self.owners = owners

    def _send(self, chat, text, markup=None):
        self.sent.append(text)


def msg(uid, chat=None, kind="private"):
    return {"from": {"id": uid}, "chat": {"id": uid if chat is None else chat, "type": kind}}


def run_cmd(bot, uid, cmd, arg, **kw):
    from agora_tracker.tgui import IntelBot
    IntelBot._leg_recovery(bot, kw.get("chat", uid), uid, msg(uid, kw.get("chat"), kw.get("kind", "private")),
                           cmd, arg)
    return bot.sent[-1]


def test_telegram_commands_are_owner_private_chat_only(live, node, svc, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    bot = Bot(live, svc)
    before = counts(owner_engine)
    for uid, kw in ((U1, {}), (U2, {}), (OWNER, {"chat": -100123, "kind": "supergroup"}),
                    (OWNER, {"chat": OWNER + 1, "kind": "private"})):
        assert run_cmd(bot, uid, "/wallet_recover", "", **kw) == "Not available."
        assert run_cmd(bot, uid, "/wallet_recover", str(leg["id"]), **kw) == "Not available."
        assert run_cmd(bot, uid, "/wallet_resolve", f"{leg['id']} 0123456789 {REASON}", **kw) == "Not available."
    off = Bot(live, None)
    assert run_cmd(off, OWNER, "/wallet_recover", "") == "Not available."
    assert counts(owner_engine) == before


def test_telegram_owner_flow_end_to_end(live, node, svc, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    bot = Bot(live, svc)
    assert "Stuck wallet legs" in run_cmd(bot, OWNER, "/wallet_recover", "")
    shown = run_cmd(bot, OWNER, "/wallet_recover", str(leg["id"]))
    assert "Provable resolution: superseded" in shown and "/wallet_resolve" in shown
    code = shown.split(f"/wallet_resolve {leg['id']} ")[1].split(" ")[0]
    assert "Refused" in run_cmd(bot, OWNER, "/wallet_resolve", f"{leg['id']} {code} short")
    assert leg_of(live, oid)["state"] == "ambiguous"
    done = run_cmd(bot, OWNER, "/wallet_resolve", f"{leg['id']} {code} {REASON}")
    assert "Recorded" in done and "No transaction was sent" in done
    assert leg_of(live, oid)["state"] == "superseded"
    assert "Refused (bad_code)" in run_cmd(bot, OWNER, "/wallet_resolve", f"{leg['id']} {code} {REASON}")
    assert "Use /wallet_recover" in run_cmd(bot, OWNER, "/wallet_recover", "abc")
    assert "Use /wallet_resolve" in run_cmd(bot, OWNER, "/wallet_resolve", "1 2")


def test_telegram_refusal_shows_the_diagnosis_and_escalation(live, node, svc, world, srcs):
    oid, leg = provable_superseded(live, node, world)
    bot = Bot(live, svc)
    shown = run_cmd(bot, OWNER, "/wallet_recover", str(leg["id"]))
    code = shown.split(f"/wallet_resolve {leg['id']} ")[1].split(" ")[0]
    srcs[0].down = True
    out = run_cmd(bot, OWNER, "/wallet_resolve", f"{leg['id']} {code} {REASON}")
    assert "Refused" in out and "STAYS BLOCKED" in out and leg_of(live, oid)["state"] == "ambiguous"


# ------------------------------------------------------------------ 11. nothing else moves
def test_a_recovery_never_creates_requests_legs_orders_or_approvals(live, node, svc, world, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    before = counts(owner_engine)
    apply(svc, leg["id"])
    after = counts(owner_engine)
    for k in ("legs", "orders", "requests", "attempts", "fills"):
        assert after[k] == before[k], k
    assert live.get_order(U1, oid).state == "cancelled"


def test_blocked_legs_keep_the_wallet_lock_whatever_the_owner_tries(live, node, svc, world, owner_engine, clock):
    oid, leg, _ = stuck_swap(live, node)                        # outcome genuinely unknown
    assert locked_legs(owner_engine) == 1
    for _ in range(3):
        p = svc.prepare(OWNER_ACTOR, leg["id"])
        assert p.code is None
        with pytest.raises(RecoveryRefused):
            svc.confirm(OWNER_ACTOR, leg["id"], "0123456789", REASON)
    assert locked_legs(owner_engine) == 1 and counts(owner_engine)["recoveries"] == 0
    assert leg_of(live, oid)["state"] == "ambiguous" and order_state(live, oid) == "ambiguous"


# ------------------------------------------------------------------ 12. migration
def test_migration_0015_creates_a_guarded_append_only_table(pg_engine):
    assert raw(pg_engine, "SELECT relrowsecurity FROM pg_class WHERE relname = 'leg_recoveries'")[0][0]
    trig = {r[0] for r in raw(pg_engine, "SELECT tgname FROM pg_trigger WHERE tgrelid = "
                                         "'trading_private.leg_recoveries'::regclass AND NOT tgisinternal")}
    assert {"leg_recoveries_append_only", "leg_recoveries_no_truncate"} <= trig
    grants = {r[0] for r in raw(pg_engine, "SELECT privilege_type FROM information_schema.role_table_grants "
                                           "WHERE table_name = 'leg_recoveries' AND grantee = 'trading_rw'")}
    assert grants == {"SELECT", "INSERT"}
    kinds = raw(pg_engine, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                           "conname = 'order_events_kind_check'")[0][0]
    assert "LEG_RECOVERY" in kinds and "LEG_RECOVERY_REFUSED" in kinds
    downgrade(pg_engine, "0014")
    assert raw(pg_engine, "SELECT count(*) FROM information_schema.tables WHERE table_schema = "
                          "'trading_private' AND table_name = 'leg_recoveries'")[0][0] == 0
    upgrade(pg_engine)
    assert raw(pg_engine, "SELECT count(*) FROM information_schema.tables WHERE table_schema = "
                          "'trading_private' AND table_name = 'leg_recoveries'")[0][0] == 1


# ------------------------------------------------------------------ 13. F-15: independence of the sources
def test_two_hosts_of_one_operator_never_yield_a_proposal_or_a_confirmable_recovery(
        live, node, world, clock, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    same = [Src(world, "rpc1.shop.example", clock), Src(world, "rpc2.shop.example", clock)]
    assert same[0].operator == same[1].operator
    svc = recovery(live, same, clock)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and d.decision_key is None and d.corroborated is False
    assert "sources_not_independent" in json.dumps(d.evidence) and d.blocked_reason
    before = counts(owner_engine)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    assert p.code is None
    with pytest.raises(RecoveryRefused):
        svc.confirm(OWNER_ACTOR, leg["id"], "0123456789", REASON)
    after = counts(owner_engine)                  # only the audited refusal is appended
    assert after == {**before, "events": before["events"] + 1}
    assert raw(owner_engine, "SELECT kind FROM trading_private.order_events ORDER BY chain_no DESC LIMIT 1"
               )[0][0] == "LEG_RECOVERY_REFUSED"
    assert leg_of(live, oid)["state"] == "ambiguous" and locked_legs(owner_engine) == 1


def test_a_single_source_is_diagnosable_but_never_corroborated(live, node, world, clock, srcs):
    oid, leg = provable_superseded(live, node, world)
    svc = recovery(live, [srcs[0]], clock)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and d.corroborated is False


def test_the_recovery_evidence_passes_the_shared_ledger_gate_with_operators(
        live, node, world, svc, owner_engine):
    oid, leg = provable_superseded(live, node, world)
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal and d.corroborated is True
    assert sorted(s["operator"] for s in d.evidence["sources"]) == ["rpc-a.example", "rpc-b.example"]
    assert "rpc-a.example" in render_diagnosis(d)


# ------------------------------------------------------------------ 14. F-04: finality is more than depth
def test_owner_recovery_never_proposes_supersession_before_the_finalized_tag(
        live, node, world, svc, clock, owner_engine):
    oid, leg = provable_superseded(live, node, world)        # consuming tx at 1050, 51 deep
    world.finalized_lag = 60                                 # finalized height 1040 < 1050
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal is None and d.reason == "replacement_not_final_yet"
    assert "finalized height" in d.detail
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    assert p.code is None and locked_legs(owner_engine) == 1
    world.no_finalized_tag = True                            # a node without the tag: still nothing
    world.finalized_lag = 0
    assert svc.diagnose(OWNER_ACTOR, leg["id"]).proposal is None
    world.no_finalized_tag = False
    d = svc.diagnose(OWNER_ACTOR, leg["id"])
    assert d.proposal == "superseded" and d.evidence["finalized"] == {"number": HEAD}


# ------------------------------------------------------------------ 15. lock order (CI `busy`)
@pytest.mark.parametrize("path", ["recovery_refusal", "wallet_result"])
def test_event_writers_take_the_order_row_before_the_audit_chain_lock(
        live, node, world, owner_engine, path):
    """A transaction holding the order row (like apply_leg_recovery) and then appending an audit
    event must never deadlock against a concurrent refusal audit / wallet-result event."""
    import sqlalchemy as sa
    oid, leg = provable_superseded(live, node, world)
    out = {}
    with owner_engine.connect() as a:
        tx = a.begin()
        a.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
        a.execute(sa.text("SELECT 1 FROM trading_private.orders WHERE id = :o FOR UPDATE"),
                  {"o": oid})

        def other():
            try:
                if path == "recovery_refusal":
                    out["r"] = live.record_recovery_refusal(
                        OWNER_ACTOR, leg["id"], request_key="refused-" + "a" * 32,
                        code="evidence_changed", evidence_digest=None)
                else:
                    out["r"] = live.record_wallet_result(
                        U1, oid, outcome="error", tx_hash=None, reason="timeout",
                        request_key="wr-" + "b" * 30)
            except Exception as exc:                     # noqa: BLE001
                out["r"] = exc
        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.2)                                  # `other` is now waiting somewhere
        t0 = time.monotonic()
        a.execute(sa.text(
            "INSERT INTO trading_private.order_events (order_id, user_id, actor_kind, actor_id, "
            "kind, request_key, detail, prev_hash, hash) VALUES (:o, :u, 'owner', :a, "
            "'LEG_RECOVERY_REFUSED', :k, '{}'::jsonb, '', '')"),
            {"o": oid, "u": U1, "a": OWNER, "k": "holder-" + "c" * 30})   # chain lock, after order
        waited = time.monotonic() - t0
        tx.commit()
        t.join(15)
    # With the inverted order the holder's event insert blocks on the chain lock until PostgreSQL
    # detects the deadlock (deadlock_timeout, 1 s) and aborts the other side, which then retries.
    # With the correct order the other side waits on the ORDER row and the holder never waits.
    assert waited < 0.4, f"holder waited {waited:.2f}s for the audit-chain lock (deadlock)"
    assert out["r"] is True


def test_a_transient_busy_ledger_keeps_the_confirmation_code_valid(live, node, world, svc,
                                                                   owner_engine, monkeypatch):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])
    real = live.apply_leg_recovery
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LedgerRefused("busy", "the ledger is busy; retry with the same key")
        return real(*a, **k)
    monkeypatch.setattr(live, "apply_leg_recovery", flaky)
    with pytest.raises(RecoveryRefused) as e:
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code == "busy" and counts(owner_engine)["recoveries"] == 0
    assert svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON).applied is True     # same code
    assert counts(owner_engine)["recoveries"] == 1
    with pytest.raises(RecoveryRefused):                                          # now consumed
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)


def test_any_other_refusal_still_consumes_the_code(live, node, world, svc, monkeypatch):
    oid, leg = provable_superseded(live, node, world)
    p = svc.prepare(OWNER_ACTOR, leg["id"])

    def refuse(*a, **k):
        raise LedgerRefused("stale_version", "changed")
    monkeypatch.setattr(live, "apply_leg_recovery", refuse)
    with pytest.raises(RecoveryRefused):
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    with pytest.raises(RecoveryRefused) as e:
        svc.confirm(OWNER_ACTOR, leg["id"], p.code, REASON)
    assert e.value.code == "bad_code"
