"""Submission reconciliation (issue #93): decide what became of a wallet request from chain
evidence only, and apply it to the ledger atomically.

Evidence ladder for one unsettled leg (`prompted`, `hash_reported` or `ambiguous`):

  hash known, found, payload matches      -> PENDING / MINED   (nonce recorded; #94 owns receipts)
  hash known, found, payload differs      -> MISMATCH          (ambiguous; never "the reviewed trade")
  hash known, absent, nonce known+consumed-> REPLACED_SAME (same payload, e.g. speed-up) or
                                             SUPERSEDED (other tx owns the nonce: provably not ours)
  hash absent/unknown, nonce hint consumed-> FOUND_BY_SCAN if an identical request sits at the hinted
                                             nonce, else UNRESOLVED (ambiguous, wallet stays blocked)
  nothing found, swap deadline long past,
  wallet nonce not consumed               -> ABANDONED (a swap cannot execute after its deadline)
  any source unreachable / disagreeing    -> UNRESOLVED/UNKNOWN: nothing is changed, nothing is retried

Absence is only believed when EVERY configured source is healthy and agrees; presence from any one
healthy source is positive evidence. Nothing here signs, replaces or rebroadcasts a transaction."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..ledger.core import LedgerRefused, OrderLedger
from ..ledger.states import OrderState
from ..paper import TradeRefused
from .approvals import Head
from .requests import WalletRequestError, decode_swap, tx_mismatches

log = logging.getLogger(__name__)

ADVISORY = frozenset({"gas_limit", "fee_cap"})      # the wallet's own fee UI may legitimately differ
SCAN_BLOCKS = 300
DEADLINE_MARGIN_S = 90
APPROVAL_ABANDON_AFTER_S = 3600


class TxRead(Protocol):
    def head(self) -> Head: ...
    def transaction(self, tx_hash: str) -> dict | None: ...
    def tx_count(self, address: str, tag: str) -> int: ...
    def block_transactions(self, number: int) -> list[dict]: ...


@dataclass(frozen=True)
class Verdict:
    kind: str                       # see module docstring
    reason: str
    nonce: int | None = None
    final_tx_hash: str | None = None
    seen: bool = False
    mismatches: tuple[str, ...] = ()


@dataclass
class Report:
    checked: int = 0
    counts: dict[str, int] = field(default_factory=dict)

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


class LegReconciler:
    def __init__(self, ledger: OrderLedger, sources: list[TxRead], *,
                 clock: Callable[[], float] = time.time, scan_blocks: int = SCAN_BLOCKS,
                 deadline_margin_s: int = DEADLINE_MARGIN_S,
                 approval_abandon_after_s: int = APPROVAL_ABANDON_AFTER_S) -> None:
        if not sources:
            raise ValueError("at least one chain source is required")
        self.ledger, self.sources, self.clock = ledger, sources, clock
        self.scan_blocks, self.margin = scan_blocks, deadline_margin_s
        self.approval_abandon_after_s = approval_abandon_after_s

    # ------------------------------------------------------------------ evidence
    def _lookup(self, tx_hash: str) -> tuple[str, dict | None]:
        """('found', tx) | ('absent', None) | ('unknown', None) | ('conflict', None)."""
        found: list[dict] = []
        healthy = 0
        for src in self.sources:
            try:
                tx = src.transaction(tx_hash)
            except TradeRefused:
                continue
            healthy += 1
            if tx is not None:
                found.append(tx)
        if found:
            if len({(str(t.get("from", "")).lower(), str(t.get("input", "")).lower(),
                     str(t.get("to", "")).lower(), str(t.get("value", ""))) for t in found}) > 1:
                return "conflict", None
            return "found", found[0]
        return ("absent", None) if healthy == len(self.sources) else ("unknown", None)

    def _counts(self, sender: str) -> tuple[int, int] | None:
        latest = pending = None
        for src in self.sources:
            try:
                a, b = src.tx_count(sender, "latest"), src.tx_count(sender, "pending")
            except TradeRefused:
                return None
            latest = a if latest is None else max(latest, a)       # the most advanced view wins
            pending = b if pending is None else max(pending, b)
        assert latest is not None and pending is not None
        return latest, pending

    def _scan(self, sender: str, nonce: int, since_block: int | None) -> tuple[str, dict | None]:
        """Find the mined transaction of `sender` with `nonce`: ('found', tx) | ('none', None) |
        ('unknown', None) when the window is incomplete or a source failed."""
        for src in self.sources:
            try:
                head = src.head().number
            except TradeRefused:
                continue
            lo = max(since_block or 0, head - self.scan_blocks)
            complete = (since_block or 0) >= head - self.scan_blocks
            try:
                for n in range(head, lo - 1, -1):
                    for tx in src.block_transactions(n):
                        try:
                            same = (str(tx.get("from", "")).lower() == sender
                                    and int(str(tx.get("nonce")), 16) == nonce)
                        except ValueError:
                            continue
                        if same:
                            return "found", tx
            except TradeRefused:
                continue
            return ("none", None) if complete else ("unknown", None)
        return "unknown", None

    # ------------------------------------------------------------------ decision
    def classify(self, leg: dict) -> Verdict:
        exp = expected_of(leg)
        sender, now = leg["wallet"], float(self.clock())
        tx_hash, nonce = leg["tx_hash"], leg["nonce"]
        nonce = int(nonce) if nonce is not None else None
        hint = int(leg["nonce_hint"]) if leg["nonce_hint"] is not None else None
        deadline = swap_deadline(leg)

        if tx_hash is not None:
            status, tx = self._lookup(tx_hash)
            if status == "conflict":
                return Verdict("mismatch", "sources_disagree_on_transaction")
            if status == "unknown":
                return Verdict("unknown", "chain_source_unavailable")
            if status == "found" and tx is not None:
                bad = critical(tx_mismatches(exp, tx, tx_hash))
                if bad:
                    return Verdict("mismatch", "payload_mismatch:" + ",".join(bad)[:150],
                                   mismatches=tuple(bad), seen=True)
                try:
                    n = int(str(tx.get("nonce")), 16)
                except ValueError:
                    return Verdict("mismatch", "malformed_nonce", seen=True)
                return Verdict("mined" if tx.get("blockNumber") else "pending", "found_by_hash",
                               nonce=n, seen=True)
            # hash absent from every healthy source
            if nonce is not None:
                return self._after_absence(leg, exp, sender, nonce, now, deadline, certain=True)
            return self._after_absence(leg, exp, sender, hint, now, deadline, certain=False)
        return self._after_absence(leg, exp, sender, hint, now, deadline, certain=False)

    def _after_absence(self, leg, exp, sender, nonce, now, deadline, *, certain: bool) -> Verdict:
        counts = self._counts(sender)
        if counts is None:
            return Verdict("unknown", "chain_source_unavailable")
        latest, pending = counts
        if nonce is not None and latest > nonce:
            where, tx = self._scan(sender, nonce, leg["prompt_block"])
            if where == "found" and tx is not None:
                other = critical(tx_mismatches(exp, tx, None))
                h = str(tx.get("hash", "")).lower()
                if not other:
                    return Verdict("replaced_same", "identical_request_at_nonce",
                                   nonce=int(str(tx.get("nonce")), 16), final_tx_hash=h, seen=True)
                if certain:
                    return Verdict("superseded", "nonce_used_by_other_transaction", nonce=nonce,
                                   final_tx_hash=h, seen=True)
                return Verdict("unresolved", "nonce_hint_used_by_other_transaction")
            return Verdict("unresolved", "nonce_consumed_but_not_found_in_scan_window")
        if pending > latest:
            return Verdict("unresolved", "unidentified_pending_transactions")
        # Nothing of this wallet's moved on chain since the prompt.
        if deadline is not None and now > deadline + self.margin:
            return Verdict("abandoned", "swap_deadline_passed_without_any_transaction")
        if (leg["kind"] != "swap" and leg["prompted_at"] is not None
                and now - leg["prompted_at"] > self.approval_abandon_after_s and leg["tx_hash"] is None):
            return Verdict("abandoned", "approval_never_sent")
        return Verdict("unresolved", "no_evidence_yet")

    # ------------------------------------------------------------------ application
    def apply(self, leg: dict, v: Verdict) -> bool:
        lid, ver = leg["id"], leg["version"]
        swap = leg["kind"] == "swap"
        S = OrderState
        if v.kind in ("pending", "mined", "replaced_same"):
            changed = (leg["state"] != "hash_reported" or leg["nonce"] is None
                       or leg["seen_at"] is None or (v.final_tx_hash and not leg["final_tx_hash"]))
            if not changed:
                return False
            return self.ledger.resolve_leg(
                lid, expected_version=ver, to_state="hash_reported", resolution=v.reason,
                nonce=v.nonce, final_tx_hash=v.final_tx_hash, seen=True,
                order_path=(S.SUBMITTED,) if swap else ())
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
                order_reason="nonce used by another transaction")
        if v.kind == "abandoned":
            return self.ledger.resolve_leg(
                lid, expected_version=ver, to_state="abandoned", resolution=v.reason,
                order_path=(S.CANCELLED,) if swap and leg["state"] == "prompted"
                else (S.AMBIGUOUS, S.CANCELLED) if swap else (),
                order_reason="no transaction was ever seen")
        if v.kind == "unresolved" and leg["state"] == "prompted" and leg["answered_at"] is None:
            age = float(self.clock()) - (leg["prompted_at"] or 0)
            if age > 900:      # answer never arrived: the wallet may still have sent something
                return self.ledger.resolve_leg(
                    lid, expected_version=ver, to_state="ambiguous", resolution="answer_lost",
                    order_path=(S.AMBIGUOUS,) if swap else (), order_reason="wallet answer lost")
        return False

    def run_once(self, limit: int = 100,
                 should_continue: Callable[[], bool] | None = None) -> Report:
        """One pass. `should_continue` (optional) lets a supervisor bound the pass by wall time or
        a shutdown request; legs not reached wait for the next pass (nothing is half-applied:
        each leg is its own atomic ledger transaction)."""
        rep = Report()
        for leg in self.ledger.active_legs(limit=limit):
            if should_continue is not None and not should_continue():
                break
            try:
                v = self.classify(leg)
                rep.add(v.kind)
                self.apply(leg, v)
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
