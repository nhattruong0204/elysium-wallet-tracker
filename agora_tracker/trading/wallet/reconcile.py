"""Submission reconciliation (issue #93, hardened by #95 F-15): decide what became of a wallet
request from CORROBORATED chain evidence only, and apply it to the ledger atomically.

Every decision is made inside one corroboration session (`wallet/corroboration.py`): >= 2
operationally distinct RPC sources, one shared anchor block, every fact read at blocks <= the
anchor and identical on every source. Evidence ladder for one unsettled leg (`prompted`,
`hash_reported` or `ambiguous`):

  hash known, found, payload matches      -> PENDING / MINED   (nonce recorded; #94 owns receipts)
  hash known, found, payload differs      -> MISMATCH          (ambiguous; never "the reviewed trade")
  hash known, absent, nonce known+consumed-> REPLACED_SAME (same payload, e.g. speed-up) or
                                             SUPERSEDED (other tx owns the nonce AND is final: at
                                             least `finality_confirmations` deep at the anchor)
  hash absent/unknown, nonce hint consumed-> REPLACED_SAME if an identical request sits at the
                                             hinted nonce, else UNRESOLVED (wallet stays blocked)
  nothing found, swap deadline long past on
  the sources' block clock, nothing queued,
  every wallet tx since the prompt checked-> ABANDONED (a swap cannot execute after its deadline)
  sources disagree                        -> CONFLICT: nothing is changed, the disagreement is
                                             appended to the audit trail
  any source unreachable / stale / forked -> UNKNOWN: nothing is changed, nothing is retried

Lock-releasing verdicts (superseded / abandoned) and any recorded `final_tx_hash` are written only
together with the session's provenance; the ledger refuses them otherwise. Absence is believed
only when EVERY source is healthy and agrees at the anchor; one source's presence claim can never
release a lock. Nothing here signs, replaces or rebroadcasts a transaction."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..ledger.core import LedgerRefused, OrderLedger
from ..ledger.states import OrderState
from ..paper import TradeRefused
from .corroboration import (
    DEFAULT_CALLS,
    DEFAULT_SECONDS,
    BudgetExhausted,
    Conflict,
    Corroborator,
    EvidenceError,
    Session,
)
from .requests import WalletRequestError, decode_swap, tx_mismatches

log = logging.getLogger(__name__)

ADVISORY = frozenset({"gas_limit", "fee_cap"})      # the wallet's own fee UI may legitimately differ
DEADLINE_MARGIN_S = 90
APPROVAL_ABANDON_AFTER_S = 3600


@dataclass(frozen=True)
class Verdict:
    kind: str                       # see module docstring
    reason: str
    nonce: int | None = None
    final_tx_hash: str | None = None
    seen: bool = False
    mismatches: tuple[str, ...] = ()
    evidence: dict[str, Any] | None = None      # provenance + facts for the ledger gate


@dataclass
class Report:
    checked: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    codes: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str) -> None:
        self.checked += 1
        self.counts[kind] = self.counts.get(kind, 0) + 1


@dataclass(frozen=True)
class _Expected:
    sender: str
    to: str
    value_wei: int
    data: str
    chain_id: int
    gas_limit: int | None
    gas_price_wei: int | None


def expected_of(leg: dict) -> _Expected:
    gas = int(leg["gas_limit"]) if leg["gas_limit"] is not None else None
    fee = int(leg["max_fee_wei"]) if leg["max_fee_wei"] is not None else None
    price = fee // gas if gas and fee else None
    return _Expected(leg["wallet"], leg["expected_to"], int(leg["expected_value_wei"]),
                     leg["expected_data"], leg["chain_id"], gas, price)


def swap_deadline(leg: dict) -> int | None:
    if leg["kind"] != "swap":
        return None
    side = "buy" if int(leg["expected_value_wei"]) > 0 else "sell"
    try:
        return int(decode_swap(leg["expected_data"], side)["deadline"])
    except WalletRequestError:
        return None


def critical(mm: list[str]) -> list[str]:
    return [m for m in mm if m not in ADVISORY]


def _nonce_of(tx: dict) -> int | None:
    try:
        return int(str(tx.get("nonce")), 16)
    except ValueError:
        return None


class LegReconciler:
    def __init__(self, ledger: OrderLedger, corroborator: Corroborator, *,
                 clock: Callable[[], float] = time.time,
                 deadline_margin_s: int = DEADLINE_MARGIN_S,
                 approval_abandon_after_s: int = APPROVAL_ABANDON_AFTER_S,
                 rpc_budget: int = DEFAULT_CALLS, rpc_seconds: float = DEFAULT_SECONDS) -> None:
        self.ledger, self.corro, self.clock = ledger, corroborator, clock
        self.margin = deadline_margin_s
        self.approval_abandon_after_s = approval_abandon_after_s
        self.rpc_budget, self.rpc_seconds = rpc_budget, rpc_seconds

    # ------------------------------------------------------------------ decision
    def classify(self, leg: dict, sess: Session | None = None) -> Verdict:
        """Never raises for an evidence problem: that is a verdict (`unknown` / `conflict`) that
        changes nothing. A spent budget propagates so the pass can stop."""
        sess = sess or self.corro.session(calls=self.rpc_budget, seconds=self.rpc_seconds)
        mark = sess.mark()
        try:
            return self._classify(leg, sess, mark)
        except BudgetExhausted:
            raise
        except Conflict as exc:
            try:
                ev = sess.provenance(strict=False, since=mark) | {"source": exc.source}
            except EvidenceError:
                ev = None
            return Verdict("conflict", exc.code, evidence=ev)
        except EvidenceError as exc:
            return Verdict("unknown", exc.code)

    def _evidence(self, sess: Session, mark: tuple[int, int], leg: dict, outcome: str,
                  facts: dict[str, Any]) -> dict[str, Any]:
        return sess.provenance(since=mark) | {"facts": {
            "leg_id": leg["id"], "leg_version": leg["version"], "outcome": outcome} | facts}

    def _classify(self, leg: dict, sess: Session, mark: tuple[int, int]) -> Verdict:
        exp = expected_of(leg)
        sender = leg["wallet"]
        tx_hash, nonce = leg["tx_hash"], leg["nonce"]
        nonce = int(nonce) if nonce is not None else None
        hint = int(leg["nonce_hint"]) if leg["nonce_hint"] is not None else None
        sess.pin()                                  # no corroborated anchor => UNKNOWN, nothing else

        if tx_hash is not None:
            inc = sess.inclusion(tx_hash)
            if inc.status != "absent" and inc.tx is not None:
                tx = inc.tx
                bad = critical(tx_mismatches(exp, tx, tx_hash))
                if bad:
                    return Verdict("mismatch", "payload_mismatch:" + ",".join(bad)[:150],
                                   mismatches=tuple(bad), seen=True)
                n = _nonce_of(tx)
                if n is None:
                    return Verdict("mismatch", "malformed_nonce", seen=True)
                mined = inc.status in ("mined", "ahead")
                return Verdict("mined" if mined else "pending", "found_by_hash", nonce=n, seen=True)
            # the hash is absent from EVERY source at the shared anchor
            if nonce is not None:
                return self._after_absence(leg, exp, sender, nonce, certain=True, sess=sess,
                                           mark=mark)
        return self._after_absence(leg, exp, sender, hint, certain=False, sess=sess, mark=mark)

    def _after_absence(self, leg: dict, exp: _Expected, sender: str, nonce: int | None, *,
                       certain: bool, sess: Session, mark: tuple[int, int]) -> Verdict:
        anchor = sess.anchor()
        latest = sess.nonce_at(sender, anchor.number)       # identical on every source, at the anchor
        if nonce is not None and latest > nonce:
            cons = sess.consuming_tx(sender, nonce, leg["prompt_block"])
            other = critical(tx_mismatches(exp, cons.tx, None))
            h = str(cons.tx.get("hash", "")).lower()
            ctx = {"hash": h, "block_number": cons.block_number, "block_hash": cons.block_hash,
                   "nonce": nonce, "confirmations": cons.confirmations, "mismatches": other}
            if not other:
                n = _nonce_of(cons.tx)
                return Verdict("replaced_same", "identical_request_at_nonce",
                               nonce=nonce if n is None else n, final_tx_hash=h, seen=True,
                               evidence=self._evidence(sess, mark, leg, "hash_reported",
                                                       {"final_tx_hash": h, "consuming_tx": ctx}))
            if not certain:
                return Verdict("unresolved", "nonce_hint_used_by_other_transaction")
            if cons.confirmations < self.corro.finality:
                return Verdict("unresolved", "replacement_not_final_yet")
            return Verdict("superseded", "nonce_used_by_other_transaction", nonce=nonce,
                           final_tx_hash=h, seen=True,
                           evidence=self._evidence(sess, mark, leg, "superseded",
                                                   {"final_tx_hash": h, "consuming_tx": ctx}))
        if sess.queued(sender):
            return Verdict("unresolved", "unidentified_pending_transactions")
        # Nothing of this wallet is queued. Could the request still land? Only if its deadline has
        # not passed (swap) or it was never sent for long enough (approval) - judged on the sources'
        # block clock, never on this host's clock alone.
        ts = anchor.timestamp
        facts: dict[str, Any] = {"anchor_timestamp": ts, "wallet_queue_empty": True}
        deadline = swap_deadline(leg)
        if leg["kind"] == "swap":
            if deadline is None or ts <= deadline + self.margin:
                return self._maybe_unresolved(leg)
            facts |= {"swap_deadline": deadline, "deadline_margin_s": self.margin}
        elif (leg["prompted_at"] is not None and leg["tx_hash"] is None
              and ts - int(leg["prompted_at"]) > self.approval_abandon_after_s):
            facts |= {"policy": "approval_never_sent", "prompted_at": int(leg["prompted_at"]),
                      "age_s": ts - int(leg["prompted_at"]), "min_age_s": self.approval_abandon_after_s}
        else:
            return self._maybe_unresolved(leg)
        lo = leg["prompt_block"]
        if lo is None:
            return Verdict("unresolved", "no_prompt_block_recorded")
        act = sess.wallet_activity(sender, lo)
        if act.too_many:
            return Verdict("unresolved", "too_many_wallet_transactions_to_check")
        checked = []
        for c in act.txs:
            h = str(c.tx.get("hash", "")).lower()
            checked.append({"nonce": _nonce_of(c.tx), "hash": h, "block_number": c.block_number,
                            "block_hash": c.block_hash})
            if not critical(tx_mismatches(exp, c.tx, None)):
                ctx = {"hash": h, "block_number": c.block_number, "block_hash": c.block_hash,
                       "nonce": _nonce_of(c.tx), "confirmations": c.confirmations,
                       "mismatches": []}
                return Verdict("replaced_same", "identical_request_found_in_wallet_history",
                               nonce=_nonce_of(c.tx), final_tx_hash=h, seen=True,
                               evidence=self._evidence(sess, mark, leg, "hash_reported",
                                                       {"final_tx_hash": h, "consuming_tx": ctx}))
        facts |= {"nonce_at_prompt_block": act.nonce_at_start, "nonce_at_anchor": act.nonce_at_anchor,
                  "checked_nonces": checked}
        why = ("swap_deadline_passed_without_any_transaction" if leg["kind"] == "swap"
               else "approval_never_sent")
        return Verdict("abandoned", why,
                       evidence=self._evidence(sess, mark, leg, "abandoned", facts))

    @staticmethod
    def _maybe_unresolved(leg: dict) -> Verdict:
        return Verdict("unresolved", "no_evidence_yet")

    # ------------------------------------------------------------------ application
    def apply(self, leg: dict, v: Verdict) -> bool:
        lid, ver = leg["id"], leg["version"]
        swap = leg["kind"] == "swap"
        S = OrderState
        if v.kind == "conflict":
            return self.ledger.record_chain_conflict(lid, code=v.reason, evidence=v.evidence)
        if v.kind in ("pending", "mined", "replaced_same"):
            changed = (leg["state"] != "hash_reported" or leg["nonce"] is None
                       or leg["seen_at"] is None or (v.final_tx_hash and not leg["final_tx_hash"]))
            if not changed:
                return False
            return self.ledger.resolve_leg(
                lid, expected_version=ver, to_state="hash_reported", resolution=v.reason,
                nonce=v.nonce, final_tx_hash=v.final_tx_hash, seen=True,
                order_path=(S.SUBMITTED,) if swap else (), evidence=v.evidence)
        if v.kind == "mismatch":
            log.error("wallet leg %s transaction does not match the request: %s", lid, v.reason)
            if leg["state"] == "ambiguous":
                return False
            return self.ledger.resolve_leg(
                lid, expected_version=ver, to_state="ambiguous", resolution=v.reason, seen=v.seen,
                order_path=(S.AMBIGUOUS,) if swap else (), order_reason=v.reason[:190])
        if v.kind == "superseded":
            return self.ledger.resolve_leg(
                lid, expected_version=ver, to_state="superseded", resolution=v.reason,
                nonce=v.nonce, final_tx_hash=v.final_tx_hash, seen=True,
                order_path=(S.AMBIGUOUS, S.CANCELLED) if swap else (),
                order_reason="nonce used by another transaction", evidence=v.evidence)
        if v.kind == "abandoned":
            return self.ledger.resolve_leg(
                lid, expected_version=ver, to_state="abandoned", resolution=v.reason,
                order_path=(S.CANCELLED,) if swap and leg["state"] == "prompted"
                else (S.AMBIGUOUS, S.CANCELLED) if swap else (),
                order_reason="no transaction was ever seen", evidence=v.evidence)
        if v.kind == "unresolved" and leg["state"] == "prompted" and leg["answered_at"] is None:
            age = float(self.clock()) - (leg["prompted_at"] or 0)
            if age > 900:      # answer never arrived: the wallet may still have sent something
                return self.ledger.resolve_leg(
                    lid, expected_version=ver, to_state="ambiguous", resolution="answer_lost",
                    order_path=(S.AMBIGUOUS,) if swap else (), order_reason="wallet answer lost")
        return False

    def run_once(self, limit: int = 100,
                 should_continue: Callable[[], bool] | None = None) -> Report:
        """One pass, one bounded corroboration session. `should_continue` (optional) lets a
        supervisor bound the pass by wall time or a shutdown request; legs not reached wait for the
        next pass (nothing is half-applied: each leg is its own atomic ledger transaction)."""
        rep = Report()
        sess = self.corro.session(calls=self.rpc_budget, seconds=self.rpc_seconds)
        for leg in self.ledger.active_legs(limit=limit):
            if should_continue is not None and not should_continue():
                break
            try:
                v = self.classify(leg, sess)
                rep.add(v.kind)
                if v.kind in ("unknown", "conflict"):
                    rep.codes[v.reason] = rep.codes.get(v.reason, 0) + 1
                self.apply(leg, v)
            except BudgetExhausted:
                rep.add("budget_exhausted")
                break
            except (TradeRefused, LedgerRefused) as exc:
                rep.add("error")
                log.warning("reconcile of leg %s skipped: %s", leg.get("id"), exc)
        return rep


def timeline(leg: dict, *, quote_ready_at: float | None, included_at: float | None = None,
             settled_at: float | None = None) -> dict[str, float | None]:
    """Stage durations in seconds, each measured on its own (None until the stage has happened).
    `answer_to_seen_s` is when OUR reconciler first saw the transaction, so it is bounded below
    by the polling interval: it is an observation latency, not the node's acceptance time. No
    endpoint should be called "fastest" without same-workload measurements from the production
    host (docs/TRADING_SUBMISSION_RECONCILIATION.md)."""
    def gap(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else round(b - a, 3)
    return {
        "quote_to_prompt_s": gap(quote_ready_at, leg["prompted_at"]),
        "prompt_to_answer_s": gap(leg["prompted_at"], leg["answered_at"]),
        "answer_to_seen_s": gap(leg["answered_at"], leg["seen_at"]),
        "seen_to_included_s": gap(leg["seen_at"], included_at),
        "included_to_settled_s": gap(included_at, settled_at),
    }
