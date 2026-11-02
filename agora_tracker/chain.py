"""On-chain follower: pulls ERC-20 Transfer logs touching tracked wallets and builds trade events.

Why logs + balance diffs instead of decoding each launchpad's swap ABI:
  - Works for every launchpad (Kleos, BOXA, ElysPump, ...) and every Uniswap pool, including
    ones launched after this script was written, with zero per-protocol code.
  - Native-HYPE legs are invisible in logs (internal calls), so we take the wallet's balance
    change across the block and add the gas fee back.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from .classify import TradeEvent, classify
from .config import ChainConfig
from .db import Store
from .rpc import RpcClient, RpcError

log = logging.getLogger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SEL_SYMBOL = "0x95d89b41"
SEL_DECIMALS = "0x313ce567"


def pad_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def topic_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_abi_string(hexdata: str | None) -> str | None:
    if not hexdata or hexdata == "0x":
        return None
    b = bytes.fromhex(hexdata[2:])
    try:
        if len(b) >= 64 and int.from_bytes(b[:32], "big") == 32:
            n = int.from_bytes(b[32:64], "big")
            return b[64:64 + n].decode("utf-8", "replace").strip() or None
        return b[:32].rstrip(b"\x00").decode("utf-8", "replace").strip() or None  # bytes32
    except Exception:
        return None


class TokenCache:
    def __init__(self, rpc: RpcClient, store: Store):
        self.rpc, self.store = rpc, store

    def resolve(self, tokens: set[str]) -> None:
        missing = [t for t in tokens if self.store.get_token(t) is None]
        if not missing:
            return
        calls = []
        for t in missing:
            calls.append(("eth_call", [{"to": t, "data": SEL_SYMBOL}, "latest"]))
            calls.append(("eth_call", [{"to": t, "data": SEL_DECIMALS}, "latest"]))
        res = self.rpc.batch(calls)
        for i, t in enumerate(missing):
            sym_raw, dec_raw = res[2 * i], res[2 * i + 1]
            sym = decode_abi_string(sym_raw if isinstance(sym_raw, str) else None) or t[:8]
            try:
                dec = int(dec_raw, 16) if isinstance(dec_raw, str) and dec_raw != "0x" else 18
            except ValueError:
                dec = 18
            if dec > 36:
                dec = 18
            self.store.put_token(t, sym, dec)

    def get(self, token: str) -> tuple[str, int]:
        return self.store.get_token(token) or (token[:8], 18)


@dataclass
class _TxFlows:
    tx: str
    block: int
    wallet: str
    deltas: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class ChainFollower:
    def __init__(self, rpc: RpcClient, store: Store, cfg: ChainConfig, dust_wei: int,
                 ignore_tokens: set[str]):
        self.rpc, self.store, self.cfg = rpc, store, cfg
        self.dust_wei = dust_wei
        self.ignore_tokens = ignore_tokens
        self.tokens = TokenCache(rpc, store)

    # --- logs -----------------------------------------------------------------
    def _get_logs(self, frm: int, to: int, topics: list) -> list[dict]:
        try:
            return self.rpc.call("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                                  "topics": topics}]) or []
        except RpcError as e:
            if to <= frm:
                raise
            mid = (frm + to) // 2
            log.debug("getLogs %d-%d failed (%s); splitting", frm, to, e)
            return self._get_logs(frm, mid, topics) + self._get_logs(mid + 1, to, topics)

    def fetch_logs(self, frm: int, to: int, wallets: list[str]) -> list[dict]:
        seen: dict[tuple[str, str], dict] = {}
        step = max(1, self.cfg.wallet_chunk)
        for i in range(0, len(wallets), step):
            padded = [pad_topic(w) for w in wallets[i:i + step]]
            for topics in ([TRANSFER_TOPIC, padded], [TRANSFER_TOPIC, None, padded]):
                for lg in self._get_logs(frm, to, topics):
                    if lg.get("removed"):
                        continue
                    seen[(lg["transactionHash"], lg["logIndex"])] = lg
        return list(seen.values())

    # --- main -----------------------------------------------------------------
    def scan(self, frm: int, to: int, wallets: set[str]) -> tuple[list[TradeEvent], dict[int, int]]:
        """Returns events for [frm, to] and {block: timestamp} for blocks with events."""
        if not wallets:
            return [], {}
        logs = self.fetch_logs(frm, to, sorted(wallets))
        flows: dict[tuple[str, str], _TxFlows] = {}
        wnative = self.cfg.wrapped_native
        for lg in logs:
            topics = lg.get("topics") or []
            if len(topics) != 3 or not lg.get("data") or lg["data"] == "0x":
                continue  # ERC-721 (4 topics) or malformed
            token = lg["address"].lower()
            src, dst = topic_addr(topics[1]), topic_addr(topics[2])
            amount = int(lg["data"][:66], 16)
            if amount == 0 or src == dst:
                continue
            tx, block = lg["transactionHash"].lower(), int(lg["blockNumber"], 16)
            for w, sign in ((src, -1), (dst, 1)):
                if w in wallets:
                    f = flows.setdefault((tx, w), _TxFlows(tx, block, w))
                    f.deltas[token] += sign * amount
        if not flows:
            return [], {}

        txs = sorted({f.tx for f in flows.values()})
        res = self.rpc.batch([("eth_getTransactionByHash", [t]) for t in txs] +
                             [("eth_getTransactionReceipt", [t]) for t in txs])
        tx_info = {t: res[i] for i, t in enumerate(txs)}
        receipts = {t: res[len(txs) + i] for i, t in enumerate(txs)}

        # wallet -> block -> number of its txs in that block. Balance diff is exact only when 1.
        per_block: dict[tuple[str, int], set[str]] = defaultdict(set)
        for f in flows.values():
            per_block[(f.wallet, f.block)].add(f.tx)
        single = [(w, b) for (w, b), s in per_block.items() if len(s) == 1]
        bal_res = self.rpc.batch(
            [("eth_getBalance", [w, hex(b)]) for w, b in single] +
            [("eth_getBalance", [w, hex(b - 1)]) for w, b in single])
        bal_delta: dict[tuple[str, int], int] = {}
        for i, (w, b) in enumerate(single):
            after, before = bal_res[i], bal_res[len(single) + i]
            if isinstance(after, str) and isinstance(before, str):
                bal_delta[(w, b)] = int(after, 16) - int(before, 16)

        events: list[TradeEvent] = []
        for f in flows.values():
            info = tx_info.get(f.tx)
            rcpt = receipts.get(f.tx)
            if not isinstance(info, dict):
                log.warning("tx %s not found; skipping", f.tx)
                continue
            if isinstance(rcpt, dict) and rcpt.get("status") == "0x0":
                continue
            sender = (info.get("from") or "").lower()
            is_sender = sender == f.wallet
            fee = 0
            if is_sender and isinstance(rcpt, dict):
                gp = rcpt.get("effectiveGasPrice") or info.get("gasPrice") or "0x0"
                fee = int(rcpt.get("gasUsed", "0x0"), 16) * int(gp, 16)

            deltas = dict(f.deltas)
            whype = deltas.pop(wnative, 0) if wnative else 0
            approx = False
            bd = bal_delta.get((f.wallet, f.block))
            if bd is not None:
                trade_hype = bd + fee + whype
            elif is_sender:
                trade_hype = -int(info.get("value", "0x0"), 16) + whype
                approx = True
            else:
                trade_hype = whype if whype else None
                approx = True
            deltas = {t: d for t, d in deltas.items() if t not in self.ignore_tokens}
            events.extend(classify(tx=f.tx, block=f.block, wallet=f.wallet, token_deltas=deltas,
                                   trade_hype_wei=trade_hype, dust_wei=self.dust_wei,
                                   approx=approx))

        blocks = sorted({e.block for e in events})
        ts: dict[int, int] = {}
        if blocks:
            for b, blk in zip(blocks, self.rpc.batch(
                    [("eth_getBlockByNumber", [hex(b), False]) for b in blocks])):
                if isinstance(blk, dict):
                    ts[b] = int(blk["timestamp"], 16)
            self.tokens.resolve({e.token for e in events} |
                                {e.counter_token for e in events if e.counter_token})
        events.sort(key=lambda e: (e.block, e.tx, e.wallet))
        return events, ts

    def filter_eoas(self, addrs: set[str]) -> set[str]:
        """Drop contracts (pools, routers, vaults) the leaderboard might list."""
        unknown = [a for a in addrs if self.store.get_is_contract(a) is None]
        if unknown:
            for a, code in zip(unknown, self.rpc.batch([("eth_getCode", [a, "latest"])
                                                        for a in unknown])):
                if isinstance(code, str):
                    # EIP-7702 delegated EOAs have code 0xef0100<addr>; they are still wallets.
                    is_contract = code not in ("0x", "0x0") and not code.startswith("0xef0100")
                    self.store.put_is_contract(a, is_contract)
        return {a for a in addrs if not self.store.get_is_contract(a)}
