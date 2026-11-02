"""Probe 7: Agora API surface - token lists, candles/history, launches."""
import json, re, requests
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
S = requests.Session(); S.headers.update({"user-agent": UA, "accept": "application/json"})
html = S.get("https://www.agoradex.app/", timeout=20).text
chunks = sorted(set(re.findall(r'/_next/static/[^"\']+\.js', html)))
paths = set()
for c in chunks:
    try:
        js = S.get("https://www.agoradex.app" + c, timeout=20).text
    except Exception:
        continue
    for m in re.findall(r'["\'`](/api/[a-zA-Z0-9_\-/${}.?=&]+)', js):
        paths.add(m)
    for m in re.findall(r'api\.agoradex\.app(/[a-zA-Z0-9_\-/${}.?=&]+)', js):
        paths.add(m)
print("JS chunks:", len(chunks))
print("API paths in JS:"); [print("  ", p) for p in sorted(paths)]
# also traders page + token page chunks
for page in ("/traders", "/token/0x9a9815af521149e8d982791703a31bca2f95f534", "/tokens", "/launches"):
    h = S.get("https://www.agoradex.app" + page, timeout=20).text
    for c in sorted(set(re.findall(r'/_next/static/[^"\']+\.js', h)) - set(chunks)):
        chunks.append(c)
        try:
            js = S.get("https://www.agoradex.app" + c, timeout=20).text
        except Exception:
            continue
        for m in re.findall(r'["\'`](/api/[a-zA-Z0-9_\-/${}.?=&]+)', js):
            if m not in paths: print("  (page", page, ")", m); paths.add(m)
T = "0x9a9815af521149e8d982791703a31bca2f95f534"
A = "https://api.agoradex.app"
cands = ["/api/tokens", "/api/tokens?limit=5", "/api/tokens?sort=volume&limit=5", "/api/tokens?range=24h&limit=5",
         f"/api/tokens/{T}/candles", f"/api/tokens/{T}/candles?interval=1h", f"/api/tokens/{T}/chart",
         f"/api/tokens/{T}/ohlc", f"/api/tokens/{T}/ohlcv", f"/api/tokens/{T}/history", f"/api/tokens/{T}/trades",
         f"/api/tokens/{T}/holders", f"/api/candles/{T}", f"/api/chart/{T}", "/api/launches", "/api/trending",
         "/api/stats", "/api/market", "/api/tokens/trending", "/api/tokens/new"]
for p in sorted(paths):
    if "$" not in p and "{" not in p: cands.append(p)
    else: cands.append(re.sub(r"\$\{[^}]*\}", T, p))
seen = set()
for p in cands:
    if p in seen: continue
    seen.add(p)
    try:
        r = S.get(A + p, timeout=20)
        body = r.text
        ct = r.headers.get("content-type", "")
        print(f"\n{r.status_code} {p} [{ct[:20]}] len={len(body)}")
        if r.ok and "json" in ct:
            j = r.json()
            if isinstance(j, dict):
                print("  keys:", list(j)[:25])
                for k, v in j.items():
                    if isinstance(v, list) and v:
                        print(f"  {k}[0] ({len(v)} items):", json.dumps(v[0])[:600]); break
                else:
                    print("  ", json.dumps(j)[:700])
            elif isinstance(j, list):
                print(f"  list({len(j)}):", json.dumps(j[0])[:600] if j else "[]")
    except Exception as e:
        print("ERR", p, e)
