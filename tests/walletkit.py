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

    latest_nonce = 6
    pending_nonce = 6

    def tx_count(self, address, tag):
        self._up()
        return self.latest_nonce if tag == "latest" else self.pending_nonce

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


# ------------------------------------------------------------------ independent RPC providers (#95 F-15)
from agora_tracker.trading.wallet.corroboration import Corroborator  # noqa: E402
from agora_tracker.trading.wallet.rpc import RpcError  # noqa: E402


class Provider:
    """One RPC endpoint's VIEW of a scriptable world (a `ReceiptChain`-like object). The world is
    the canonical chain; a provider can lag, go stale, fail, hide or fabricate facts, or lack
    historical state, independently of every other provider. `host` and `operator` are distinct
    per provider exactly like `ExecutionRpc.host` / `.operator`."""

    chain_id = 99901
    chain_verified = True

    def __init__(self, world, host: str, clock, *, lag: int = 0, age: int = 2,
                 operator: str | None = None) -> None:
        from agora_tracker.trading.wallet.rpc import operator_of
        self.w, self.host, self.clock, self.lag, self.age = world, host, clock, lag, age
        self.operator = operator or operator_of(host)
        self.down = False
        self.no_history = False                 # "pruned node": every balance read is refused
        self.header_override: dict[int, str] = {}
        self.hidden: set[str] = set()
        self.tx_override: dict[str, dict] = {}
        self.rc_override: dict[str, dict] = {}
        self.tok_shift = 0
        self.nat_shift = 0
        self.nonce_bias = 0
        self.calls: list[str] = []

    # -- plumbing
    def _go(self, name: str) -> None:
        self.calls.append(name)
        if self.down or getattr(self.w, "down", False):
            raise TradeRefused("rpc down")

    @property
    def n(self) -> int:
        return self.w.head_n - self.lag

    def _hdr(self, n: int) -> str:
        if n in self.header_override:
            return self.header_override[n]
        return getattr(self.w, "canon", {}).get(n) or block_hash(n)

    def _txs(self) -> dict:
        return self.w.txs

    def _mined(self, t: dict) -> dict:
        """Node-style worlds omit the block hash of a mined tx: fill in the canonical one."""
        t = dict(t)
        if t.get("blockNumber") and not t.get("blockHash"):
            t["blockHash"] = self._hdr(int(t["blockNumber"], 16))
        return t

    # -- reads
    def head(self):
        self._go("head")
        return Head(self.n, self._hdr(self.n), int(self.clock()) - self.age)

    def block_header(self, n):
        self._go("block_header")
        if n > self.n or n in getattr(self.w, "headerless", ()):
            return None
        return {"hash": self._hdr(n), "number": hex(n)}

    def transaction(self, h):
        self._go("transaction")
        if h in self.hidden:
            return None
        t = self.tx_override.get(h) or self._txs().get(h)
        if t is None:
            return None
        t = self._mined(t)
        if t.get("blockNumber") and int(t["blockNumber"], 16) > self.n:
            t["blockNumber"] = t["blockHash"] = None            # not yet in this provider's chain
        return t

    def receipt(self, h):
        self._go("receipt")
        if h in self.hidden:
            return None
        r = self.rc_override.get(h)
        if r is None and hasattr(self.w, "receipts"):
            r = self.w.receipts.get(h)
        elif r is None:                                    # node-style world: synthesize it
            t = self._txs().get(h)
            if t is None or not t.get("blockNumber"):
                return None
            t = self._mined(t)
            r = {"transactionHash": h, "from": t["from"], "to": t["to"], "status": "0x1",
                 "blockHash": t["blockHash"], "blockNumber": t["blockNumber"],
                 "gasUsed": "0x5208", "effectiveGasPrice": hex(10**9), "contractAddress": None,
                 "logs": []}
        if r is None or int(r["blockNumber"], 16) > self.n:
            return None
        return dict(r)

    def block_transactions(self, n):
        self._go("block_transactions")
        return [self._mined(t) for t in self.w.blocks.get(n, []) if t["hash"] not in self.hidden]

    def tx_count(self, address, tag):
        self._go("tx_count")
        counts = getattr(self.w, "nonces", None)
        if tag not in ("latest", "pending"):
            if counts is not None:
                return counts(address, int(tag, 16)) + self.nonce_bias
            return self._nonce_at(address, int(tag, 16)) + self.nonce_bias
        latest = self._latest()
        pending = getattr(self.w, "pending_nonce", None)
        if not isinstance(pending, int):
            pending = getattr(self.w, "pending", latest)
        return (latest if tag == "latest" else pending) + self.nonce_bias

    def _latest(self) -> int:
        v = getattr(self.w, "latest_nonce", None)
        return v if isinstance(v, int) else getattr(self.w, "latest", 6)

    def _nonce_at(self, address: str, b: int) -> int:
        """Node-style world: the wallet's nonce at block `b` = base + its txs mined by then."""
        mined = [(n, t) for n, ts in self.w.blocks.items() for t in ts if t["from"] == address]
        latest = self._latest()
        return (latest - len(mined)) + sum(1 for n, _ in mined if n <= b)

    def native_balance(self, address, block):
        self._go("native_balance")
        if self.no_history:
            raise RpcError(-32000)
        return self.w.native_balance(address, block) + self.nat_shift

    def token_balance(self, token, address, block):
        self._go("token_balance")
        if self.no_history:
            raise RpcError(-32000)
        return self.w.token_balance(token, address, block) + self.tok_shift


def providers(world, clock, n: int = 2, **kw) -> list[Provider]:
    """`n` operationally distinct providers (alpha.test, beta.test, ...) over one world."""
    names = ["alpha", "beta", "gamma", "delta"]
    return [Provider(world, f"node.{names[i]}.test", clock, **kw) for i in range(n)]


def corroborator(world, clock, n: int = 2, *, finality: int = 12, **kw) -> tuple[Corroborator, list[Provider]]:
    ps = providers(world, clock, n)
    return Corroborator(ps, finality=finality, clock=clock, **kw), ps


def fake_evidence(clock, leg, outcome: str, *, tx_hash=None, block_number: int = 1000,
                  block_hash_: str | None = None, status: int = 1, confirmations: int = 6,
                  finality: int = 12, fill=None, basis: str | None = None, taken_at=None,
                  sources=("alpha.test", "beta.test"), **facts) -> dict:
    """A syntactically valid, corroborated provenance record for tests that drive the LEDGER
    directly (the ledger checks structure; wallet/corroboration.py establishes truth)."""
    now = float(clock()) if taken_at is None else taken_at
    bh = block_hash_ or block_hash(block_number)
    f = {"leg_id": leg["id"], "leg_version": leg["version"], "outcome": outcome,
         "tx_hash": tx_hash or leg.get("final_tx_hash") or leg.get("tx_hash"),
         "block_number": block_number, "block_hash": bh, "receipt_status": status,
         "confirmations": confirmations}
    if basis:
        f["basis"] = basis
    f.update(facts)
    ev = {"schema": 1, "chain_id": 99901, "taken_at": now, "max_head_age_s": 60,
          "finality": finality,
          "anchor": {"number": 2000, "hash": block_hash(2000), "timestamp": int(now) - 2},
          "sources": [{"name": f"node.{s}", "operator": s, "head_number": 2000,
                       "head_hash": block_hash(2000), "head_timestamp": int(now) - 2}
                      for s in sources],
          "failed": [], "problems": [], "calls": 8, "reads": [], "source_agreement": True,
          "facts": f}
    if fill is not None:
        ev["fill"] = {"verdict": fill.verdict, "method_agreement": fill.method_agreement,
                      "inputs_source_agreed": True, "basis": fill.basis}
    return ev


def session_of(world, clock=None, n: int = 2, **kw):
    """A corroborated session over `n` providers of `world` (for unit tests of the fill code)."""
    corro, _ = corroborator(world, clock or (lambda: NOW), n, **kw)
    return corro.session()
