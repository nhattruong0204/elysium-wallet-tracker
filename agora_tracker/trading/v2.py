"""Read-only canonical Agora V2 pool quotes and unsigned-payload simulation.

The aggregator's proprietary router ABI is NOT assumed. This adapter uses published
Uniswap V2 interfaces and exact runtime hashes including checked router immutables.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .chain import ReadRpc, SimulationReverted, address, call_data, uint
from .paper import CHAIN, ExecutionRoute, Quote, TradeIntent, TradeRefused

CONTRACTS = json.loads((Path(__file__).parent / "contracts" / "v2.json").read_text())
ROUTER = CONTRACTS["router_address"]
FACTORY = CONTRACTS["factory_address"]
WRAPPED = CONTRACTS["wrapped_native"]
DIRECT_ROUTE = ExecutionRoute(
    CHAIN, ROUTER, CONTRACTS["router"]["runtime_keccak256"], "canonical-v2"
)
# ERC-20 view selectors are from EIP-20. No approval/send method is exposed.
DECIMALS = "0x313ce567"
BALANCE = "0x70a08231"
ALLOWANCE = "0xdd62ed3e"


def amount_out(n: int, reserve_in: int, reserve_out: int) -> int:
    if min(n, reserve_in, reserve_out) <= 0:
        raise TradeRefused("No input or liquidity")
    return n * 997 * reserve_out // (reserve_in * 1000 + n * 997)


class DirectV2Provider:
    def __init__(self, rpc: ReadRpc, *, agora=None, now=time.time):
        self.rpc, self.agora, self.now = rpc, agora, now
        self._lock = threading.Lock()
        self.metrics: dict[str, float] = {}

    def _function(
        self, kind: str, target: str, name: str, values: list, block: str, outputs: list[str]
    ) -> tuple:
        started = time.monotonic()
        try:
            return self.rpc.view(
                target, call_data(CONTRACTS[kind]["abi"], name, values), block, outputs
            )
        finally:
            self.metrics[name] = (time.monotonic() - started) * 1000

    def _balances(self, wallet: str, token: str) -> tuple[int, int, int, str]:
        wallet, token = address(wallet), address(token)
        block, _ = self.rpc.snapshot()
        decimals = self.rpc.view(token, DECIMALS, block, ["uint8"])[0]
        if not 0 <= decimals <= 36:
            raise TradeRefused("Unsupported token decimals")
        balance = self.rpc.view(token, BALANCE + wallet[2:].zfill(64), block, ["uint256"])[0]
        native = uint(self.rpc.call("eth_getBalance", [wallet, block]))
        return balance, native, decimals, block

    def balances(self, wallet: str, token: str) -> tuple[int, int, int, str]:
        if not self._lock.acquire(blocking=False):
            raise TradeRefused("Paper quote busy; try again shortly")
        try:
            with self.rpc.budget(10):
                return self._balances(wallet, token)
        finally:
            self._lock.release()

    def quote(self, intent: TradeIntent, slippage_bps: int) -> Quote:
        if not self._lock.acquire(blocking=False):
            raise TradeRefused("Paper quote busy; try again shortly")
        try:
            with self.rpc.budget(20):
                return self._quote(intent, slippage_bps)
        finally:
            self._lock.release()

    def _quote(self, i: TradeIntent, slippage: int) -> Quote:
        started = self.now()
        self.metrics = {}
        if i.chain_id != CHAIN or i.side not in ("buy", "sell") or not i.wallet:
            raise TradeRefused("Set a public watch-only wallet with /paper_wallet ADDRESS")
        if type(slippage) is not int or not 10 <= slippage <= 100:
            raise TradeRefused("Unsupported paper slippage")
        wallet, token = address(i.wallet), address(i.token)
        if token == WRAPPED:
            raise TradeRefused("Wrapped/native conversion is not a supported swap")
        block, head = self.rpc.snapshot()
        for kind, target in [("router", ROUTER), ("factory", FACTORY)]:
            if self.rpc.code_hash(target, block) != CONTRACTS[kind]["runtime_keccak256"]:
                raise TradeRefused("Verified contract runtime changed")
        if self._function("router", ROUTER, "factory", [], block, ["address"])[0] != FACTORY:
            raise TradeRefused("Router factory mismatch")
        if self._function("router", ROUTER, "WETH", [], block, ["address"])[0] != WRAPPED:
            raise TradeRefused("Wrapped HYPE mismatch")
        pair = self._function("factory", FACTORY, "getPair", [WRAPPED, token], block, ["address"])[
            0
        ]
        address(pair)
        if self.rpc.code_hash(pair, block) != CONTRACTS["pair"]["runtime_keccak256"]:
            raise TradeRefused("Unsupported pair runtime")
        if self._function("pair", pair, "factory", [], block, ["address"])[0] != FACTORY:
            raise TradeRefused("Pair factory mismatch")
        t0 = self._function("pair", pair, "token0", [], block, ["address"])[0]
        t1 = self._function("pair", pair, "token1", [], block, ["address"])[0]
        if {t0, t1} != {token, WRAPPED}:
            raise TradeRefused("Pair token mismatch")
        r0, r1, _ = self._function(
            "pair", pair, "getReserves", [], block, ["uint112", "uint112", "uint32"]
        )
        # All balances/allowances/reserves use the same pinned block.
        decimals = self.rpc.view(token, DECIMALS, block, ["uint8"])[0]
        if not 0 <= decimals <= 36:
            raise TradeRefused("Unsupported token decimals")
        token_balance = self.rpc.view(token, BALANCE + wallet[2:].zfill(64), block, ["uint256"])[0]
        native = uint(self.rpc.call("eth_getBalance", [wallet, block]))
        n = i.amount_wei if i.side == "buy" else token_balance * (i.sell_bps or 0) // 10000
        if type(n) is not int or n <= 0 or (i.side == "buy" and n > 10**18):
            raise TradeRefused("Select a positive paper amount within the cap")
        if i.side == "sell" and native <= 0:
            raise TradeRefused("No HYPE available for future sell gas")
        balance = native if i.side == "buy" else token_balance
        if n > balance:
            raise TradeRefused("Insufficient verified wallet balance")
        path = [WRAPPED, token] if i.side == "buy" else [token, WRAPPED]
        rin, rout = (r0, r1) if t0 == path[0] else (r1, r0)
        if (r0 if t0 == WRAPPED else r1) < 5 * 10**18:
            raise TradeRefused("Pool has less than 5 HYPE reserve; insufficient paper liquidity")
        out = amount_out(n, rin, rout)
        quoted = self._function("router", ROUTER, "getAmountsOut", [n, path], block, ["uint256[]"])[
            0
        ]
        if tuple(quoted) != (n, out) or out <= 0:
            raise TradeRefused("Router/reserve quote mismatch")
        impact = (n * rout * 10000 - out * rin * 10000 + n * rout - 1) // (n * rout)
        if not 0 <= impact <= 100:
            raise TradeRefused("Price impact including pool fee exceeds 1%")
        minimum = out * (10000 - slippage) // 10000
        if minimum <= 0:
            raise TradeRefused("Minimum output rounds to zero")
        deadline = uint(head["timestamp"]) + 120
        if i.side == "buy":
            data = call_data(
                CONTRACTS["router"]["abi"],
                "swapExactETHForTokensSupportingFeeOnTransferTokens",
                [minimum, path, wallet, deadline],
            )
            value = n
            allowance = n
        else:
            data = call_data(
                CONTRACTS["router"]["abi"],
                "swapExactTokensForETHSupportingFeeOnTransferTokens",
                [n, minimum, path, wallet, deadline],
            )
            value = 0
            allowance = self.rpc.view(
                token, ALLOWANCE + wallet[2:].zfill(64) + ROUTER[2:].zfill(64), block, ["uint256"]
            )[0]
        tx = {"from": wallet, "to": ROUTER, "data": data, "value": hex(value)}
        simulation, gas, gas_price, reserve = "success", None, None, None
        needs_approval = i.side == "sell" and allowance < n
        # Missing approvals are a conditional PAPER scenario, never a successful simulation.
        try:
            raw = self.rpc.call("eth_call", [tx, block])
            # These canonical methods enforce minOut on recipient balance changes
            # (BUY) or received wrapped HYPE (SELL) and deliberately return no amount.
            # eth_call proves the floor at this block, not an exact realized fill.
            if raw != "0x":
                raise TradeRefused("Invalid simulation ABI output")
        except SimulationReverted:
            if not needs_approval:
                raise
            simulation = "approval_required"
        except TradeRefused:
            raise
        else:
            if needs_approval:
                raise TradeRefused("Unexpected simulation with insufficient allowance")
            gas = uint(self.rpc.call("eth_estimateGas", [tx, block]))
            gas_price = uint(self.rpc.call("eth_gasPrice", []))
            if gas <= 0 or gas_price <= 0 or gas > 1_000_000:
                raise TradeRefused("Unsafe or unavailable gas estimate")
            reserve = (gas * 120 + 99) // 100 * gas_price
            if reserve > 10**16 or native < reserve + value:
                raise TradeRefused("Insufficient HYPE including gas reserve")
        comparison = {}
        if self.agora:
            try:
                comparison = self.agora.compare(i, n, decimals, slippage)
            except TradeRefused:
                comparison = {"agora_status": "unavailable"}
        expires = min(started + 30, uint(head["timestamp"]) + 30, i.expires_at)
        if self.now() >= expires:
            raise TradeRefused("Quote expired while fetching data; refresh")
        return Quote(
            i.id,
            CHAIN,
            token,
            i.side,
            n,
            out,
            minimum,
            balance,
            reserve,
            impact,
            started,
            expires,
            DIRECT_ROUTE,
            simulation == "success",
            native_balance_wei=native,
            wallet=wallet,
            token_decimals=decimals,
            simulation_status=simulation,
            approval_required_wei=n if needs_approval else 0,
            approval_spender=ROUTER if needs_approval else None,
            block_number=int(block, 16),
            block_hash=head.get("hash"),
            deadline=deadline,
            transaction=tx,
            gas_estimate=gas,
            gas_price_wei=gas_price,
            pool=pair,
            pool_fee_bps=30,
            quote_latency_ms=round((self.now() - started) * 1000, 2),
            **comparison,
        )


def validate_plan(i: TradeIntent, q: Quote, now: float) -> None:
    if (
        q.route != DIRECT_ROUTE
        or not i.wallet
        or q.wallet != i.wallet
        or type(q.token_decimals) is not int
        or not 0 <= q.token_decimals <= 36
        or type(q.block_number) is not int
        or q.block_number < 0
        or not isinstance(q.block_hash, str)
        or len(q.block_hash) != 66
        or type(q.deadline) is not int
        or not now < q.deadline <= q.created_at + 150
        or not q.pool
        or q.pool_fee_bps != 30
    ):
        raise TradeRefused("Incomplete or invalid direct pool plan")
    address(q.pool)
    if i.side == "buy":
        data = call_data(
            CONTRACTS["router"]["abi"],
            "swapExactETHForTokensSupportingFeeOnTransferTokens",
            [q.min_out_wei, [WRAPPED, i.token], i.wallet, q.deadline],
        )
    else:
        data = call_data(
            CONTRACTS["router"]["abi"],
            "swapExactTokensForETHSupportingFeeOnTransferTokens",
            [q.amount_in_wei, q.min_out_wei, [i.token, WRAPPED], i.wallet, q.deadline],
        )
    expected = {
        "from": i.wallet,
        "to": ROUTER,
        "data": data,
        "value": hex(q.amount_in_wei if i.side == "buy" else 0),
    }
    if q.transaction != expected:
        raise TradeRefused(
            "Transaction destination, recipient, value, minimum or deadline mismatch"
        )
    if q.simulated is True:
        if (
            q.simulation_status != "success"
            or type(q.gas_estimate) is not int
            or not 0 < q.gas_estimate <= 1_000_000
            or type(q.gas_price_wei) is not int
            or q.gas_price_wei <= 0
            or q.gas_wei != (q.gas_estimate * 120 + 99) // 100 * q.gas_price_wei
            or q.approval_required_wei
        ):
            raise TradeRefused("Invalid simulation/gas evidence")
    elif q.simulation_status != "approval_required" or i.side != "sell":
        raise TradeRefused("Unsupported simulation status")
