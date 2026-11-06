#!/usr/bin/env python3
"""Bounded, read-only benchmark of the tracker's eth_getLogs strategy against real RPC endpoints.

Why: the scanner asks every range twice per wallet chunk (Transfer from-wallet, Transfer
to-wallet). Cost therefore grows linearly with the tracked-wallet count, and providers may cap
the topic OR-list length, the block range, the response size or the request rate. This script
measures those limits instead of guessing. Stdlib only; prints aggregates and error classes,
never URLs with keys or wallet addresses.

  scan_bench.py --rpc URL [--rpc URL ...] --wallets FILE [--range 2000] [--budget 120]

* `--wallets` holds real tracked addresses (one per line); sizes above that are padded with
  deterministic synthetic addresses (valid filters that match nothing, so they measure the
  filter cost, not the match volume).
* Each endpoint gets at most `--budget` requests, spaced by `--pause` seconds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def pad(a: str) -> str:
    return "0x" + "0" * 24 + a[2:].lower()


def synthetic(i: int) -> str:
    return "0x" + hashlib.sha256(f"scan-bench-{i}".encode()).hexdigest()[:40]


class Budget(Exception):
    pass


class Endpoint:
    def __init__(self, url: str, budget: int, pause: float, timeout: float):
        self.url, self.budget, self.pause, self.timeout = url, budget, pause, timeout
        self.host = urlsplit(url).hostname or "?"
        self.n = 0

    def post(self, method: str, params: list) -> tuple[float, int, object, str | None]:
        """(seconds, response bytes, result, error class)."""
        if self.n >= self.budget:
            raise Budget()
        self.n += 1
        time.sleep(self.pause)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "User-Agent": "scan-bench/1"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            return time.monotonic() - t0, 0, None, f"http{e.code}"
        except Exception as e:  # noqa: BLE001
            return time.monotonic() - t0, 0, None, type(e).__name__
        dt = time.monotonic() - t0
        try:
            j = json.loads(raw)
        except ValueError:
            return dt, len(raw), None, "badjson"
        if j.get("error"):
            msg = str(j["error"].get("message", ""))[:90]
            return dt, len(raw), None, f"rpc:{j['error'].get('code')}:{msg}"
        return dt, len(raw), j.get("result"), None


def get_logs(ep: Endpoint, frm: int, to: int, topics: list):
    return ep.post("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to), "topics": topics}])


def scan_cost(ep: Endpoint, frm: int, to: int, wallets: list[str], chunk: int) -> dict:
    lat, size, logs, errs = [], 0, 0, {}
    t0 = time.monotonic()
    for i in range(0, len(wallets), chunk):
        padded = [pad(w) for w in wallets[i:i + chunk]]
        for topics in ([TRANSFER, padded], [TRANSFER, None, padded]):
            dt, nbytes, res, err = get_logs(ep, frm, to, topics)
            lat.append(dt)
            size += nbytes
            if err:
                errs[err] = errs.get(err, 0) + 1
            elif isinstance(res, list):
                logs += len(res)
    return {"requests": len(lat), "wall_s": round(time.monotonic() - t0, 2),
            "p50_ms": int(statistics.median(lat) * 1000), "max_ms": int(max(lat) * 1000),
            "bytes": size, "logs": logs, "errors": errs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", action="append", required=True)
    ap.add_argument("--wallets", required=True)
    ap.add_argument("--range", type=int, default=2000)
    ap.add_argument("--budget", type=int, default=120)
    ap.add_argument("--pause", type=float, default=0.15)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--sizes", default="100,331,1000")
    ap.add_argument("--chunks", default="50,100,200")
    a = ap.parse_args()
    real = [x.strip().lower() for x in open(a.wallets) if x.strip().startswith("0x")]
    print(f"real tracked wallets in the list: {len(real)}; range {a.range} blocks; "
          f"budget {a.budget} requests per endpoint")
    sizes = [int(x) for x in a.sizes.split(",")]
    chunks = [int(x) for x in a.chunks.split(",")]
    for url in a.rpc:
        ep = Endpoint(url, a.budget, a.pause, a.timeout)
        print(f"\n== endpoint {ep.host}")
        try:
            dt, _, res, err = ep.post("eth_blockNumber", [])
            if err or not isinstance(res, str):
                print(f"  unreachable: {err}")
                continue
            head = int(res, 16)
            to = head - 60                       # settled, in the node's hot data
            frm = to - a.range + 1
            print(f"  head {head}; range {frm}-{to}; eth_blockNumber {int(dt * 1000)} ms")
            base = scan_cost(ep, frm, to, real[:50], 50)
            print(f"  baseline 50 wallets, chunk 50 : {base}")
            for n in sizes:
                ws = (real + [synthetic(i) for i in range(max(0, n - len(real)))])[:n]
                for ch in chunks:
                    if ch > n and ch != chunks[0]:
                        continue
                    r = scan_cost(ep, frm, to, ws, ch)
                    print(f"  wallets {n:5d} chunk {ch:4d}: {r}")
            print("  -- OR-list limit (200-block range, one query per size)")
            small_to, small_from = to, to - 199
            for n in (50, 100, 200, 500, 1000, 2000, 5000):
                ws = [synthetic(100000 + i) for i in range(n)]
                dt, nbytes, res, err = get_logs(ep, small_from, small_to, [TRANSFER, [pad(w) for w in ws]])
                print(f"  OR-list {n:5d}: {'ok ' + str(int(dt * 1000)) + ' ms' if not err else err}")
                if err:
                    break
            print("  -- block-range limit (one wallet)")
            for span in (2000, 5000, 10000, 20000):
                dt, nbytes, res, err = get_logs(ep, to - span + 1, to,
                                                [TRANSFER, [pad(real[0])]])
                print(f"  range {span:6d}: {'ok ' + str(int(dt * 1000)) + ' ms' if not err else err}")
                if err:
                    break
        except Budget:
            print(f"  budget of {a.budget} requests reached; stopped")
        print(f"  requests used: {ep.n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
