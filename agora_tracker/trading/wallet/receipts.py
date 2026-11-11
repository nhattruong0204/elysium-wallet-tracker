"""Receipt reconciliation (issue #94, hardened by #95 F-15): turn mined transactions into
truthful ledger facts, never on one provider's word.

Every pass opens ONE corroboration session (`wallet/corroboration.py`): the heads of >= 2
operationally distinct RPC sources are read, the lowest is the anchor, every source must report
the identical header there, and every fact below is read at blocks <= that anchor and required to
be identical on EVERY source. A provider that is merely a block or two behind moves the anchor; it
is not a disagreement. Anything else that is not unanimous fails closed (nothing is recorded; a
disagreement is appended to the audit trail).

Per leg that has a transaction hash and is not final yet:

  1. `Session.inclusion`: transaction, receipt, inclusion block and its canonical hash, identical
     on all sources (a receipt without a transaction, a different block or a different receipt
     on one source is a conflict, not a verdict);
  2. validate them against the leg (hash, sender, target, value, calldata, status, block hash);
     a pair that does not match the request is NEVER accounted: it is left for the #93 reconciler;
  3. record the inclusion (`included` / `reverted`) with its fill, provisional until the block is
     `finality_confirmations` deep at the shared anchor, then final. The configured finality depth
     is used unchanged;
  4. detect reorgs: a recorded block that all sources agree is no longer canonical (or a receipt
     that every source has lost while the recorded block is no longer the canonical one) rolls the
     provisional fill back and marks the leg/order `orphaned`; a receipt in a different block is a
     fresh inclusion (new fill row, the old one rolled back). One source saying "gone" is not
     enough to orphan anything.

Rules that keep it honest:
  * the ledger refuses every write above unless it carries the session's provenance (F-15), so a
    caller that forgot to corroborate cannot finalize, certify or orphan;
  * a fill is `verified` only with two SEPARATE properties: the two on-chain accounting methods
    agree (`FillRecord.method_agreement`) AND every input was read through the corroborated
    session (see fills.py);
  * the receipt worker does not consult the pause / execution flag: pausing stops NEW prompts, but
    what already happened on chain must still be recorded;
  * an approval leg changes nothing about its order and never has a fill; the swap is a new plan;
  * a hash reported by a wallet is never trusted over the chain: no receipt -> still `submitted`;
  * unreadable evidence => nothing recorded this pass (retry), and after `evidence_retries` passes the
    fill is recorded as `unknown` (never a guess) so PnL stays unknown rather than wrong;
  * every pass has a hard RPC-call and wall-time budget so a backlog cannot starve the execution
    RPC, whatever the number of providers."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..ledger.core import LedgerRefused, OrderLedger
from ..ledger.legs import FillRecord
from ..paper import TradeRefused
from .corroboration import (
    DEFAULT_CALLS,
    DEFAULT_SECONDS,
    BudgetExhausted,
    Conflict,
    Corroborator,
    EvidenceError,
    Inclusion,
    Session,
)
from .fills import EvidenceUnavailable, ReceiptError, compute_fill, gas_cost, validate

log = logging.getLogger(__name__)

DEFAULT_RPC_BUDGET = DEFAULT_CALLS
DEFAULT_EVIDENCE_RETRIES = 3


@dataclass
class PassReport:
    checked: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    codes: dict[str, int] = field(default_factory=dict)     # evidence error codes (stable, bounded)
    budget_exhausted: bool = False
    calls: int = 0

    def add(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def code(self, code: str) -> None:
        self.codes[code] = self.codes.get(code, 0) + 1


class ReceiptReconciler:
    def __init__(self, ledger: OrderLedger, corroborator: Corroborator, *,
                 rpc_budget: int = DEFAULT_RPC_BUDGET, rpc_seconds: float = DEFAULT_SECONDS,
                 evidence_retries: int = DEFAULT_EVIDENCE_RETRIES,
                 clock: Callable[[], float] = time.time) -> None:
        self.ledger, self.corro, self.clock = ledger, corroborator, clock
        self.finality = corroborator.finality
        self.rpc_budget, self.rpc_seconds = rpc_budget, rpc_seconds
        self.evidence_retries = evidence_retries
        self._evidence_failures: dict[tuple[int, str], int] = {}

    # ------------------------------------------------------------------ one leg
    def process(self, leg: dict, sess: Session) -> str:
        """Returns a short outcome label (also the metric label). Raises `EvidenceError` when the
        sources cannot establish the facts (the pass records it and moves on)."""
        tx_hash = leg["final_tx_hash"] or leg["tx_hash"]
        if not tx_hash:
            return "no_hash"
        mark = sess.mark()
        inc = sess.inclusion(tx_hash)
        if leg["block_hash"] is not None and leg["state"] in ("included", "reverted"):
            return self._recheck(leg, tx_hash, inc, sess, mark)
        return self._first_sight(leg, tx_hash, inc, sess, mark)

    def _first_sight(self, leg: dict, tx_hash: str, inc: Inclusion, sess: Session,
                     mark: tuple[int, int]) -> str:
        if inc.status in ("absent", "pending", "ahead"):
            return "pending"        # a wallet-reported hash is only an acknowledgment; ahead = retry
        if inc.status == "not_canonical":
            return "not_canonical"  # every source agrees the receipt is from a non-canonical block
        return self._record(leg, tx_hash, inc, sess, mark)

    def _record(self, leg: dict, tx_hash: str, inc: Inclusion, sess: Session,
                mark: tuple[int, int]) -> str:
        tx, receipt = inc.tx, inc.receipt
        assert tx is not None and receipt is not None
        assert inc.block_number is not None and inc.block_hash is not None
        assert inc.confirmations is not None
        problems = validate(leg, tx, receipt, tx_hash)
        if problems:
            log.error("leg %s receipt does not match the request: %s", leg["id"], problems)
            return "invalid"
        number, block_hash = inc.block_number, inc.block_hash
        final = sess.is_final(number, inc.confirmations)      # depth AND finalized tag (F-04)
        status = 1 if receipt["status"] == "0x1" else 0
        state = "included" if status else "reverted"
        used, cost = gas_cost(tx, receipt)

        fill: FillRecord | None = None
        if leg["kind"] == "swap":
            key = (leg["id"], block_hash)
            fails = self._evidence_failures.get(key, 0)
            try:
                fill = compute_fill(leg, tx, receipt, tx_hash, sess,
                                    strict=fails < self.evidence_retries)
            except EvidenceUnavailable:
                self._evidence_failures[key] = fails + 1
                return "evidence_unavailable"
            except ReceiptError as exc:
                log.error("leg %s receipt unusable: %s", leg["id"], exc)
                return "invalid"
            self._evidence_failures.pop(key, None)
        evidence = sess.provenance(since=mark) | {
            "facts": {"leg_id": leg["id"], "leg_version": leg["version"], "outcome": state,
                      "tx_hash": tx_hash, "block_number": number, "block_hash": block_hash,
                      "receipt_status": status, "confirmations": inc.confirmations}}
        if fill is not None:
            evidence["fill"] = {"verdict": fill.verdict, "method_agreement": fill.method_agreement,
                                "inputs_source_agreed": sess.corroborated, "basis": fill.basis}
        try:
            ok = self.ledger.apply_receipt(
                leg["id"], expected_version=leg["version"], state=state, block_number=number,
                block_hash=block_hash, receipt_status=status, gas_used=used, gas_cost_wei=cost,
                fill=fill, finalize=final, evidence=evidence)
        except LedgerRefused as exc:
            if exc.code != "evidence_required":
                raise
            log.error("leg %s receipt refused by the evidence gate: %s", leg["id"], exc)
            return "evidence_refused"
        if not ok:
            return "stale"
        return ("final_" if final else "provisional_") + state

    def _recheck(self, leg: dict, tx_hash: str, inc: Inclusion, sess: Session,
                 mark: tuple[int, int]) -> str:
        """The leg already has an inclusion recorded: is it still canonical, and is it final?"""
        number, block_hash = leg["block_number"], leg["block_hash"]
        if inc.status == "mined":
            if inc.block_hash != block_hash:
                # re-included in a different canonical block: a fresh inclusion (new fill row,
                # the old provisional one is rolled back by the ledger)
                return self._record(leg, tx_hash, inc, sess, mark)
            assert inc.confirmations is not None
            if not sess.is_final(inc.block_number or number, inc.confirmations):
                return "provisional_unchanged"
            # canonical and deep enough: finalize. The fill is re-derived (strictly) and keyed by
            # block hash, so an existing provisional row is promoted, never duplicated or rewritten.
            return self._record(leg, tx_hash, inc, sess, mark)
        anchor = sess.anchor()
        if number > anchor.number:
            return "pending"            # the shared view has not reached the recorded block
        if inc.status == "ahead":
            return "pending"
        if inc.status == "not_canonical" and inc.block_hash != block_hash:
            return "pending"            # a different, non-canonical block: nothing about ours
        # absent / pending (receipt lost by EVERY source) / our block reported non-canonical by
        # every source. Only a corroborated, different canonical block at that height is a reorg.
        canonical_here = sess.header_hash(number)
        if canonical_here == block_hash:
            raise Conflict("receipt_missing_in_canonical_block",
                           "every source lost the receipt although the recorded block is canonical")
        basis = "not_canonical" if inc.status == "not_canonical" else "absent"
        evidence = sess.provenance(since=mark) | {
            "facts": {"leg_id": leg["id"], "leg_version": leg["version"], "outcome": "reorg",
                      "block_hash": block_hash, "block_number": number, "basis": basis,
                      "canonical_hash_at_height": canonical_here, "tx_hash": tx_hash}}
        try:
            ok = self.ledger.apply_reorg(leg["id"], expected_version=leg["version"],
                                         reason="recorded block is no longer canonical",
                                         evidence=evidence)
        except LedgerRefused as exc:
            if exc.code != "evidence_required":
                raise
            log.error("leg %s reorg refused by the evidence gate: %s", leg["id"], exc)
            return "evidence_refused"
        return "orphaned" if ok else "stale"

    # ------------------------------------------------------------------ the pass
    def run_once(self, limit: int = 100,
                 should_continue: Callable[[], bool] | None = None) -> PassReport:
        """One pass. `should_continue` (optional) lets a supervisor bound the pass by wall time or
        a shutdown request; legs not reached wait for the next pass."""
        rep = PassReport()
        legs = self.ledger.receipt_candidates(limit=limit)
        if not legs:
            return rep
        sess = self.corro.session(calls=self.rpc_budget, seconds=self.rpc_seconds)
        try:
            sess.pin()                      # no usable shared anchor => nothing at all is recorded
        except BudgetExhausted:
            rep.budget_exhausted = True
            rep.add("budget_exhausted")
            return rep
        except EvidenceError as exc:
            rep.add("unverifiable")
            rep.code(exc.code)
            log.warning("receipt pass: no corroborated anchor (%s)", exc.code)
            return rep
        for leg in legs:
            if should_continue is not None and not should_continue():
                break
            rep.checked += 1
            try:
                rep.add(self.process(leg, sess))
            except BudgetExhausted:
                rep.budget_exhausted = True
                rep.add("budget_exhausted")
                break
            except Conflict as exc:
                rep.add("conflict")
                rep.code(exc.code)
                self._record_conflict(leg, exc, sess)
            except EvidenceError as exc:
                rep.add("unverifiable")
                rep.code(exc.code)
                log.warning("receipt check of leg %s: evidence unavailable (%s)", leg.get("id"),
                            exc.code)
            except (TradeRefused, LedgerRefused) as exc:
                rep.add("error")
                log.warning("receipt check of leg %s skipped: %s", leg.get("id"), exc)
        rep.calls = sess.spent
        return rep

    def _record_conflict(self, leg: dict, exc: Conflict, sess: Session) -> None:
        try:
            ev = sess.provenance(strict=False) | {"source": exc.source,
                                                  "tx_hash": leg["final_tx_hash"] or leg["tx_hash"]}
            self.ledger.record_chain_conflict(leg["id"], code=exc.code, evidence=ev)
        except Exception:                                        # noqa: BLE001 - audit best effort
            log.warning("could not record a chain conflict for leg %s", leg.get("id"))
        log.error("leg %s: RPC sources disagree (%s); nothing was recorded", leg.get("id"), exc.code)


def serve(reconciler: ReceiptReconciler, stop: threading.Event, *, interval: float = 5.0,
          max_backoff: float = 60.0) -> int:
    """Run passes until `stop` is set (dormant: nothing calls this by default). A failing pass
    never ends the loop; consecutive failures back off exponentially up to `max_backoff`. Returns
    the number of passes made. Runs regardless of the pause flag by design."""
    passes, failures = 0, 0
    while not stop.is_set():
        try:
            reconciler.run_once()
            failures = 0
        except Exception:                                  # noqa: BLE001 - the loop must survive
            failures += 1
            log.exception("receipt pass failed (%d consecutive)", failures)
        passes += 1
        stop.wait(min(max_backoff, interval * (2 ** min(failures, 6))))
    return passes
