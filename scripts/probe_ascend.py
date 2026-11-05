"""Read-only probe of Ascend's v4 contracts on real pools (verifies encodings before use)."""
import json
import subprocess
import sys

from agora_tracker.config import load_config
from agora_tracker.launchintel import keccak256, selector
from agora_tracker.rpc import RpcClient

cfg = load_config("/config/config.yaml")
c = cfg.chain
rpc = RpcClient(c.rpc_urls, c.rpc_timeout_s, c.rpc_max_retries, c.rpc_batch_size, expected_chain_id=c.chain_id)
HOOK = "0xe0def4221b9840a610d4e7d55812a79a6431a8ec"
EF = "0x63f6a8a8d0257c0363251a50cde8a8b1046e4d19"
SV = "0x27ed73ae0652fdcf71d6793412a7525f32be9dfe"
QT = "0x379f556c5a748bd7cc9baf0f7a344094ac1814f5"
PM = "0xe2e105d3f7209a9da11f83ccf3e7398a753823f1"
T_L = "0x" + keccak256(b"Launched(bytes32,address,address,address,uint256,uint256,uint24,uint24)").hex()
T_E = "0x" + keccak256(b"EscrowLaunched(bytes32,address,address,address,address,address,bool)").hex()
W = lambda x: f"{x:064x}"
P = lambda a: "0" * 24 + a[2:].lower()
H = lambda r: int(r, 16) if r and r != "0x" else 0

def call(to, sig, args=""):
    try:
        return rpc.call("eth_call", [{"to": to, "data": selector(sig) + args}, "latest"])
    except Exception as e:
        return f"ERR {str(e)[:120]}"

head = H(rpc.call("eth_blockNumber", []))
logs = rpc.call("eth_getLogs", [{"fromBlock": hex(head - 20000), "toBlock": hex(head),
                                 "address": [HOOK, EF], "topics": [[T_L, T_E]]}]) or []
print("recent logs", len(logs), "launched", sum(l["topics"][0] == T_L for l in logs),
      "escrow", sum(l["topics"][0] == T_E for l in logs))
esc = {l["topics"][1]: l for l in logs if l["topics"][0] == T_E}
launches = [l for l in logs if l["topics"][0] == T_L][-4:]
extra = sys.argv[1:]
for lg in launches:
    pid, tok = lg["topics"][1], "0x" + lg["topics"][2][-40:]
    d = lg["data"][2:]
    quote = "0x" + d[24:64]
    c0, c1 = sorted([tok, quote])
    key = P(c0) + P(c1) + W(0x800000) + W(200) + P(HOOK)
    pid_calc = "0x" + keccak256(bytes.fromhex(key)).hex()
    print(f"\n token {tok} quote {quote} poolId(event)={pid[:18]} calc_match={pid_calc == pid}")
    print("  supplySeeded", H("0x" + d[64:128]) / 1e18, "devBuyTokens", H("0x" + d[128:192]) / 1e18,
          "fee", H("0x" + d[192:256]), "builderFee", H("0x" + d[256:320]))
    a = pid[2:]
    for sig in ("currentFee(bytes32)", "poolTokenIs0(bytes32)", "poolLaunchedAt(bytes32)", "bidInventory(bytes32)",
                "accruedFees(bytes32)", "poolStartTick(bytes32)", "emaSqrtPrice(bytes32)"):
        r = call(HOOK, sig, a); print(f"  {sig:28s} {r if str(r).startswith('ERR') else H(r)}")
    print("  paused()", call(HOOK, "paused()"))
    s0 = call(SV, "getSlot0(bytes32)", a); liq = call(SV, "getLiquidity(bytes32)", a)
    print("  getSlot0", s0[:130] if isinstance(s0, str) else s0, "\n  getLiquidity", H(liq) if not str(liq).startswith("ERR") else liq)
    e = call(EF, "escrowOf(bytes32)", a); ea = "0x" + e[-40:] if not str(e).startswith("ERR") else None
    print("  escrowOf", ea, "| event escrow", "0x" + esc[pid]["topics"][2][-40:] if pid in esc else None)
    if ea:
        rec = "0x" + esc[pid]["topics"][3][-40:] if pid in esc else None
        print("  escrow quote balance", H(call(quote, "balanceOf(address)", P(ea))) / 1e18,
              "claimable(recipient)", H(call(ea, "claimable(address)", P(rec))) / 1e18 if rec else None)
    print("  PoolManager quote balance (all pools)", H(call(quote, "balanceOf(address)", P(PM))) / 1e18)
    is0 = H(call(HOOK, "poolTokenIs0(bytes32)", a)) == 1
    qsig = "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
    for frac in (0.01, 0.10, 1.0):
        amt = int(H("0x" + d[64:128]) * frac)
        body = W(0x20) + P(c0) + P(c1) + W(0x800000) + W(200) + P(HOOK) + W(1 if is0 else 0) + W(amt) + W(0x100) + W(0)
        r = call(QT, qsig, body)
        out = r if str(r).startswith("ERR") else H("0x" + r[2:66]) / 1e18
        print(f"  quote sell {frac*100:.0f}% of supply -> {out} quote")
abi = subprocess.run(["curl", "-s", "-m", "20", "https://ascendlaunch.xyz/abis/ascend-v5-99901-422319.json"], capture_output=True, text=True).stdout
try:
    j = json.loads(abi)
    def views(x):
        return [f"{f['name']}({','.join(i['type'] for i in f.get('inputs', []))})->{','.join(o['type'] for o in f.get('outputs', []))}"
                for f in x if f.get("type") == "function" and f.get("stateMutability") in ("view", "pure")]
    if isinstance(j, dict):
        for k, v in j.items():
            ab = v.get("abi") if isinstance(v, dict) else v
            if isinstance(ab, list) and "scrow" in k:
                print("\nABI", k, views(ab))
        print("ABI contracts:", list(j.keys())[:30])
except Exception as ex:
    print("ABI fetch/parse failed:", str(ex)[:100], abi[:200])
