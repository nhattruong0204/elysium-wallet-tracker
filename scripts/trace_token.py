#!/usr/bin/env python3
"""Read-only on-chain due-diligence trace for one ERC-20 on Elysium (chain 99901).

Uses only JSON-RPC reads (eth_call, eth_getCode, eth_getLogs, eth_getBalance, ...): nothing is
signed or sent. Stdlib only, so it runs on the VPS without installing anything. Answers:

  * who created the token and through which contract (the launchpad / factory)
  * how the supply is distributed now (pool / curve, creator, top holders)
  * who got in first, and which contract they paid (the venue to trade on)
  * which owner powers exist in the bytecode (mint, pause, blacklist, fees, upgrade...)
  * how much native HYPE the venue holds (a hard ceiling on exit liquidity)

Output is on-chain public data only. It is evidence, not advice.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
DEAD = {ZERO, "0x" + "0" * 39 + "1", "0x000000000000000000000000000000000000dead"}
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"


# ---- keccak-256 (hashlib only has NIST SHA3, which pads differently) ------------------------
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
       0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
       0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56],
        [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def _f(a: list[list[int]]) -> None:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        a[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    rate = 136
    p = bytearray(data) + b"\x01"
    p += b"\x00" * ((-len(p)) % rate)
    p[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(p), rate):
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(p[off + 8 * i: off + 8 * i + 8], "little")
        _f(a)
    out = b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out[:32]


def selector(sig: str) -> str:
    return "0x" + keccak256(sig.encode()).hex()[:8]


# ---- selector knowledge --------------------------------------------------------------------
RISK = {  # function -> why it matters to a buyer
    "mint(address,uint256)": "owner can mint more supply (dilution)",
    "mint(uint256)": "mint function",
    "pause()": "trading/transfers can be paused",
    "unpause()": "pausable",
    "blacklist(address)": "addresses can be blacklisted (sell-block risk)",
    "addBlacklist(address)": "addresses can be blacklisted (sell-block risk)",
    "setBlacklist(address,bool)": "addresses can be blacklisted (sell-block risk)",
    "blacklistAddress(address,bool)": "addresses can be blacklisted (sell-block risk)",
    "setBots(address[])": "bot list (can block sells)",
    "setTax(uint256)": "transfer tax can be changed",
    "setFee(uint256)": "fee can be changed",
    "setFees(uint256,uint256)": "fees can be changed",
    "setBuyFee(uint256)": "fee can be changed",
    "setSellFee(uint256)": "sell fee can be changed (honeypot lever)",
    "setMaxTxAmount(uint256)": "max-tx limit can be changed",
    "setMaxWallet(uint256)": "max-wallet limit can be changed",
    "setTradingEnabled(bool)": "trading can be switched off",
    "enableTrading()": "trading gate exists",
    "tradingEnabled()": "trading gate exists",
    "excludeFromFees(address,bool)": "fee exemptions",
    "upgradeTo(address)": "upgradeable proxy (logic can change)",
    "upgradeToAndCall(address,bytes)": "upgradeable proxy (logic can change)",
    "setImplementation(address)": "upgradeable proxy (logic can change)",
    "withdraw()": "owner withdraw",
    "rescueTokens(address,uint256)": "owner can pull tokens",
    "emergencyWithdraw()": "owner withdraw",
}
INFO = {
    "owner()": "has an owner", "renounceOwnership()": "ownership can be renounced",
    "transferOwnership(address)": "ownership transferable", "burn(uint256)": "burnable",
    "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)": "EIP-2612 permit",
}
VENUE = ["buy()", "sell(uint256)", "buy(address)", "buyTokens(address,uint256)",
         "sellTokens(address,uint256)", "swap(uint256,uint256,address,bytes)",
         "swapExactETHForTokens(uint256,address[],address,uint256)",
         "swapExactTokensForETH(uint256,uint256,address[],address,uint256)",
         "addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)",
         "addLiquidityETH(address,uint256,uint256,uint256,address,uint256)",
         "createToken(string,string)", "createPair(address,address)", "launch()",
         "migrate()", "graduate()", "getAmountOut(uint256,uint256,uint256)",
         "getReserves()", "token0()", "token1()", "factory()", "WETH()",
         "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
         "execute(bytes,bytes[],uint256)", "multicall(bytes[])", "initialize(address,address)"]
SELECTORS: dict[str, tuple[str, str]] = {}
for _s, _why in {**RISK, **INFO}.items():
    SELECTORS[selector(_s)] = (_s, _why)
for _s in VENUE:
    SELECTORS.setdefault(selector(_s), (_s, "venue/launchpad function"))


def push4s(code_hex: str) -> set[str]:
    """Selectors the dispatcher compares against (PUSH4 operands in the runtime bytecode)."""
    b = bytes.fromhex(code_hex[2:])
    out, i = set(), 0
    while i < len(b):
        op = b[i]
        if op == 0x63 and i + 5 <= len(b):
            out.add("0x" + b[i + 1: i + 5].hex())
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)
    return out


# ---- RPC -----------------------------------------------------------------------------------
class Rpc:
    def __init__(self, urls: list[str]):
        self.urls, self.i, self.calls = urls, 0, 0

    def call(self, method: str, params: list, tries: int = 3):
        last: Exception | None = None
        for k in range(tries * len(self.urls)):
            url = self.urls[(self.i + k) % len(self.urls)]
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            try:
                req = urllib.request.Request(url, body, {"content-type": "application/json"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    d = json.load(r)
                self.calls += 1
                if "error" in d:
                    raise RuntimeError(str(d["error"])[:160])
                self.i = (self.i + k) % len(self.urls)
                return d["result"]
            except Exception as e:  # noqa: BLE001 - failover across nodes
                last = e
                time.sleep(0.3)
        raise RuntimeError(f"{method} failed on all nodes: {last}")

    def maybe(self, method: str, params: list):
        try:
            return self.call(method, params, tries=1)
        except Exception:  # noqa: BLE001
            return None


def hx(v: str | None) -> int:
    return int(v, 16) if v and v != "0x" else 0


def clone_target(code_hex: str) -> str | None:
    """Implementation address of an EIP-1167 style minimal proxy (short code with PUSH20 + DELEGATECALL)."""
    b = bytes.fromhex(code_hex[2:]) if code_hex and code_hex != "0x" else b""
    if not (20 < len(b) <= 64) or 0xF4 not in b:
        return None
    i = b.find(0x73)
    return "0x" + b[i + 1: i + 21].hex() if i >= 0 and i + 21 <= len(b) else None


def pad_arg(a: str) -> str:
    return "0" * 24 + a[2:].lower()


def short(a: str) -> str:
    return a[:8] + "…" + a[-4:]


def decode_str(h: str | None) -> str | None:
    if not h or h == "0x":
        return None
    try:
        b = bytes.fromhex(h[2:])
        if len(b) == 32:
            return b.rstrip(b"\x00").decode("utf-8", "replace")
        n = int.from_bytes(b[32:64], "big")
        return b[64: 64 + n].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def ts(rpc: Rpc, cache: dict[int, int], n: int) -> str:
    if n not in cache:
        blk = rpc.maybe("eth_getBlockByNumber", [hex(n), False])
        cache[n] = hx(blk["timestamp"]) if blk else 0
    t = cache[n]
    if not t:
        return "?"
    d = datetime.fromtimestamp(t, timezone.utc)
    return d.strftime("%Y-%m-%d %H:%M:%S UTC") + (d + timedelta(hours=7)).strftime(" (%H:%M +07)")


def get_logs(rpc: Rpc, token: str, head: int) -> list[dict]:
    logs: list[dict] = []
    start, step = 0, 50000
    while start <= head:
        end = min(head, start + step - 1)
        try:
            part = rpc.call("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                             "address": token, "topics": [TRANSFER]}], tries=1)
        except Exception:  # noqa: BLE001
            if step <= 250:
                raise
            step //= 2
            continue
        logs += part
        start = end + 1
        step = min(step * 2, 200000)
    return logs


# ---- main ----------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("token")
    ap.add_argument("--rpc", action="append", required=True)
    ap.add_argument("--tx", action="append", default=[], help="tx hashes already seen (e.g. the alerts)")
    ap.add_argument("--known", help="file of 'launchpad txhash' lines to fingerprint known venues")
    ap.add_argument("--hype-usd", type=float, default=0.0)
    a = ap.parse_args(argv)
    token = a.token.lower()
    rpc = Rpc(a.rpc)
    cache: dict[int, int] = {}
    head = hx(rpc.call("eth_blockNumber", []))
    print(f"chain {hx(rpc.call('eth_chainId', []))}  head block {head}  ({ts(rpc, cache, head)})")

    # A. token basics ----------------------------------------------------------------------
    print("\n## A. Token")
    code = rpc.call("eth_getCode", [token, "latest"])
    print(f"contract code: {(len(code) - 2) // 2} bytes" + ("  <-- NOT A CONTRACT" if code == "0x" else ""))
    name = decode_str(rpc.maybe("eth_call", [{"to": token, "data": "0x06fdde03"}, "latest"]))
    sym = decode_str(rpc.maybe("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"]))
    dec = hx(rpc.maybe("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])) or 18
    supply = hx(rpc.maybe("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]))
    print(f"name={name!r} symbol={sym!r} decimals={dec} totalSupply={supply / 10**dec:,.4f}")
    own = rpc.maybe("eth_call", [{"to": token, "data": "0x8da5cb5b"}, "latest"])
    owner = ("0x" + own[-40:]) if own and len(own) >= 66 else None
    if owner:
        print(f"owner(): {owner}" + ("  (renounced)" if owner in DEAD else ""))
    impl = rpc.maybe("eth_getStorageAt", [token, EIP1967_IMPL, "latest"])
    logic_code = code
    tgt = clone_target(code)
    if impl and hx(impl):
        print(f"EIP-1967 PROXY -> implementation 0x{impl[-40:]}  (logic can be swapped by the admin)")
        logic_code = rpc.maybe("eth_getCode", ["0x" + impl[-40:], "latest"]) or code
    elif tgt:
        print(f"minimal-proxy CLONE (EIP-1167) of {tgt}: fixed logic, analysing the implementation")
        logic_code = rpc.maybe("eth_getCode", [tgt, "latest"]) or code

    # B. owner powers ----------------------------------------------------------------------
    print("\n## B. Powers present in the bytecode (dispatcher selectors)")
    sels = push4s(logic_code) if logic_code != "0x" else set()
    found = [(SELECTORS[s][0], SELECTORS[s][1]) for s in sorted(sels) if s in SELECTORS]
    risky = [f for f in found if f[0] in RISK]
    for n, why in risky:
        print(f"  RISK  {n:<38} {why}")
    for n, why in found:
        if n not in RISK:
            print(f"  info  {n:<38} {why}")
    if not risky:
        print("  no known owner-power selectors matched (does not prove safety: custom names are not matched)")
    print(f"  ({len(sels)} distinct selectors in the dispatcher; static check only, NOT a sell simulation)")

    # C. creation --------------------------------------------------------------------------
    print("\n## C. Creation")
    logs = get_logs(rpc, token, head)
    print(f"Transfer events: {len(logs)}")
    if not logs:
        print("no Transfer logs found; token never moved on this chain")
        return 0
    logs.sort(key=lambda lg: (hx(lg["blockNumber"]), hx(lg["logIndex"])))
    first = logs[0]
    fb, ftx = hx(first["blockNumber"]), first["transactionHash"]
    print(f"first event: block {fb}  {ts(rpc, cache, fb)}  tx {ftx}")
    tx = rpc.maybe("eth_getTransactionByHash", [ftx]) or {}
    rc = rpc.maybe("eth_getTransactionReceipt", [ftx]) or {}
    frm, to = tx.get("from"), tx.get("to")
    print(f"tx.from (creator / caller): {frm}")
    if to is None:
        print(f"tx.to: none -> direct contract deployment, contractAddress={rc.get('contractAddress')}")
    else:
        c = rpc.maybe("eth_getCode", [to, "latest"]) or "0x"
        print(f"tx.to (factory / launchpad contract): {to}  code={(len(c) - 2) // 2} bytes")
    emit = Counter(lg["address"].lower() for lg in rc.get("logs", []))
    if emit:
        print("contracts that emitted events in the creation tx: " +
              ", ".join(f"{short(k)} x{v}" for k, v in emit.most_common(6)))
    print(f"creator native balance now: {hx(rpc.maybe('eth_getBalance', [frm, 'latest'])) / 1e18:,.4f} HYPE" if frm else "")

    # D. supply / holders ------------------------------------------------------------------
    print("\n## D. Supply and holders (from Transfer logs)")
    bal: dict[str, int] = defaultdict(int)
    receivers: list[tuple[int, str, int, str]] = []
    seen: set[str] = set()
    minted = burned = 0
    for lg in logs:
        f, t = "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:]
        v = hx(lg["data"])
        if f == ZERO:
            minted += v
        else:
            bal[f] -= v
        if t in DEAD:
            burned += v
        else:
            bal[t] += v
        if t not in seen and t not in DEAD:
            seen.add(t)
            receivers.append((hx(lg["blockNumber"]), t, v, lg["transactionHash"]))
    ts_total = supply or minted
    print(f"minted={minted / 10**dec:,.2f}  burned/dead={burned / 10**dec:,.2f}  "
          f"holders={sum(1 for v in bal.values() if v > 0)}")
    labels: dict[str, str] = {}
    for addr, v in sorted(bal.items(), key=lambda kv: -kv[1])[:10]:
        if v <= 0:
            continue
        c = rpc.maybe("eth_getCode", [addr, "latest"]) or "0x"
        kind = f"CONTRACT {(len(c) - 2) // 2}B" if c != "0x" else "wallet"
        labels[addr] = kind
        tag = " <- creator" if addr == (frm or "").lower() else ""
        print(f"  {addr}  {v / 10**dec:>20,.2f}  {100 * v / ts_total if ts_total else 0:5.1f}%  {kind}{tag}")

    # E. first holders and the venue they used --------------------------------------------
    print("\n## E. Who got in first and which contract they paid")
    print("first receivers (block offset from creation, amount, tx):")
    for b, addr, v, h in receivers[:12]:
        print(f"  +{b - fb:>5} blk  {addr}  {v / 10**dec:>16,.2f}  {h}")
    hashes = list(dict.fromkeys(a.tx + [h for _, _, _, h in receivers[:12]] + [lg["transactionHash"] for lg in logs[-6:]]))
    venues: Counter = Counter()
    value_paid: dict[str, float] = defaultdict(float)
    for h in hashes[:30]:
        t = rpc.maybe("eth_getTransactionByHash", [h])
        if not t or not t.get("to"):
            continue
        venues[t["to"].lower()] += 1
        value_paid[t["to"].lower()] += hx(t.get("value")) / 1e18
    print("tx.to tally across these transactions (the contract users call to trade):")
    known: dict[str, str] = {}
    if a.known:
        for line in open(a.known):
            parts = line.split()
            if len(parts) == 2:
                t = rpc.maybe("eth_getTransactionByHash", [parts[1]])
                if t and t.get("to"):
                    known.setdefault(t["to"].lower(), parts[0])
    for v, n in venues.most_common(5):
        c = rpc.maybe("eth_getCode", [v, "latest"]) or "0x"
        s = push4s(c) if c != "0x" else set()
        names = sorted({SELECTORS[x][0].split("(")[0] for x in s if x in SELECTORS and x not in {selector(r) for r in RISK}})
        who = known.get(v)
        print(f"  {v}  x{n}  code={(len(c) - 2) // 2}B  native sent={value_paid[v]:,.4f} HYPE  "
              + (f"== KNOWN LAUNCHPAD: {who}" if who else "== NOT a venue we know (Kleos/BOXA/ElysPump/Agora)"))
        if names:
            print(f"      recognised functions: {', '.join(names[:12])}")

    # F. pool / liquidity ------------------------------------------------------------------
    print("\n## F. The pool (the contract that actually sends tokens to buyers) and exit liquidity")
    senders: Counter = Counter()
    for lg in logs:
        f = "0x" + lg["topics"][1][-40:]
        if f != ZERO:
            senders[f] += 1

    def ccall(to: str, sig: str, arg: str = "") -> str | None:
        r = rpc.maybe("eth_call", [{"to": to, "data": selector(sig) + arg}, "latest"])
        return r if r and r != "0x" else None

    def caddr(to: str, sig: str) -> str | None:
        r = ccall(to, sig)
        return "0x" + r[-40:] if r and len(r) >= 66 else None

    def bal_of(tok: str, holder: str) -> int:
        return hx(ccall(tok, "balanceOf(address)", pad_arg(holder)))

    pools = []
    for addr, n in senders.most_common(8):
        c = rpc.maybe("eth_getCode", [addr, "latest"]) or "0x"
        if c != "0x" and addr != (to or "").lower():
            pools.append((addr, n))
    for addr, n in pools[:3]:
        t0, t1 = caddr(addr, "token0()"), caddr(addr, "token1()")
        print(f"  pool candidate {addr}: sent tokens in {n} transfers; token0={t0} token1={t1}")
        fee, liq = ccall(addr, "fee()"), ccall(addr, "liquidity()")
        if fee:
            print(f"    fee tier {hx(fee) / 10000:g}%   in-range liquidity {hx(liq):,}" if liq else f"    fee tier {hx(fee) / 10000:g}%")
        if t0 and t1 and token in (t0.lower(), t1.lower()):
            other = t1 if t0.lower() == token else t0
            osym = decode_str(ccall(other, "symbol()")) or "?"
            odec = hx(ccall(other, "decimals()")) or 18
            ob, tb = bal_of(other, addr) / 10**odec, bal_of(token, addr) / 10**dec
            print(f"    pair: {sym} / {osym} ({other})")
            usd = f" (~${ob * a.hype_usd:,.0f})" if a.hype_usd and osym.upper() in ("WHYPE", "HYPE", "WETH") else ""
            print(f"    pool holds {ob:,.4f} {osym}{usd} and {tb:,.2f} {sym}  <-- {osym} balance is the ceiling on what sellers can take out")
            s0 = ccall(addr, "slot0()")
            if s0 and len(s0) >= 66:
                sq = hx("0x" + s0[2:66]) / 2**96
                px = sq * sq * 10 ** ((dec if t0.lower() == token else odec) - (odec if t0.lower() == token else dec))
                p_hype = px if t0.lower() == token else (1 / px if px else 0)
                line = f"    spot price ~ {p_hype:.3e} {osym} per {sym}"
                if a.hype_usd and osym.upper() in ("WHYPE", "HYPE", "WETH"):
                    line += f"  =  ${p_hype * a.hype_usd:.3e};  FDV ~ ${p_hype * a.hype_usd * ts_total / 10**dec:,.0f}"
                print(line)
        elif t0 is None:
            print(f"    not a v2/v3 pool (no token0/token1): native balance {hx(rpc.maybe('eth_getBalance', [addr, 'latest'])) / 1e18:,.4f} HYPE")
    for v, _ in venues.most_common(3):
        w9, fc = caddr(v, "WETH9()"), caddr(v, "factory()")
        own2 = caddr(v, "owner()")
        extra = ", ".join(x for x in (f"WETH9={w9}" if w9 else "", f"dex factory={fc}" if fc else "", f"owner={own2}" if own2 else "") if x)
        print(f"  router {short(v)}: {extra or 'no WETH9/factory/owner getters'}")
    for label, c in (("creation contract (tx.to)", to),):
        if c:
            o = caddr(c, "owner()")
            print(f"  {label} {short(c)}: owner={o or 'n/a'}" + ("  (same as creator)" if o and frm and o.lower() == frm.lower() else ""))

    # F2. sells and creator behaviour -----------------------------------------------------
    print("\n## F2. Observed sells and the creator's behaviour")
    pool_set = {x for x, _ in pools[:3]}
    sold: dict[str, int] = defaultdict(int)
    for lg in logs:
        f, t = "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:]
        if t in pool_set and f not in pool_set and f != ZERO:
            sold[f] += hx(lg["data"])
    cr = (frm or "").lower()
    print(f"wallets that sent tokens INTO a pool (= sold or added liquidity): {len(sold)}; "
          f"total {sum(sold.values()) / 10**dec:,.2f} {sym}")
    print("  (successful sells by many different wallets is evidence the token can be sold; it is not a guarantee for later sells)")
    got = sum(hx(lg["data"]) for lg in logs if "0x" + lg["topics"][2][-40:] == cr)
    out = sum(hx(lg["data"]) for lg in logs if "0x" + lg["topics"][1][-40:] == cr)
    print(f"creator received {got / 10**dec:,.2f}, sent out {out / 10**dec:,.2f}, holds {bal.get(cr, 0) / 10**dec:,.2f} "
          f"({100 * bal.get(cr, 0) / ts_total if ts_total else 0:.1f}% of supply)")
    if cr in sold:
        print(f"creator SOLD into a pool: {sold[cr] / 10**dec:,.2f} {sym}")

    # G. recent activity -------------------------------------------------------------------
    print("\n## G. Recent activity")
    lastb = hx(logs[-1]["blockNumber"])
    print(f"last transfer: block {lastb} {ts(rpc, cache, lastb)};  transfers in last ~3600 blocks: "
          f"{sum(1 for lg in logs if hx(lg['blockNumber']) > head - 3600)}")
    print(f"\n(rpc calls: {rpc.calls}; read-only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
