"""Wallet-managed order flow (issue #92): ledger-gated prompts and honest result handling.

The server prepares exact, bound requests; the user's own wallet signs and broadcasts. Nothing here
signs, holds keys, or broadcasts. A reported transaction hash is an acknowledgment that the wallet
claims to have sent something, never a fill: the order moves to `submitted` and stays unsettled
until receipt reconciliation (#94) proves otherwise."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..ledger.core import Actor, LedgerRefused, OrderLedger
from ..ledger.states import OrderState
from ..plans import UnsignedTradePlan, fingerprint
from .approvals import ApprovalPlanner
from .requests import WalletRequest, swap_request

log = logging.getLogger(__name__)

OUTCOMES = ("hash", "rejected", "error")
TXHASH = re.compile(r"^0x[0-9a-f]{64}$")
MAX_OPEN_REQUESTS = 2000


class FlowRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class _Entry:
    request: WalletRequest
    user_id: int
    session: str
    answered: tuple[str, str | None] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class RequestStore:
    """Open wallet prompts, bound to (user, session). Bounded, expiring, in-memory by design:
    a lost prompt fails closed (the swap leg is durable in the ledger and cannot be re-issued)."""

    def __init__(self, clock: Callable[[], float] = time.time, limit: int = MAX_OPEN_REQUESTS):
        self._clock, self._limit = clock, limit
        self._items: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _sweep(self) -> None:
        now = self._clock()
        for k in [k for k, e in self._items.items() if now > e.request.expires_at + 900]:
            del self._items[k]

    def put(self, request: WalletRequest, user_id: int, session: str) -> None:
        with self._lock:
            self._sweep()
            if len(self._items) >= self._limit:
                raise FlowRefused("busy", "too many open wallet requests")
            self._items[request.id] = _Entry(request, user_id, session)

    def get(self, request_id: str, user_id: int, session: str) -> _Entry | None:
        with self._lock:
            e = self._items.get(request_id)
        if e is None or e.user_id != user_id or e.session != session:
            return None            # same answer for "unknown" and "someone else's"
        return e

    def open_for(self, order_id: str, kind_group: str, user_id: int, session: str
                 ) -> WalletRequest | None:
        now = self._clock()
        with self._lock:
            for e in self._items.values():
                r = e.request
                if (e.answered is None and e.user_id == user_id and e.session == session
                        and r.order_id == order_id and now < r.expires_at
                        and ("swap" if r.kind == "swap" else "approval") == kind_group):
                    return r
        return None


# Wallet-reported reasons are untrusted browser input. Only these codes are ever stored; any other
# text (URLs, keys, messages from a hostile page or extension) becomes "unspecified".
REASON_CODES = frozenset({"user_rejected", "disconnected", "unsupported", "chain_mismatch",
                          "timeout", "insufficient_funds", "unspecified"})


def _clean(reason: str | None) -> str | None:
    if reason is None or reason == "":
        return None
    return reason if reason in REASON_CODES else "unspecified"


class WalletFlow:
    def __init__(self, ledger: OrderLedger, planner: ApprovalPlanner, store: RequestStore | None
                 = None, *, clock: Callable[[], float] = time.time, ttl: float = 120.0) -> None:
        self.ledger, self.planner = ledger, planner
        self.clock, self.ttl = clock, ttl
        self.store = store or RequestStore(clock)

    # ------------------------------------------------------------------ guards
    def _order(self, user_id: int, order_id: str, plan: UnsignedTradePlan, state: OrderState):
        try:
            view = self.ledger.get_order(user_id, order_id)
        except LedgerRefused as r:
            raise FlowRefused(r.code, r.message) from None
        if (view.plan_id != plan.id or view.plan_digest != fingerprint(plan)
                or view.wallet != plan.wallet or view.chain_id != plan.chain_id):
            raise FlowRefused("plan_mismatch", "the order was not created from this plan")
        if view.state != state.value:
            raise FlowRefused("invalid_state", f"order is {view.state}, expected {state.value}")
        return view

    def _authorize(self, user_id: int, order_id: str, kind: str, r: WalletRequest) -> None:
        try:
            fresh = self.ledger.authorize_wallet_request(
                user_id, order_id, kind=kind, request_key=r.id, digest=r.digest)
        except LedgerRefused as e:
            raise FlowRefused(e.code, e.message) from None
        if not fresh:
            raise FlowRefused("duplicate", "this wallet request was already authorized")

    # ------------------------------------------------------------------ prompts
    def approval_prompt(self, *, user_id: int, session: str, order_id: str,
                        plan: UnsignedTradePlan) -> dict:
        self._order(user_id, order_id, plan, OrderState.REQUIRES_APPROVAL)
        again = self.store.open_for(order_id, "approval", user_id, session)
        if again is not None:                                  # double click / page reload
            return {"status": "pending", "request": again.public()}
        step = self.planner.next_step(plan, order_id)
        if step.request is None:
            return {"status": "satisfied", "allowance_wei": str(step.allowance_wei),
                    "notes": list(step.notes), "next": "requote"}
        self._authorize(user_id, order_id, "approval", step.request)
        self.store.put(step.request, user_id, session)
        return {"status": step.action, "allowance_wei": str(step.allowance_wei),
                "notes": list(step.notes), "request": step.request.public()}

    def swap_prompt(self, *, user_id: int, session: str, order_id: str, plan: UnsignedTradePlan,
                    revalidate: Callable[[UnsignedTradePlan], object]) -> dict:
        self._order(user_id, order_id, plan, OrderState.AWAITING_WALLET)
        again = self.store.open_for(order_id, "swap", user_id, session)
        if again is not None:
            return {"status": "pending", "request": again.public()}
        revalidate(plan)                         # raises: price/reserves/allowance/code moved
        r = swap_request(plan=plan, order_id=order_id, now=float(self.clock()), ttl=self.ttl)
        self._authorize(user_id, order_id, "swap", r)
        self.store.put(r, user_id, session)
        return {"status": "ready", "request": r.public()}

    # ------------------------------------------------------------------ answers
    def report(self, *, user_id: int, session: str, request_id: str, outcome: str,
               tx_hash: str | None = None, reason: str | None = None) -> dict:
        if outcome not in OUTCOMES:
            raise FlowRefused("invalid_request", "unknown outcome")
        if outcome == "hash" and not (isinstance(tx_hash, str) and TXHASH.fullmatch(tx_hash)):
            raise FlowRefused("invalid_request", "malformed transaction hash")
        if outcome != "hash":
            tx_hash = None
        entry = self.store.get(request_id, user_id, session)
        if entry is None:
            raise FlowRefused("not_found", "unknown wallet request")
        with entry.lock:
            if entry.answered is not None:
                if entry.answered == (outcome, tx_hash):
                    return {"status": "duplicate", "outcome": outcome}
                raise FlowRefused("conflict", "this wallet request was already answered")
            entry.answered = (outcome, tx_hash)
            return self._apply(entry, outcome, tx_hash, _clean(reason))

    def _apply(self, entry: _Entry, outcome: str, tx_hash: str | None, reason: str | None) -> dict:
        r, uid = entry.request, entry.user_id
        leg = "swap" if r.kind == "swap" else "approval"
        audit = {"hash": "sent", "rejected": "rejected", "error": "error"}[outcome]
        try:
            self.ledger.record_wallet_result(uid, r.order_id, request_key=r.id, outcome=audit,
                                             tx_hash=tx_hash, reason=reason)
            if outcome == "hash":
                self.ledger.record_attempt(r.order_id, kind=leg, tx_hash=tx_hash)
                if leg == "swap":
                    self.ledger.transition(r.order_id, OrderState.SUBMITTED, actor=Actor.system(),
                                           request_key=f"submitted:{r.id}")
                return {"status": "acknowledged", "leg": leg, "tx_hash": tx_hash,
                        "settled": False,
                        "message": "The wallet reported a transaction hash. This is not a fill; "
                                   "the order stays unsettled until the receipt is verified."}
            if outcome == "rejected":
                if leg == "swap":
                    self.ledger.transition(r.order_id, OrderState.CANCELLED, actor=Actor.system(),
                                           request_key=f"rejected:{r.id}",
                                           reason="wallet_rejected")
                return {"status": "rejected", "leg": leg, "settled": False}
        except LedgerRefused as e:
            log.error("ledger refused wallet answer order=%s leg=%s code=%s", r.order_id, leg, e.code)
            # the wallet may really have sent it: report honestly, never pretend it did not
            raise FlowRefused(e.code, e.message) from None
        # "error": the wallet failed without a hash. We cannot know nothing was sent, so the
        # order is left as-is for chain reconciliation; no automatic second prompt is possible.
        return {"status": "unknown", "leg": leg, "settled": False,
                "message": "The wallet reported an error. If a transaction was sent it will be "
                           "found by reconciliation; do not retry from another device."}
