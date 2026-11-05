"""Read-only launchpad audit: Ascend's real token set from chain events, and how tracked
wallets fared per launchpad. Prints aggregates only (public on Actions logs)."""
import statistics as st
import sys
import time
from collections import Counter, defaultdict

import sqlalchemy as sa

from agora_tracker.config import load_config
from agora_tracker.db import Store
from agora_tracker.launchintel import keccak256
from agora_tracker.rpc import RpcClient

cfg = load_config("/config/config.yaml")
c = cfg.chain
rpc = RpcClient(c.rpc_urls, c.rpc_timeout_s, c.rpc_max_retries, c.rpc_batch_size,
                expected_chain_id=c.chain_id)
store = Store(chain_id=c.chain_id)
HOOK = "0xe0def4221b9840a610d4e7d55812a79a6431a8ec"
TOPIC = "0x" + keccak256(b"Launched(bytes32,address,address,address,uint256,uint256,uint24,uint24)").hex()
WHYPE, USDC, KNTQ = ("0xba876237b0f021a6c6d852d6c26e107997cc5e81",
                     "0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65",
                     "0xe45a355d9e9cad2ad85f2d03e3ee3102a036acf0")

head = int(rpc.call("eth_blockNumber", []), 16)
start, step = 422319, 5000
ascend: dict[str, dict] = {}
b = start
t0 = time.time()
while b <= head:
    e = min(head, b + step - 1)
    try:
        logs = rpc.call("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(e),
                                         "address": HOOK, "topics": [TOPIC]}]) or []
    except Exception as ex:  # range too big: halve
        if step > 500:
            step //= 2
            continue
        print("  getLogs failed at", b, ex); break
    for lg in logs:
        tok = "0x" + lg["topics"][2][-40:]
        d = lg["data"][2:]
        quote = "0x" + d[24:64]
        fee = int(d[64 * 3:64 * 4], 16) if len(d) >= 256 else None
        dev = int(d[64 * 2:64 * 3], 16) if len(d) >= 192 else 0
        sup = int(d[64:128], 16) if len(d) >= 128 else 0
        ascend[tok] = {"block": int(lg["blockNumber"], 16), "quote": quote, "fee": fee,
                       "creator": "0x" + lg["topics"][3][-40:], "dev_pct": 100 * dev / sup if sup else None}
    b = e + 1
print(f"=== Ascend on-chain: {len(ascend)} launches since block {start} (head {head}), scanned in {time.time()-t0:.0f}s")
q = Counter({WHYPE: "HYPE", USDC: "USDC", KNTQ: "KNTQ"}.get(v["quote"], "other") for v in ascend.values())
print("  quote asset:", dict(q))
fees = Counter(v["fee"] for v in ascend.values())
print("  fee (pips, 10000=1%):", dict(fees.most_common(8)))
devs = [v["dev_pct"] for v in ascend.values() if v["dev_pct"] is not None]
if devs:
    print(f"  dev buy % of supply: median {st.median(devs):.2f}, p90 {sorted(devs)[int(len(devs)*.9)]:.2f}, "
          f">5%: {sum(x > 5 for x in devs)}, zero: {sum(x == 0 for x in devs)}")
cr = Counter(v["creator"] for v in ascend.values())
print("  distinct creators:", len(cr), "| top creator launches:", [n for _, n in cr.most_common(5)])
per_day = Counter(time.strftime("%m-%d", time.gmtime(0)) for _ in [])

with store.engine.connect() as cn:
    prof = dict(cn.execute(sa.text("SELECT address, coalesce(launchpad,'(none)') FROM token_profiles")).all())
    lab = Counter(prof.get(t, "(not in Agora)") for t in ascend)
    print("  how Agora labels Ascend's tokens:", dict(lab.most_common()))
    def pad(tok):
        return "ascend" if tok in ascend else prof.get(tok, "(not in Agora)")
    rows = cn.execute(sa.text("""
        SELECT token, wallet, kind, hype_wei, block_time FROM trades
        WHERE kind IN ('BUY','SELL') AND confidence='high' AND hype_wei IS NOT NULL
          AND block_time > now() - interval '7 days'
          AND token <> ALL(:m)"""), {"m": list(cfg.alerts.mute_tokens)}).all()
    pnl = cn.execute(sa.text("""
        SELECT token, pnl_wei FROM realized_pnl WHERE status='measured' AND created_at > now() - interval '7 days'
          AND token <> ALL(:m)"""), {"m": list(cfg.alerts.mute_tokens)}).all()
    snaps = cn.execute(sa.text("""
        SELECT DISTINCT ON (token) token, mcap_hype FROM token_market_snapshots
        WHERE captured_at > now() - interval '2 hours' AND mcap_hype > 0 ORDER BY token, captured_at DESC""")).all()
    first = cn.execute(sa.text("""
        WITH f AS (SELECT token, min(block_time) t FROM trades WHERE kind='BUY' AND confidence='high'
                   AND block_time > now() - interval '7 days' GROUP BY token)
        SELECT f.token, (SELECT s.mcap_hype FROM token_market_snapshots s WHERE s.token=f.token
                         AND s.captured_at BETWEEN f.t - interval '30 min' AND f.t + interval '30 min'
                         AND s.mcap_hype > 0 ORDER BY abs(extract(epoch FROM s.captured_at - f.t)) LIMIT 1) m
        FROM f""")).all()

now_m = {t: float(m) for t, m in snaps}
first_m = {t: float(m) for t, m in first if m}
agg = defaultdict(lambda: {"tok": set(), "w": set(), "buy": 0.0, "sell": 0.0, "pnl": 0.0, "pn": 0})
for tok, w, kind, wei, _ in rows:
    a = agg[pad(tok)]
    a["tok"].add(tok); a["w"].add(w)
    a["buy" if kind == "BUY" else "sell"] += int(wei) / 1e18
for tok, p in pnl:
    a = agg[pad(tok)]; a["pnl"] += int(p) / 1e18; a["pn"] += 1
print("\n=== Tracked wallets per launchpad, last 7 days (verified legs; muted tokens excluded)")
print("  launchpad        tokens wallets  buy_HYPE  sell_HYPE  realized_PnL_HYPE(n)  first-buy->now MC: median x, %<0.5x, %>2x (n)")
for name, a in sorted(agg.items(), key=lambda kv: -kv[1]["buy"]):
    xs = [now_m[t] / first_m[t] for t in a["tok"] if t in now_m and t in first_m]
    xs.sort()
    mx = f"{st.median(xs):.2f}x {100*sum(x<0.5 for x in xs)/len(xs):.0f}% {100*sum(x>2 for x in xs)/len(xs):.0f}% ({len(xs)})" if xs else "n/a"
    print(f"  {name[:15]:15s} {len(a['tok']):6d} {len(a['w']):7d} {a['buy']:9.1f} {a['sell']:10.1f} {a['pnl']:12.1f} ({a['pn']:4d})    {mx}")
