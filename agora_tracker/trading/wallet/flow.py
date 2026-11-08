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
from typing import Protocol

from ..ledger.core import LedgerRefused, OrderLedger
from ..ledger.legs import WALLET_REASONS, LegSpec
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

    def known(self, request_id: str) -> bool:
        """True if this process issued the prompt (to someone). Used only to keep the session
        binding strict while the prompt is live."""
        with self._lock:
            return request_id in self._items

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


def _clean(reason: str | None) -> str | None:
    """Wallet-reported reasons are untrusted browser input: only known codes are stored."""
    if reason is None or reason == "":
        return None
    return reason if reason in WALLET_REASONS else "unspecified"


class NonceReader(Protocol):
    def pending_nonce(self, address: str) -> int: ...


class WalletFlow:
    def __init__(self, ledger: OrderLedger, planner: ApprovalPlanner, store: RequestStore | None
                 = None, *, clock: Callable[[], float] = time.time, ttl: float = 120.0,
                 nonce_reader: NonceReader | None = None) -> None:
        self.ledger, self.planner = ledger, planner
        self.clock, self.ttl = clock, ttl
        self.store = store or RequestStore(clock)
        # With a reader configured the wallet's pending nonce at prompt time is REQUIRED: without
        # it a lost answer could never be matched to chain evidence, so the prompt fails closed.
        self.nonce_reader = nonce_reader

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

    def _nonce_hint(self, wallet: str) -> int | None:
        if self.nonce_reader is None:
            return None
        try:
            n = self.nonce_reader.pending_nonce(wallet)
        except Exception:
            log.warning("pending nonce unavailable for a wallet prompt", exc_info=False)
            raise FlowRefused("nonce_unavailable", "cannot read the wallet nonce; try again") from None
        if type(n) is not int or n < 0:
            raise FlowRefused("nonce_unavailable", "cannot read the wallet nonce; try again")
        return n

    def _authorize(self, user_id: int, order_id: str, kind: str, r: WalletRequest) -> None:
        leg = LegSpec(kind=r.kind, wallet=r.sender, chain_id=r.chain_id, to=r.to,
                      value_wei=r.value_wei, data=r.data, gas_limit=r.gas_limit,
                      max_fee_wei=r.max_gas_fee_wei, nonce_hint=self._nonce_hint(r.sender),
                      prompt_block=r.block_number)
        try:
            fresh = self.ledger.authorize_wallet_request(
                user_id, order_id, kind=kind, request_key=r.id, digest=r.digest, leg=leg)
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
        # The in-memory prompt only adds a session check. After a restart it is gone, but the
        # durable leg (scoped to this user by the database) still accepts the wallet's answer, so
        # a late hash is never lost.
        entry = self.store.get(request_id, user_id, session)
        if entry is None and self.store.known(request_id):
            raise FlowRefused("not_found", "unknown wallet request")
        try:
            res = self.ledger.report_wallet_answer(user_id, request_id, outcome=outcome,
                                                   tx_hash=tx_hash, reason=_clean(reason))
        except LedgerRefused as e:
            log.error("ledger refused a wallet answer (%s) code=%s", outcome, e.code)
            raise FlowRefused(e.code, e.message) from None
        leg = res.leg["kind"]
        group = "swap" if leg == "swap" else "approval"
        if not res.applied:
            return {"status": "duplicate", "outcome": outcome, "leg": group}
        if res.order_conflict:
            log.error("order did not follow wallet hash order=%s", res.order.id)
        if outcome == "hash":
            return {"status": "acknowledged", "leg": group, "tx_hash": tx_hash, "settled": False,
                    "message": "The wallet reported a transaction hash. This is not a fill; "
                               "the order stays unsettled until the receipt is verified."}
        if outcome == "rejected":
            return {"status": "rejected", "leg": group, "settled": False}
        return {"status": "ambiguous", "leg": group, "settled": False,
                "message": "The wallet reported an error. It may still have sent the transaction: "
                           "the order is marked ambiguous until the chain shows what happened. "
                           "Do not retry from another device."}
