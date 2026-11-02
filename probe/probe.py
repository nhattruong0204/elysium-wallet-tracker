"""Probe 5: do the live Elysium RPCs agree on block hashes? What does getLogs do past head?"""
import time, requests
S = requests.Session()
EPS = ["https://rpc.thekleos.xyz/rpc", "https://rpc-live.boxa.club", "https://rpc-live.elyspump.com",
       "https://rpc.boxa.club"]
T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
def call(u, m, p):
    try: return S.post(u, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=20).json()
    except Exception as e: return {"error": repr(e)}
for rnd in range(3):
    heads = {u: int(call(u, "eth_blockNumber", [])["result"], 16) for u in EPS}
    print("heads", {u.split('//')[1]: h for u, h in heads.items()})
    common = min(heads[u] for u in EPS[:3])
    for b in (common, common - 10, common - 500, 198000):
        hs = {u.split('//')[1]: (call(u, "eth_getBlockByNumber", [hex(b), False]).get("result") or {}).get("hash", "null")[:14] for u in EPS}
        print(f"  block {b}: agree={len(set(hs.values()))==1}", hs)
    for u in EPS:
        h = heads[u]
        r = call(u, "eth_getLogs", [{"fromBlock": hex(h - 5), "toBlock": hex(h + 50), "topics": [T]}])
        print(f"  {u.split('//')[1]} logs to head+50 ->", (f"{len(r['result'])} logs (silently clamped)" if "result" in r else r["error"]))
        r = call(u, "eth_getBlockByNumber", [hex(h + 50), False])
        print(f"  {u.split('//')[1]} block head+50 ->", r.get("result", r.get("error")))
    time.sleep(20)
