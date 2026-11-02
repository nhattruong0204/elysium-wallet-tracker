"""Probe 2: Agora token API shape + launchpad token-page URL formats."""
import json, re
from urllib.parse import urljoin
import requests
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
S = requests.Session(); S.headers.update({"user-agent": UA})

def get(url, **kw):
    try: return S.get(url, timeout=20, **kw)
    except Exception as e: print("  ERR", url, e); return None

def sec(t): print(f"\n######## {t}")

tr = get("https://api.agoradex.app/api/traders", params={"limit": 3}).json()
sec("traders.tokens sample + hypeUsd"); print("hypeUsd:", tr.get("hypeUsd"))
toks = tr.get("tokens")
print(type(toks).__name__, (list(toks.items())[:2] if isinstance(toks, dict) else toks[:2]))
token = tr["traders"][0]["top"][0]

sec(f"/api/tokens/{token}?range=24h")
r = get(f"https://api.agoradex.app/api/tokens/{token}", params={"range": "24h"})
print(r.status_code if r else None, r.headers.get("content-type") if r else None)
try: print(json.dumps(r.json(), indent=1)[:4000])
except Exception: print((r.text if r else "")[:500])

sec("/api/tokens (list) sample")
r = get("https://api.agoradex.app/api/tokens", params={"limit": 2})
try:
    j = r.json(); print(json.dumps(j, indent=1)[:2500])
except Exception: print(r.status_code if r else None)

sec(f"/api/traders/{tr['traders'][0]['address']}")
r = get(f"https://api.agoradex.app/api/traders/{tr['traders'][0]['address']}")
try: print(json.dumps(r.json(), indent=1)[:1500])
except Exception: print(r.status_code if r else None)

sec("launchpad sites: bundle route templates")
for site in ["https://www.thekleos.xyz", "https://boxa.club", "https://elysp.com"]:
    r = get(site)
    if r is None: continue
    print(f"{site} -> {r.status_code}")
    html = r.text
    scripts = list(dict.fromkeys(urljoin(site + "/", m) for m in re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', html)))
    blob = html
    for u in scripts[:60]:
        rr = get(u)
        if rr is not None: blob += rr.text
    tm = sorted(set(re.findall(r'`[^`]{0,60}\$\{[^}]{1,40}\}[^`]{0,40}`', blob)))
    tm = [x for x in tm if re.search(r'^`/|token|coin|/t/|/c/|explorer|/tx/|/address/', x, re.I)]
    print("  templates:", tm[:40])
    rt = sorted(set(re.findall(r'["\'`](/(?:token|tokens|coin|coins|t|c|launch|trade|pair)/[A-Za-z0-9_\-/${}.]*)', blob)))
    print("  routes:", rt[:30])
    ex = sorted(set(re.findall(r'https?://[A-Za-z0-9.\-]*(?:scan|explorer|blockscout)[A-Za-z0-9.\-]*[^"\'`\s]{0,40}', blob)))
    print("  explorers:", ex[:20])

sec("candidate token page URLs")
for pat in ["https://www.thekleos.xyz/token/{a}", "https://www.thekleos.xyz/coin/{a}", "https://www.thekleos.xyz/{a}",
            "https://boxa.club/token/{a}", "https://boxa.club/coin/{a}", "https://boxa.club/{a}",
            "https://www.agoradex.app/token/{a}"]:
    u = pat.format(a=token); r = get(u, allow_redirects=True)
    if r is not None:
        print(f"{r.status_code} len={len(r.text):>7} mentions_addr={token.lower() in r.text.lower()} {u} -> {r.url}")
