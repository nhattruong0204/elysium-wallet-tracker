"""Finite-approval planning (issue #92).

Given a reviewed *conditional* SELL plan, decide what the user's wallet must be asked to do before
a swap can exist, using only read-only chain access:

    allowance == required        -> nothing to sign; requote to get an executable plan
    allowance == 0               -> approve(ROUTER, exact amount)
    0 < allowance < required     -> approve(ROUTER, exact amount) if the token accepts it, else
                                    approve(ROUTER, 0) first (decided by *simulating* both)

Invariants (each has a test): the spender is the frozen V2 router whose runtime hash is re-checked
at the head; the amount is exactly the reviewed input amount and is never near uint256-max; the
planner never emits a swap and never implies one; a nonstandard `approve` return value is refused
rather than guessed; after the approval receipt the allowance must equal the requested amount
exactly (`confirm_allowance`)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from ..paper import TradeRefused
from ..plans import UnsignedTradePlan
from ..v2 import ALLOWANCE, ROUTER
from .requests import (
    MAX_APPROVAL_GAS,
    UNLIMITED_FLOOR,
    WalletRequest,
    approval_request,
    encode_approve,
)

log = logging.getLogger(__name__)

MAX_HEAD_AGE_S = 30
GAS_HEADROOM_BPS = 12_000          # estimate * 1.20
PRICE_HEADROOM_BPS = 12_500        # node gas price * 1.25 as the user's fee cap
WORD = re.compile(r"^0x(?:[0-9a-f]{64})?$")


class ApprovalError(TradeRefused):
    """Refused approval planning. `code` is stable for the UI and the audit trail."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ChainReverted(Exception):
    """The simulated call reverted (distinct from transport failure, which is TradeRefused)."""


@dataclass(frozen=True)
class Head:
    number: int
    hash: str
    timestamp: int


class ChainRead(Protocol):
    """The only chain access the wallet layer needs. Implementations must be read-only."""

    def head(self) -> Head: ...
    def call(self, to: str, data: str, block: str, sender: str | None = None) -> str: ...
    def estimate_gas(self, tx: dict, block: str) -> int: ...
    def gas_price(self) -> int: ...
    def code_hash(self, address: str, block: str) -> str: ...


@dataclass(frozen=True)
class ApprovalStep:
    action: str                       # "satisfied" | "approve" | "reset_then_approve"
    request: WalletRequest | None     # the NEXT prompt (reset when action is reset_then_approve)
    allowance_wei: int
    required_wei: int
    head: Head
    notes: tuple[str, ...] = ()


def _check_word(result: str, what: str) -> int:
    if not isinstance(result, str) or not WORD.fullmatch(result):
        raise ApprovalError("bad_return", f"{what}: malformed return data")
    if result == "0x":
        raise ApprovalError("bad_return", f"{what}: no return data")
    return int(result, 16)


def read_allowance(chain: ChainRead, token: str, owner: str, spender: str, block: str) -> int:
    data = ALLOWANCE + owner[2:].zfill(64) + spender[2:].zfill(64)
    return _check_word(chain.call(token, data, block), "allowance")


def _hex_block(n: int) -> str:
    return hex(n)


class ApprovalPlanner:
    def __init__(self, chain: ChainRead, *, clock=None, ttl: float = 120.0) -> None:
        import time
        self.chain = chain
        self.clock = clock or time.time
        self.ttl = ttl

    # -- validation --------------------------------------------------------------------------
    @staticmethod
    def _check_plan(plan: UnsignedTradePlan) -> int:
        if plan.side != "sell" or plan.simulation_status != "approval_required":
            raise ApprovalError("not_conditional", "only a conditional SELL needs an approval")
        n = plan.finite_approval_wei
        if (plan.spender != ROUTER or plan.snapshot.route.router != ROUTER
                or type(n) is not int or n != plan.amount_in_wei or not 0 < n < UNLIMITED_FLOOR):
            raise ApprovalError("bad_plan", "plan does not describe one exact finite approval")
        return n

    def _fresh_head(self) -> Head:
        h = self.chain.head()
        age = self.clock() - h.timestamp
        if not -2 <= age <= MAX_HEAD_AGE_S:
            raise ApprovalError("stale_head", "chain head is stale or in the future")
        return h

    def _verify_code(self, plan: UnsignedTradePlan, block: str) -> None:
        route = plan.snapshot.route
        for who, addr, want in (("router", ROUTER, route.router_hash),
                                ("token", plan.token, route.token_hash)):
            if self.chain.code_hash(addr, block) != want:
                raise ApprovalError("code_changed", f"{who} runtime code changed since review")

    def _simulate(self, plan: UnsignedTradePlan, spender: str, amount: int, block: str) -> int:
        """eth_call then estimate: both must succeed and the return value must be a clean `true`
        (or empty, for the common void-returning tokens). Returns the gas estimate."""
        data = encode_approve(spender, amount)
        out = self.chain.call(plan.token, data, block, sender=plan.wallet)
        if out != "0x":
            if not WORD.fullmatch(out or "") or int(out, 16) != 1:
                raise ApprovalError("nonstandard_return", "approve() returned something other than true")
        tx = {"from": plan.wallet, "to": plan.token, "value": "0x0", "data": data}
        gas = self.chain.estimate_gas(tx, block)
        if type(gas) is not int or gas <= 0:
            raise ApprovalError("bad_gas", "gas estimate is not usable")
        return gas

    def _try(self, plan, amount, block) -> int | None:
        try:
            return self._simulate(plan, ROUTER, amount, block)
        except ChainReverted:
            return None

    # -- planning ----------------------------------------------------------------------------
    def next_step(self, plan: UnsignedTradePlan, order_id: str) -> ApprovalStep:
        required = self._check_plan(plan)
        now = float(self.clock())
        if now >= plan.expires_at:
            raise ApprovalError("expired", "plan expired; requote")
        head = self._fresh_head()
        block = _hex_block(head.number)
        self._verify_code(plan, block)
        allowance = read_allowance(self.chain, plan.token, plan.wallet, ROUTER, block)
        notes: list[str] = []
        if allowance >= UNLIMITED_FLOOR:
            notes.append("existing_unlimited_allowance")
        if allowance >= required:
            return ApprovalStep("satisfied", None, allowance, required, head, tuple(notes))

        price = self.chain.gas_price()
        if type(price) is not int or price <= 0:
            raise ApprovalError("bad_gas", "gas price is not usable")
        cap = price * PRICE_HEADROOM_BPS // 10_000

        def build(kind: str, amount: int, gas: int) -> WalletRequest:
            limit = min(gas * GAS_HEADROOM_BPS // 10_000, MAX_APPROVAL_GAS)
            if gas > MAX_APPROVAL_GAS:
                raise ApprovalError("gas_too_high", "approve() needs unusually much gas")
            return approval_request(plan=plan, order_id=order_id, kind=kind, spender=ROUTER,
                                    amount=amount, gas_limit=limit, gas_price=cap, now=now,
                                    ttl=self.ttl, block_number=head.number, block_hash=head.hash)

        gas = self._try(plan, required, block)
        if gas is not None:
            return ApprovalStep("approve", build("approve", required, gas), allowance, required,
                                head, tuple(notes))
        if allowance > 0:
            gas0 = self._try(plan, 0, block)
            if gas0 is not None:
                notes.append("zero_reset_required")
                return ApprovalStep("reset_then_approve", build("reset", 0, gas0), allowance,
                                    required, head, tuple(notes))
        raise ApprovalError("approve_reverts", "the token rejects this approval in simulation")

    def confirm_allowance(self, plan: UnsignedTradePlan, kind: str) -> int:
        """After an approval receipt: the allowance must be EXACTLY what was requested (0 for a
        reset, the reviewed amount for an approve). Anything else is a cap mismatch and the flow
        stops: we never proceed on an allowance we did not ask for."""
        required = self._check_plan(plan)
        head = self._fresh_head()
        got = read_allowance(self.chain, plan.token, plan.wallet, ROUTER, _hex_block(head.number))
        want = 0 if kind == "reset" else required
        if got != want:
            log.warning("approval cap mismatch plan=%s kind=%s want=%s got=%s", plan.id, kind, want, got)
            raise ApprovalError("cap_mismatch", "on-chain allowance differs from the requested amount")
        return got
