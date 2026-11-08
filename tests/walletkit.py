"""Shared fixtures for the wallet-managed flow (#92-#94): realistic plans with REAL calldata built
independently of the code under test, and a scriptable chain. No crypto library, no network."""

from __future__ import annotations

import copy
from dataclasses import replace

from agora_tracker.trading.plans import (
    PlanSnapshot,
    UnsignedTradePlan,
    UnsignedTransaction,
    VerifiedRoute,
)
from agora_tracker.trading.v2 import FACTORY, ROUTER, WRAPPED

E18 = 10**18
WALLET = "0x" + "a1" * 20
OTHER = "0x" + "b2" * 20
TOKEN = "0x" + "11" * 20
PAIR = "0x" + "03" * 20
NOW = 1_800_000_000.0
SEL_BUY, SEL_SELL, SEL_APPROVE = "b6f9de95", "791ac947", "095ea7b3"


def w(n: int) -> str:
    return f"{n:064x}"


def a(addr: str) -> str:
    return addr[2:].rjust(64, "0")


def swap_data(side, *, amount_in, min_out, path, to, deadline) -> str:
    """Hand-built canonical calldata (independent of agora_tracker.trading.wallet)."""
    if side == "buy":
        head = [w(min_out), w(0x80), a(to), w(deadline)]
        return "0x" + SEL_BUY + "".join(head) + w(2) + a(path[0]) + a(path[1])
    head = [w(amount_in), w(min_out), w(0xA0), a(to), w(deadline)]
    return "0x" + SEL_SELL + "".join(head) + w(2) + a(path[0]) + a(path[1])


def approve_data(spender, amount) -> str:
    return "0x" + SEL_APPROVE + a(spender) + w(amount)


def make_plan(side="buy", *, status="success", amount_in=E18 // 2, out=3 * E18, min_out=None,
              now=NOW, wallet=WALLET, token=TOKEN, deadline_in=600, expires_in=300,
              allowance=None, uid=7, gas=210_000, price=10**9) -> UnsignedTradePlan:
    min_out = out * 995 // 1000 if min_out is None else min_out
    path = (WRAPPED, token) if side == "buy" else (token, WRAPPED)
    deadline = int(now) + deadline_in
    data = swap_data(side, amount_in=amount_in, min_out=min_out, path=path, to=wallet,
                     deadline=deadline)
    cond = status == "approval_required"
    route = VerifiedRoute(ROUTER, FACTORY, PAIR, tuple(sorted((WRAPPED, token))),
                          "0x" + "01" * 32, "0x" + "02" * 32, "0x" + "03" * 32, "0x" + "04" * 32,
                          "0x" + "05" * 32)
    if allowance is None:
        allowance = 0 if cond else (amount_in if side == "sell" else None)
    snap = PlanSnapshot(1000, "0x" + "ab" * 32, int(now), 18, 10 * E18, 5 * E18, allowance,
                        100_000 * E18, 1_000 * E18, route)
    tx = UnsignedTransaction(wallet, ROUTER, amount_in if side == "buy" else 0, data,
                             None if cond else gas, None if cond else price)
    return UnsignedTradePlan(
        id="P" * 24, user_id=uid, session_binding="f" * 64, intent_id="I" * 16, wallet=wallet,
        chain_id=99901, token=token, side=side, path=path, amount_in_wei=amount_in,
        amount_out_wei=out, min_out_wei=min_out, slippage_bps=50, price_impact_bps=20,
        max_price_impact_bps=100, pool_fee_bps=30, recipient=wallet,
        spender=ROUTER if side == "sell" else None,
        finite_approval_wei=amount_in if cond else 0, snapshot=snap, transaction=tx,
        max_gas_fee_wei=None if cond else gas * price, simulation_status=status,
        created_at=now, expires_at=now + expires_in, deadline=deadline)


def tamper(plan: UnsignedTradePlan, **tx_changes) -> UnsignedTradePlan:
    return replace(plan, transaction=replace(plan.transaction, **tx_changes))


def tx_of(request, *, sender=None, **changes) -> dict:
    """What a node would return for a transaction sent from `request` (perfect match)."""
    t = {"hash": "0x" + "cd" * 32, "from": sender or request.sender, "to": request.to,
         "value": hex(request.value_wei), "input": request.data, "gas": hex(request.gas_limit or 0),
         "gasPrice": hex(request.gas_price_wei or 0), "chainId": hex(request.chain_id),
         "nonce": "0x5", "blockHash": None, "blockNumber": None}
    t.update(copy.deepcopy(changes))
    return t


# ------------------------------------------------------------------ scriptable chain
from agora_tracker.trading.wallet.approvals import ChainReverted, Head  # noqa: E402

ALLOWANCE_SEL = "0xdd62ed3e"


class FakeChain:
    """Read-only chain double. Token behaviour is configurable to cover real-world quirks."""

    def __init__(self, *, now=NOW, allowance=0, token_hash="0x" + "04" * 32,
                 router_hash="0x" + "01" * 32, void_return=False, return_value=1,
                 revert_nonzero_to_nonzero=False, revert_all=False, gas=46_000, price=10**9,
                 head_age=2):
        self.now, self.allowance, self.gas, self.price = now, allowance, gas, price
        self.hashes = {ROUTER: router_hash, TOKEN: token_hash}
        self.void_return, self.return_value = void_return, return_value
        self.quirk, self.revert_all = revert_nonzero_to_nonzero, revert_all
        self.number, self.head_age = 1000, head_age
        self.calls = []

    def head(self):
        return Head(self.number, "0x" + "ab" * 32, int(self.now) - self.head_age)

    def code_hash(self, address, block):
        return self.hashes.get(address, "0x" + "00" * 32)

    def gas_price(self):
        return self.price

    def estimate_gas(self, tx, block):
        self._approve_effect(tx["data"])
        return self.gas

    def call(self, to, data, block, sender=None):
        self.calls.append((to, data[:10]))
        if data.startswith(ALLOWANCE_SEL):
            return "0x" + w(self.allowance)
        return self._approve_effect(data)

    def _approve_effect(self, data):
        if self.revert_all:
            raise ChainReverted("revert")
        amount = int(data[74:], 16)
        if self.quirk and self.allowance != 0 and amount != 0:
            raise ChainReverted("USDT-style non-zero to non-zero")
        return "0x" if self.void_return else "0x" + w(self.return_value)


# ------------------------------------------------------------------ real HTTP server (no httpx)
import threading  # noqa: E402
import time  # noqa: E402

import requests  # noqa: E402
import uvicorn  # noqa: E402


class Http:
    """Tiny requests-based client with the few TestClient methods the tests use."""

    def __init__(self, base: str) -> None:
        self.base = base

    def request(self, method, path, *, json=None, content=None, headers=None):
        return requests.request(method, self.base + path, json=json, data=content,
                                headers=headers, timeout=10)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)


class Served:
    def __init__(self, app) -> None:
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                                    log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.02)
        assert self.server.started
        self.base = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        self.client = Http(self.base)

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)


# ------------------------------------------------------------------ receipts / fills (#94)
from agora_tracker.trading.paper import TradeRefused  # noqa: E402

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"


def block_hash(number: int, fork: int = 0) -> str:
    return "0x" + f"{fork:02x}{number:062x}"


def transfer_log(token, frm, to, amount) -> dict:
    return {"address": token, "topics": [TRANSFER, "0x" + a(frm), "0x" + a(to)], "data": "0x" + w(amount)}


def deposit_log(party, amount) -> dict:
    return {"address": WRAPPED, "topics": [DEPOSIT, "0x" + a(party)], "data": "0x" + w(amount)}


def withdrawal_log(party, amount) -> dict:
    return {"address": WRAPPED, "topics": [WITHDRAWAL, "0x" + a(party)], "data": "0x" + w(amount)}


class ReceiptChain:
    """Scriptable chain for receipt/fill tests: blocks, canonical headers, receipts, balances.
    A missing balance raises like a pruned/unavailable state would."""

    def __init__(self, head: int = 2000):
        self.head_n = head
        self.canon: dict[int, str] = {}
        self.txs: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.blocks: dict[int, list[dict]] = {}
        self.tok: dict[tuple[str, str, int], int] = {}
        self.nat: dict[tuple[str, int], int] = {}
        self.down = False

    def _up(self):
        if self.down:
            raise TradeRefused("node down")

    def head(self):
        self._up()
        return Head(self.head_n, block_hash(self.head_n), 0)

    def transaction(self, h):
        self._up()
        return self.txs.get(h)

    def receipt(self, h):
        self._up()
        return self.receipts.get(h)

    def block_header(self, n):
        self._up()
        return {"hash": self.canon[n], "number": hex(n)} if n in self.canon else None

    def block_transactions(self, n):
        self._up()
        return self.blocks.get(n, [])

    def native_balance(self, address, block):
        self._up()
        if (address, block) not in self.nat:
            raise TradeRefused("state unavailable")
        return self.nat[(address, block)]

    def token_balance(self, token, address, block):
        self._up()
        if (token, address, block) not in self.tok:
            raise TradeRefused("state unavailable")
        return self.tok[(token, address, block)]

    def mine(self, leg, tx_hash, number, *, fork=0, status=1, logs=(), gas_used=150_000,
             price=10**9, nonce=5, others=(), tx=None, rc=None):
        """Put `leg`'s transaction in block `number` and make that block canonical."""
        bh = block_hash(number, fork)
        t = {"hash": tx_hash, "from": leg["wallet"], "to": leg["expected_to"],
             "value": hex(int(leg["expected_value_wei"])), "input": leg["expected_data"],
             "gas": hex(int(leg["gas_limit"] or 210_000)), "gasPrice": hex(price),
             "chainId": "0x1863d", "nonce": hex(nonce), "blockHash": bh, "blockNumber": hex(number)}
        r = {"transactionHash": tx_hash, "from": leg["wallet"], "to": leg["expected_to"],
             "status": hex(status), "blockHash": bh, "blockNumber": hex(number),
             "gasUsed": hex(gas_used), "effectiveGasPrice": hex(price), "contractAddress": None,
             "logs": [dict(lg) for lg in logs]}
        t.update(tx or {})
        r.update(rc or {})
        self.canon[number] = bh
        self.txs[tx_hash], self.receipts[tx_hash] = t, r
        self.blocks[number] = [t, *others]
        return t, r

    def drop(self, tx_hash):
        self.txs.pop(tx_hash, None)
        self.receipts.pop(tx_hash, None)

    def buy_balances(self, number, tokens, *, before=0, wallet=WALLET, token=TOKEN):
        self.tok[(token, wallet, number - 1)] = before
        self.tok[(token, wallet, number)] = before + tokens
