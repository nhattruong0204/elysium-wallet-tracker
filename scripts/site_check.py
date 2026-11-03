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


def pages_in_browser(base: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    toks = api_json(base, "/api/v1/tokens?range=30d")
    items = (toks.get("page") or {}).get("items") or []
    paths = ["/", "/tokens", "/smart-money", "/early", "/scanner", "/about"]
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
        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://onchainflow.app")
    ap.add_argument("--out", default="shots")
    a = ap.parse_args()
    host = urlparse(a.base).hostname
    tls(host)
    http_basics(a.base)
    pages_in_browser(a.base, Path(a.out))
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL CHECKS PASSED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
