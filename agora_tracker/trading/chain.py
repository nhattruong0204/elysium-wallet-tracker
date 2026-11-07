"""Bounded private paper research transport. No write method can be dispatched."""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import requests
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError, EncodingError
from eth_hash.auto import keccak

from .http import bounded_json
from .paper import ADDR, CHAIN, TradeRefused


class SimulationReverted(TradeRefused):
    pass


READS = frozenset(
    {
        "eth_chainId",
        "eth_getBlockByNumber",
        "eth_getBalance",
        "eth_getCode",
        "eth_call",
        "eth_estimateGas",
        "eth_gasPrice",
    }
)


def address(value: str) -> str:
    if not isinstance(value, str) or not ADDR.fullmatch(value) or int(value, 16) == 0:
        raise TradeRefused("A nonzero public EVM address is required")
    return value.lower()


def uint(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        raise TradeRefused("Invalid RPC quantity")
    n = int(value, 16)
    if n >= 2**256:
        raise TradeRefused("RPC integer overflow")
    return n


def units(n: int, decimals: int) -> str:
    whole, frac = divmod(n, 10**decimals)
    return str(whole) if not decimals else f"{whole}.{frac:0{decimals}d}".rstrip("0").rstrip(".")


def call_data(abi: list[dict], name: str, values: list) -> str:
    f = next(x for x in abi if x.get("type") == "function" and x["name"] == name)
    types = [x["type"] for x in f["inputs"]]
    signature = name + "(" + ",".join(types) + ")"
    return "0x" + (keccak(signature.encode())[:4] + encode(types, values)).hex()


class ReadRpc:
    def __init__(self, url: str, *, now=time.time):
        if urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            raise TradeRefused("Paper RPC requires HTTPS")
        self.url, self.now = url, now
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._id = 0
        self._budget = threading.local()
        self.timings: list[tuple[str, float]] = []
        self.trace: list[dict] = []

    @contextmanager
    def budget(self, seconds: float):
        previous = getattr(self._budget, "deadline", None)
        self._budget.deadline = min(previous or float("inf"), time.monotonic() + seconds)
        try:
            yield
        finally:
            self._budget.deadline = previous

    @contextmanager
    def operation(self, name: str):
        previous = getattr(self._budget, "operation", None)
        self._budget.operation = name
        try:
            yield
        finally:
            self._budget.operation = previous

    def call(self, method: str, params: list):
        if method not in READS:
            raise TradeRefused("Only allowlisted read-only RPC methods are permitted")
        with self._lock:
            self._id += 1
            started = time.monotonic()
            remaining = (getattr(self._budget, "deadline", None) or float("inf")) - started
            if remaining <= 0:
                raise TradeRefused("Paper RPC request budget exhausted; refresh later")
            outcome = "refused"
            try:
                r = self.session.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
                    timeout=min(5, remaining),
                    allow_redirects=False,
                    stream=True,
                )
                if r.status_code != 200:
                    r.close()
                    raise TradeRefused("Paper RPC unavailable or rate limited")
                d = bounded_json(r, deadline=started+min(5, remaining))
                if (
                    not isinstance(d, dict)
                    or d.get("jsonrpc") != "2.0"
                    or type(d.get("id")) is not int
                    or d["id"] != self._id
                ):
                    raise TradeRefused("Invalid paper RPC response")
                if isinstance(d.get("error"), dict) and d["error"].get("code") == 3:
                    raise SimulationReverted(
                        "Proposed transaction reverted in read-only simulation"
                    )
                if "error" in d or "result" not in d:
                    raise TradeRefused("Read-only simulation/call refused by RPC")
                outcome = "success"
                return d["result"]
            except (requests.RequestException, ValueError) as e:
                if isinstance(e, TradeRefused):
                    raise
                raise TradeRefused("Paper RPC transport unavailable") from None
            finally:
                self.timings.append((method, (time.monotonic() - started) * 1000))
                self.timings = self.timings[-128:]
                self.trace.append(
                    {
                        "operation": getattr(self._budget, "operation", None) or method,
                        "method": method,
                        "ms": (time.monotonic() - started) * 1000,
                        "outcome": outcome,
                    }
                )
                self.trace = self.trace[-128:]

    def snapshot(self) -> tuple[str, dict]:
        with self.operation("chain.verify"):
            chain = uint(self.call("eth_chainId", []))
        if chain != CHAIN:
            raise TradeRefused("Paper RPC chain mismatch")
        with self.operation("head.latest"):
            block = self.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(block, dict):
            raise TradeRefused("Invalid block")
        timestamp = uint(block.get("timestamp"))
        if not 0 <= self.now() - timestamp <= 30:
            raise TradeRefused("Paper RPC head is stale or future dated")
        if not isinstance(block.get("hash"), str) or not re.fullmatch(
            r"0x[0-9a-fA-F]{64}", block["hash"]
        ):
            raise TradeRefused("Invalid block hash")
        return hex(uint(block.get("number"))), block

    def check_block(self, block: str, expected: dict) -> None:
        with self.operation("head.recheck"):
            current = self.call("eth_getBlockByNumber", [block, False])
        if (
            not isinstance(current, dict)
            or current.get("hash") != expected.get("hash")
            or current.get("number") != block
            or current.get("timestamp") != expected.get("timestamp")
        ):
            raise TradeRefused("Pinned block changed during quote; refresh required")

    def view(self, target: str, data: str, block: str, types: list[str]) -> tuple:
        result = self.call("eth_call", [{"to": address(target), "data": data}, block])
        try:
            if not isinstance(result, str) or not result.startswith("0x") or len(result) > 8194:
                raise ValueError
            raw = bytes.fromhex(result[2:])
            decoded = decode(types, raw)
            if encode(types, list(decoded)) != raw:
                raise ValueError
            return decoded
        except (ValueError, TypeError, OverflowError, DecodingError, EncodingError):
            raise TradeRefused("Invalid ABI return data") from None

    def code_hash(self, target: str, block: str) -> str:
        result = self.call("eth_getCode", [address(target), block])
        if not isinstance(result, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", result):
            raise TradeRefused("Missing or malformed contract code")
        return "0x" + keccak(bytes.fromhex(result[2:])).hex()
