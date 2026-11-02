"""Probe 4: BOXA developer page -> RPC endpoints; benchmark them against Kleos."""
import json, re, time, statistics
import requests
from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
S = requests.Session(); S.headers.update({"user-agent": UA})
urls, bodies = set(), []
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(user_agent=UA)
    def on_resp(r):
        try:
            if any(x in r.url for x in (".js", "/api/")) and "boxa" in r.url:
                bodies.append((r.url, r.text()))
        except Exception:
            pass
    pg.on("response", on_resp)
    pg.goto("https://boxa.club/developers", wait_until="networkidle", timeout=60000)
    time.sleep(3)
    text = pg.inner_text("body")
    print("===== PAGE TEXT =====")
    print(text[:9000])
    for a in pg.query_selector_all("a"):
        h = a.get_attribute("href") or ""
        if h and not h.startswith("#"):
            urls.add(h)
    b.close()
print("===== LINKS =====")
for u in sorted(urls): print(u)
cands = set(re.findall(r"(?:https|wss)://[a-zA-Z0-9.\-]+(?:/[a-zA-Z0-9_\-./]*)?", text))
for u, t in bodies:
    for m in re.findall(r"(?:https|wss)://[a-zA-Z0-9.\-]*(?:rpc|node|ws)[a-zA-Z0-9.\-]*(?:/[a-zA-Z0-9_\-./]*)?", t):
        cands.add(m)
    for m in re.findall(r"(?:https|wss)://[a-zA-Z0-9.\-]+/[a-zA-Z0-9_\-./]*rpc[a-zA-Z0-9_\-./]*", t):
        cands.add(m)
print("===== RPC-LIKE URLS =====")
rpcs = sorted(u for u in cands if "rpc" in u.lower() or u.startswith("wss"))
for u in rpcs: print(u)

def call(url, m, prm, timeout=20):
    t = time.time()
    r = S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": prm}, timeout=timeout)
    return r.json(), (time.time() - t) * 1000

KLEOS = "https://rpc.thekleos.xyz/rpc"
TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
test = [KLEOS] + [u for u in rpcs if u.startswith("https") and "thekleos" not in u]
for url in test:
    print(f"\n===== BENCH {url}")
    try:
        cid, ms = call(url, "eth_chainId", []); print("chainId", cid.get("result"), cid.get("error"), f"{ms:.0f}ms")
        if cid.get("result") is None: continue
        lat = []
        for _ in range(10):
            j, ms = call(url, "eth_blockNumber", []); lat.append(ms)
        head = int(j["result"], 16)
        print("head", head, "latency p50 %.0fms max %.0fms" % (statistics.median(lat), max(lat)))
        for span in (100, 1000, 2000, 5000, 10000):
            j, ms = call(url, "eth_getLogs", [{"fromBlock": hex(head - span), "toBlock": hex(head), "topics": [TOPIC]}], 60)
            print(f"getLogs span {span}: ", (f"{len(j['result'])} logs" if "result" in j else j.get("error")), f"{ms:.0f}ms")
        j, _ = call(url, "eth_getLogs", [{"fromBlock": hex(head - 300), "toBlock": hex(head), "topics": [TOPIC]}])
        lg = (j.get("result") or [None])[-1]
        if lg:
            tx = lg["transactionHash"]; addr = "0x" + lg["topics"][2][-40:]; bn = int(lg["blockNumber"], 16)
            for m, prm in (("eth_getTransactionByHash", [tx]), ("eth_getTransactionReceipt", [tx]),
                           ("eth_getBalance", [addr, hex(bn - 1)]), ("eth_getBalance", [addr, hex(max(1, head - 50000))]),
                           ("eth_getTransactionCount", [addr, hex(bn - 1)]), ("eth_getCode", [addr, "latest"]),
                           ("eth_getBlockByNumber", [hex(bn), False])):
                j, ms = call(url, m, prm)
                print(m, prm[1:] or "", "ok" if j.get("result") is not None else j.get("error"), f"{ms:.0f}ms")
        r = S.post(url, json=[{"jsonrpc": "2.0", "id": i, "method": "eth_blockNumber", "params": []} for i in range(50)], timeout=30)
        jj = r.json()
        print("batch 50:", f"{len(jj)} responses" if isinstance(jj, list) else jj)
        t = time.time(); errs = 0
        for i in range(40):
            j, _ = call(url, "eth_blockNumber", [])
            errs += "error" in j
        print(f"burst 40 sequential: {time.time()-t:.1f}s, errors {errs}")
    except Exception as e:
        print("ERR", type(e).__name__, e)
