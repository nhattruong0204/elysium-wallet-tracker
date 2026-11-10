"""Exact wallet requests: what the user's wallet is asked to do, and how to check it was done.

Everything here is pure (no I/O, no `eth_abi`), so every byte can be asserted in tests. Calldata is
built and parsed with fixed layouts: anything that does not match the expected layout exactly is
refused, so a wrong spender, target, amount, path, recipient or extra data can never pass as the
reviewed request.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass
from typing import Protocol

from ..paper import CHAIN, TradeRefused
from ..plans import UnsignedTradePlan, fingerprint

HEX_ADDR = re.compile(r"^0x[0-9a-f]{40}$")
HEX_HASH = re.compile(r"^0x[0-9a-f]{64}$")
UINT256 = 2**256

# Selectors are fixed constants (a test re-derives them with keccak in CI).
SEL_APPROVE = "0x095ea7b3"        # approve(address,uint256)
SEL_BUY = "0xb6f9de95"            # swapExactETHForTokensSupportingFeeOnTransferTokens
SEL_SELL = "0x791ac947"           # swapExactTokensForETHSupportingFeeOnTransferTokens

# A finite approval is at most the exact input amount of one reviewed trade. Anything near the
# "unlimited" sentinel is refused outright, whatever the caller says.
UNLIMITED_FLOOR = 2**128
MAX_APPROVAL_GAS = 200_000
KINDS = ("reset", "approve", "swap")


class WalletRequestError(TradeRefused):
    """A request that must not be shown to a wallet."""


def _addr(value: object, what: str) -> str:
    if not isinstance(value, str) or not HEX_ADDR.fullmatch(value) or int(value, 16) == 0:
        raise WalletRequestError(f"{what} must be a lowercase nonzero address")
    return value


def _word(n: int) -> str:
    if type(n) is not int or not 0 <= n < UINT256:
        raise WalletRequestError("value does not fit in uint256")
    return f"{n:064x}"


def _addr_word(a: str) -> str:
    return "0" * 24 + _addr(a, "address")[2:]


def encode_approve(spender: str, amount: int) -> str:
    """approve(spender, amount) with a finite amount. 0 is allowed only for a reset."""
    if type(amount) is not int or not 0 <= amount < UNLIMITED_FLOOR:
        raise WalletRequestError("approval amount is not a finite exact amount")
    return SEL_APPROVE + _addr_word(spender) + _word(amount)


def decode_approve(data: str) -> tuple[str, int]:
    if (not isinstance(data, str) or not data.startswith(SEL_APPROVE)
            or len(data) != 10 + 128 or not re.fullmatch(r"0x[0-9a-f]+", data)):
        raise WalletRequestError("not an exact approve(address,uint256) call")
    spender_word, amount_word = data[10:74], data[74:]
    if spender_word[:24] != "0" * 24:
        raise WalletRequestError("approve spender word is not a clean address")
    return _addr("0x" + spender_word[24:], "spender"), int(amount_word, 16)


def _words(body: str) -> list[int]:
    return [int(body[i:i + 64], 16) for i in range(0, len(body), 64)]


def decode_swap(data: str, side: str) -> dict:
    """Parse the only two swap calls we ever prepare, with their exact canonical layout."""
    if side == "buy":
        sel, nwords, head = SEL_BUY, 7, 4
    elif side == "sell":
        sel, nwords, head = SEL_SELL, 8, 5
    else:
        raise WalletRequestError("unknown side")
    if (not isinstance(data, str) or not data.startswith(sel)
            or len(data) != 10 + 64 * nwords or not re.fullmatch(r"0x[0-9a-f]+", data)):
        raise WalletRequestError("not the exact reviewed swap call")
    w = _words(data[10:])
    off = 32 * head
    amount_in = w[0] if side == "sell" else None
    base = 1 if side == "sell" else 0
    min_out, offset, to, deadline = w[base], w[base + 1], w[base + 2], w[base + 3]
    length, p0, p1 = w[head], w[head + 1], w[head + 2]
    if offset != off or length != 2 or to >> 160 or p0 >> 160 or p1 >> 160:
        raise WalletRequestError("swap calldata layout is not canonical")
    return {"amount_in": amount_in, "min_out": min_out, "to": f"0x{to:040x}",
            "deadline": deadline, "path": (f"0x{p0:040x}", f"0x{p1:040x}")}


@dataclass(frozen=True)
class WalletRequest:
    """One wallet prompt, frozen. `fingerprint(request)` is its binding digest."""
    id: str
    kind: str
    order_id: str
    plan_id: str
    plan_digest: str
    intent_id: str
    chain_id: int
    sender: str
    to: str
    value_wei: int
    data: str
    gas_limit: int | None
    gas_price_wei: int | None
    max_gas_fee_wei: int | None
    token: str
    spender: str | None
    amount_wei: int | None
    recipient: str | None
    path: tuple[str, ...]
    min_out_wei: int | None
    deadline: int | None
    block_number: int
    block_hash: str
    issued_at: float
    expires_at: float

    @property
    def digest(self) -> str:
        return fingerprint(self)

    def tx_object(self) -> dict:
        tx = {"from": self.sender, "to": self.to, "value": hex(self.value_wei), "data": self.data}
        if self.gas_limit is not None:
            tx["gas"] = hex(self.gas_limit)
        if self.gas_price_wei is not None:
            tx["gasPrice"] = hex(self.gas_price_wei)
        return tx

    def public(self) -> dict:
        """What the browser receives. Financial integers are decimal strings; no session data."""
        d = asdict(self)
        for k in ("value_wei", "gas_limit", "gas_price_wei", "max_gas_fee_wei", "amount_wei",
                  "min_out_wei"):
            if d[k] is not None:
                d[k] = str(d[k])
        d["path"] = list(self.path)
        d["digest"] = self.digest
        d["eip1193"] = {"method": "eth_sendTransaction", "params": [self.tx_object()]}
        d["chain_hex"] = hex(self.chain_id)
        return d


def _new_id() -> str:
    return secrets.token_urlsafe(18)


def check_self_consistent(r: WalletRequest) -> None:
    """Re-derive every claim of a request from its own calldata. Called when a request is built
    and again before it is released to a client, so a field cannot drift from the bytes."""
    if r.kind not in KINDS or r.chain_id != CHAIN:
        raise WalletRequestError("unknown leg or wrong chain")
    _addr(r.sender, "sender")
    _addr(r.to, "target")
    if not isinstance(r.data, str) or not re.fullmatch(r"0x(?:[0-9a-f]{2})+", r.data):
        raise WalletRequestError("calldata is not lowercase hex")
    if r.kind in ("reset", "approve"):
        spender, amount = decode_approve(r.data)
        if (r.value_wei != 0 or r.to != r.token or spender != r.spender or amount != r.amount_wei
                or (r.kind == "reset") != (amount == 0)):
            raise WalletRequestError("approval fields do not match its calldata")
        if r.gas_limit is None or not 0 < r.gas_limit <= MAX_APPROVAL_GAS:
            raise WalletRequestError("approval needs a bounded gas limit")
    else:
        side = "buy" if r.path and r.path[0] != r.token else "sell"
        s = decode_swap(r.data, side)
        if (s["to"] != r.recipient or s["to"] != r.sender or s["min_out"] != r.min_out_wei
                or s["deadline"] != r.deadline or s["path"] != r.path
                or (side == "buy" and r.value_wei <= 0) or (side == "sell" and r.value_wei != 0)):
            raise WalletRequestError("swap fields do not match its calldata")


def approval_request(*, plan: UnsignedTradePlan, order_id: str, kind: str, spender: str,
                     amount: int, gas_limit: int, gas_price: int, now: float, ttl: float,
                     block_number: int, block_hash: str) -> WalletRequest:
    if kind not in ("reset", "approve") or plan.side != "sell":
        raise WalletRequestError("approvals exist only for SELL legs")
    max_fee = gas_limit * gas_price
    r = WalletRequest(
        id=_new_id(), kind=kind, order_id=order_id, plan_id=plan.id, plan_digest=fingerprint(plan),
        intent_id=plan.intent_id, chain_id=plan.chain_id, sender=plan.wallet, to=plan.token,
        value_wei=0, data=encode_approve(spender, amount), gas_limit=gas_limit,
        gas_price_wei=gas_price, max_gas_fee_wei=max_fee, token=plan.token, spender=spender,
        amount_wei=amount, recipient=None, path=(), min_out_wei=None,
        deadline=None, block_number=block_number, block_hash=block_hash, issued_at=now,
        expires_at=min(now + ttl, plan.expires_at))
    check_self_consistent(r)
    return r


def swap_request(*, plan: UnsignedTradePlan, order_id: str, now: float, ttl: float
                 ) -> WalletRequest:
    """The executable swap, taken verbatim from the immutable reviewed plan."""
    if plan.simulation_status != "success":
        # a conditional SELL is not executable: it must go through approval and a NEW review
        raise WalletRequestError("only a successfully simulated plan can become a swap request")
    tx = plan.transaction
    r = WalletRequest(
        id=_new_id(), kind="swap", order_id=order_id, plan_id=plan.id, plan_digest=fingerprint(plan),
        intent_id=plan.intent_id, chain_id=plan.chain_id, sender=tx.sender, to=tx.target,
        value_wei=tx.value_wei, data=tx.calldata, gas_limit=tx.gas_limit,
        gas_price_wei=tx.gas_price_wei, max_gas_fee_wei=plan.max_gas_fee_wei, token=plan.token,
        spender=None, amount_wei=plan.amount_in_wei, recipient=plan.recipient, path=plan.path,
        min_out_wei=plan.min_out_wei, deadline=plan.deadline, block_number=plan.snapshot.number,
        block_hash=plan.snapshot.block_hash, issued_at=now,
        expires_at=min(now + ttl, plan.expires_at, float(plan.deadline)))
    if tx.gas_limit is None or tx.gas_price_wei is None or plan.max_gas_fee_wei is None:
        raise WalletRequestError("a swap request needs explicit gas caps")
    if tx.sender != plan.wallet or tx.target != plan.snapshot.route.router:
        raise WalletRequestError("swap sender/target differ from the reviewed plan")
    check_self_consistent(r)
    amount_in = decode_swap(r.data, plan.side)["amount_in"]
    if (plan.side == "sell" and amount_in != plan.amount_in_wei) or (
            plan.side == "buy" and r.value_wei != plan.amount_in_wei):
        raise WalletRequestError("swap input differs from the reviewed amount")
    return r


def _qty(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-f][0-9a-f]*)", value.lower()):
        raise WalletRequestError("malformed quantity")
    return int(value, 16)


class ExpectedTx(Protocol):
    """What a transaction is compared against: a WalletRequest, or a durable leg's payload."""
    @property
    def sender(self) -> str: ...
    @property
    def to(self) -> str: ...
    @property
    def value_wei(self) -> int: ...
    @property
    def data(self) -> str: ...
    @property
    def chain_id(self) -> int: ...
    @property
    def gas_limit(self) -> int | None: ...
    @property
    def gas_price_wei(self) -> int | None: ...


def tx_mismatches(r: ExpectedTx, tx: object, reported_hash: str | None) -> list[str]:
    """Compare what a node says the wallet actually sent with what was requested. An empty list
    means identical; anything else is an anomaly and must not be treated as the reviewed trade."""
    if not isinstance(tx, dict):
        return ["no_transaction"]
    out: list[str] = []

    def eq(name: str, got: object, want: object) -> None:
        if got != want:
            out.append(name)

    try:
        if reported_hash is not None:
            eq("hash", str(tx.get("hash", "")).lower(), reported_hash)
        eq("from", str(tx.get("from", "")).lower(), r.sender)
        eq("to", str(tx.get("to", "")).lower(), r.to)
        eq("value", _qty(tx.get("value", "0x0")), r.value_wei)
        eq("input", str(tx.get("input", tx.get("data", ""))).lower(), r.data)
        if tx.get("chainId") is not None:
            eq("chain", _qty(tx["chainId"]), r.chain_id)
        if r.gas_limit is not None and _qty(tx.get("gas", "0x0")) > r.gas_limit:
            out.append("gas_limit")
        cap = r.gas_price_wei
        if cap is not None:
            price = tx.get("maxFeePerGas", tx.get("gasPrice"))
            if price is not None and _qty(price) > cap:
                out.append("fee_cap")
    except WalletRequestError:
        out.append("malformed")
    return out
