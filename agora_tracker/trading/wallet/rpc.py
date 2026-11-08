"""Isolated execution RPC (issue #93): read-only, bounded, breaker-protected.

Why a separate class: reconciliation and wallet-prompt checks must never compete with, or be
starved by, the scanner / notifier / Revival RPC use, and a hostile or flaky endpoint must not be
able to hang a request thread. What it will and will not do:

* ONLY read methods are allowlisted. `eth_sendRawTransaction`, `eth_sendTransaction` and signing
  methods are refused before anything is sent: the wallet is the only broadcaster, so there is no
  server-side submission or rebroadcast path to misuse. (A test enumerates the forbidden names.)
* chain id 99901 is verified before the first call and every `CHAIN_RECHECK_S`; a mismatch trips
  the breaker hard.
* the head must be fresh (<= `MAX_HEAD_AGE_S`) before it is trusted for decisions.
* concurrency is bounded by a semaphore with a short wait: overload fails fast (`RpcBusy`).
* 429/503 honour `Retry-After` (capped), then back off with jitter; repeated failure opens a
  circuit breaker that fails fast until a half-open probe succeeds.
* the URL (which may embed an API key) and raw bodies are never put in an exception, a log line or
  a metric label; only the host name is logged.
* latency and outcome counters per method are exposed by `metrics()`. No endpoint is claimed to be
  "fastest": that needs same-workload measurements from the production VPS (see the docs)."""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..paper import CHAIN, TradeRefused
from .approvals import ChainReverted, Head

log = logging.getLogger(__name__)

READ_METHODS = frozenset({
    "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getTransactionByHash",
    "eth_getTransactionReceipt", "eth_getTransactionCount", "eth_call", "eth_getCode",
    "eth_estimateGas", "eth_gasPrice", "eth_getBalance",
})
FORBIDDEN_PREFIXES = ("eth_send", "eth_sign", "personal_", "wallet_", "miner_", "admin_", "debug_",
                      "txpool_", "engine_")
MAX_HEAD_AGE_S = 30
CHAIN_RECHECK_S = 300.0
MAX_BODY = 1_000_000
RETRY_AFTER_CAP_S = 5.0
LATENCY_BUCKETS_MS = (25, 50, 100, 250, 500, 1000, 2500, 5000)
HEX = re.compile(r"^0x[0-9a-fA-F]*$")
HASH = re.compile(r"^0x[0-9a-f]{64}$")
ADDR = re.compile(r"^0x[0-9a-f]{40}$")
BLOCK_TAG = re.compile(r"^0x(?:0|[1-9a-f][0-9a-f]{0,15})$")


class RpcUnavailable(TradeRefused):
    """Endpoint unusable right now (transport, breaker, wrong chain, stale head, malformed)."""


class RpcBusy(RpcUnavailable):
    """Execution RPC concurrency budget exhausted (fail fast; never queue behind a slow node)."""


class RpcError(TradeRefused):
    def __init__(self, code: int) -> None:
        super().__init__("execution RPC returned an error")
        self.code = code


class Transport(Protocol):
    def post(self, url: str, payload: dict, timeout: float) -> tuple[int, dict, bytes]: ...


class RequestsTransport:
    """Default transport: no redirects, no proxies from the environment surprises, bounded body."""

    def __init__(self) -> None:
        import requests
        self._s = requests.Session()
        self._s.trust_env = False

    def post(self, url: str, payload: dict, timeout: float) -> tuple[int, dict, bytes]:
        import requests
        try:
            r = self._s.post(url, json=payload, timeout=timeout, allow_redirects=False, stream=True)
            body = r.raw.read(MAX_BODY + 1, decode_content=True)
            headers = {k.lower(): v for k, v in r.headers.items()}
            status = r.status_code
            r.close()
        except requests.RequestException:
            raise RpcUnavailable("execution RPC transport error") from None
        if len(body) > MAX_BODY:
            raise RpcUnavailable("execution RPC response too large")
        return status, headers, body


class CircuitBreaker:
    def __init__(self, *, threshold: int = 5, reset_after: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.reset_after, self.clock = threshold, reset_after, clock
        self._fails = 0
        self._opened_at: float | None = None
        self._probe = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            return "half_open" if self.clock() - self._opened_at >= self.reset_after else "open"

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if self.clock() - self._opened_at >= self.reset_after and not self._probe:
                self._probe = True               # exactly one half-open probe at a time
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self._fails, self._opened_at, self._probe = 0, None, False

    def failure(self, *, hard: bool = False) -> None:
        with self._lock:
            self._fails += 1
            self._probe = False
            if hard or self._fails >= self.threshold or self._opened_at is not None:
                self._opened_at = self.clock()


def _qty(v: object) -> int:
    if not isinstance(v, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", v):
        raise RpcUnavailable("malformed quantity from execution RPC")
    return int(v, 16)


class ExecutionRpc:
    def __init__(self, url: str, *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 max_concurrency: int = 4, queue_wait: float = 0.25, timeout: float = 5.0,
                 retries: int = 2, breaker: CircuitBreaker | None = None,
                 chain_id: int = CHAIN) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("execution RPC url must be http(s)")
        self._url = url
        self.host = parts.hostname                 # the only part of the URL ever logged
        self.transport = transport or RequestsTransport()
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.timeout, self.retries, self.queue_wait = timeout, retries, queue_wait
        self.breaker = breaker or CircuitBreaker(clock=clock)
        self.chain_id = chain_id
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._id = 0
        self._lock = threading.Lock()
        self._chain_ok_at: float | None = None
        self._m: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ metrics
    def _count(self, method: str, outcome: str, ms: float | None = None) -> None:
        with self._lock:
            m = self._m.setdefault(method, {"ok": 0, "error": 0, "reverted": 0, "rate_limited": 0,
                                            "unavailable": 0, "buckets": [0] * (len(LATENCY_BUCKETS_MS) + 1),
                                            "total_ms": 0.0})
            m[outcome] += 1
            if ms is not None:
                m["total_ms"] += ms
                i = next((i for i, b in enumerate(LATENCY_BUCKETS_MS) if ms <= b), len(LATENCY_BUCKETS_MS))
                m["buckets"][i] += 1

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            methods = {k: {**v, "buckets": list(v["buckets"])} for k, v in self._m.items()}
        return {"host": self.host, "breaker": self.breaker.state, "methods": methods,
                "bucket_upper_ms": list(LATENCY_BUCKETS_MS)}

    # ------------------------------------------------------------------ core
    def rpc(self, method: str, params: list) -> Any:
        if method not in READ_METHODS or method.startswith(FORBIDDEN_PREFIXES):
            raise TradeRefused("method is not permitted on the execution RPC")
        if not self.breaker.allow():
            self._count(method, "unavailable")
            raise RpcUnavailable("execution RPC circuit is open")
        if not self._slots.acquire(timeout=self.queue_wait):
            self._count(method, "unavailable")
            raise RpcBusy("execution RPC is at its concurrency limit")
        try:
            if method != "eth_chainId":
                self._verify_chain()
            return self._send(method, params)
        finally:
            self._slots.release()

    def _verify_chain(self) -> None:
        now = self.clock()
        if self._chain_ok_at is not None and now - self._chain_ok_at < CHAIN_RECHECK_S:
            return
        got = _qty(self._send("eth_chainId", []))
        if got != self.chain_id:
            self.breaker.failure(hard=True)
            log.error("execution RPC host=%s is on the wrong chain", self.host)
            raise RpcUnavailable("execution RPC is on the wrong chain")
        self._chain_ok_at = now

    def _send(self, method: str, params: list) -> Any:
        attempt = 0
        while True:
            with self._lock:
                self._id += 1
                rid = self._id
            started = self.clock()
            try:
                status, headers, body = self.transport.post(
                    self._url, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                    self.timeout)
            except RpcUnavailable:
                ms = (self.clock() - started) * 1000
                self._count(method, "unavailable", ms)
                if attempt < self.retries:
                    attempt += 1
                    self._backoff(attempt, None)
                    continue
                self.breaker.failure()
                raise
            ms = (self.clock() - started) * 1000
            if status in (429, 503):
                self._count(method, "rate_limited", ms)
                if attempt < self.retries:
                    attempt += 1
                    self._backoff(attempt, headers.get("retry-after"))
                    continue
                self.breaker.failure()
                raise RpcUnavailable("execution RPC is rate limiting")
            if status != 200:
                self._count(method, "unavailable", ms)
                self.breaker.failure()
                raise RpcUnavailable("execution RPC returned an unexpected status")
            return self._parse(method, rid, body, ms)

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(0.1 * (2 ** attempt), 2.0) * random.uniform(0.5, 1.0)
        if retry_after and re.fullmatch(r"\d{1,3}", retry_after):
            delay = max(delay, min(float(retry_after), RETRY_AFTER_CAP_S))
        self.sleep(delay)

    def _parse(self, method: str, rid: int, body: bytes, ms: float) -> Any:
        import json
        try:
            d = json.loads(body)
        except ValueError:
            d = None
        if (not isinstance(d, dict) or d.get("jsonrpc") != "2.0" or type(d.get("id")) is not int
                or d["id"] != rid):
            self._count(method, "unavailable", ms)
            self.breaker.failure()
            raise RpcUnavailable("execution RPC sent an invalid response")
        err = d.get("error")
        if isinstance(err, dict):
            self.breaker.success()                  # the node is healthy; the call was refused
            raw_code = err.get("code")
            code: int = raw_code if type(raw_code) is int else -1
            if code == 3 or "revert" in str(err.get("message", "")).lower():
                self._count(method, "reverted", ms)
                raise ChainReverted("call reverted")
            self._count(method, "error", ms)
            raise RpcError(code)
        if "result" not in d:
            self._count(method, "unavailable", ms)
            self.breaker.failure()
            raise RpcUnavailable("execution RPC sent no result")
        self.breaker.success()
        self._count(method, "ok", ms)
        return d["result"]

    # ------------------------------------------------------------------ typed reads
    def head(self) -> Head:
        b = self.rpc("eth_getBlockByNumber", ["latest", False])
        if not isinstance(b, dict) or not isinstance(b.get("hash"), str) or not HASH.fullmatch(b["hash"]):
            raise RpcUnavailable("malformed head block")
        h = Head(_qty(b.get("number")), b["hash"], _qty(b.get("timestamp")))
        age = self.wall() - h.timestamp
        if not -2 <= age <= MAX_HEAD_AGE_S:
            raise RpcUnavailable("execution RPC head is stale or in the future")
        return h

    def call(self, to: str, data: str, block: str, sender: str | None = None) -> str:
        tx: dict[str, str] = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        out = self.rpc("eth_call", [tx, block])
        if not isinstance(out, str) or not HEX.fullmatch(out) or len(out) > 8194:
            raise RpcUnavailable("malformed call result")
        return out.lower()

    def estimate_gas(self, tx: dict, block: str) -> int:
        return _qty(self.rpc("eth_estimateGas", [tx, block]))

    def gas_price(self) -> int:
        return _qty(self.rpc("eth_gasPrice", []))

    def code_hash(self, address: str, block: str) -> str:
        from eth_hash.auto import keccak
        code = self.rpc("eth_getCode", [address, block])
        if not isinstance(code, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", code):
            raise RpcUnavailable("missing or malformed contract code")
        return "0x" + keccak(bytes.fromhex(code[2:])).hex()

    def tx_count(self, address: str, tag: str) -> int:
        """Nonce of `address`: at "latest"/"pending" or (owner recovery, F-02) at a specific block
        given as a lower-case hex quantity, so evidence can be anchored to one block."""
        if (tag not in ("latest", "pending") and not BLOCK_TAG.fullmatch(tag)) \
                or not ADDR.fullmatch(address):
            raise TradeRefused("invalid nonce query")
        return _qty(self.rpc("eth_getTransactionCount", [address, tag]))

    def pending_nonce(self, address: str) -> int:
        return self.tx_count(address, "pending")

    def transaction(self, tx_hash: str) -> dict | None:
        if not HASH.fullmatch(tx_hash):
            raise TradeRefused("invalid transaction hash")
        out = self.rpc("eth_getTransactionByHash", [tx_hash])
        if out is None:
            return None
        if not isinstance(out, dict):
            raise RpcUnavailable("malformed transaction")
        return out

    def receipt(self, tx_hash: str) -> dict | None:
        if not HASH.fullmatch(tx_hash):
            raise TradeRefused("invalid transaction hash")
        out = self.rpc("eth_getTransactionReceipt", [tx_hash])
        if out is None:
            return None
        if not isinstance(out, dict):
            raise RpcUnavailable("malformed receipt")
        return out

    def block_transactions(self, number: int) -> list[dict]:
        b = self.rpc("eth_getBlockByNumber", [hex(number), True])
        if b is None:
            raise RpcUnavailable("block not available yet")
        if not isinstance(b, dict) or not isinstance(b.get("transactions"), list):
            raise RpcUnavailable("malformed block")
        return [t for t in b["transactions"] if isinstance(t, dict)]

    def block_header(self, number: int) -> dict | None:
        """Header (no transactions) of a canonical block, or None if the node does not have it."""
        b = self.rpc("eth_getBlockByNumber", [hex(number), False])
        if b is None:
            return None
        if (not isinstance(b, dict) or not isinstance(b.get("hash"), str)
                or not HASH.fullmatch(b["hash"]) or _qty(b.get("number")) != number):
            raise RpcUnavailable("malformed block header")
        return b

    def native_balance(self, address: str, block: int) -> int:
        if not ADDR.fullmatch(address):
            raise TradeRefused("invalid address")
        return _qty(self.rpc("eth_getBalance", [address, hex(block)]))

    def token_balance(self, token: str, address: str, block: int) -> int:
        if not ADDR.fullmatch(token) or not ADDR.fullmatch(address):
            raise TradeRefused("invalid address")
        out = self.call(token, "0x70a08231" + address[2:].rjust(64, "0"), hex(block))
        if not re.fullmatch(r"0x[0-9a-f]{64}", out):
            raise RpcUnavailable("malformed balance")
        return int(out, 16)
