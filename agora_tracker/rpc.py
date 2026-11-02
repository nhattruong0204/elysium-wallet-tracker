"""JSON-RPC client with endpoint failover, retries with backoff, and batching.

Safety rules:
  * Every endpoint's eth_chainId is verified before its first real request. An endpoint on
    the wrong chain is quarantined for the life of the process, so failover can never mix
    networks silently.
  * Rate-limit / overload JSON-RPC errors are retried with backoff instead of being returned
    to callers (who might otherwise react by sending *more* requests, e.g. splitting ranges).
  * URLs are redacted wherever they are logged, because provider URLs often embed API keys.
"""

from __future__ import annotations

import itertools
import logging
import random
import re
import time
from typing import Any
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

_RETRYABLE_CODES = {-32005, -32029, -32603, 429}
_RETRYABLE_MSG = re.compile(r"rate.?limit|too many requests|capacity|temporar|try again|"
                            r"overload|busy|header not found|request count|quota", re.I)


# Errors meaning "your query is too big": split the range instead of retrying it.
RANGE_ERROR_MSG = re.compile(r"range|too many (results|logs|blocks)|more than \d+|limit exceeded|"
                             r"exceed|response size|query timeout|block range", re.I)


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code

    @property
    def retryable(self) -> bool:
        if self.is_range_error:
            return False
        return self.code in _RETRYABLE_CODES or bool(_RETRYABLE_MSG.search(str(self)))

    @property
    def is_range_error(self) -> bool:
        msg = str(self)
        return bool(RANGE_ERROR_MSG.search(msg)) and not _RETRYABLE_MSG.search(msg)


class ChainMismatchError(RpcError):
    pass


def redact(url: str) -> str:
    """https://rpc.example.com/v2/abcdef123456... -> https://rpc.example.com/v2/***"""
    try:
        p = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    parts = []
    for seg in p.path.split("/"):
        long_token = len(seg) >= 16 and re.fullmatch(r"[A-Za-z0-9_\-]+", seg)
        parts.append("***" if long_token else seg)
    path = "/".join(parts)
    return f"{p.scheme}://{host}{path}" + ("?***" if p.query else "")


class RpcClient:
    def __init__(self, urls: list[str], timeout: float = 15.0, max_retries: int = 4,
                 batch_size: int = 50, expected_chain_id: int | None = None):
        if not urls:
            raise ValueError("no RPC urls")
        self.urls = list(urls)
        self.timeout = timeout
        self.max_retries = max_retries
        self.batch_size = batch_size
        self.expected_chain_id = expected_chain_id
        self._idx = 0
        self._ids = itertools.count(1)
        self._batch_ok = True
        self._batch_rejects = 0
        self._verified: set[str] = set()
        self._quarantined: dict[str, str] = {}
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"

    # --- endpoint management ----------------------------------------------------
    @property
    def url(self) -> str:
        live = self.live_urls
        if not live:
            raise RpcError("all RPC endpoints quarantined: "
                           + "; ".join(f"{redact(u)}: {r}" for u, r in self._quarantined.items()))
        return live[self._idx % len(live)]

    @property
    def live_urls(self) -> list[str]:
        return [u for u in self.urls if u not in self._quarantined]

    def _rotate(self) -> None:
        self._idx += 1
        if len(self.live_urls) > 1:
            log.warning("RPC failover -> %s", redact(self.url))

    def _raw_post(self, url: str, payload: Any) -> Any:
        r = self.session.post(url, json=payload, timeout=self.timeout)
        if r.status_code == 429 or r.status_code >= 500:
            raise RpcError(f"HTTP {r.status_code}", 429 if r.status_code == 429 else None)
        r.raise_for_status()
        return r.json()

    def _ensure_verified(self, url: str) -> None:
        if self.expected_chain_id is None or url in self._verified:
            return
        resp = self._raw_post(url, {"jsonrpc": "2.0", "id": 0, "method": "eth_chainId",
                                    "params": []})
        if not isinstance(resp, dict) or resp.get("error") or not resp.get("result"):
            raise RpcError(f"eth_chainId failed on {redact(url)}: {resp!r:.200}")
        cid = int(resp["result"], 16)
        if cid != self.expected_chain_id:
            reason = f"chain id {cid} != expected {self.expected_chain_id}"
            self._quarantined[url] = reason
            log.error("RPC %s QUARANTINED: %s", redact(url), reason)
            raise ChainMismatchError(f"{redact(url)}: {reason}")
        self._verified.add(url)
        log.info("RPC %s verified on chain %d", redact(url), cid)

    def verify_all(self) -> dict[str, str]:
        """Check every endpoint now. Returns {redacted_url: 'ok' | reason}."""
        out = {}
        for u in list(self.urls):
            try:
                self._ensure_verified(u)
                out[redact(u)] = "ok"
            except (RpcError, requests.RequestException, ValueError) as e:
                out[redact(u)] = self._quarantined.get(u, str(e))
        return out

    # --- transport --------------------------------------------------------------
    def _post(self, payload: Any) -> Any:
        """Transport-level retries. JSON-RPC errors are returned to the caller."""
        delay = 0.5
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            url = self.url  # raises when every endpoint is quarantined
            try:
                self._ensure_verified(url)
                return self._raw_post(url, payload)
            except ChainMismatchError as e:
                last = e  # quarantined; next iteration picks another endpoint
                continue
            except (requests.RequestException, RpcError, ValueError) as e:
                last = e
                log.debug("RPC attempt %d on %s failed: %s", attempt + 1, redact(url), e)
                self._rotate()
                time.sleep(delay + random.random() * 0.25)
                delay = min(delay * 2, 10)
        raise RpcError(f"RPC unreachable after {self.max_retries + 1} attempts: {last}")

    def call(self, method: str, params: list[Any]) -> Any:
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            resp = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method,
                               "params": params})
            if not isinstance(resp, dict):
                raise RpcError(f"{method}: malformed response {type(resp).__name__}")
            if not resp.get("error"):
                return resp.get("result")
            err = resp["error"] if isinstance(resp["error"], dict) else {"message": resp["error"]}
            e = RpcError(f"{method}: {err.get('message')}", err.get("code"))
            if not e.retryable or attempt == self.max_retries:
                raise e
            log.warning("%s rate-limited/overloaded on %s (%s); backing off %.1fs",
                        method, redact(self.url), e, delay)
            self._rotate()
            time.sleep(delay + random.random() * 0.5)
            delay = min(delay * 2, 30)
        raise AssertionError("unreachable")

    def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """Returns results in order; a failed item becomes an RpcError instance (not raised).
        Results are matched to requests by JSON-RPC id, never by response position."""
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
                       for i, (m, p) in zip(ids, calls, strict=True)]
            resp = self._post(payload)
            if isinstance(resp, list):
                self._batch_rejects = 0
                by_id = {r.get("id"): r for r in resp if isinstance(r, dict)}
                results: list[Any] = []
                for i, (m, _) in zip(ids, calls, strict=True):
                    r = by_id.get(i)
                    if r is None:
                        results.append(RpcError(f"{m}: missing in batch response"))
                    elif r.get("error"):
                        err = r["error"]
                        if not isinstance(err, dict):
                            err = {"message": err}
                        results.append(RpcError(f"{m}: {err.get('message')}", err.get("code")))
                    else:
                        results.append(r.get("result"))
                return results
            self._batch_rejects += 1
            if self._batch_rejects >= 3:
                log.warning("RPC endpoint rejected batch requests 3x; falling back to sequential")
                self._batch_ok = False
            else:
                log.info("non-list batch response (%.120r); retrying items sequentially", resp)
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
