"""Read-only RPC diagnostics for chain 99901 (#95 F-07, F-04 evidence, F-15 inventory).

Run by an operator against the REAL endpoints the worker will use, from the production VPS:

    TRADING_EXEC_RPC_URL=... TRADING_CORROBORATION_RPC_URLS=label=...,... \\
        python -m agora_tracker.trading.wallet.rpcdiag --duration 120 --out rpc-report.json

What it measures (each item is a named check with PASS / WARN / FAIL / SKIP and its data):

* chain id; client version and upstream fingerprint headers (independence *indicators* only);
* head freshness, block rate, parent-hash continuity and non-decreasing timestamps;
* the `safe` / `finalized` tags: served?, lag behind `latest` in blocks and seconds, progress;
* whether the node HONOURS a historical block parameter (a sender's nonce at N-1 vs N around one
  of its own transactions; Multicall3 `getBlockNumber()` at a past block when deployed);
* how far back historical `eth_getBalance` works (archive depth);
* transaction / receipt / block consistency for real transactions of a recent block;
* reorgs observed while sampling (a number whose hash changed), with depth;
* timeouts, connection failures, HTTP 429 / 5xx and `Retry-After`; p50/p95/p99 latency per method;
* optional, bounded concurrency probe;
* across providers: same chain, same hash at common heights, head skew, same finalized ancestry,
  same historical answers, and shared-upstream indicators.

Safety properties (tests enforce them):
* ONLY read methods are ever sent (the execution allowlist plus `web3_clientVersion` /
  `net_version`); anything that sends, signs or administers is refused before any I/O;
* every provider has a hard call budget and a request-rate ceiling; the concurrency probe is off
  unless asked for and capped; no transaction is built, signed or broadcast;
* endpoint URLs (which may embed API keys) are read from environment variables only and never
  written to the report, a log line or an exception: only the host name and operator label appear.

It can show that providers DISAGREE or share an upstream; it can never prove independence.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .rpc import FORBIDDEN_PREFIXES, READ_METHODS, operator_of

log = logging.getLogger(__name__)

CHAIN_ID = 99901
DIAG_METHODS = READ_METHODS | {"web3_clientVersion", "net_version"}
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
GET_BLOCK_NUMBER = "0x42cbb15c"                      # Multicall3.getBlockNumber()
FINGERPRINT_HEADERS = ("server", "via", "x-powered-by", "x-served-by", "x-upstream", "x-node-id")
ARCHIVE_DEPTHS = (1, 64, 1_000, 10_000, 100_000, 1_000_000, 10_000_000)
_LABELED = re.compile(r"^([a-z0-9][a-z0-9._-]{0,63})=(https?://.+)$", re.I)
HASH = re.compile(r"^0x[0-9a-f]{64}$")


class DiagError(Exception):
    """A classified failure of one call. `kind` is safe to report (never contains the URL)."""

    def __init__(self, kind: str, detail: str = "", retry_after: str | None = None) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail, self.retry_after = kind, detail[:120], retry_after


@dataclass
class CallRecord:
    method: str
    ms: float
    outcome: str                  # ok | rpc_error | timeout | connect | http_429 | http_5xx | ...
    retry_after: str | None = None


@dataclass
class Check:
    name: str
    status: str                   # PASS | WARN | FAIL | SKIP
    detail: str
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------------------- client
class RawRpc:
    """Minimal JSON-RPC client that RECORDS what happened (status, Retry-After, latency) instead of
    hiding it behind retries. Read-only, budgeted, rate-limited, URL never exposed."""

    def __init__(self, url: str, *, label: str | None = None, timeout: float = 8.0,
                 max_calls: int = 400, max_rps: float = 4.0, session: Any = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("RPC url must be http(s)")
        self._url = url
        self.host = parts.hostname
        self.operator = (label or "").lower() or operator_of(self.host)
        self.name = f"{self.operator}:{self.host}"
        self.timeout, self.max_calls, self.min_gap = timeout, max_calls, 1.0 / max(0.1, max_rps)
        if session is None:
            import requests
            session = requests.Session()
        self._s = session
        self.clock, self.sleep = clock, sleep
        self.records: list[CallRecord] = []
        self.headers: dict[str, str] = {}
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._id = 0

    @property
    def calls(self) -> int:
        return len(self.records)

    def _throttle(self) -> None:
        with self._lock:
            now = self.clock()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self.min_gap
        if wait > 0:
            self.sleep(wait)

    def call(self, method: str, params: list) -> Any:
        if method not in DIAG_METHODS or method.startswith(FORBIDDEN_PREFIXES):
            raise PermissionError(f"{method} is not a read method")     # before any I/O
        with self._lock:
            if len(self.records) >= self.max_calls:
                raise DiagError("budget_exhausted")
            self._id += 1
            rid = self._id
        self._throttle()
        t0 = self.clock()
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        try:
            r = self._s.post(self._url, json=payload, timeout=self.timeout,
                             headers={"content-type": "application/json"})
        except Exception as exc:                                      # noqa: BLE001 - classify
            ms = (self.clock() - t0) * 1000
            kind = "timeout" if "timeout" in type(exc).__name__.lower() else "connect"
            self._rec(method, ms, kind)
            raise DiagError(kind) from None
        ms = (self.clock() - t0) * 1000
        hdrs = {k.lower(): str(v)[:80] for k, v in getattr(r, "headers", {}).items()}
        if not self.headers:
            self.headers = {k: hdrs[k] for k in FINGERPRINT_HEADERS if k in hdrs}
        status = int(getattr(r, "status_code", 0))
        if status == 429:
            ra = hdrs.get("retry-after")
            self._rec(method, ms, "http_429", ra)
            raise DiagError("http_429", retry_after=ra)
        if status >= 500:
            self._rec(method, ms, "http_5xx")
            raise DiagError("http_5xx", str(status))
        if status != 200:
            self._rec(method, ms, "http_other")
            raise DiagError("http_other", str(status))
        try:
            body = r.json()
        except Exception:                                             # noqa: BLE001
            self._rec(method, ms, "malformed")
            raise DiagError("malformed", "not json") from None
        if not isinstance(body, dict) or body.get("id") != rid or body.get("jsonrpc") != "2.0":
            self._rec(method, ms, "malformed")
            raise DiagError("malformed", "bad envelope")
        if isinstance(body.get("error"), dict):
            self._rec(method, ms, "rpc_error")
            err = body["error"]
            raise DiagError("rpc_error", f"{err.get('code')}: {str(err.get('message', ''))[:80]}")
        if "result" not in body:
            self._rec(method, ms, "malformed")
            raise DiagError("malformed", "no result")
        self._rec(method, ms, "ok")
        return body["result"]

    def _rec(self, method: str, ms: float, outcome: str, ra: str | None = None) -> None:
        with self._lock:
            self.records.append(CallRecord(method, round(ms, 2), outcome, ra))

    # typed helpers -------------------------------------------------------------------------
    def block(self, tag: int | str, full: bool = False) -> dict | None:
        b = self.call("eth_getBlockByNumber", [hex(tag) if isinstance(tag, int) else tag, full])
        if b is not None and (not isinstance(b, dict) or not HASH.fullmatch(str(b.get("hash", "")))):
            raise DiagError("malformed", "block")
        return b


def qty(v: Any) -> int:
    if not isinstance(v, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", v):
        raise DiagError("malformed", "quantity")
    return int(v, 16)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return round(s[k], 1)


# ---------------------------------------------------------------------------------------- probe
class ProviderProbe:
    """All single-provider checks. Every failure is recorded as data, never raised."""

    def __init__(self, rpc: RawRpc, *, chain_id: int = CHAIN_ID,
                 wall: Callable[[], float] = time.time) -> None:
        self.rpc, self.chain_id, self.wall = rpc, chain_id, wall
        self.checks: list[Check] = []
        self.heads: list[tuple[float, int, str, int]] = []      # (wall, number, hash, timestamp)
        self.seen: dict[int, str] = {}
        self.reorgs: list[dict] = []
        self.finalized: list[tuple[float, int]] = []
        self.sample: dict[str, Any] = {}                         # a recent block with a tx

    def add(self, name: str, status: str, detail: str, **data: Any) -> Check:
        c = Check(name, status, detail, data)
        self.checks.append(c)
        return c

    def guarded(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except DiagError as e:
            self.add(name, "FAIL" if e.kind != "budget_exhausted" else "SKIP",
                     f"{e.kind} {e.detail}".strip())
        except Exception as e:                                       # noqa: BLE001 - report
            self.add(name, "FAIL", f"unexpected {type(e).__name__}")

    # -- identity
    def chain(self) -> bool:
        got = qty(self.rpc.call("eth_chainId", []))
        ok = got == self.chain_id
        self.add("chain_id", "PASS" if ok else "FAIL", f"eth_chainId={got}", chain_id=got)
        try:
            cv = self.rpc.call("web3_clientVersion", [])
            self.add("client_version", "PASS", "reported", client_version=str(cv)[:120])
        except DiagError as e:
            self.add("client_version", "SKIP", f"not served ({e.kind})")
        return ok

    # -- head sampling
    def observe_head(self, recheck: int = 3) -> None:
        b = self.rpc.block("latest")
        if b is None:
            raise DiagError("malformed", "no latest block")
        n, h, ts = qty(b["number"]), str(b["hash"]).lower(), qty(b["timestamp"])
        self.heads.append((self.wall(), n, h, ts))
        self._remember(n, h)
        for k in range(1, recheck + 1):              # re-read a few earlier heights: reorg check
            if n - k in self.seen:
                bb = self.rpc.block(n - k)
                if bb is not None:
                    self._remember(n - k, str(bb["hash"]).lower())

    def _remember(self, n: int, h: str) -> None:
        old = self.seen.get(n)
        if old is not None and old != h:
            depth = max(self.heads[-1][1] - n + 1, 1) if self.heads else 1
            self.reorgs.append({"number": n, "depth_at_detection": depth})
        self.seen[n] = h

    def observe_finalized(self) -> None:
        try:
            b = self.rpc.block("finalized")
        except DiagError as e:
            if e.kind == "rpc_error":
                return
            raise
        if b is not None:
            self.finalized.append((self.wall(), qty(b["number"])))

    # -- one-shot checks
    def continuity(self, depth: int = 20) -> None:
        latest = self.rpc.block("latest")
        assert latest is not None
        n = qty(latest["number"])
        prev = latest
        breaks, ts_back = [], 0
        for k in range(1, depth + 1):
            b = self.rpc.block(n - k)
            if b is None:
                breaks.append(n - k)
                break
            if str(prev.get("parentHash", "")).lower() != str(b["hash"]).lower():
                breaks.append(n - k)
            if qty(prev["timestamp"]) < qty(b["timestamp"]):
                ts_back += 1
            prev = b
        st = "PASS" if not breaks and not ts_back else "FAIL"
        self.add("parent_hash_continuity", st,
                 f"{depth} blocks below {n}: {len(breaks)} breaks, {ts_back} timestamp reversals",
                 breaks=breaks[:5], timestamp_reversals=ts_back)

    def tags(self) -> None:
        got: dict[str, Any] = {}
        for tag in ("safe", "finalized"):          # read the tags BEFORE latest: no false "above"
            try:
                got[tag] = self.rpc.block(tag)
            except DiagError as e:
                got[tag] = e
        latest = self.rpc.block("latest")
        assert latest is not None
        ln, lts = qty(latest["number"]), qty(latest["timestamp"])
        out: dict[str, Any] = {}
        for tag, b in got.items():
            if isinstance(b, DiagError):
                out[tag] = {"served": False, "error": b.kind}
                continue
            if b is None:
                out[tag] = {"served": False, "error": "null"}
                continue
            n = qty(b["number"])
            out[tag] = {"served": True, "number": n, "lag_blocks": ln - n,
                        "lag_s": lts - qty(b["timestamp"]), "above_latest": n > ln}
        fin = out["finalized"]
        if not fin.get("served"):
            st, why = "FAIL", "finalized tag not served: finality cannot be corroborated (F-04)"
        elif fin.get("above_latest"):
            st, why = "FAIL", "finalized is above latest (malformed)"
        elif fin["lag_s"] <= 2:
            # a rollup's parent-derived finality trails the sequencer by batch posting at least
            st, why = "WARN", (f"finalized trails latest by only {fin['lag_s']} s: the tag may "
                               "mirror the sequencer instead of the parent chain")
        else:
            st, why = "PASS", f"finalized lags latest by {fin['lag_blocks']} blocks / {fin['lag_s']} s"
        self.add("finality_tags", st, why, **out)

    def find_sample(self, search: int = 40) -> None:
        """A recent block that contains a transaction from an EOA (used by the history checks)."""
        latest = self.rpc.block("latest")
        assert latest is not None
        n = qty(latest["number"])
        for k in range(2, search + 2):
            b = self.rpc.block(n - k, True)
            if b is None:
                continue
            for tx in b.get("transactions") or []:
                if isinstance(tx, dict) and tx.get("from") and tx.get("nonce") is not None:
                    self.sample = {"block": n - k, "hash": str(b["hash"]).lower(),
                                   "tx": str(tx["hash"]).lower(), "from": str(tx["from"]).lower(),
                                   "nonce": qty(tx["nonce"]), "to": str(tx.get("to") or "").lower()}
                    self.add("sample_block", "PASS", f"block {n - k} has a transaction",
                             block=n - k)
                    return
        self.add("sample_block", "SKIP", f"no transaction in the last {search} blocks")

    def historical_honoured(self) -> None:
        s = self.sample
        if not s:
            self.add("historical_block_param", "SKIP", "no sample transaction")
            return
        b, k = s["block"], s["nonce"]
        before = qty(self.rpc.call("eth_getTransactionCount", [s["from"], hex(b - 1)]))
        at = qty(self.rpc.call("eth_getTransactionCount", [s["from"], hex(b)]))
        latest = qty(self.rpc.call("eth_getTransactionCount", [s["from"], "latest"]))
        ok = before <= k < at
        ignored = before == at and before > k          # N-1 already "contains" block N's tx
        st = "PASS" if ok else "FAIL"
        why = ("nonce at N-1 <= tx nonce < nonce at N" if ok else
               "node appears to IGNORE the block parameter" if ignored else
               "nonce history inconsistent with the block's own transaction")
        self.add("historical_block_param", st, why, block=b, tx_nonce=k, nonce_before=before,
                 nonce_at=at, nonce_latest=latest)
        bal_before = qty(self.rpc.call("eth_getBalance", [s["from"], hex(b - 1)]))
        bal_at = qty(self.rpc.call("eth_getBalance", [s["from"], hex(b)]))
        self.add("historical_balance_n_minus_1", "PASS" if bal_before != bal_at else "WARN",
                 "balance at N-1 and N differ (the sender paid gas)" if bal_before != bal_at else
                 "balance unchanged across the sender's own transaction (gas-free or ignored)",
                 block=b)
        code = self.rpc.call("eth_getCode", [MULTICALL3, "latest"])
        if not isinstance(code, str) or len(code) <= 2:
            self.add("historical_eth_call", "SKIP", "Multicall3 not deployed at the canonical address")
            return
        target = max(1, b - 100)
        got = self.rpc.call("eth_call", [{"to": MULTICALL3, "data": GET_BLOCK_NUMBER}, hex(target)])
        n = qty(got) if isinstance(got, str) and len(got) > 2 else -1
        self.add("historical_eth_call", "PASS" if n == target else "FAIL",
                 f"getBlockNumber() at {target} returned {n}", block=target, returned=n)

    def archive_depth(self) -> None:
        s = self.sample
        latest = self.rpc.block("latest")
        assert latest is not None
        n = qty(latest["number"])
        who = s.get("from") or MULTICALL3
        ok_depth, failed_at = 0, None
        for d in ARCHIVE_DEPTHS:
            if d >= n:
                break
            try:
                self.rpc.call("eth_getBalance", [who, hex(n - d)])
                ok_depth = d
            except DiagError as e:
                if e.kind != "rpc_error":
                    raise
                failed_at = d
                break
        st = "PASS" if failed_at is None else ("WARN" if ok_depth >= 10_000 else "FAIL")
        self.add("archive_depth", st, f"historical balance works {ok_depth} blocks back"
                 + (f"; refused at {failed_at}" if failed_at else ""),
                 max_ok_depth=ok_depth, refused_at=failed_at, head=n)

    def tx_receipt_consistency(self) -> None:
        s = self.sample
        if not s:
            self.add("tx_receipt_consistency", "SKIP", "no sample transaction")
            return
        tx = self.rpc.call("eth_getTransactionByHash", [s["tx"]])
        rc = self.rpc.call("eth_getTransactionReceipt", [s["tx"]])
        problems = []
        if not isinstance(tx, dict) or not isinstance(rc, dict):
            problems.append("missing")
        else:
            if str(tx.get("blockHash", "")).lower() != s["hash"]:
                problems.append("tx_block_hash")
            if str(rc.get("blockHash", "")).lower() != s["hash"]:
                problems.append("receipt_block_hash")
            if str(rc.get("transactionHash", "")).lower() != s["tx"]:
                problems.append("receipt_tx_hash")
            if str(rc.get("from", "")).lower() != s["from"]:
                problems.append("receipt_from")
            if rc.get("status") not in ("0x0", "0x1"):
                problems.append("receipt_status")
        self.add("tx_receipt_consistency", "PASS" if not problems else "FAIL",
                 "consistent" if not problems else ", ".join(problems), problems=problems)

    def concurrency(self, parallel: int, rounds: int) -> None:
        if parallel <= 1:
            self.add("concurrency_probe", "SKIP", "not requested (--concurrency)")
            return
        errs = 0
        with ThreadPoolExecutor(parallel) as ex:
            for _ in range(rounds):
                for f in [ex.submit(self.rpc.call, "eth_blockNumber", []) for _ in range(parallel)]:
                    try:
                        f.result()
                    except DiagError:
                        errs += 1
        total = parallel * rounds
        self.add("concurrency_probe", "PASS" if errs == 0 else "WARN",
                 f"{errs}/{total} parallel calls failed at concurrency {parallel}",
                 parallel=parallel, failed=errs, total=total)

    # -- summaries
    def summarize(self, duration_s: float) -> None:
        if len(self.heads) >= 2:
            (w0, n0, _, _), (w1, n1, _, _) = self.heads[0], self.heads[-1]
            rate = (n1 - n0) / max(0.001, w1 - w0)
            ages = [w - ts for w, _, _, ts in self.heads]
            stalled = n1 == n0
            st = "FAIL" if stalled or max(ages) > 60 else ("WARN" if max(ages) > 10 else "PASS")
            self.add("head_freshness", st,
                     f"{len(self.heads)} samples over {duration_s:.0f}s; {rate:.2f} blocks/s; "
                     f"max head age {max(ages):.1f}s" + ("; head STALLED" if stalled else ""),
                     blocks_per_s=round(rate, 3), max_age_s=round(max(ages), 2),
                     median_age_s=round(statistics.median(ages), 2))
        elif self.heads:
            w, _, _, ts = self.heads[0]
            self.add("head_freshness", "WARN", f"one sample; head age {w - ts:.1f}s")
        self.add("reorgs_observed", "PASS" if not self.reorgs else "WARN",
                 f"{len(self.reorgs)} hash changes at already-seen heights",
                 reorgs=self.reorgs[:20])
        if len(self.finalized) >= 2:
            moved = self.finalized[-1][1] - self.finalized[0][1]
            self.add("finalized_progress", "PASS" if moved > 0 else "WARN",
                     f"finalized advanced {moved} blocks while sampling", advanced=moved)
        recs = self.rpc.records
        by: dict[str, list[float]] = {}
        for r in recs:
            if r.outcome == "ok":
                by.setdefault(r.method, []).append(r.ms)
        lat = {m: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "p99": pct(v, 99)}
               for m, v in sorted(by.items())}
        outcomes: dict[str, int] = {}
        for r in recs:
            outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
        ra = sorted({r.retry_after for r in recs if r.retry_after})
        bad = sum(v for k, v in outcomes.items() if k not in ("ok", "rpc_error"))
        st = "PASS" if bad == 0 else ("WARN" if bad <= max(2, len(recs) // 20) else "FAIL")
        self.add("transport", st, f"{len(recs)} calls; outcomes {outcomes}",
                 outcomes=outcomes, retry_after_values=ra[:10], latency_ms=lat)


# ---------------------------------------------------------------------------------------- cross
def cross_checks(probes: list[ProviderProbe]) -> list[Check]:
    out: list[Check] = []
    live = [p for p in probes if p.heads]
    if len(probes) < 2:
        out.append(Check("independent_sources", "FAIL",
                         f"{len(probes)} provider(s) configured; corroboration needs >= 2"))
    ops = [p.rpc.operator for p in probes]
    if len(set(ops)) != len(ops):
        out.append(Check("distinct_operators", "FAIL", "two endpoints share an operator label",
                         {"operators": ops}))
    elif len(probes) >= 2:
        out.append(Check("distinct_operators", "PASS", "operator labels are distinct (names only)",
                         {"operators": ops}))
    if len(live) < 2:
        return out
    # same chain at common heights
    common = min(p.heads[-1][1] for p in live)
    hashes: dict[int, set[str]] = {}
    for h in (common, common - 10, common - 100):
        for p in live:
            v = p.seen.get(h)
            if v is None and h >= 0:
                try:
                    b = p.rpc.block(h)
                    v = None if b is None else str(b["hash"]).lower()
                except DiagError:
                    v = None
            if v is not None:
                hashes.setdefault(h, set()).add(v)
    diverged = sorted(h for h, s in hashes.items() if len(s) > 1)
    out.append(Check("same_canonical_chain", "FAIL" if diverged else "PASS",
                     "providers return different hashes at " + str(diverged) if diverged
                     else f"identical hashes at heights {sorted(hashes)}",
                     {"diverged_heights": diverged}))
    # head skew (samples are taken round-robin within the same iteration)
    k = min(len(p.heads) for p in live)
    skews = [max(p.heads[i][1] for p in live) - min(p.heads[i][1] for p in live) for i in range(k)]
    if skews:
        out.append(Check("head_skew", "PASS" if max(skews) <= 64 else "WARN",
                         f"max {max(skews)} / median {statistics.median(skews)} blocks between "
                         "providers (corroboration tolerates 64)",
                         {"max_blocks": max(skews), "median_blocks": statistics.median(skews)}))
    # historical agreement on a shared sample
    sample = next((p.sample for p in live if p.sample), None)
    if sample:
        vals: dict[str, Any] = {}
        for p in live:
            try:
                vals[p.rpc.name] = (
                    qty(p.rpc.call("eth_getTransactionCount", [sample["from"], hex(sample["block"] - 1)])),
                    qty(p.rpc.call("eth_getBalance", [sample["from"], hex(sample["block"] - 1)])),
                    qty(p.rpc.call("eth_getBalance", [sample["from"], hex(sample["block"])])))
            except DiagError as e:
                vals[p.rpc.name] = ("error", e.kind)
        same = len({json.dumps(v) for v in vals.values()}) == 1
        out.append(Check("same_historical_state", "PASS" if same else "FAIL",
                         "identical nonce/balances at N-1 and N" if same else
                         "providers disagree on historical state", {"block": sample["block"]}))
    # finalized agreement
    fins = {p.rpc.name: p.finalized[-1][1] for p in live if p.finalized}
    if len(fins) == len(live):
        lo = min(fins.values())
        hs = set()
        for p in live:
            try:
                b = p.rpc.block(lo)
                hs.add(None if b is None else str(b["hash"]).lower())
            except DiagError:
                hs.add(None)
        out.append(Check("same_finalized_ancestry", "PASS" if len(hs) == 1 and None not in hs else "FAIL",
                         f"lowest finalized {lo}; identical hash there: {len(hs) == 1}",
                         {"finalized": fins}))
    else:
        out.append(Check("same_finalized_ancestry", "FAIL",
                         "not every provider serves the finalized tag", {"finalized": fins}))
    # shared-upstream indicators (never proof of independence)
    fp = {}
    for p in live:
        cv = next((c.data.get("client_version") for c in p.checks if c.name == "client_version"), None)
        fp[p.rpc.name] = json.dumps({"client": cv, "headers": p.rpc.headers}, sort_keys=True)
    dup = len(set(fp.values())) < len(fp)
    out.append(Check("upstream_indicators", "WARN" if dup else "PASS",
                     "identical client version AND response fingerprint: possibly one upstream"
                     if dup else "fingerprints differ (does NOT prove independence)",
                     {"fingerprints": {k: json.loads(v) for k, v in fp.items()}}))
    return out


# ---------------------------------------------------------------------------------------- driver
def endpoints_from_env(env_names: Iterable[str], environ: Mapping[str, str]) -> list[tuple[str | None, str]]:
    out = []
    for name in env_names:
        for entry in (e.strip() for e in environ.get(name, "").split(",")):
            if not entry:
                continue
            m = _LABELED.match(entry)
            out.append((m.group(1).lower(), m.group(2)) if m else (None, entry))
    return out


def run(endpoints: list[tuple[str | None, str]], *, duration_s: float = 60.0,
        interval_s: float = 2.0, max_calls: int = 400, max_rps: float = 4.0, timeout: float = 8.0,
        concurrency: int = 1, concurrency_rounds: int = 3, session_factory: Callable[[], Any] | None = None,
        wall: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
        mono: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    started = wall()
    if not endpoints:
        return {"verdict": "BLOCKED", "reason": "no endpoints configured in the environment",
                "started_at": started, "providers": [], "cross": []}
    probes = []
    for label, url in endpoints:
        rpc = RawRpc(url, label=label, timeout=timeout, max_calls=max_calls, max_rps=max_rps,
                     session=session_factory() if session_factory else None, clock=mono, sleep=sleep)
        probes.append(ProviderProbe(rpc, wall=wall))
    usable = []
    for p in probes:
        try:
            if p.chain():
                usable.append(p)
        except DiagError as e:
            p.add("chain_id", "FAIL", f"unreachable: {e.kind}")
    for p in usable:
        for name, fn in (("continuity", p.continuity), ("finality_tags", p.tags),
                         ("sample_block", p.find_sample), ("historical", p.historical_honoured),
                         ("archive_depth", p.archive_depth),
                         ("tx_receipt_consistency", p.tx_receipt_consistency)):
            p.guarded(name, fn)
    end = mono() + max(0.0, duration_s)
    while True:                                     # round-robin sampling: comparable heads
        for p in usable:
            p.guarded("head_sample", p.observe_head)
            p.guarded("finalized_sample", p.observe_finalized)
        if mono() >= end:
            break
        sleep(interval_s)
    for p in usable:
        def conc(p: ProviderProbe = p) -> None:
            p.concurrency(concurrency, concurrency_rounds)
        p.guarded("concurrency_probe", conc)
        p.summarize(duration_s)
    cross = cross_checks(usable if usable else probes) if probes else []
    providers: list[dict[str, Any]] = []
    for p in probes:
        providers.append({"name": p.rpc.name, "host": p.rpc.host, "operator": p.rpc.operator,
                          "calls": p.rpc.calls, "fingerprint_headers": p.rpc.headers,
                          "checks": [asdict(c) for c in p.checks]})
    statuses = [c["status"] for pr in providers for c in pr["checks"]] + [c.status for c in cross]
    verdict = ("BLOCKED" if not usable else "FAIL" if "FAIL" in statuses
               else "WARN" if "WARN" in statuses else "PASS")
    return {"tool": "rpcdiag", "schema": 1, "chain_id": CHAIN_ID, "verdict": verdict,
            "started_at": started, "duration_s": duration_s, "providers": providers,
            "cross": [asdict(c) for c in cross],
            "note": "Read-only. Can show disagreement or shared upstream; cannot prove independence."}


def render_markdown(rep: dict[str, Any]) -> str:
    lines = [f"# RPC diagnostic report (chain {rep.get('chain_id', CHAIN_ID)})", "",
             f"Verdict: **{rep['verdict']}**", ""]
    if rep.get("reason"):
        lines += [rep["reason"], ""]
    for p in rep.get("providers", []):
        lines += [f"## {p['operator']} ({p['host']}) — {p['calls']} calls", "",
                  "| Check | Status | Detail |", "|---|---|---|"]
        lines += [f"| {c['name']} | {c['status']} | {c['detail'].replace('|', '/')} |"
                  for c in p["checks"]]
        lines.append("")
    if rep.get("cross"):
        lines += ["## Across providers", "", "| Check | Status | Detail |", "|---|---|---|"]
        lines += [f"| {c['name']} | {c['status']} | {c['detail'].replace('|', '/')} |"
                  for c in rep["cross"]]
    lines += ["", rep.get("note", "")]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", action="append", default=None,
                    help="environment variable holding comma-separated [label=]url entries "
                         "(repeatable; default TRADING_EXEC_RPC_URL and TRADING_CORROBORATION_RPC_URLS)")
    ap.add_argument("--duration", type=float, default=60.0, help="head sampling window, seconds")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between head samples")
    ap.add_argument("--max-calls", type=int, default=400, help="hard call budget per provider")
    ap.add_argument("--max-rps", type=float, default=4.0, help="request-rate ceiling per provider")
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--concurrency", type=int, default=1, help="parallel probe width (1 = off, max 8)")
    ap.add_argument("--out", help="write the JSON report here (no URLs are included)")
    ap.add_argument("--markdown", help="write a Markdown summary here")
    a = ap.parse_args(argv)
    if not (1 <= a.max_calls <= 5000 and 0.1 <= a.max_rps <= 20 and 1 <= a.concurrency <= 8
            and 0 <= a.duration <= 3600 and 0.2 <= a.interval <= 60):
        ap.error("bounds: max-calls 1..5000, max-rps 0.1..20, concurrency 1..8, "
                 "duration 0..3600, interval 0.2..60")
    logging.basicConfig(level=logging.WARNING)
    names = a.env or ["TRADING_EXEC_RPC_URL", "TRADING_CORROBORATION_RPC_URLS"]
    try:
        eps = endpoints_from_env(names, environ)
        rep = run(eps, duration_s=a.duration, interval_s=a.interval, max_calls=a.max_calls,
                  max_rps=a.max_rps, timeout=a.timeout, concurrency=a.concurrency)
    except ValueError:
        print("an endpoint in the environment is not a valid http(s) url", file=sys.stderr)
        return 2
    text = json.dumps(rep, indent=2, sort_keys=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as f:
            f.write(render_markdown(rep))
    print(render_markdown(rep))
    return {"PASS": 0, "WARN": 0, "FAIL": 1}.get(rep["verdict"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
