"""JSON-RPC client with endpoint failover, retries with backoff, and batching.

Safety rules:
  * Every endpoint's eth_chainId is verified before its first real request. An endpoint on
    the wrong chain is quarantined for the life of the process, so failover can never mix
    networks silently.
  * Rate-limit / overload JSON-RPC errors are retried with backoff instead of being returned
    to callers (who might otherwise react by sending *more* requests, e.g. splitting ranges).
  * URLs are redacted wherever they are logged, because provider URLs often embed API keys.
  * A scan range can be pinned to one endpoint (``with rpc.pinned():``) so its head, logs,
    transactions and receipts all come from the same node. Mixing nodes inside one range
    risks asking a slightly-behind node for logs of blocks it has not seen yet: it would
    answer "no logs" and those trades would be skipped while the cursor advances.
  * Endpoints whose head is more than ``max_head_lag`` blocks behind the best one are
    skipped until they catch up (e.g. a "batch-confirmed" RPC hours behind the live head).
  * Batch limits are learned per endpoint ("Batch size must be 1 to 20"), so one strict
    provider does not turn batching off for the others.
"""

from __future__ import annotations

import itertools
import logging
import random
import re
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

_RETRYABLE_CODES = {-32005, -32029, -32603, 429}
_RETRYABLE_MSG = re.compile(r"rate.?limit|too many requests|capacity|temporar|try again|"
                            r"overload|busy|header not found|request count|quota", re.I)


# Errors meaning "your query is too big": split the range instead of retrying it.
RANGE_ERROR_MSG = re.compile(r"range|too many (results|logs|blocks)|more than \d+|at most [\d,]+|in pieces|"
                             r"limit exceeded|"
                             r"exceed|response size|query timeout|block range", re.I)


# Elysium nodes: "block range extends beyond current head block"
_BEYOND_HEAD_MSG = re.compile(r"beyond (the )?(current )?head|header not found|unknown block", re.I)
_BATCH_LIMIT_MSG = re.compile(r"batch[^0-9]{0,40}?(?:\d+\s*(?:to|-)\s*)?(\d+)", re.I)


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
        if _BEYOND_HEAD_MSG.search(msg):
            return False  # the node is behind: splitting would not help, retry the range
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
                 batch_size: int = 50, expected_chain_id: int | None = None,
                 max_head_lag: int | None = None):
        if not urls:
            raise ValueError("no RPC urls")
        self.urls = list(urls)
        self.timeout = timeout
        self.max_retries = max_retries
        self.batch_size = batch_size
        self.expected_chain_id = expected_chain_id
        self._idx = 0
        self._ids = itertools.count(1)
        self.max_head_lag = max_head_lag
        self._batch_limit: dict[str, int] = {}     # learned per endpoint
        self._batch_rejects: dict[str, int] = {}
        self._batch_off: set[str] = set()          # endpoints that reject batches outright
        self._verified: set[str] = set()
        self._quarantined: dict[str, str] = {}     # wrong chain: never used again
        self._lagging: set[str] = set()            # behind / unreachable: skipped for now
        self._pinned: str | None = None
        self.heads: dict[str, int | None] = {}
        # Cumulative cost counters (monotonic). Callers diff two snapshots to price one piece
        # of work: HTTP requests, JSON-RPC calls, seconds spent waiting, per-method calls.
        self.metrics: dict[str, float] = defaultdict(float)
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"

    # --- endpoint management ----------------------------------------------------
    @property
    def url(self) -> str:
        if self._pinned is not None:
            if self._pinned in self._quarantined:
                raise RpcError(f"pinned endpoint {redact(self._pinned)} was quarantined")
            return self._pinned
        live = self.live_urls
        if not live:
            raise RpcError("all RPC endpoints quarantined: "
                           + "; ".join(f"{redact(u)}: {r}" for u, r in self._quarantined.items()))
        return live[self._idx % len(live)]

    @property
    def live_urls(self) -> list[str]:
        """Endpoints on the right chain, preferring those not lagging. If every endpoint is
        lagging/unreachable, all of them stay eligible: something is better than nothing."""
        ok = [u for u in self.urls if u not in self._quarantined]
        fresh = [u for u in ok if u not in self._lagging]
        return fresh or ok

    def _rotate(self) -> None:
        if self._pinned is not None:
            return  # pinned() moves on once, after the unit of work failed
        self._idx += 1
        if len(self.live_urls) > 1:
            log.warning("RPC failover -> %s", redact(self.url))

    @contextmanager
    def pinned(self) -> Iterator[str]:
        """Send every call inside the block to one endpoint. Transport failures are retried
        on that endpoint briefly, then raised, so the caller retries the whole unit of work;
        the next unpinned use starts on the next endpoint."""
        url = self.url
        self._pinned = url
        try:
            yield url
        except RpcError:
            self._pinned = None
            if self.url == url:  # start the retry on the next endpoint
                self._idx += 1
            raise
        finally:
            self._pinned = None

    def refresh_heads(self) -> dict[str, int | None]:
        """Fetch every endpoint's head; mark those behind the best by more than max_head_lag
        (or unreachable) as lagging. Returns {url: head or None}."""
        heads: dict[str, int | None] = {}
        for u in self.urls:
            if u in self._quarantined:
                continue
            try:
                self._ensure_verified(u)
                r = self._raw_post(u, {"jsonrpc": "2.0", "id": 0, "method": "eth_blockNumber",
                                       "params": []})
                heads[u] = int(r["result"], 16)
            except (RpcError, requests.RequestException, ValueError, KeyError, TypeError) as e:
                log.debug("head check %s failed: %s", redact(u), e)
                heads[u] = None
        best = max((h for h in heads.values() if h is not None), default=None)
        lagging: set[str] = set()
        for u, h in heads.items():
            if h is None or (best is not None and self.max_head_lag is not None
                             and best - h > self.max_head_lag):
                lagging.add(u)
        for u in lagging - self._lagging:
            h = heads.get(u)
            log.warning("RPC %s %s; skipping it until it catches up", redact(u),
                        "unreachable" if h is None else f"{best - h} blocks behind")  # type: ignore[operator]
        for u in self._lagging - lagging:
            log.info("RPC %s caught up (head %s)", redact(u), heads.get(u))
        self._lagging = lagging
        self.heads = heads
        return heads

    def _raw_post(self, url: str, payload: Any) -> Any:
        m = self.metrics
        m["http"] += 1
        for item in (payload if isinstance(payload, list) else [payload]):
            m["calls"] += 1
            if isinstance(item, dict):
                m["m:" + str(item.get("method"))] += 1
        t0 = time.monotonic()
        try:
            return self._raw_post_inner(url, payload)
        finally:
            m["seconds"] += time.monotonic() - t0

    def _raw_post_inner(self, url: str, payload: Any) -> Any:
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
        attempts = min(2, self.max_retries + 1) if self._pinned else self.max_retries + 1
        for attempt in range(attempts):
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
        if self._pinned:
            raise RpcError(f"pinned endpoint {redact(self._pinned)} failed: {last}")
        raise RpcError(f"RPC unreachable after {attempts} attempts: {last}")

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
        i = 0
        while i < len(calls):
            n = max(1, min(self.batch_size, self._batch_limit.get(self.url, self.batch_size)))
            res = self._batch_chunk(calls[i:i + n])
            if res is None:  # learned a smaller limit for this endpoint: re-chunk
                continue
            out.extend(res)
            i += n
        return out

    def _batch_ok_for(self, url: str) -> bool:
        return url not in self._batch_off

    def _batch_chunk(self, calls: list[tuple[str, list[Any]]]) -> list[Any] | None:
        if not calls:
            return []
        if len(calls) > 1 and self._batch_ok_for(self.url):
            ids = [next(self._ids) for _ in calls]
            payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                       for i, (m, p) in zip(ids, calls, strict=True)]
            resp = self._post(payload)
            url = self.url  # the endpoint that answered (failover happens before retries)
            if isinstance(resp, list):
                self._batch_rejects[url] = 0
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
            err = resp.get("error") if isinstance(resp, dict) else None
            msg = str(err.get("message", "") if isinstance(err, dict) else err or "")
            lim = _BATCH_LIMIT_MSG.search(msg)
            if lim and 1 <= int(lim.group(1)) < len(calls):
                self._batch_limit[url] = int(lim.group(1))
                log.info("RPC %s accepts batches of at most %d", redact(url), self._batch_limit[url])
                return None
            self._batch_rejects[url] = self._batch_rejects.get(url, 0) + 1
            if self._batch_rejects[url] >= 3:
                log.warning("RPC %s rejected batch requests 3x; sequential for it", redact(url))
                self._batch_off.add(url)
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
