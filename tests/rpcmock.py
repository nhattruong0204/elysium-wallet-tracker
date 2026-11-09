"""An ISOLATED loopback JSON-RPC server that plays chain-99901 providers for the read-only RPC
diagnostics (#95 F-07). Honest, lagging, stale, forked, lying, history-ignoring, pruned,
rate-limited, slow, malformed and wrong-chain behaviours are scripted here, so adversarial cases are
never run against real third-party endpoints. Every method that reaches the wire is recorded."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SENDER = "0x" + "5e" * 20
FIRST = 1_000_000          # first block of the scripted chain; one tx from SENDER per block
GAS = 21_000 * 10**9
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"


def bhash(n: int, fork: int = 0) -> str:
    return "0x" + hashlib.sha256(f"{n}:{fork}".encode()).hexdigest()


class Chain:
    """Time-driven canonical chain: `block_time` seconds per block, head timestamp ~ now."""

    def __init__(self, *, block_time: float = 0.05, history: int = 5_000_000) -> None:
        self.block_time = block_time
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        self.base = FIRST + history                  # head at t0
        self.forks: dict[int, int] = {}              # height -> fork id (a reorg rewrites these)

    def head(self) -> int:
        return self.base + int((time.monotonic() - self.t0) / self.block_time)

    def ts(self, n: int) -> int:
        return int(self.wall0 - (self.base - n) * self.block_time)

    def hash(self, n: int) -> str:
        return bhash(n, self.forks.get(n, 0))

    def reorg(self, depth: int, fork: int = 1) -> None:
        h = self.head()
        for n in range(h - depth + 1, h + 1000):
            self.forks[n] = fork

    @staticmethod
    def nonce_at(b: int) -> int:                     # SENDER's count at the END of block b
        return max(0, b - FIRST + 1)

    def balance_at(self, b: int) -> int:
        return 10**24 - self.nonce_at(b) * GAS


@dataclass
class Behaviour:
    chain_id: int = 99901
    lag: int = 0
    stale_at: int | None = None            # head frozen at this height
    finalized_lag: int | None = 300        # None => tag not served
    ignore_block_param: bool = False
    lie_heights: set[int] = field(default_factory=set)
    lie_from_head_minus: int | None = None  # every height <= head-k returns a different hash
    archive_depth: int | None = None       # historical state refused beyond this depth
    http_429_every: int = 0
    retry_after: str | None = "2"
    slow_s: float = 0.0
    malformed_every: int = 0
    client_version: str = "Nitro/v3.6.0-mock"
    server_header: str = "mock-node"
    no_multicall: bool = False


class Provider:
    def __init__(self, chain: Chain, beh: Behaviour | None = None) -> None:
        self.chain, self.b = chain, beh or Behaviour()
        self.methods: list[str] = []
        self.n = 0
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://127.0.0.1:{port}/v1/SECRETKEY{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---------------------------------------------------------------- chain view
    def head(self) -> int:
        h = self.chain.head() - self.b.lag
        return min(h, self.b.stale_at) if self.b.stale_at is not None else h

    def hash(self, n: int) -> str:
        if n in self.b.lie_heights or (self.b.lie_from_head_minus is not None
                                       and n <= self.chain.head() - self.b.lie_from_head_minus):
            return bhash(n, 99)
        return self.chain.hash(n)

    def block(self, n: int, full: bool) -> dict | None:
        if n > self.head() or n < 0:
            return None
        txs: list = []
        if n >= FIRST:
            tx = {"hash": bhash(n, 7), "from": SENDER, "to": "0x" + "11" * 20,
                  "nonce": hex(n - FIRST), "blockNumber": hex(n), "blockHash": self.hash(n),
                  "value": "0x0", "input": "0x"}
            txs = [tx if full else tx["hash"]]
        return {"number": hex(n), "hash": self.hash(n), "parentHash": self.hash(n - 1),
                "timestamp": hex(self.chain.ts(n)), "transactions": txs}

    def _block_param(self, tag: str) -> int:
        h = self.head()
        if tag in ("latest", "pending"):
            return h
        if tag == "safe":
            return h - (self.b.finalized_lag or 0) // 2
        if tag == "finalized":
            return h - (self.b.finalized_lag or 0)
        n = int(tag, 16)
        if self.b.ignore_block_param:
            return h
        if self.b.archive_depth is not None and h - n > self.b.archive_depth:
            raise LookupError("missing trie node")
        return n

    # ---------------------------------------------------------------- JSON-RPC
    def dispatch(self, method: str, params: list):
        if method == "eth_chainId":
            return hex(self.b.chain_id)
        if method == "web3_clientVersion":
            return self.b.client_version
        if method == "eth_blockNumber":
            return hex(self.head())
        if method == "eth_getBlockByNumber":
            tag, full = params
            if tag == "finalized" and self.b.finalized_lag is None:
                raise ValueError("finalized not supported")
            if tag in ("safe",) and self.b.finalized_lag is None:
                raise ValueError("safe not supported")
            n = self.head() if tag == "latest" else (
                self.head() - (self.b.finalized_lag or 0) if tag == "finalized" else
                self.head() - (self.b.finalized_lag or 0) // 2 if tag == "safe" else int(tag, 16))
            return self.block(n, bool(full))
        if method == "eth_getTransactionByHash":
            n = self._tx_block(params[0])
            if n is None:
                return None
            return self.block(n, True)["transactions"][0]  # type: ignore[index]
        if method == "eth_getTransactionReceipt":
            n = self._tx_block(params[0])
            if n is None:
                return None
            return {"transactionHash": params[0], "from": SENDER, "to": "0x" + "11" * 20,
                    "blockNumber": hex(n), "blockHash": self.hash(n), "status": "0x1",
                    "gasUsed": "0x5208", "logs": []}
        if method == "eth_getTransactionCount":
            return hex(Chain.nonce_at(self._block_param(params[1])))
        if method == "eth_getBalance":
            return hex(self.chain.balance_at(self._block_param(params[1])))
        if method == "eth_getCode":
            return "0x" if self.b.no_multicall or params[0] != MULTICALL3 else "0x6080"
        if method == "eth_call":
            n = self._block_param(params[1])
            return "0x" + hex(n)[2:].rjust(64, "0")
        raise ValueError("method not found")

    def _tx_block(self, h: str) -> int | None:
        head = self.head()
        for n in range(head, max(FIRST, head - 500), -1):
            if bhash(n, 7) == h:
                return n
        return None

    def _handler(self):
        prov = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):          # quiet
                pass

            def do_POST(self):                  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                req = json.loads(raw)
                with prov._lock:
                    prov.n += 1
                    k = prov.n
                    prov.methods.append(req.get("method"))
                if prov.b.slow_s:
                    time.sleep(prov.b.slow_s)
                if prov.b.http_429_every and k % prov.b.http_429_every == 0:
                    self.send_response(429)
                    if prov.b.retry_after:
                        self.send_header("Retry-After", prov.b.retry_after)
                    self.end_headers()
                    return
                if prov.b.malformed_every and k % prov.b.malformed_every == 0:
                    body = b"<html>gateway</html>"
                else:
                    try:
                        res = {"jsonrpc": "2.0", "id": req["id"],
                               "result": prov.dispatch(req["method"], req.get("params", []))}
                    except LookupError as e:
                        res = {"jsonrpc": "2.0", "id": req["id"],
                               "error": {"code": -32000, "message": str(e)}}
                    except ValueError as e:
                        res = {"jsonrpc": "2.0", "id": req["id"],
                               "error": {"code": -32601, "message": str(e)}}
                    body = json.dumps(res).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("server", prov.b.server_header)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        return H
