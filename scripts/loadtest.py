#!/usr/bin/env python3
"""Open-loop load test for the dashboard, run ON the VPS against the loopback port.

Arrivals are scheduled at a fixed rate (default 5 req/s) regardless of response time, so a
slow server shows up as latency and queueing instead of silently lowering the load. The URL
mix is what real visitors do: the five sections, a spread of time ranges, search, sorting,
pagination, and detail pages for real tokens/wallets taken from the dashboard's own API.

While it runs it samples the web container (memory/CPU), the host (load, memory), the number
of dashboard_ro DB connections, and the tracker's health. It stops early (exit 3) if the
bots look unhealthy or the error rate / memory exceed the guard rails, because production
bots matter more than the test.

Only aggregate numbers are printed: no addresses, no response bodies.

    loadtest.py --base http://127.0.0.1:8100 --rps 5 --duration 300
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict


def get(url: str, timeout: float = 15.0) -> tuple[int, float, int]:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:      # noqa: S310 (loopback)
            body = r.read()
            return r.status, time.perf_counter() - t0, len(body)
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, time.perf_counter() - t0, 0
    except Exception:
        return 0, time.perf_counter() - t0, 0


def fetch_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:           # noqa: S310
            return json.load(r)
    except Exception:
        return None


def build_mix(base: str) -> list[tuple[str, str, int]]:
    """(label, path, weight)"""
    ranges = ["15m", "1h", "6h", "24h", "7d", "30d"]
    mix: list[tuple[str, str, int]] = [
        ("overview", "/", 8), ("tokens", "/tokens", 6), ("smart", "/smart-money", 5),
        ("early", "/early", 4), ("scanner", "/scanner", 3), ("about", "/about", 1),
        ("healthz", "/healthz", 2), ("api-overview", "/api/v1/overview", 2),
        ("api-health", "/api/v1/health", 1), ("search", "/search?q=a", 2),
    ]
    for r in ranges:
        mix.append((f"overview-{r}", f"/?range={r}", 2))
        mix.append((f"tokens-{r}", f"/tokens?range={r}&sort=net&dir=desc", 1))
        mix.append((f"smart-{r}", f"/smart-money?range={r}", 1))
        mix.append((f"early-{r}", f"/early?range={r}", 1))
    mix.append(("tokens-p2", "/tokens?range=7d&page=2", 1))
    # Activity: pages across ranges / filters / sorts, and the viewers' polling (the hot path)
    head = ((fetch_json(f"{base}/api/v1/activity?range=all&size=1") or {}).get("items") or [{}])[0].get("id")
    for r in ("15m", "1h", "4h", "12h", "24h", "7d", "30d", "all"):
        mix.append((f"activity-{r}", f"/activity?range={r}", 2))
    for act in ("buy", "sell", "transfer_in", "transfer_out", "swap", "other"):
        mix.append(("activity-filter", f"/activity?range=7d&action={act}", 1))
    for srt in ("oldest", "largest_buy", "largest_sell"):
        mix.append(("activity-sort", f"/activity?range=24h&sort={srt}", 1))
    mix.append(("activity-sort-all", "/activity?range=all&sort=largest_buy", 1))
    mix.append(("activity-search", "/activity?range=all&q=0x", 1))
    mix.append(("activity-api", "/api/v1/activity?range=24h&size=50", 4))
    if head:
        mix.append(("activity-poll", f"/api/v1/activity?range=24h&since_id={head}", 10))
    toks = fetch_json(f"{base}/api/v1/tokens?range=30d") or {}
    for row in ((toks.get("page") or {}).get("items") or [])[:15]:
        a = row.get("token")
        if a:
            mix.append(("token-detail", f"/tokens/{a}?range=7d", 2))
    for view in ("net", "buyers", "new", "repeat", "exits", "clusters", "traders-agora",
                 "traders-realized", "traders-active", "traders-consistent", "traders-movers"):
        mix.append(("smart-view", f"/smart-money?view={view}&range=7d", 1))
    seen: set[str] = set()
    sm = fetch_json(f"{base}/api/v1/smart-money?view=traders-agora&range=30d") or {}
    for row in ((sm.get("page") or {}).get("items") or [])[:12]:
        a = row.get("wallet") or row.get("address")
        if a and a not in seen:
            seen.add(a)
            mix.append(("wallet-detail", f"/wallets/{a}?range=30d", 2))
    return mix


def sh(cmd: list[str], timeout: float = 20.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def mem_mb(s: str) -> float:
    s = s.split("/")[0].strip()
    for unit, mul in (("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024)):
        if s.endswith(unit):
            return float(s[: -len(unit)]) * mul
    return 0.0


def sample(container: str, db_container: str, tracker: str) -> dict:
    out: dict = {"t": time.time()}
    st = sh(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}|{{.CPUPerc}}", container])
    if "|" in st:
        m, c = st.split("|", 1)
        out["web_mem_mb"] = mem_mb(m)
        out["web_cpu"] = float(c.strip().rstrip("%") or 0)
    out["load1"] = float(open("/proc/loadavg").read().split()[0])
    mi = {k: int(v.split()[0]) for k, v in (ln.split(":", 1) for ln in open("/proc/meminfo"))}
    out["host_avail_mb"] = mi.get("MemAvailable", 0) // 1024
    out["swap_used_mb"] = (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) // 1024
    n = sh(["docker", "exec", db_container, "psql", "-U", "tracker", "-d", "tracker", "-Atc",
            "select count(*) from pg_stat_activity where usename='dashboard_ro'"])
    out["db_conns"] = int(n) if n.isdigit() else -1
    h = sh(["docker", "inspect", "-f", "{{.State.Health.Status}}", tracker])
    out["tracker_health"] = h or "unknown"
    # behavioural liveness: the indexer's heartbeat age (the docker flag can be stale/unrelated,
    # e.g. a schema-version mismatch after a migration, while the bot itself works)
    hb = sh(["docker", "exec", db_container, "psql", "-U", "tracker", "-d", "tracker", "-Atc",
             "select round(extract(epoch from now() - updated_at)) from service_state "
             "where key = 'heartbeat'"])
    out["heartbeat_age"] = int(float(hb)) if hb.replace(".", "", 1).isdigit() else -1
    return out


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8100")
    ap.add_argument("--rps", type=float, default=5.0)
    ap.add_argument("--duration", type=float, default=300.0)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--web", default="elysium-web")
    ap.add_argument("--db", default="elysium-postgres")
    ap.add_argument("--tracker", default="elysium-wallet-tracker")
    ap.add_argument("--max-error-rate", type=float, default=0.02)
    ap.add_argument("--max-web-mem-mb", type=float, default=230.0)
    ap.add_argument("--min-host-avail-mb", type=float, default=150.0)
    ap.add_argument("--max-heartbeat-age", type=int, default=180)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    random.seed(a.seed)

    mix = build_mix(a.base)
    labels = [m[0] for m in mix]
    weights = [m[2] for m in mix]
    print(f"mix: {len(mix)} urls ({len(set(labels))} kinds); {a.rps} rps for {a.duration:.0f}s")

    lock = threading.Lock()
    results: list[tuple[str, int, float, int]] = []
    stop = threading.Event()
    abort_reason: list[str] = []
    sem = threading.BoundedSemaphore(a.workers)

    def fire(label: str, path: str) -> None:
        try:
            code, dt, size = get(a.base + path)
            with lock:
                results.append((label, code, dt, size))
        finally:
            sem.release()

    samples: list[dict] = []

    def sampler() -> None:
        while not stop.wait(15):
            s = sample(a.web, a.db, a.tracker)
            samples.append(s)
            with lock:
                recent = results[-100:]
            err = sum(1 for r in recent if r[1] >= 500 or r[1] == 0) / max(1, len(recent))
            print(f"  t+{s['t'] - t_start:4.0f}s web={s.get('web_mem_mb', 0):5.0f}MB "
                  f"cpu={s.get('web_cpu', 0):5.1f}% load1={s['load1']:.2f} "
                  f"avail={s['host_avail_mb']}MB swap={s['swap_used_mb']}MB "
                  f"db_conns={s['db_conns']} tracker={s['tracker_health']} hb={s['heartbeat_age']}s "
                  f"recent_err={err:.1%} n={len(results)}", flush=True)
            if s["tracker_health"] != base_sample["tracker_health"] and \
                    s["tracker_health"] not in ("healthy", "unknown", "starting"):
                abort_reason.append(f"tracker health changed to {s['tracker_health']}")
            if s["heartbeat_age"] > a.max_heartbeat_age:
                abort_reason.append(f"tracker heartbeat {s['heartbeat_age']} s old")
            if s.get("web_mem_mb", 0) > a.max_web_mem_mb:
                abort_reason.append(f"web memory {s['web_mem_mb']:.0f} MB")
            if s["host_avail_mb"] < a.min_host_avail_mb:
                abort_reason.append(f"host memory available {s['host_avail_mb']} MB")
            if len(recent) >= 50 and err > 0.25:
                abort_reason.append(f"recent error rate {err:.0%}")
            if abort_reason:
                stop.set()

    base_sample = sample(a.web, a.db, a.tracker)
    t_start = time.time()
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    threads: list[threading.Thread] = []
    interval = 1.0 / a.rps
    n = 0
    t0 = time.perf_counter()
    while not stop.is_set() and time.perf_counter() - t0 < a.duration:
        due = t0 + n * interval
        delay = due - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        n += 1
        label, path, _ = random.choices(mix, weights=weights)[0]
        if not sem.acquire(timeout=5):
            with lock:
                results.append((label, 0, 5.0, 0))               # generator saturated: count it
            continue
        t = threading.Thread(target=fire, args=(label, path), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=20)
    stop.set()
    th.join(timeout=5)
    end_sample = sample(a.web, a.db, a.tracker)

    elapsed = time.perf_counter() - t0
    ok = [r for r in results if r[1] == 200]
    codes = Counter(r[1] for r in results)
    lat = [r[2] for r in ok]
    print("\n=== result")
    print(f"requests: {len(results)} in {elapsed:.0f}s = {len(results) / elapsed:.2f} rps; "
          f"status: {dict(sorted(codes.items()))}")
    err_rate = sum(c for k, c in codes.items() if k >= 500 or k == 0) / max(1, len(results))
    print(f"error rate (5xx/transport): {err_rate:.2%}; 429s: {codes.get(429, 0)}")
    print(f"latency ok-only (s): p50={pct(lat, 50):.3f} p90={pct(lat, 90):.3f} "
          f"p95={pct(lat, 95):.3f} p99={pct(lat, 99):.3f} max={max(lat, default=0):.3f} "
          f"mean={statistics.fmean(lat) if lat else 0:.3f}")
    by: dict[str, list[float]] = defaultdict(list)
    for lab, code, dt, _ in results:
        if code == 200:
            by[lab.split("-")[0] if lab.startswith(("overview-", "tokens-", "smart-", "early-"))
               else lab].append(dt)
    print("per page kind (n, p50, p95 s):")
    for k in sorted(by):
        print(f"  {k:14s} {len(by[k]):5d}  {pct(by[k], 50):.3f}  {pct(by[k], 95):.3f}")
    peak_mem = max((s.get("web_mem_mb", 0) for s in samples), default=0)
    peak_cpu = max((s.get("web_cpu", 0) for s in samples), default=0)
    peak_load = max((s["load1"] for s in samples), default=0)
    peak_db = max((s["db_conns"] for s in samples), default=0)
    min_avail = min((s["host_avail_mb"] for s in samples), default=0)
    print(f"web memory: before={base_sample.get('web_mem_mb', 0):.0f}MB peak={peak_mem:.0f}MB "
          f"after={end_sample.get('web_mem_mb', 0):.0f}MB (limit 256MB); peak cpu={peak_cpu:.0f}%")
    print(f"host: peak load1={peak_load:.2f}, min available={min_avail}MB; "
          f"peak dashboard_ro connections={peak_db} (limit 5); "
          f"tracker health before/after: {base_sample['tracker_health']}/{end_sample['tracker_health']}; "
          f"heartbeat age before/after/max: {base_sample['heartbeat_age']}/"
          f"{end_sample['heartbeat_age']}/{max((x['heartbeat_age'] for x in samples), default=0)} s")
    if abort_reason:
        print("ABORTED EARLY: " + "; ".join(sorted(set(abort_reason))))
        return 3
    if err_rate > a.max_error_rate:
        print(f"FAIL: error rate above {a.max_error_rate:.0%}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
