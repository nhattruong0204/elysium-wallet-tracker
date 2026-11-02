"""Probe 8: Agora token list item + detail candles/trades formats, ranges, paging."""
import json, requests, time
S = requests.Session(); S.headers.update({"user-agent": "Mozilla/5.0", "accept": "application/json"})
A = "https://api.agoradex.app"
T = "0x9a9815af521149e8d982791703a31bca2f95f534"
j = S.get(A + "/api/tokens?sort=volume&window=24h&launchpad=all&limit=3&offset=0", timeout=20).json()
it = j["tokens"][1]; it["stats"]["spark"] = f"<{len(it['stats'].get('spark') or [])} pts>"
print("LIST ITEM:", json.dumps(it, indent=1)[:3000])
print("totals:", j["totals"], "synced:", j["synced"])
for q in ("limit=200", "limit=500", "sort=losers&window=1h&limit=3", "sort=gainers&window=6h&limit=3",
          "sort=gainers&window=7d&limit=3", "sort=volume&window=1h&limit=3", "sort=trades&limit=3", "sort=holders&limit=3",
          "q=BLOB&limit=5", "launchpad=boxa&limit=3", "only=" + T):
    r = S.get(A + "/api/tokens?" + q, timeout=30); jj = r.json() if r.ok else r.text[:100]
    print(f"\n{q}: {r.status_code}", (len(jj.get('tokens', [])), jj.get('total'), [ (t['symbol'], round((t['stats'].get('change') or {}).get('1h') or 0, 3)) for t in jj.get('tokens', [])[:5]]) if isinstance(jj, dict) else jj)
for rng in ("1h", "6h", "24h", "7d", "30d", "all", "1d", "1w"):
    t0 = time.time(); r = S.get(f"{A}/api/tokens/{T}?range={rng}", timeout=30)
    d = r.json() if r.ok else {}
    c = d.get("candles") or []
    tr = d.get("trades") or []
    print(f"\nrange={rng}: {r.status_code} {time.time()-t0:.1f}s candles={len(c)} trades={len(tr)} devTrades={len(d.get('devTrades') or [])}")
    if c: print("  candle first:", json.dumps(c[0]), "last:", json.dumps(c[-1]))
    if tr and rng == "24h": print("  trade:", json.dumps(tr[0])[:500])
    if rng == "24h": print("  stats:", json.dumps({k: v for k, v in d.get("stats", {}).items() if k != "spark"})[:900]); print("  tax:", d.get("tax"), "version:", d.get("version"), "tradable:", d.get("tradable"))
