"""Exact, revocable read-only plans over the existing verified V2 PAPER provider.

No caller-supplied transaction, approval payload, signature or broadcast is accepted.
Every nested plan field is immutable. A validation result is a snapshot, not a permit.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .chain import SimulationReverted, address, call_data, uint
from .paper import CHAIN, PaperFlow, Quote, TradeIntent, TradeRefused
from .v2 import ALLOWANCE, BALANCE, DECIMALS, FACTORY, ROUTER, WRAPPED, DirectV2Provider, amount_out

# Freeze the already-reviewed artifact, not an ABI supplied by an HTTP caller.
MANIFEST_BYTES = (Path(__file__).parent / "contracts" / "v2.json").read_bytes()
MANIFEST_SHA256 = "b8ed8dcd6f45b05be111bffb190168ebb8501a6c052a6e737c68f853a51e9599"
if hashlib.sha256(MANIFEST_BYTES).hexdigest() != MANIFEST_SHA256:
    raise RuntimeError("Verified V2 manifest changed; independent review required")
_MANIFEST = json.loads(MANIFEST_BYTES)


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(asdict(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def integer(value, bits=256) -> int:
    if type(value) is not int or not 0 <= value < 2**bits:
        raise TradeRefused("Invalid exact integer state")
    return value


@dataclass(frozen=True)
class PlanOwner:
    user_id: int
    wallet: str
    session_binding: str
    expires_at: float


@dataclass(frozen=True)
class VerifiedRoute:
    router: str
    factory: str
    pair: str
    tokens: tuple[str, str]
    router_hash: str
    factory_hash: str
    pair_hash: str
    token_hash: str
    wrapped_hash: str


@dataclass(frozen=True)
class PlanSnapshot:
    number: int
    block_hash: str
    timestamp: int
    token_decimals: int
    token_balance_wei: int
    native_balance_wei: int
    allowance_wei: int | None
    reserve0_wei: int
    reserve1_wei: int
    route: VerifiedRoute

    def header(self) -> dict:
        return {
            "number": hex(self.number),
            "hash": self.block_hash,
            "timestamp": hex(self.timestamp),
        }

    def mutable_identity(self):
        return (
            self.token_decimals,
            self.token_balance_wei,
            self.native_balance_wei,
            self.allowance_wei,
            self.reserve0_wei,
            self.reserve1_wei,
            self.route,
        )


@dataclass(frozen=True)
class UnsignedTransaction:
    sender: str
    target: str
    value_wei: int
    calldata: str
    gas_limit: int | None
    gas_price_wei: int | None

    def rpc_payload(self) -> dict:
        tx = {
            "from": self.sender,
            "to": self.target,
            "value": hex(self.value_wei),
            "data": self.calldata,
        }
        if self.gas_limit is not None:
            tx.update(gas=hex(self.gas_limit), gasPrice=hex(self.gas_price_wei or 0))
        return tx


@dataclass(frozen=True)
class UnsignedTradePlan:
    id: str
    user_id: int
    session_binding: str
    intent_id: str
    wallet: str
    chain_id: int
    token: str
    side: str
    path: tuple[str, str]
    amount_in_wei: int
    amount_out_wei: int
    min_out_wei: int
    slippage_bps: int
    price_impact_bps: int
    max_price_impact_bps: int
    pool_fee_bps: int
    recipient: str
    spender: str | None
    finite_approval_wei: int
    snapshot: PlanSnapshot
    transaction: UnsignedTransaction
    max_gas_fee_wei: int | None
    simulation_status: str
    created_at: float
    expires_at: float
    deadline: int

    def public(self) -> dict:
        data = asdict(self)
        data.pop("session_binding")
        data.pop("user_id")

        def wire(value):
            if isinstance(value, dict):
                return {
                    k: str(v) if k.endswith("_wei") and v is not None else wire(v)
                    for k, v in value.items()
                }
            if isinstance(value, (list, tuple)):
                return [wire(v) for v in value]
            return value

        result = wire(data)
        result["digest"] = fingerprint(self)
        result["transaction_payload"] = self.transaction.rpc_payload()
        result["delivered_output"] = "unknown"
        result["explanation"] = (
            "Read-only snapshot only. Pool output assumes normal transfers; taxes and restrictions "
            "are unverified. Actual delivered output and future execution are not guaranteed."
        )
        result["signing_enabled"] = False
        return result


@dataclass(frozen=True)
class Revalidation:
    block_number: int
    block_hash: str
    checked_at: float
    simulation_status: str
    gas_estimate: int | None


@dataclass(frozen=True)
class _Record:
    plan: UnsignedTradePlan
    digest: str
    intent: TradeIntent
    quote_digest: str
    review_token: str


class PlanBook:
    def __init__(self, flow: PaperFlow):
        if not isinstance(flow.provider, DirectV2Provider):
            raise TradeRefused("Exact plans require the existing verified V2 provider")
        self.flow, self.provider, self.rpc = flow, flow.provider, flow.provider.rpc
        self._lock = threading.RLock()
        self._slots: dict[tuple[int, str], tuple[str, float, _Record | None]] = {}

    def revoke(self, uid: int, sid: str, plan_id: str | None = None):
        with self._lock:
            key = (uid, sid)
            slot = self._slots.get(key)
            if slot and (plan_id is None or (slot[2] and slot[2].plan.id == plan_id)):
                self._slots[key] = (secrets.token_hex(16), slot[1], None)

    def _begin(self, owner: PlanOwner, sid: str):
        with self._lock:
            self._slots = {k: v for k, v in self._slots.items() if v[1] > self.flow.now()}
            self.revoke(owner.user_id, sid)
            i = self._owner(owner, sid)
            if len(self._slots) >= 256 and (owner.user_id, sid) not in self._slots:
                raise TradeRefused("Plan capacity reached")
            generation = secrets.token_hex(16)
            self._slots[(owner.user_id, sid)] = (generation, i.expires_at, None)
            return generation

    def _owner(self, owner: PlanOwner, sid: str) -> TradeIntent:
        if (
            type(owner.user_id) is not int
            or not isinstance(owner.session_binding, str)
            or not re.fullmatch(r"[a-f0-9]{64}", owner.session_binding)
            or not isinstance(owner.expires_at, (int, float))
            or not math.isfinite(owner.expires_at)
            or not self.flow.now() < owner.expires_at
        ):
            raise TradeRefused("Ownership-proven session required")
        if self.flow.provider is not self.provider:
            raise TradeRefused("Verified provider changed")
        i = self.flow.get(owner.user_id, owner.user_id, sid)
        if address(owner.wallet) != owner.wallet or i.wallet != owner.wallet:
            raise TradeRefused("Plan wallet mismatch")
        return i

    def _view(self, kind, target, name, values, block, outputs):
        return self.rpc.view(
            target, call_data(_MANIFEST[kind]["abi"], name, values), block, outputs
        )

    def _state(self, i: TradeIntent, pair: str, block: str, head: dict) -> PlanSnapshot:
        if not i.wallet:
            raise TradeRefused("Ownership-proven wallet required")
        wallet, token = address(i.wallet), address(i.token)
        number, timestamp = uint(head["number"]), uint(head["timestamp"])
        if number > 2**53 - 1 or block != hex(number) or not 0 <= self.flow.now() - timestamp <= 30:
            raise TradeRefused("Stale or invalid pinned state")
        hashes = []
        for kind, target in (("router", ROUTER), ("factory", FACTORY), ("pair", pair)):
            observed = self.rpc.code_hash(target, block)
            if observed != _MANIFEST[kind]["runtime_keccak256"]:
                raise TradeRefused("Frozen V2 runtime mismatch")
            hashes.append(observed)
        if (
            self._view("router", ROUTER, "factory", [], block, ["address"])[0] != FACTORY
            or self._view("router", ROUTER, "WETH", [], block, ["address"])[0] != WRAPPED
            or self._view("factory", FACTORY, "getPair", [WRAPPED, token], block, ["address"])[0]
            != pair
            or self._view("pair", pair, "factory", [], block, ["address"])[0] != FACTORY
        ):
            raise TradeRefused("Router/factory/pair mismatch")
        tokens = tuple(
            self._view("pair", pair, n, [], block, ["address"])[0] for n in ("token0", "token1")
        )
        if tokens != tuple(sorted((WRAPPED, token))):
            raise TradeRefused("Noncanonical pair tokens")
        if self.rpc.call("eth_getCode", [wallet, block]) != "0x":
            raise TradeRefused("Contract/delegated wallet unsupported")
        # Observed token code hashes are change detectors, not a token-safety approval.
        token_hash, wrapped_hash = (self.rpc.code_hash(t, block) for t in (token, WRAPPED))
        if any(
            not isinstance(h, str) or not re.fullmatch(r"0x[0-9a-f]{64}", h)
            for h in (token_hash, wrapped_hash)
        ):
            raise TradeRefused("Invalid observed token runtime hash")
        decimals = integer(self.rpc.view(token, DECIMALS, block, ["uint8"])[0], 8)
        if decimals > 36:
            raise TradeRefused("Unsupported token decimals")
        token_balance = integer(
            self.rpc.view(token, BALANCE + wallet[2:].zfill(64), block, ["uint256"])[0]
        )
        native = uint(self.rpc.call("eth_getBalance", [wallet, block]))
        allowance = (
            None
            if i.side == "buy"
            else integer(
                self.rpc.view(
                    token,
                    ALLOWANCE + wallet[2:].zfill(64) + ROUTER[2:].zfill(64),
                    block,
                    ["uint256"],
                )[0]
            )
        )
        reserves = self._view(
            "pair", pair, "getReserves", [], block, ["uint112", "uint112", "uint32"]
        )
        r0, r1 = integer(reserves[0], 112), integer(reserves[1], 112)
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", head["hash"]):
            raise TradeRefused("Invalid block hash")
        self.rpc.check_block(block, head)
        return PlanSnapshot(
            number,
            head["hash"],
            timestamp,
            decimals,
            token_balance,
            native,
            allowance,
            r0,
            r1,
            VerifiedRoute(
                ROUTER,
                FACTORY,
                pair,
                (tokens[0], tokens[1]),
                hashes[0],
                hashes[1],
                hashes[2],
                token_hash,
                wrapped_hash,
            ),
        )

    @staticmethod
    def _quote_matches(i: TradeIntent, q: Quote, s: PlanSnapshot, slippage: int):
        from .v2 import validate_plan

        validate_plan(i, q, q.created_at)
        path = (WRAPPED, i.token) if i.side == "buy" else (i.token, WRAPPED)
        rin, rout = (
            (s.reserve0_wei, s.reserve1_wei)
            if s.route.tokens[0] == path[0]
            else (s.reserve1_wei, s.reserve0_wei)
        )
        n = i.amount_wei if i.side == "buy" else s.token_balance_wei * (i.sell_bps or 0) // 10000
        n = integer(n)
        if (s.reserve0_wei if s.route.tokens[0] == WRAPPED else s.reserve1_wei) < 5 * 10**18:
            raise TradeRefused("Insufficient verified HYPE liquidity")
        out = amount_out(n, rin, rout)
        if (
            q.amount_in_wei != n
            or q.amount_out_wei != out
            or q.min_out_wei != out * (10000 - slippage) // 10000
            or q.token_decimals != s.token_decimals
            or q.native_balance_wei != s.native_balance_wei
            or q.balance_wei != (s.native_balance_wei if i.side == "buy" else s.token_balance_wei)
            or q.deadline != s.timestamp + 120
        ):
            raise TradeRefused("Quote/state mismatch")
        impact = (n * rout * 10000 - out * rin * 10000 + n * rout - 1) // (n * rout)
        if q.price_impact_bps != impact or not 0 <= impact <= 100:
            raise TradeRefused("Invalid exact price impact")
        method = (
            "swapExactETHForTokensSupportingFeeOnTransferTokens"
            if i.side == "buy"
            else "swapExactTokensForETHSupportingFeeOnTransferTokens"
        )
        values = [q.min_out_wei, list(path), i.wallet, q.deadline]
        if i.side == "sell":
            values.insert(0, n)
        expected = {
            "from": i.wallet,
            "to": ROUTER,
            "value": hex(n if i.side == "buy" else 0),
            "data": call_data(_MANIFEST["router"]["abi"], method, values),
        }
        if q.transaction != expected:
            raise TradeRefused("Unsigned payload does not match frozen ABI")
        conditional = i.side == "sell" and (s.allowance_wei or 0) < n
        if conditional != (q.simulation_status == "approval_required"):
            raise TradeRefused("Allowance/simulation mismatch")

    def _simulate(self, p: UnsignedTradePlan, s: PlanSnapshot) -> Revalidation:
        tx, block = p.transaction.rpc_payload(), hex(s.number)
        conditional = p.simulation_status == "approval_required"
        try:
            result = self.rpc.call("eth_call", [tx, block])
            if result != "0x" or conditional:
                raise TradeRefused("Invalid exact simulation result")
        except SimulationReverted:
            if not conditional:
                raise
            return Revalidation(s.number, s.block_hash, self.flow.now(), "approval_required", None)
        gas = uint(self.rpc.call("eth_estimateGas", [tx, block]))
        price = uint(self.rpc.call("eth_gasPrice", []))
        if (
            not 0 < gas <= (p.transaction.gas_limit or 0)
            or not 0 < price <= (p.transaction.gas_price_wei or 0)
            or s.native_balance_wei < p.transaction.value_wei + (p.max_gas_fee_wei or 0)
        ):
            raise TradeRefused("Gas/balance cap changed; requote")
        return Revalidation(s.number, s.block_hash, self.flow.now(), "success", gas)

    def build(
        self, owner: PlanOwner, sid: str, value: str, slippage: int
    ) -> tuple[UnsignedTradePlan, Quote]:
        generation = self._begin(owner, sid)
        try:
            if not isinstance(value, str) or type(slippage) is not int or not 10 <= slippage <= 100:
                raise TradeRefused("Invalid exact plan amount/slippage")
            self.flow.select(owner.user_id, owner.user_id, sid, value)
            q = self.flow.review(owner.user_id, owner.user_id, sid, slippage)
            i = self._owner(owner, sid)
            if not q.pool or not q.transaction or q.deadline is None:
                raise TradeRefused("Incomplete unsigned quote")
            with self.rpc.budget(20):
                block, head = self.rpc.snapshot()
                if (q.block_number, q.block_hash) != (int(block, 16), head["hash"]):
                    raise TradeRefused("Head advanced while building; requote")
                s = self._state(i, address(q.pool), block, head)
                self._quote_matches(i, q, s, slippage)
                gas = (q.gas_estimate * 120 + 99) // 100 if q.gas_estimate is not None else None
                p = UnsignedTradePlan(
                    secrets.token_urlsafe(18),
                    owner.user_id,
                    owner.session_binding,
                    sid,
                    owner.wallet,
                    CHAIN,
                    i.token,
                    i.side,
                    (WRAPPED, i.token) if i.side == "buy" else (i.token, WRAPPED),
                    q.amount_in_wei,
                    q.amount_out_wei,
                    q.min_out_wei,
                    slippage,
                    q.price_impact_bps,
                    100,
                    30,
                    owner.wallet,
                    ROUTER if i.side == "sell" else None,
                    q.approval_required_wei,
                    s,
                    UnsignedTransaction(
                        owner.wallet,
                        ROUTER,
                        q.amount_in_wei if i.side == "buy" else 0,
                        q.transaction["data"],
                        gas,
                        q.gas_price_wei,
                    ),
                    q.gas_wei,
                    q.simulation_status,
                    q.created_at,
                    min(q.expires_at, owner.expires_at),
                    q.deadline,
                )
                self._simulate(p, s)
                self.rpc.check_block(block, head)
                self._same_head(s)
            token = self.flow.confirmation_token(owner.user_id, owner.user_id, sid)
            if not token:
                raise TradeRefused("PAPER review changed")
            record = _Record(p, fingerprint(p), i, fingerprint(q), token)
            with self._lock:
                if self._slots[(owner.user_id, sid)][0] != generation:
                    raise TradeRefused("Plan selection changed during build")
                self._flow_matches(owner, record)
                self._slots[(owner.user_id, sid)] = (generation, p.expires_at, record)
            return p, q
        except Exception:
            with self._lock:
                slot = self._slots.get((owner.user_id, sid))
                if slot and slot[0] == generation:
                    self.revoke(owner.user_id, sid)
            raise

    def _same_head(self, snapshot: PlanSnapshot):
        block, head = self.rpc.snapshot()
        if block != hex(snapshot.number) or head != snapshot.header():
            # RPC may include other header fields; compare the pinned identity only.
            if (block, head.get("hash"), head.get("timestamp")) != (
                hex(snapshot.number),
                snapshot.block_hash,
                hex(snapshot.timestamp),
            ):
                raise TradeRefused("Head changed during validation; retry")

    def _flow_matches(self, owner: PlanOwner, r: _Record):
        i = self._owner(owner, r.plan.intent_id)
        current_quote = self.flow.reviewed(owner.user_id, owner.user_id, i.id)
        if (
            current_quote is None
            or i != r.intent
            or self.flow.confirmation_token(owner.user_id, owner.user_id, i.id) != r.review_token
            or fingerprint(current_quote) != r.quote_digest
        ):
            raise TradeRefused("PAPER review or parameters changed")

    def _get(self, owner: PlanOwner, sid: str, plan_id: str) -> _Record:
        slot = self._slots.get((owner.user_id, sid))
        r = slot[2] if slot else None
        if (
            not r
            or r.plan.id != plan_id
            or r.plan.wallet != owner.wallet
            or r.plan.session_binding != owner.session_binding
            or self.flow.now() >= r.plan.expires_at
            or self.flow.now() >= r.plan.deadline
            or fingerprint(r.plan) != r.digest
        ):
            raise TradeRefused("Plan expired, changed or revoked")
        self._flow_matches(owner, r)
        return r

    def revalidate(
        self, owner: PlanOwner, sid: str, plan_id: str
    ) -> tuple[UnsignedTradePlan, Revalidation]:
        try:
            with self._lock:
                r = self._get(owner, sid, plan_id)
            p = r.plan
            with self.rpc.budget(20):
                self.rpc.check_block(hex(p.snapshot.number), p.snapshot.header())
                block, head = self.rpc.snapshot()
                if int(block, 16) < p.snapshot.number:
                    raise TradeRefused("Head regressed")
                s = self._state(r.intent, p.snapshot.route.pair, block, head)
                if s.mutable_identity() != p.snapshot.mutable_identity():
                    raise TradeRefused("Balance/allowance/reserves/code changed; requote")
                evidence = self._simulate(p, s)
                self.rpc.check_block(block, head)
                self._same_head(s)
            with self._lock:
                if self._get(owner, sid, plan_id) is not r:
                    raise TradeRefused("Plan changed during revalidation")
            return p, evidence
        except Exception:
            self.revoke(owner.user_id, sid, plan_id)
            raise
