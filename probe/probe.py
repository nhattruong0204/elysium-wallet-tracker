"""Probe 3: elyscan.live URL formats with a real recent Elysium tx; BOXA page for a non-BOXA token."""
import json, re
import requests
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
S = requests.Session(); S.headers.update({"user-agent": UA})
RPC = "https://rpc.thekleos.xyz/rpc"
def rpc(m, p): return S.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=20).json()["result"]
head = int(rpc("eth_blockNumber", []), 16)
TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
logs = rpc("eth_getLogs", [{"fromBlock": hex(head - 300), "toBlock": hex(head), "topics": [TOPIC]}])
lg = logs[-1]
tx, token, addr = lg["transactionHash"], lg["address"], "0x" + lg["topics"][2][-40:]
print("head", head, "tx", tx, "token", token, "addr", addr)
for path in [f"/tx/{tx}", f"/address/{addr}", f"/token/{token}", "/api/v2/config/network", "/api/v2/stats",
             f"/api/v2/transactions/{tx}"]:
    u = "https://elyscan.live" + path
    try:
        r = S.get(u, timeout=20)
        t = r.text
        title = re.search(r"<title>([^<]{0,120})", t)
        print(f"{r.status_code} len={len(t):>7} has_hash={tx[2:12] in t or addr[2:12] in t.lower() or token[2:12] in t.lower()} "
              f"title={title.group(1) if title else '-'} {u}")
        if "/api/" in path and r.headers.get("content-type", "").startswith("application/json"):
            print("   ", json.dumps(r.json())[:400])
    except Exception as e:
        print("ERR", u, e)
blob = "BLOB"
r = S.get("https://boxa.club/token?address=0x9a9815af521149e8d982791703a31bca2f95f534", timeout=20)
print("boxa foreign token page mentions BLOB:", blob in r.text, len(r.text))
r = S.get("https://www.thekleos.xyz/token/0x9a9815af521149e8d982791703a31bca2f95f534", timeout=20)
print("kleos page title:", re.search(r"<title>([^<]{0,120})", r.text).group(1) if "<title>" in r.text else "-")
