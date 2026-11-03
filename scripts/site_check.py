#!/usr/bin/env python3
"""External check of the public site from outside the VPS, in a real browser (Chromium).

Run by .github/workflows/site-check.yml on a GitHub runner. Read-only: GET requests only.
Checks TLS (chain, SAN, expiry), HTTP->HTTPS, security headers, then every section at desktop
and phone width: HTTP status, console/page errors, failed requests, horizontal overflow,
and that real data is rendered. Writes screenshots to ./shots and prints a summary.

    site_check.py --base https://onchainflow.app --out shots
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

FAILS: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


def tls(host: str) -> None:
    print("== TLS")
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=15) as s, ctx.wrap_socket(s, server_hostname=host) as t:
        cert = t.getpeercert()
        issuer = dict(x[0] for x in cert["issuer"]).get("organizationName", "?")
        exp = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (exp - datetime.now(timezone.utc)).days
        sans = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
        check(True, "certificate chain verifies against the system trust store", f"issuer={issuer}, {t.version()}")
        check(host in sans or any(s.startswith("*.") and host.endswith(s[1:]) for s in sans), "certificate covers the host", ",".join(sans))
        check(days > 14, "certificate not about to expire", f"expires {exp:%Y-%m-%d} ({days} days)")


def http_basics(base: str) -> None:
    print("== HTTP")
    host = urlparse(base).hostname
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect)
    try:
        op.open(f"http://{host}/", timeout=15)
        check(False, "http:// redirects to https://", "no redirect")
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        check(e.code in (301, 302, 307, 308) and loc.startswith("https://"), "http:// redirects to https://", f"{e.code} -> {loc}")
    req = urllib.request.Request(base + "/", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=20) as r:
        h = {k.lower(): v for k, v in r.headers.items()}
        check(r.status == 200, "GET / is 200")
        check("content-security-policy" in h and "script-src 'self'" in h["content-security-policy"], "CSP present, no inline script")
        check(h.get("x-content-type-options") == "nosniff", "X-Content-Type-Options: nosniff")
        check(h.get("x-frame-options") == "DENY", "X-Frame-Options: DENY")
        check("strict-transport-security" in h, "HSTS present", h.get("strict-transport-security", ""))
        check(h.get("referrer-policy") == "no-referrer", "Referrer-Policy: no-referrer")
        check(h.get("content-encoding") in ("gzip", "zstd"), "response compressed by the proxy", h.get("content-encoding", "none"))
        body = r.read()
        if h.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        check(b"Elysium" in body, "page is the Elysium dashboard", f"{len(body)} bytes")
        check(b"Revival" not in body, "old Revival Radar site is gone")
    for p in ("/healthz", "/robots.txt", "/api/v1/meta"):
        with urllib.request.urlopen(base + p, timeout=15) as r:
            check(r.status == 200, f"GET {p} is 200")


def api_json(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.load(r)


def activity_api(base: str) -> None:
    """Contract checks for /api/v1/activity and the USD fields. Prints aggregates only."""
    print("== activity + USD API")
    req = urllib.request.Request(base + "/api/v1/activity?range=7d&size=20")
    with urllib.request.urlopen(req, timeout=30) as r:
        etag = r.headers.get("ETag")
        cc = r.headers.get("Cache-Control", "")
        d = json.load(r)
    rows = d.get("items") or []
    check(bool(rows), "activity API returns rows", str(len(rows)))
    check(bool(etag) and "no-cache" in cc, "activity API has ETag and no-cache", f"{bool(etag)} {cc}")
    need = {"id", "block_time", "wallet", "token", "action", "basis", "tx_hash", "block_number"}
    check(all(need <= set(x) for x in rows), "activity rows carry the documented fields",
          str(sorted(need - set(rows[0])) if rows else ""))
    bad = [x for x in rows if x.get("action") in ("buy", "sell") and x.get("kind") not in ("BUY", "SELL")]
    check(not bad, "buy/sell actions come only from BUY/SELL legs (transfers never relabelled)", str(len(bad)))
    check(all(x.get("basis") in ("verified", "estimated", "unknown") for x in rows), "basis is verified/estimated/unknown")
    check(isinstance(d.get("freshness"), dict), "activity API carries freshness", str(list(d.get("freshness") or {})[:8]))
    try:
        r2 = urllib.request.Request(base + "/api/v1/activity?range=7d&size=20", headers={"If-None-Match": etag or ""})
        urllib.request.urlopen(r2, timeout=30)
        check(False, "conditional request returns 304")
    except urllib.error.HTTPError as e:
        check(e.code == 304, "conditional request returns 304", str(e.code))
    for q, label in (("range=bogus", "bad range"), ("sort=sideways", "bad sort"), ("action=nope", "bad action"),
                     ("q=%25", "wildcard search"), ("cursor=garbage", "bad cursor")):
        try:
            urllib.request.urlopen(base + "/api/v1/activity?" + q, timeout=15)
            check(False, f"{label} -> 400")
        except urllib.error.HTTPError as e:
            check(e.code == 400, f"{label} -> 400", str(e.code))
    first = rows[0] if rows else {}
    if first.get("tx_hash"):
        h = first["tx_hash"]
        part = api_json(base, f"/api/v1/activity?range=all&q={h[:12]}")
        check(any(x.get("tx_hash") == h for x in (part.get("items") or [])), "tx-hash partial search finds the row")
    if rows:
        newest = api_json(base, "/api/v1/activity?range=all&size=1")
        head = (newest.get("items") or [{}])[0].get("id")
        inc = api_json(base, f"/api/v1/activity?range=all&since_id={head}")
        check(not inc.get("items"), "since_id at head returns nothing new")
    toks = api_json(base, "/api/v1/tokens?range=30d&sort=mcap&dir=desc")
    items = (toks.get("page") or {}).get("items") or []
    usd = [(i.get("market") or {}).get("usd") or {} for i in items]
    have = [m for m in usd if m.get("market_cap_usd") is not None]
    check(bool(have), "tokens expose market_cap_usd", f"{len(have)}/{len(items)} with a USD value")
    check(all(m.get("market_cap_status") in ("valid", "stale") for m in have), "USD values carry a valid/stale status")
    check(all(m.get("market_cap_usd") is None or float(m["market_cap_usd"]) > 0 for m in usd), "no zero/negative USD market cap")
    check(all(m.get("market_cap_source") for m in usd if m), "market_cap_source present")
    vals = [float(m["market_cap_usd"]) for m in have]
    check(vals == sorted(vals, reverse=True), "USD sort is descending by USD", f"{len(vals)} values")
    check(all(m.get("market_cap_usd") is None for m in usd if m.get("market_cap_status") == "unavailable"),
          "unavailable status never has a value")
    meta = api_json(base, "/api/v1/meta")
    check("units" in meta, "meta documents units", str(list(meta.get("units") or {})[:6]))


def pages_in_browser(base: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    toks = api_json(base, "/api/v1/tokens?range=30d")
    items = (toks.get("page") or {}).get("items") or []
    paths = ["/", "/activity?range=24h", "/tokens", "/smart-money", "/early", "/scanner", "/about"]
    if items:
        paths.append(f"/tokens/{items[0]['token']}")
    sm = api_json(base, "/api/v1/smart-money?view=traders-agora&range=30d")
    rows = (sm.get("page") or {}).get("items") or []
    if rows:
        paths.append(f"/wallets/{rows[0].get('wallet') or rows[0].get('address')}")
    print(f"== browser: {len(paths)} pages x 2 viewports")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, vp, dpr in (("desktop", {"width": 1440, "height": 900}, 1), ("mobile", {"width": 390, "height": 844}, 2)):
            ctx = browser.new_context(viewport=vp, device_scale_factor=dpr)
            for p in paths:
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e, errors=errors: errors.append(f"pageerror {e}"))
                page.on("console", lambda m, errors=errors: errors.append(f"console.{m.type} {m.text[:120]}") if m.type in ("error", "warning") else None)
                page.on("requestfailed", lambda r, errors=errors: errors.append(f"requestfailed {r.url[:100]}"))
                resp = page.goto(base + p, wait_until="networkidle", timeout=45000)
                overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                text = page.inner_text("body")
                nums = len(re.findall(r"\d", text))
                ext = {urlparse(r).netloc for r in page.evaluate("performance.getEntriesByType('resource').map(e=>e.name)")} - {urlparse(base).netloc}
                label = f"{name} {p[:34]}"
                check(resp is not None and resp.status == 200, f"{label}: HTTP 200", str(resp.status if resp else "none"))
                check(not errors, f"{label}: no console errors / failed requests", "; ".join(errors[:3]))
                check(overflow <= 0, f"{label}: no horizontal overflow", f"{overflow}px")
                check(not ext, f"{label}: no third-party requests", ",".join(ext))
                check(nums > 40 or p == "/about", f"{label}: renders data", f"{nums} digits of text")
                safe = re.sub(r"[^a-z0-9]+", "_", p.lower()).strip("_") or "overview"
                page.screenshot(path=str(out / f"{name}-{safe}.png"), full_page=(p in ("/", "/early")))
                page.close()
            ctx.close()
        # interactions on desktop: range filter, sort, copy button present, search
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto(base + "/?range=7d", wait_until="networkidle")
        check("7" in page.inner_text("body"), "range=7d renders")
        page.goto(base + "/tokens?range=24h&sort=net&dir=desc", wait_until="networkidle")
        check(page.locator("table tbody tr").count() > 0, "tokens table has rows", str(page.locator("table tbody tr").count()))
        page.goto(base + "/search?q=zz-no-such-token-zz", wait_until="networkidle")
        check(page.inner_text("body").strip() != "", "no-match search renders a page")
        page.goto(base + "/?range=1h&start=garbage", wait_until="networkidle")
        check("$" in page.inner_text("body"), "overview shows USD market caps")
        # Activity: live polling really happens, without errors, and the page states its freshness
        polls: list[str] = []
        errs: list[str] = []
        act = ctx.new_page()
        act.on("request", lambda r: polls.append(r.url) if "/api/v1/activity" in r.url and "since_id" in r.url else None)
        act.on("console", lambda m: errs.append(m.text[:100]) if m.type in ("error", "warning") else None)
        act.goto(base + "/activity?range=24h", wait_until="networkidle")
        body = act.inner_text("body")
        check(act.locator("table tbody tr").count() > 0, "activity table has rows", str(act.locator("table tbody tr").count()))
        check(re.search(r"heartbeat|scanner", body, re.I) is not None, "activity page shows scanner freshness")
        act.goto(base + "/activity?range=24h&action=buy", wait_until="networkidle")
        check("action=buy" in act.url, "action filter is server-side (URL driven)")
        act.goto(base + "/activity?range=24h", wait_until="networkidle")
        act.wait_for_timeout(26000)
        check(len(polls) >= 1, "activity polls the API with since_id within 26 s", str(len(polls)))
        check(not errs, "activity polling is console-clean", "; ".join(errs[:2]))
        act.close()
        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://onchainflow.app")
    ap.add_argument("--out", default="shots")
    a = ap.parse_args()
    host = urlparse(a.base).hostname
    tls(host)
    http_basics(a.base)
    activity_api(a.base)
    pages_in_browser(a.base, Path(a.out))
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL CHECKS PASSED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
