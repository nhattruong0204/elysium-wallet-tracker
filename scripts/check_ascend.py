"""Post-deploy check: Ascend index state and the new DD block for recently traded Ascend coins."""
import sqlalchemy as sa

from agora_tracker import cards
from agora_tracker.cards import Links
from agora_tracker.config import load_config
from agora_tracker.db import Store
from agora_tracker.launchintel import DueDiligence
from agora_tracker.rpc import RpcClient

cfg = load_config("/config/config.yaml")
c = cfg.chain
store = Store(chain_id=c.chain_id)
rpc = RpcClient(c.rpc_urls, c.rpc_timeout_s, c.rpc_max_retries, c.rpc_batch_size, expected_chain_id=c.chain_id)
with store.engine.connect() as cn:
    n, lo, hi = cn.execute(sa.text("SELECT count(*), min(block_number), max(block_number) FROM ascend_launches")).one()
    print(f"ascend_launches: {n} rows, blocks {lo}..{hi}, cursor {store.get_kv('ascend_cursor')}")
    toks = [r[0] for r in cn.execute(sa.text("""
        SELECT t.token FROM trades t JOIN ascend_launches a ON a.token = t.token
        WHERE t.kind = 'BUY' AND t.block_time > now() - interval '24 hours'
        GROUP BY t.token ORDER BY max(t.block_time) DESC LIMIT 3"""))]
dd = DueDiligence(rpc, wrapped=cfg.intel.wrapped_native, factories=cfg.intel.dd_factories,
                  ascend_lookup=store.ascend_launch, budget_s=8)
for tk in toks:
    d = dd.probe(tk)
    print(f"\n{tk} venue={d.venue} partial={d.partial}")
    for line in cards.dd_lines(d, Links(cfg.links), 40.0):
        print("   ", line)
