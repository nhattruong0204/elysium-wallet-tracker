"""Probe 9: which Elysium RPCs accept eth_sendRawTransaction? Sends a deliberately
malformed raw tx (cannot be valid or broadcast) and reports each node's answer."""
import requests
EPS = ["https://rpc.thekleos.xyz/rpc", "https://rpc-live.boxa.club", "https://rpc.boxa.club",
       "https://rpc-live.elyspump.com", "https://rpc.elyspump.com", "https://boxa.club/api/rpc",
       "https://rpc.elysium.kinetiq.xyz", "https://rpc.elysiumchain.xyz"]
BAD_TX = "0x02f86b8301863d8085e8d4a5100085e8d4a5100082520894000000000000000000000000000000000000dead80c080a0" + "11" * 32 + "a0" + "22" * 32
for u in EPS:
    try:
        r = requests.post(u, json={"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [BAD_TX]}, timeout=20)
        try:
            j = r.json(); out = j.get("error") or j.get("result")
        except ValueError:
            out = r.text[:150]
        cid = requests.post(u, json={"jsonrpc": "2.0", "id": 2, "method": "eth_chainId", "params": []}, timeout=20).json().get("result")
        print(f"{u:40} chainId={cid} HTTP {r.status_code} -> {out}")
    except Exception as e:
        print(f"{u:40} ERR {type(e).__name__}: {str(e)[:100]}")
