"""Research probe (runs on a GitHub runner): link formats + Agora token data endpoints."""
import json, re, sys
from urllib.parse import urljoin, urlsplit
import requests
sys.path.insert(0, ".")
from agora_tracker.discover import collect_candidates, UA

S = requests.Session(); S.headers.update({"user-agent": UA, "accept": "application/json, text/html"})

def get(url, **kw):
    try:
        r = S.get(url, timeout=20, **kw)
        return r
    except Exception as e:
        print(f"  ERR {url}: {e}"); return None

def section(t): print(f"\n######## {t}")

section("agora traders sample")
r = get("https://api.agoradex.app/api/traders", params={"sort": "pnl", "limit": 3})
tr = r.json()
print("top keys:", list(tr.keys()) if isinstance(tr, dict) else type(tr))
first = tr["traders"][0]
print(json.dumps(first, indent=1)[:2500])

section("agora bundles: all absolute hosts + api/token paths")
abs_urls, paths = collect_candidates("https://www.agoradex.app/traders")
hosts = sorted({urlsplit(u).netloc for u in abs_urls})
print("hosts:", hosts)
r = get("https://www.agoradex.app/traders")
html = r.text if r else ""
# grab every quoted string that looks like a path or url from bundles again (broader)
scripts = list(dict.fromkeys(urljoin("https://www.agoradex.app/", m) for m in re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', html)))
blob = html
seen = set()
queue = list(scripts)
while queue and len(seen) < 150:
    u = queue.pop(0)
    if u in seen: continue
    seen.add(u)
    rr = get(u)
    if not rr: continue
    blob += rr.text
    for c in re.findall(r'"(static/chunks/[^"]+\.js)"', rr.text):
        queue.append(urljoin("https://www.agoradex.app/", "/_next/" + c))
api_paths = sorted(set(re.findall(r'["\'`](/api/[A-Za-z0-9_\-/.${}]+)', blob)))
print("api paths:", api_paths[:120])
ext = sorted(set(re.findall(r'https?://[A-Za-z0-9.\-]+(?:/[A-Za-z0-9_\-/.${}?=&]*)?', blob)))
ext = [u for u in ext if not re.search(r'w3\.org|reactjs|nextjs|mozilla|github|schema\.org|googleapis|gstatic|walletconnect|sentry', u)]
print("absolute urls (first 150):")
for u in ext[:150]: print("  ", u)
routes = sorted(set(re.findall(r'["\'`](/(?:token|tokens|coin|coins|t|c|trade|swap|launch|wallet|address|profile|trader|traders)/?[A-Za-z0-9_\-/${}]*)["\'`]', blob)))
print("page routes:", routes[:80])
tmpl = sorted(set(re.findall(r'`[^`]{0,80}\$\{[^}]{1,40}\}[^`]{0,80}`', blob)))
tmpl = [x for x in tmpl if re.search(r'token|coin|kleos|boxa|explorer|/tx/|address|launch', x, re.I)]
print("template strings mentioning token/launchpad/explorer:")
for x in tmpl[:120]: print("  ", x)
