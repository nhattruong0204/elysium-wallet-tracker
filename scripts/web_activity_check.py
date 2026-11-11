#!/usr/bin/env python3
"""Read-only check of the public Activity feed's default token visibility.

Runs on the VPS host (stdlib only) against the loopback port and/or the public domain:

    python3 scripts/web_activity_check.py --base http://127.0.0.1:8100 \
        --hidden 0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65 [--expect-hidden]

Every request carries a unique cache-busting parameter (the server ignores unknown
parameters), so a browser or proxy cache cannot be mistaken for a server-side result.
Without --expect-hidden it only reports (the "before" snapshot); with it, it exits 1 when the
hidden token shows up in a default feed, when other tokens disappear, or when paging, the
since_id poll or the explicit token filter misbehave. GET requests only; nothing is written.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ADDR_LEN = 42


def fetch(base: str, path: str, params: dict[str, object] | None = None) -> tuple[int, str, dict]:
    q = {**(params or {}), "_cb": f"{time.time():.6f}"}
    url = f"{base}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": "onchainflow-activity-check/1",
                                               "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def api(base: str, path: str, **params: object) -> dict:
    code, body, _ = fetch(base, path, params)
    if code != 200:
        raise RuntimeError(f"{path} {params} -> HTTP {code}: {body[:200]}")
    return json.loads(body)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--hidden", required=True, help="exact token contract address")
    ap.add_argument("--expect-hidden", action="store_true")
    ap.add_argument("--range", default="24h")
    a = ap.parse_args()
    base, hid = a.base.rstrip("/"), a.hidden.strip().lower()
    if not (hid.startswith("0x") and len(hid) == ADDR_LEN):
        ap.error("--hidden must be a full 0x address")
    fails: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"  [{'ok' if ok else 'FAIL' if a.expect_hidden else 'info'}] {what}")
        if not ok:
            fails.append(what)

    rng = a.range
    # 1. default first page
    p1 = api(base, "/api/v1/activity", range=rng, limit=100)
    it = p1["items"]
    n_hid = sum(i["token"] == hid for i in it)
    syms = collections.Counter(f"{i.get('symbol') or '?'}@{i['token'][:8]}" for i in it)
    print(f"default feed ({base}, range={rng}): {len(it)} items, hidden-token items {n_hid}, "
          f"distinct tokens {len({i['token'] for i in it})}, total={p1['total']}, "
          f"head_id={p1['head_id']}, newest={it[0]['block_time'] if it else None}")
    print(f"  top tokens on page 1: {syms.most_common(6)}")
    print(f"  scanner: {p1['freshness'].get('scanner_status')}, "
          f"last indexed block {p1['freshness'].get('last_indexed_block')}")
    check(n_hid == 0, "default page 1 has no hidden-token rows")
    others = [i for i in it if i["token"] != hid]
    check(len(others) > 0, "other tokens are still listed")
    # 2. page 2 by cursor, and back
    nxt = p1["page"]["next_cursor"]
    if nxt:
        p2 = api(base, "/api/v1/activity", range=rng, limit=100, cursor=nxt, dir="next")
        ids1, ids2 = [i["id"] for i in it], [i["id"] for i in p2["items"]]
        keys = [(i["block_time"], i["id"]) for i in it + p2["items"]]
        print(f"page 2: {len(ids2)} items, hidden-token items "
              f"{sum(i['token'] == hid for i in p2['items'])}")
        check(sum(i["token"] == hid for i in p2["items"]) == 0, "page 2 has no hidden-token rows")
        check(not set(ids1) & set(ids2), "pages 1 and 2 do not overlap")
        check(keys == sorted(keys, reverse=True), "pages 1+2 are newest-first without reordering")
        prv = p2["page"]["prev_cursor"]
        if prv:
            back = api(base, "/api/v1/activity", range=rng, limit=100, cursor=prv, dir="prev")
            check([i["id"] for i in back["items"]] == ids1, "prev cursor returns page 1 exactly")
    else:
        print("page 2: none (fewer than 100 rows in range)")
    # 3. live poll (since_id) a little behind the head
    if it:
        since = min(i["id"] for i in it[:20]) - 1
        s = api(base, "/api/v1/activity", range=rng, since_id=since)
        hid_s = sum(i["token"] == hid for i in s["items"])
        print(f"since_id={since}: {s['count']} items, hidden-token items {hid_s}, "
              f"head_id={s['head_id']}, overflow={s['overflow']}")
        check(hid_s == 0, "since_id poll has no hidden-token rows")
        check(all(i["id"] > since for i in s["items"]), "since_id poll returns only newer ids")
    # 4. explicit filter on the hidden token, and an ordinary token
    t = api(base, "/api/v1/activity", range=rng, limit=50, token=hid)
    print(f"explicit token={hid[:10]}..: {len(t['items'])} items, total={t['total']}")
    check(all(i["token"] == hid for i in t["items"]), "explicit filter returns only that token")
    check(len(t["items"]) > 0 or not a.expect_hidden,
          "explicit filter still shows the hidden token (when it traded in range)")
    if others:
        tok = collections.Counter(i["token"] for i in others).most_common(1)[0][0]
        o = api(base, "/api/v1/activity", range=rng, limit=20, token=tok)
        print(f"ordinary token {tok[:10]}..: {len(o['items'])} items, total={o['total']}")
        check(len(o["items"]) > 0 and all(i["token"] == tok for i in o["items"]),
              "an ordinary token's filter works")
    # 5. server-rendered pages and the overview preview
    for path, params in (("/activity", {"range": rng}), ("/", {}), ("/scanner", {}),
                         (f"/tokens/{hid}", {}), ("/healthz", {})):
        code, body, hdr = fetch(base, path, params)
        extra = ""
        if path == "/activity":
            extra = f", hidden-token mentions {body.lower().count(hid)}"
            check(code == 200 and hid not in body.lower(), "/activity HTML has no hidden-token rows")
        print(f"GET {path}: {code}, {len(body)} bytes, cache-control={hdr.get('cache-control')}{extra}")
        if path != "/activity":
            check(code == 200, f"{path} answers 200")
    ov = api(base, "/api/v1/overview")
    rec = ov.get("recent") or []
    print(f"overview preview: {len(rec)} rows, hidden-token rows "
          f"{sum(r['token'] == hid for r in rec)}; still ranked in top_tokens: "
          f"{any(r.get('token') == hid for r in ov.get('top_tokens') or [])}")
    check(all(r["token"] != hid for r in rec), "overview preview has no hidden-token rows")
    print(f"RESULT: {'PASS' if not fails else 'FAIL: ' + '; '.join(fails)}")
    return 1 if (fails and a.expect_hidden) else 0


if __name__ == "__main__":
    sys.exit(main())
