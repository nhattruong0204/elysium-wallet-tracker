"""JSON-RPC client with endpoint failover, retries with backoff, and batching."""

from __future__ import annotations

import itertools
import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger(__name__)


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class RpcClient:
    def __init__(self, urls: list[str], timeout: float = 15.0, max_retries: int = 4,
                 batch_size: int = 50):
        if not urls:
            raise ValueError("no RPC urls")
        self.urls = urls
        self.timeout = timeout
        self.max_retries = max_retries
        self.batch_size = batch_size
        self._idx = 0
        self._ids = itertools.count(1)
        self._batch_ok = True
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"

    @property
    def url(self) -> str:
        return self.urls[self._idx % len(self.urls)]

    def _rotate(self) -> None:
        self._idx += 1
        if len(self.urls) > 1:
            log.warning("RPC failover -> %s", self.url)

    def _post(self, payload: Any) -> Any:
        """Transport-level retries only. JSON-RPC errors are returned to the caller."""
        delay = 0.5
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.post(self.url, json=payload, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RpcError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, RpcError, ValueError) as e:
                last = e
                log.debug("RPC attempt %d on %s failed: %s", attempt + 1, self.url, e)
                self._rotate()
                time.sleep(delay + random.random() * 0.25)
                delay = min(delay * 2, 10)
        raise RpcError(f"RPC unreachable after {self.max_retries + 1} attempts: {last}")

    def call(self, method: str, params: list[Any]) -> Any:
        resp = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method,
                           "params": params})
        if resp.get("error"):
            err = resp["error"]
            raise RpcError(f"{method}: {err.get('message')}", err.get("code"))
        return resp.get("result")

    def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """Returns results in order; a failed item becomes an RpcError instance (not raised)."""
        out: list[Any] = []
        for i in range(0, len(calls), self.batch_size):
            out.extend(self._batch_chunk(calls[i:i + self.batch_size]))
        return out

    def _batch_chunk(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        if not calls:
            return []
        if self._batch_ok:
            ids = [next(self._ids) for _ in calls]
            payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                       for i, (m, p) in zip(ids, calls)]
            resp = self._post(payload)
            if isinstance(resp, list):
                by_id = {r.get("id"): r for r in resp}
                results = []
                for i, (m, _) in zip(ids, calls):
                    r = by_id.get(i)
                    if r is None:
                        results.append(RpcError(f"{m}: missing in batch response"))
                    elif r.get("error"):
                        results.append(RpcError(f"{m}: {r['error'].get('message')}",
                                                r["error"].get("code")))
                    else:
                        results.append(r.get("result"))
                return results
            log.warning("RPC endpoint rejected batch requests; falling back to sequential")
            self._batch_ok = False
        results = []
        for m, p in calls:
            try:
                results.append(self.call(m, p))
            except RpcError as e:
                results.append(e)
        return results

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)
