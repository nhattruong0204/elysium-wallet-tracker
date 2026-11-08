"""Owner-audited recovery of wallet legs stuck `ambiguous` / `orphaned` (#95 finding F-02).

A leg stays `ambiguous` (and keeps the wallet+chain lock) or `orphaned` when reconciliation cannot
prove what became of the transaction. This module lets an OWNER look at the evidence read-only
and, only when it is provable and the owner confirms with a reason, record a correction.

Safety rules (each one is covered by tests/test_wallet_recovery*.py):

* Diagnosis is read-only: it only calls chain READ methods and never writes to the ledger.
* A correction needs evidence from at least TWO distinct, configured sources, ALL of which must be
  healthy, fresh, on the same canonical chain at one anchor block and in agreement. One stale,
  failing or disagreeing source => no correction.
* Only two corrections exist, both terminal for the LEG and neither touches a receipt, fill,
  hash or nonce:
    - `superseded`: another, different transaction from the same wallet occupies the nonce of this
      request and is `finality_confirmations` deep in the canonical chain. The request can never
      be mined, whatever the wallet does later.
    - `abandoned` (swaps only): the swap's on-chain deadline has passed (+ margin, measured on the
      sources' own block timestamps), no transaction of the wallet since the prompt matches the
      request, and the wallet has nothing queued. A late broadcast could then only revert.
  Anything else (an approval/reset with no deadline, a transaction that is pending, a nonce not
  yet consumed, a shallow replacement, a source down or disagreeing) stays BLOCKED, with an
  escalation procedure. A leg that is on chain (confirmed / reverted / replaced by an identical
  request) is NOT corrected here at all: the reconciliation workers record it themselves.
* Two steps: `prepare` shows the diagnosis and issues a one-time code bound to the owner, the leg,
  its version and the decisive facts (5 minutes, kept in memory, lost on restart). `confirm` needs
  the code AND a reason (10-190 printable characters), re-collects FRESH evidence, requires the
  same decisive facts, and only then calls `OrderLedger.apply_leg_recovery`.
* Nothing here can sign, broadcast, prompt a wallet, request an approval or create an order: the
  only chain access is the read allowlist and the only write is the ledger method above."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

from ..ledger.core import Actor, LedgerRefused, OrderLedger
from .approvals import Head
from .reconcile import critical, expected_of, swap_deadline
from .requests import tx_mismatches

log = logging.getLogger(__name__)

SCHEMA = 1
MIN_SOURCES = 2
MAX_HEAD_SKEW = 3              # blocks the sources' heads may differ by
MAX_HEAD_AGE_S = 60            # a source whose newest block is older than this is stale
DEADLINE_MARGIN_S = 600        # extra time after a swap deadline, on the sources' block clock
MAX_BISECT_SPAN = 200_000
MAX_NONCES_SCANNED = 10
CODE_TTL_S = 300
MAX_BAD_CODES = 5
MAX_OUTSTANDING_CODES = 50
REASON_MIN, REASON_MAX = 10, 190
HASH = re.compile(r"^0x[0-9a-f]{64}$")

CONFIRMED, REVERTED, REPLACED = "confirmed", "reverted", "replaced"
EXTERNAL, UNAVAILABLE, UNKNOWN, EXPIRED = ("nonce_consumed_externally", "rpc_unavailable",
                                           "unknown", "expired")
RECOVERABLE = ("ambiguous", "orphaned")
ORDER_STATES_OK = ("ambiguous", "orphaned", "awaiting_wallet", "submitted", "cancelled")


class ChainSource(Protocol):
    """Read-only. Implementations (ExecutionRpc) refuse every non-read method."""
    def head(self) -> Head: ...
    def transaction(self, tx_hash: str) -> dict | None: ...
    def receipt(self, tx_hash: str) -> dict | None: ...
    def block_header(self, number: int) -> dict | None: ...
    def block_transactions(self, number: int) -> list[dict]: ...
    def tx_count(self, address: str, tag: str) -> int: ...


class RecoveryRefused(Exception):
    """A recovery step was refused. `code` is stable and safe to show; nothing was changed."""

    def __init__(self, code: str, message: str, diagnosis: Diagnosis | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.diagnosis = code, message, diagnosis


class _Unavailable(Exception):
    pass


class _Disagree(Exception):
    pass


@dataclass(frozen=True)
class Diagnosis:
    leg_id: int
    order_id: str
    leg_state: str
    leg_version: int
    order_state: str
    kind: str
    wallet: str
    chain_id: int
    tx_hash: str | None
    final_tx_hash: str | None
    nonce: int | None
    nonce_hint: int | None
    prompt_block: int | None
    last_evidence: str | None
    seen_at: float | None
    category: str
    reason: str                     # stable machine code
    detail: str                     # one human sentence
    corroborated: bool              # two+ independent sources, all healthy and agreeing
    auto: bool                      # the reconciliation workers will settle it by themselves
    proposal: str | None            # "superseded" | "abandoned" | None (=> stays blocked)
    blocked_reason: str | None
    decision_key: str | None
    evidence: dict[str, Any]
    evidence_digest: str
    escalation: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prepared:
    diagnosis: Diagnosis
    code: str | None
    expires_at: float | None


@dataclass(frozen=True)
class _Live:
    name: str
    src: ChainSource
    head: Head


@dataclass
class _Snapshot:
    configured: int
    live: list[_Live]
    failed: list[dict]
    anchor: Head | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and self.anchor is not None


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _qty(v: Any) -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return int(str(v), 16)


def _header_of(number: int, src: ChainSource) -> dict | None:
    return src.block_header(number)


def _name(src: Any, i: int) -> str:
    n = getattr(src, "host", None) or getattr(src, "name", None) or f"source{i + 1}"
    return str(n)[:80]


# ----------------------------------------------------------------------------- confirmation codes
@dataclass(frozen=True)
class _Pending:
    owner: int
    leg_id: int
    decision_key: str
    expires_at: float
    code_digest: str


class _Codes:
    """One-time confirmation codes. In memory on purpose: a restart voids every outstanding code,
    which is the safe direction. Wrong guesses are rate limited per (owner, leg)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock, self._lock = clock, threading.Lock()
        self._by_digest: dict[str, _Pending] = {}
        self._bad: dict[tuple[int, int], tuple[int, float]] = {}

    @staticmethod
    def digest(code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()

    def issue(self, owner: int, leg_id: int, decision_key: str) -> tuple[str, float]:
        now = float(self.clock())
        with self._lock:
            self._by_digest = {d: p for d, p in self._by_digest.items() if p.expires_at > now}
            self._by_digest = {d: p for d, p in self._by_digest.items()
                               if (p.owner, p.leg_id) != (owner, leg_id)}   # newest code only
            if len(self._by_digest) >= MAX_OUTSTANDING_CODES:
                raise RecoveryRefused("busy", "too many outstanding confirmation codes")
            code = secrets.token_hex(5)
            exp = now + CODE_TTL_S
            self._by_digest[self.digest(code)] = _Pending(owner, leg_id, decision_key, exp,
                                                          self.digest(code))
            return code, exp

    def take(self, owner: int, leg_id: int, code: str) -> _Pending:
        """Consume a code (single use, even if the later checks fail)."""
        now = float(self.clock())
        key = (owner, leg_id)
        with self._lock:
            count, until = self._bad.get(key, (0, 0.0))
            if count >= MAX_BAD_CODES and now < until:
                raise RecoveryRefused("too_many_attempts", "too many wrong codes; prepare again later")
            if not isinstance(code, str) or not re.fullmatch(r"[0-9a-f]{10}", code):
                self._bad[key] = (count + 1, now + CODE_TTL_S)
                raise RecoveryRefused("bad_code", "unknown or expired confirmation code")
            p = self._by_digest.pop(self.digest(code), None)
            if (p is None or p.expires_at <= now
                    or not hmac.compare_digest(p.code_digest, self.digest(code))
                    or (p.owner, p.leg_id) != key):
                self._bad[key] = (count + 1, now + CODE_TTL_S)
                raise RecoveryRefused("bad_code", "unknown or expired confirmation code")
            self._bad.pop(key, None)
            return p


# ----------------------------------------------------------------------------- the service
class LegRecovery:
    def __init__(self, ledger: OrderLedger, sources: Sequence[ChainSource], *,
                 finality_confirmations: int = 12, clock: Callable[[], float] = time.time,
                 deadline_margin_s: int = DEADLINE_MARGIN_S) -> None:
        if not 1 <= finality_confirmations <= 256:
            raise ValueError("finality_confirmations must be in 1..256")
        self.ledger, self.clock, self.finality = ledger, clock, finality_confirmations
        self.margin = deadline_margin_s
        self.sources = [(_name(s, i), s) for i, s in enumerate(sources)]
        self._codes = _Codes(clock)
        self._busy = threading.Lock()      # one diagnosis at a time: bounded RPC load

    # ------------------------------------------------------------------ public API
    def stuck(self, actor: Actor, *, limit: int = 20) -> list[dict]:
        """Owner-only, no chain reads: the legs that need attention."""
        return self.ledger.recoverable_legs(actor, limit=limit)

    def diagnose(self, actor: Actor, leg_id: int) -> Diagnosis:
        """Read-only. Issues no code and changes nothing."""
        return self._diagnose(self.ledger.leg_for_recovery(actor, leg_id))

    def prepare(self, actor: Actor, leg_id: int) -> Prepared:
        d = self.diagnose(actor, leg_id)
        if d.proposal is None or d.decision_key is None:
            return Prepared(d, None, None)
        code, exp = self._codes.issue(int(actor.id or 0), leg_id, d.decision_key)
        return Prepared(d, code, exp)

    def confirm(self, actor: Actor, leg_id: int, code: str, reason: str):
        leg = self.ledger.leg_for_recovery(actor, leg_id)       # owner check + existence
        why = self._reason(reason)
        digest_hint: str | None = None
        try:
            pend = self._codes.take(int(actor.id or 0), leg_id, code)
            d = self._diagnose(leg)
            digest_hint = d.evidence_digest
            if d.proposal is None or d.decision_key is None:
                raise RecoveryRefused(d.blocked_reason or "no_safe_resolution",
                                      "safe resolution cannot be proven; the leg stays blocked", d)
            if d.decision_key != pend.decision_key:
                raise RecoveryRefused("evidence_changed",
                                      "the chain evidence changed since the diagnosis; diagnose again", d)
            key = "recovery-" + self._codes.digest(code)[:32]
            res = self.ledger.apply_leg_recovery(
                actor, leg_id, expected_version=d.leg_version, action=d.proposal,
                category=d.category, reason=why, evidence=d.evidence, request_key=key)
        except RecoveryRefused as exc:
            self._audit_refusal(actor, leg_id, code, exc.code, digest_hint)
            raise
        except LedgerRefused as exc:
            self._audit_refusal(actor, leg_id, code, exc.code, digest_hint)
            raise RecoveryRefused(exc.code, str(exc)) from None
        return res

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _reason(reason: str) -> str:
        r = reason.strip() if isinstance(reason, str) else ""
        if not REASON_MIN <= len(r) <= REASON_MAX or not r.isprintable():
            raise RecoveryRefused("reason_required",
                                  f"give a reason of {REASON_MIN}-{REASON_MAX} printable characters")
        return r

    def _audit_refusal(self, actor: Actor, leg_id: int, code: str, why: str,
                       digest: str | None) -> None:
        try:
            key = "refused-" + hashlib.sha256(f"{code}|{why}".encode()).hexdigest()[:32]
            self.ledger.record_recovery_refusal(actor, leg_id, request_key=key, code=why,
                                                evidence_digest=digest)
        except Exception:                                           # noqa: BLE001 - best effort
            log.warning("could not audit a refused recovery", exc_info=False)

    # ------------------------------------------------------------------ evidence
    def _snapshot(self) -> _Snapshot:
        snap = _Snapshot(configured=len(self.sources), live=[], failed=[])
        names = [n for n, _ in self.sources]
        if len(set(names)) != len(names):
            snap.problems.append("sources_not_independent")
        for name, src in self.sources:
            try:
                h = src.head()
                if not isinstance(h, Head) or not HASH.fullmatch(str(h.hash)):
                    raise _Unavailable("malformed head")
            except Exception as exc:                                # noqa: BLE001
                snap.failed.append({"name": name, "error": type(exc).__name__})
                continue
            snap.live.append(_Live(name, src, h))
        if snap.failed:
            snap.problems.append("source_unavailable")
        if len(self.sources) < MIN_SOURCES:
            snap.problems.append("needs_two_independent_sources")
        if not snap.live:
            return snap
        now = float(self.clock())
        numbers = [x.head.number for x in snap.live]
        anchor_live = min(snap.live, key=lambda x: x.head.number)
        snap.anchor = anchor_live.head
        if max(numbers) - min(numbers) > MAX_HEAD_SKEW:
            snap.problems.append("heads_diverge")
        if any(not -5 <= now - x.head.timestamp <= MAX_HEAD_AGE_S for x in snap.live):
            snap.problems.append("stale_head")
        if "stale_head" not in snap.problems and "heads_diverge" not in snap.problems:
            try:
                hashes = {self._header_hash(x, snap.anchor.number) for x in snap.live}
            except _Unavailable:
                snap.problems.append("source_unavailable")
            else:
                if len(hashes) != 1 or None in hashes:
                    snap.problems.append("sources_disagree_on_chain")
        return snap

    @staticmethod
    def _header_hash(x: _Live, number: int) -> str | None:
        try:
            h = x.src.block_header(number)
        except Exception as exc:                                    # noqa: BLE001
            raise _Unavailable(type(exc).__name__) from None
        return None if h is None else str(h.get("hash", "")).lower()

    def _each(self, snap: _Snapshot, fn: Callable[[ChainSource], Any]) -> list[Any]:
        out = []
        for x in snap.live:
            try:
                out.append(fn(x.src))
            except Exception as exc:                                # noqa: BLE001
                raise _Unavailable(f"{x.name}:{type(exc).__name__}") from None
        return out

    @staticmethod
    def _agree(values: list[Any], what: str) -> Any:
        if len({_canon(v) for v in values}) != 1:
            raise _Disagree(what)
        return values[0]

    def _locate_one(self, x: _Live, wallet: str, n: int, lo: int, hi: int) -> tuple[dict, int, str]:
        """The transaction of `wallet` with nonce `n` (state-based bisection on the sender's
        nonce, so a long window costs ~log2 reads): (tx, block number, block hash)."""
        def cnt(b: int) -> int:
            return x.src.tx_count(wallet, hex(b))
        if cnt(lo) > n:
            raise _Disagree("nonce_consumed_before_the_request")
        if cnt(hi) <= n:
            raise _Disagree("nonce_not_consumed")
        left, right = lo, hi
        while right - left > 1:
            mid = (left + right) // 2
            if cnt(mid) > n:
                right = mid
            else:
                left = mid
        hdr = x.src.block_header(right)
        if hdr is None:
            raise _Unavailable("header_missing")
        for tx in x.src.block_transactions(right):
            try:
                same = (str(tx.get("from", "")).lower() == wallet and _qty(tx.get("nonce")) == n
                        and HASH.fullmatch(str(tx.get("hash", "")).lower()))
            except ValueError:
                continue
            if same:
                bh = str(hdr.get("hash", "")).lower()
                if str(tx.get("blockHash", bh)).lower() != bh:
                    raise _Disagree("block_hash_mismatch")
                return tx, right, bh
        raise _Disagree("consuming_transaction_not_in_block")

    def _locate(self, snap: _Snapshot, wallet: str, n: int, lo: int | None) -> tuple[dict, int, str]:
        assert snap.anchor is not None
        a = snap.anchor.number
        lo = max(0, a - MAX_BISECT_SPAN) if lo is None else max(lo, a - MAX_BISECT_SPAN)
        found = []
        for x in snap.live:
            try:
                found.append(self._locate_one(x, wallet, n, lo, a))
            except (_Disagree, _Unavailable):
                raise
            except Exception as exc:                                # noqa: BLE001
                raise _Unavailable(f"{x.name}:{type(exc).__name__}") from None
        keys = {(str(t["hash"]).lower(), b, bh) for t, b, bh in found}
        if len(keys) != 1:
            raise _Disagree("sources_disagree_on_consuming_transaction")
        return found[0]

    # ------------------------------------------------------------------ diagnosis
    def _diagnose(self, leg: dict) -> Diagnosis:
        if not self._busy.acquire(blocking=False):
            raise RecoveryRefused("busy", "another recovery diagnosis is running; try again shortly")
        try:
            return self._diagnose_locked(leg)
        finally:
            self._busy.release()

    def _diagnose_locked(self, leg: dict) -> Diagnosis:
        snap: _Snapshot | None = None
        proposal: str | None = None
        facts: dict[str, Any] = {}
        decisive: dict[str, Any] = {}
        auto = False
        blocked: str | None = None
        try:
            if leg["state"] not in RECOVERABLE:
                category, reason, detail = (UNKNOWN, "leg_not_stuck",
                                            f"the leg is {leg['state']}: nothing to recover")
                blocked = f"leg_not_stuck:{leg['state']}"
            elif leg["recoveries"]:
                category, reason, detail = UNKNOWN, "already_recovered", "a recovery was already recorded"
                blocked = "already_recovered"
            else:
                snap = self._snapshot()
                category, reason, detail, proposal, auto, facts, decisive = self._classify(leg, snap)
        except _Unavailable as exc:
            category, reason, detail = (UNAVAILABLE, "chain_source_unavailable",
                                        "a chain source did not answer: " + str(exc)[:80])
            proposal, auto = None, False
        except _Disagree as exc:
            category, reason, detail = (UNKNOWN, str(exc), "chain sources disagree or the evidence is inconsistent")
            proposal, auto = None, False
        if proposal is not None:
            if snap is None or not snap.ok:
                proposal = None
            elif leg["kind"] == "swap" and leg["order_state"] not in ORDER_STATES_OK:
                proposal, blocked = None, "order_has_a_receipt_state"
        if proposal is None and blocked is None and not auto:
            blocked = self._blocked_reason(snap, reason)
        corroborated = bool(snap and snap.ok)
        evidence = self._evidence(leg, snap, category, reason, proposal, facts)
        digest = hashlib.sha256(_canon(evidence).encode()).hexdigest()
        decision = None
        if proposal is not None:
            decision = hashlib.sha256(_canon({
                "leg": leg["id"], "version": leg["version"], "state": leg["state"],
                "action": proposal, "category": category, "decisive": decisive}).encode()).hexdigest()
        return Diagnosis(
            leg_id=leg["id"], order_id=leg["order_id"], leg_state=leg["state"],
            leg_version=leg["version"], order_state=leg["order_state"], kind=leg["kind"],
            wallet=leg["wallet"], chain_id=leg["chain_id"], tx_hash=leg["tx_hash"],
            final_tx_hash=leg["final_tx_hash"],
            nonce=None if leg["nonce"] is None else int(leg["nonce"]),
            nonce_hint=None if leg["nonce_hint"] is None else int(leg["nonce_hint"]),
            prompt_block=leg["prompt_block"], last_evidence=leg["resolution"],
            seen_at=leg["seen_at"], category=category, reason=reason, detail=detail,
            corroborated=corroborated, auto=auto, proposal=proposal, blocked_reason=blocked,
            decision_key=decision, evidence=evidence, evidence_digest=digest,
            escalation=() if proposal is not None or auto else escalation(leg, category, blocked))

    @staticmethod
    def _blocked_reason(snap: _Snapshot | None, reason: str) -> str:
        if snap is not None and snap.problems:
            return snap.problems[0]
        return reason

    def _evidence(self, leg: dict, snap: _Snapshot | None, category: str, reason: str,
                  proposal: str | None, facts: dict) -> dict[str, Any]:
        srcs: list[dict[str, Any]] = []
        anchor = {"number": 0, "hash": "0x" + "0" * 64, "timestamp": 0}
        if snap is not None:
            for x in snap.live:
                srcs.append({"name": x.name, "head_number": x.head.number, "head_hash": x.head.hash,
                             "head_timestamp": x.head.timestamp})
            for f in snap.failed:
                srcs.append({"name": f["name"], "error": f["error"]})
            if snap.anchor is not None:
                anchor = {"number": snap.anchor.number, "hash": snap.anchor.hash,
                          "timestamp": snap.anchor.timestamp}
            for k, v in facts.get("per_source", {}).items():
                for s in srcs:
                    if s["name"] == k:
                        s.update(v)
        return {
            "schema": SCHEMA, "action": proposal, "category": category, "reason": reason,
            "anchor": anchor, "sources": srcs,
            "problems": list(snap.problems) if snap else [],
            "leg": {"id": leg["id"], "version": leg["version"], "state": leg["state"],
                    "kind": leg["kind"], "wallet": leg["wallet"], "chain_id": leg["chain_id"],
                    "tx_hash": leg["tx_hash"], "final_tx_hash": leg["final_tx_hash"],
                    "nonce": None if leg["nonce"] is None else int(leg["nonce"]),
                    "nonce_hint": None if leg["nonce_hint"] is None else int(leg["nonce_hint"]),
                    "prompt_block": leg["prompt_block"], "order_state": leg["order_state"]},
            "facts": {k: v for k, v in facts.items() if k != "per_source"},
        }

    # ------------------------------------------------------------------ classification
    def _classify(self, leg: dict, snap: _Snapshot):
        """-> (category, reason, detail, proposal, auto, facts, decisive)"""
        facts: dict[str, Any] = {"per_source": {}}
        if not snap.live:
            return (UNAVAILABLE, "chain_source_unavailable", "no chain source answered",
                    None, False, facts, {})
        assert snap.anchor is not None
        exp = expected_of(leg)
        wallet, anchor = leg["wallet"], snap.anchor
        h = leg["final_tx_hash"] or leg["tx_hash"]

        # ---- 1. is the recorded transaction on chain?  (presence from ANY healthy source counts)
        if h:
            views = self._each(snap, lambda s: (s.transaction(h), s.receipt(h)))
            seen = [(t, r) for t, r in views if t is not None]
            if seen:
                txs = {(str(t.get("from", "")).lower(), str(t.get("to", "")).lower(),
                        str(t.get("input", "")).lower(), str(t.get("value", ""))) for t, _ in seen}
                if len(txs) > 1:
                    raise _Disagree("sources_disagree_on_transaction")
                tx = seen[0][0]
                rcs = [r for _, r in seen if r is not None]
                bad = critical(tx_mismatches(exp, tx, h))
                if not rcs:
                    return (UNKNOWN, "transaction_pending", "the transaction is known but not mined yet",
                            None, False, facts | {"tx_hash": h}, {})
                rc = rcs[0]
                incomplete = len(seen) != len(views) or len(rcs) != len(views)
                if len({(str(r.get("blockHash", "")).lower(), str(r.get("blockNumber")),
                         str(r.get("status"))) for r in rcs}) != 1:
                    raise _Disagree("sources_disagree_on_receipt")
                number = _qty(rc.get("blockNumber"))
                bh = str(rc.get("blockHash", "")).lower()
                canon = self._each(snap, partial(_header_of, number))
                canonical = all(c is not None and str(c.get("hash", "")).lower() == bh for c in canon)
                conf = anchor.number - number + 1
                facts |= {"tx_hash": h, "block_number": number, "block_hash": bh,
                          "canonical_in_all_sources": canonical, "confirmations": conf,
                          "mismatches": bad}
                if not bad:
                    ok = str(rc.get("status")) == "0x1"
                    return ((CONFIRMED if ok else REVERTED),
                            "mined_and_matches_request",
                            "the transaction is on the canonical chain; the receipt worker records it "
                            "(no correction is needed or allowed)",
                            None, True, facts, {})
                # the wallet reported a hash whose transaction is NOT the reviewed request
                if incomplete:
                    raise _Disagree("sources_disagree_on_transaction")
                if canonical and conf >= self.finality and str(rc.get("status")) in ("0x0", "0x1"):
                    decisive = {"consuming": h, "block_hash": bh}
                    return (EXTERNAL, "reported_hash_is_a_different_transaction",
                            "the wallet executed a different transaction than the one reviewed; its "
                            "nonce is consumed and final, so the reviewed request was not executed",
                            "superseded", False, facts | {"consuming_tx": {
                                "hash": h, "block_number": number, "block_hash": bh,
                                "confirmations": conf, "mismatches": bad}}, decisive)
                return (EXTERNAL, "different_transaction_not_final_yet",
                        f"a different transaction is mined but only {conf}/{self.finality} deep",
                        None, False, facts, {})

        # ---- 2. nothing of ours on chain: look at the wallet's nonce
        counts = self._each(snap, lambda s: (s.tx_count(wallet, hex(anchor.number)),
                                             s.tx_count(wallet, "latest"),
                                             s.tx_count(wallet, "pending")))
        at_anchor = self._agree([c[0] for c in counts], "sources_disagree_on_wallet_nonce")
        for x, c in zip(snap.live, counts, strict=True):
            facts["per_source"][x.name] = {"nonce_at_anchor": c[0], "latest_nonce": c[1],
                                           "pending_nonce": c[2]}
        facts["wallet_nonce_at_anchor"] = at_anchor
        n = leg["nonce"]
        hint = leg["nonce_hint"]
        category, reason, detail = UNKNOWN, "no_transaction_evidence", "no transaction of this request was found"
        if n is not None:
            n = int(n)
            if at_anchor > n:
                tx, number, bh = self._locate(snap, wallet, n, leg["prompt_block"])
                th = str(tx["hash"]).lower()
                bad = critical(tx_mismatches(exp, tx, None))
                conf = anchor.number - number + 1
                cons = {"hash": th, "block_number": number, "block_hash": bh, "nonce": n,
                        "confirmations": conf, "mismatches": bad}
                if not bad:
                    return (REPLACED, "replaced_by_identical_request",
                            "an identical request was mined under another hash (speed-up); the "
                            "reconciler records it (no correction is needed)",
                            None, True, facts | {"consuming_tx": cons}, {})
                if conf >= self.finality:
                    return (REPLACED, "replaced_by_different_transaction",
                            "the nonce of this request is consumed by a different, final transaction; "
                            "this request can never be mined",
                            "superseded", False, facts | {"consuming_tx": cons},
                            {"consuming": th, "block_hash": bh, "nonce": n})
                return (REPLACED, "replacement_not_final_yet",
                        f"replaced by a different transaction only {conf}/{self.finality} deep",
                        None, False, facts | {"consuming_tx": cons}, {})
            category, reason = UNKNOWN, "nonce_not_consumed"
            detail = "the request's nonce is still free: the transaction could still be mined"
        elif hint is not None and at_anchor > int(hint):
            category, reason = EXTERNAL, "hinted_nonce_used_by_other_transaction"
            detail = ("the nonce the wallet had pending at the prompt was used by another "
                      "transaction; that does not prove where this request went")

        # ---- 3. a swap that can no longer execute (deadline passed)
        if leg["kind"] != "swap":
            return (category, reason, detail + "; approvals have no on-chain deadline, so a late "
                    "confirmation in the wallet cannot be excluded", None, False, facts, {})
        deadline = swap_deadline(leg)
        if deadline is None:
            return category, reason, detail + "; the swap deadline is unreadable", None, False, facts, {}
        anchor_ts = min(x.head.timestamp for x in snap.live)
        facts |= {"swap_deadline": deadline, "deadline_margin_s": self.margin,
                  "anchor_timestamp": anchor_ts}
        if anchor_ts <= deadline + self.margin:
            return (category, reason, detail + f"; the swap deadline has not passed "
                    f"({deadline + self.margin - anchor_ts}s left incl. margin)",
                    None, False, facts, {})
        lo = leg["prompt_block"]
        if lo is None:
            return category, reason, detail + "; no prompt block recorded", None, False, facts, {}
        if any(c[2] != c[1] for c in counts):
            return (category, "wallet_has_pending_transactions",
                    detail + "; the wallet has queued transactions", None, False, facts, {})
        c0 = self._agree(self._each(snap, lambda s: s.tx_count(wallet, hex(lo))),
                         "sources_disagree_on_wallet_nonce")
        if at_anchor < c0:
            raise _Disagree("wallet_nonce_went_backwards")
        if at_anchor - c0 > MAX_NONCES_SCANNED:
            return (category, "too_many_wallet_transactions_to_check", detail, None, False, facts, {})
        checked = []
        for k in range(c0, at_anchor):
            tx, number, bh = self._locate(snap, wallet, k, lo)
            checked.append({"nonce": k, "hash": str(tx["hash"]).lower(), "block_number": number,
                            "block_hash": bh})
            if not critical(tx_mismatches(exp, tx, None)):
                return (CONFIRMED, "identical_request_found_on_chain",
                        "an identical request was mined; the reconciler records it", None, True,
                        facts | {"checked_nonces": checked}, {})
        facts |= {"nonce_at_prompt_block": c0, "checked_nonces": checked}
        return (EXPIRED, "no_transaction_found_and_deadline_passed",
                "no matching transaction exists and the swap deadline has passed: a late broadcast "
                "could only revert",
                "abandoned", False, facts,
                {"deadline": deadline, "from_nonce": c0, "to_nonce": at_anchor,
                 "checked": [c["hash"] for c in checked]})


# ----------------------------------------------------------------------------- escalation
def escalation(leg: dict, category: str, blocked: str | None) -> tuple[str, ...]:
    """What a human does when a safe resolution cannot be proven. None of these steps is
    performed by the server, and none of them releases the lock by itself."""
    steps = [
        "The leg STAYS BLOCKED; the wallet+chain lock is not released. Do not retry the order.",
        "Ask the user to open their wallet and check for a pending/queued request and for any "
        "recent activity of this address; have them reject any request still waiting there.",
        "Look the address (and the hash, if any) up on the chain explorer independently.",
    ]
    if blocked in ("source_unavailable", "stale_head", "heads_diverge", "sources_disagree_on_chain",
                   "needs_two_independent_sources", "sources_not_independent"):
        steps.append("Fix the chain sources first (two independent endpoints, healthy and in step), "
                     "then diagnose again.")
    if leg["kind"] != "swap":
        steps.append("An approval/reset has no deadline: once the wallet confirms nothing is waiting "
                     "and the explorer shows nothing, the user can trade from ANOTHER wallet (the "
                     "lock is per wallet and chain). Any late approval is finite and router-bound.")
    else:
        steps.append("A swap becomes provably inert after its deadline: diagnose again then.")
    steps.append("If evidence of what happened exists (hash, explorer link), record it in the "
                 "incident ticket; the reconciler picks up on-chain facts by itself.")
    steps.append("If the lock must be lifted without proof, that is an out-of-band decision for the "
                 "security reviewer; there is deliberately no command for it.")
    return tuple(steps)


# ----------------------------------------------------------------------------- rendering
def render_stuck(legs: list[dict], esc: Callable[[str], str] = lambda x: x) -> str:
    if not legs:
        return "No wallet legs are stuck."
    lines = ["<b>Stuck wallet legs</b> (diagnose: /wallet_recover &lt;leg id&gt;)"]
    for g in legs:
        lines.append(f"#{g['id']} {esc(g['kind'])} {esc(g['state'])} order {esc(g['order_state'])} "
                     f"wallet {esc(g['wallet'][:8])}… ({esc(str(g.get('resolution')))})")
    return "\n".join(lines)


def render_diagnosis(d: Diagnosis, esc: Callable[[str], str] = lambda x: x,
                     prepared: Prepared | None = None) -> str:
    ev = d.evidence
    lines = [
        f"<b>Leg #{d.leg_id}</b> {esc(d.kind)} — leg {esc(d.leg_state)}, order {esc(d.order_state)}",
        f"wallet <code>{esc(d.wallet)}</code> chain {d.chain_id}",
        f"tx {esc(str(d.tx_hash))} final {esc(str(d.final_tx_hash))} nonce {d.nonce} "
        f"(hint {d.nonce_hint}) prompt block {d.prompt_block}",
        f"last evidence: {esc(str(d.last_evidence))}",
        f"<b>diagnosis: {esc(d.category)}</b> — {esc(d.reason)}",
        esc(d.detail),
        f"anchor block {ev['anchor']['number']}; sources: "
        + ", ".join(esc(s["name"]) + (f" (error {esc(s['error'])})" if "error" in s else "")
                    for s in ev["sources"]),
        f"corroborated by 2+ independent sources: {'yes' if d.corroborated else 'NO'}",
    ]
    if d.auto:
        lines.append("No correction needed: the reconciliation workers will record this themselves.")
    elif d.proposal is not None:
        lines.append(f"<b>Provable resolution: {esc(d.proposal)}</b> (leg only; no receipt, fill or "
                     "hash is changed; the order is cancelled; no transaction is sent).")
        if prepared is not None and prepared.code:
            lines.append("To apply, as owner in this private chat within 5 minutes:\n"
                         f"<code>/wallet_resolve {d.leg_id} {esc(prepared.code)} &lt;reason, 10-190 chars&gt;</code>")
    else:
        lines.append(f"<b>STAYS BLOCKED</b>: {esc(str(d.blocked_reason))}")
        lines.extend("• " + esc(s) for s in d.escalation)
    lines.append(f"evidence digest {d.evidence_digest[:16]}…")
    return "\n".join(lines)
