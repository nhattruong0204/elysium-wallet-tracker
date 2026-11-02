"""Probe 6: the real RpcClient + ChainFollower against the 3 live Elysium RPCs."""
import logging, sys, time
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
from agora_tracker.rpc import RpcClient
URLS = ["https://rpc.thekleos.xyz/rpc", "https://rpc-live.boxa.club", "https://rpc-live.elyspump.com",
        "https://rpc.boxa.club"]
c = RpcClient(URLS, expected_chain_id=99901, max_head_lag=50)
print("verify", c.verify_all())
print("heads", c.refresh_heads()); print("live", c.live_urls)
T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
for u in URLS[:3]:
    c._idx = URLS.index(u) if u in c.live_urls else 0
    c._idx = c.live_urls.index(u)
    with c.pinned() as pu:
        head = c.block_number()
        logs = c.call("eth_getLogs", [{"fromBlock": hex(head - 400), "toBlock": hex(head), "topics": [T]}])
        txs = sorted({l["transactionHash"] for l in logs})[:45]
        t0 = time.time()
        res = c.batch([("eth_getTransactionReceipt", [h]) for h in txs])
        bad = sum(isinstance(r, Exception) or r is None for r in res)
        print(f"{pu}: head {head}, {len(logs)} logs, {len(res)} receipts in {time.time()-t0:.1f}s, bad {bad}, "
              f"learned batch limit {c._batch_limit.get(pu)}, batch_off {pu in c._batch_off}")
