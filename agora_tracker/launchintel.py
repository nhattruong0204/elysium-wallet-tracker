"""Launch intelligence: independent-wallet scoring, due diligence and new-venue detection.

Alerts only. Nothing in here signs or sends a transaction.

* bundles    wallets that keep buying the same token in the same block as other tracked wallets
             are one operator splitting a buy. They count once when we score a cluster.
* DueDiligence  a bounded, cached, read-only look at a token through JSON-RPC: pool / bonding
             curve and its HYPE, mint/pause/blacklist powers, owner share, who sold into it.
             Every call is optional: on any failure the field is simply missing and the alert is
             still delivered.
* LaunchWatcher  new DEX pools from the chain's own PoolCreated events, for venues Agora does
             not index.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

ZERO = "0x" + "0" * 40
DEAD = {ZERO, "0x" + "0" * 39 + "1", "0x000000000000000000000000000000000000dead"}
WEI = 10**18

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


TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_POOL_CREATED = "0x" + keccak256(b"PoolCreated(address,address,uint24,int24,address)").hex()
FEE_TIERS = (100, 500, 2500, 3000, 10000)

# function -> why it matters to a buyer (kept in step with scripts/trace_token.py)
RISK = {
    "mint(address,uint256)": "owner can mint more supply",
    "mint(uint256)": "mint function",
    "pause()": "trading can be paused",
    "blacklist(address)": "blacklist (sell-block risk)",
    "addBlacklist(address)": "blacklist (sell-block risk)",
    "setBlacklist(address,bool)": "blacklist (sell-block risk)",
    "blacklistAddress(address,bool)": "blacklist (sell-block risk)",
    "setBots(address[])": "bot list (can block sells)",
    "setTax(uint256)": "tax can be changed",
    "setFee(uint256)": "fee can be changed",
    "setFees(uint256,uint256)": "fee can be changed",
    "setSellFee(uint256)": "sell fee can be changed",
    "setMaxTxAmount(uint256)": "max-tx limit can change",
    "setMaxWallet(uint256)": "max-wallet limit can change",
    "setTradingEnabled(bool)": "trading can be switched off",
    "upgradeTo(address)": "upgradeable proxy",
    "upgradeToAndCall(address,bytes)": "upgradeable proxy",
    "rescueTokens(address,uint256)": "owner can pull tokens",
}
RISK_BY_SELECTOR: dict[str, str] = {}
for _sig, _why in RISK.items():
    RISK_BY_SELECTOR.setdefault(selector(_sig), _why)


def push4s(code_hex: str) -> set[str]:
    """Selectors the dispatcher compares against (PUSH4 operands in the runtime bytecode)."""
    try:
        b = bytes.fromhex(code_hex[2:])
    except ValueError:
        return set()
    out, i = set(), 0
    while i < len(b):
        op = b[i]
        if op == 0x63 and i + 5 <= len(b):
            out.add("0x" + b[i + 1: i + 5].hex())
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)
    return out


def clone_target(code_hex: str) -> str | None:
    """Implementation of an EIP-1167 minimal proxy (short code with PUSH20 + DELEGATECALL)."""
    try:
        b = bytes.fromhex(code_hex[2:]) if code_hex and code_hex != "0x" else b""
    except ValueError:
        return None
    if not (20 < len(b) <= 64) or 0xF4 not in b:
        return None
    i = b.find(0x73)
    return "0x" + b[i + 1: i + 21].hex() if i >= 0 and i + 21 <= len(b) else None


def _hx(v: Any) -> int:
    return int(v, 16) if isinstance(v, str) and v not in ("", "0x") else 0


def _addr(v: Any) -> str | None:
    if isinstance(v, str) and len(v) >= 66:
        a = "0x" + v[-40:].lower()
        return None if a == ZERO else a
    return None


def _pad(a: str) -> str:
    return "0" * 24 + a[2:].lower()


# ---- bundles and independence ------------------------------------------------------------
def find_bundle_wallets(rows: Iterable[tuple[int, str, str]], *, min_wallets: int = 3,
                        min_hits: int = 5) -> set[str]:
    """rows = (block_number, token, wallet) for measured buys.

    A block in which >= min_wallets distinct tracked wallets bought the same token is a
    bundled buy; a wallet that takes part in >= min_hits such blocks is a bundle wallet."""
    per_block: dict[tuple[int, str], set[str]] = defaultdict(set)
    for block, token, wallet in rows:
        per_block[(block, token)].add(wallet)
    hits: Counter[str] = Counter()
    for wallets in per_block.values():
        if len(wallets) >= min_wallets:
            hits.update(wallets)
    return {w for w, n in hits.items() if n >= min_hits}


def count_independent(buyers: list[tuple[str, int]], bundles: set[str]) -> int:
    """buyers = (wallet, block). Known bundle wallets are one operator; other wallets that
    bought in the same block as each other are one operator too; the rest count one each."""
    per_block: Counter[int] = Counter(b for _, b in buyers)
    groups: set[str] = set()
    for wallet, block in buyers:
        if wallet in bundles:
            groups.add("bundle")
        elif per_block[block] > 1:
            groups.add(f"blk:{block}")
        else:
            groups.add(wallet)
    return len(groups)


def tier_label(independent: int, raw: int, window_s: int, minimum: int = 3) -> str:
    mins = max(1, round(window_s / 60))
    if independent >= minimum:
        return f"{independent} independent in {mins} min"
    if independent <= 1 and raw >= 2:
        return "bundle entry (one operator)"
    return f"{independent} independent, {raw} addresses"


# ---- due diligence -----------------------------------------------------------------------
@dataclass
class DD:
    token: str
    decimals: int | None = None
    supply: int | None = None
    owner: str | None = None
    owner_pct: float | None = None
    risks: list[str] = field(default_factory=list)
    pool: str | None = None
    pool_kind: str | None = None        # "pool" | "curve"
    liquidity_hype: float | None = None
    router: str | None = None
    sellers: int | None = None          # distinct wallets that sent tokens into the pool/curve
    sold_pct: float | None = None       # of supply
    top_holder: str | None = None
    top_pct: float | None = None
    top_is_owner: bool = False
    complete: bool = False              # balances rebuilt from the mint, so shares are exact
    partial: bool = False               # some probes failed or ran out of time


class DueDiligence:
    """Read-only token inspection through `rpc.call(method, params)` (an RpcClient).

    Budgeted and cached: a probe never takes longer than `budget_s` of RPC time, results are
    reused for `cache_s`, and after an RPC failure probing pauses for `cooldown_s` so a sick
    node cannot slow alert delivery."""

    def __init__(self, rpc: Any, *, wrapped: str | None, factories: list[str] | None = None,
                 budget_s: float = 6.0, cache_s: float = 300.0, log_blocks: int = 3600,
                 max_logs: int = 4000, cooldown_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.rpc, self.wrapped = rpc, (wrapped or "").lower() or None
        self.factories = [f.lower() for f in factories or []]
        self.budget_s, self.cache_s, self.log_blocks = budget_s, cache_s, log_blocks
        self.max_logs, self.cooldown_s, self.clock = max_logs, cooldown_s, clock
        self._cache: dict[str, tuple[float, DD]] = {}
        self._down_until = 0.0

    def add_factories(self, factories: Iterable[str]) -> None:
        for f in factories:
            if f and f.lower() not in self.factories:
                self.factories.append(f.lower())

    # -- plumbing
    class _Out(Exception):
        pass

    def _call(self, deadline: float, method: str, params: list) -> Any:
        if self.clock() > deadline:
            raise self._Out()
        try:
            return self.rpc.call(method, params)
        except Exception:
            self._down_until = self.clock() + self.cooldown_s
            raise

    def _eth_call(self, deadline: float, to: str, sig: str, arg: str = "") -> str | None:
        r = self._call(deadline, "eth_call", [{"to": to, "data": selector(sig) + arg}, "latest"])
        return r if isinstance(r, str) and r not in ("0x", "") else None

    def _try(self, dd: DD, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - every field is optional
            dd.partial = True
            log.debug("dd probe step failed: %s", e)
            return None

    # -- public
    def probe(self, token: str, tx: str | None = None) -> DD | None:
        token = token.lower()
        now = self.clock()
        hit = self._cache.get(token)
        if hit and now - hit[0] < self.cache_s:
            return hit[1]
        if now < self._down_until:
            return None
        dd = DD(token)
        deadline = now + self.budget_s
        self._try(dd, lambda: self._code(dd, deadline))
        self._try(dd, lambda: self._basics(dd, deadline))
        self._try(dd, lambda: self._venue(dd, deadline, tx))
        self._try(dd, lambda: self._flows(dd, deadline))
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[token] = (self.clock(), dd)
        return dd

    # -- steps
    def _code(self, dd: DD, deadline: float) -> None:
        code = self._call(deadline, "eth_getCode", [dd.token, "latest"]) or "0x"
        sels = push4s(code)
        impl = clone_target(code)
        if impl:
            sels |= push4s(self._call(deadline, "eth_getCode", [impl, "latest"]) or "0x")
        dd.risks = sorted({RISK_BY_SELECTOR[s] for s in sels if s in RISK_BY_SELECTOR})

    def _basics(self, dd: DD, deadline: float) -> None:
        r = self._eth_call(deadline, dd.token, "decimals()")
        dd.decimals = _hx(r) if r else 18
        r = self._eth_call(deadline, dd.token, "totalSupply()")
        dd.supply = _hx(r) if r else None
        try:
            dd.owner = _addr(self._eth_call(deadline, dd.token, "owner()"))
        except self._Out:
            raise
        except Exception:  # noqa: BLE001 - tokens without owner() revert
            dd.owner = None
        if dd.owner and dd.supply:
            r = self._eth_call(deadline, dd.token, "balanceOf(address)", _pad(dd.owner))
            dd.owner_pct = 100 * _hx(r) / dd.supply

    def _venue(self, dd: DD, deadline: float, tx: str | None) -> None:
        to = None
        if tx:
            t = self._call(deadline, "eth_getTransactionByHash", [tx])
            to = (t or {}).get("to")
            to = to.lower() if isinstance(to, str) else None
        best: tuple[float, str] | None = None
        if self.wrapped:
            for factory in self.factories:
                for fee in FEE_TIERS:
                    arg = _pad(dd.token) + _pad(self.wrapped) + f"{fee:064x}"
                    pool = _addr(self._eth_call(deadline, factory,
                                                "getPool(address,address,uint24)", arg))
                    if not pool:
                        continue
                    r = self._eth_call(deadline, self.wrapped, "balanceOf(address)", _pad(pool))
                    liq = _hx(r) / WEI if r else 0.0
                    if best is None or liq > best[0]:
                        best = (liq, pool)
        if best:
            dd.pool, dd.pool_kind, dd.liquidity_hype = best[1], "pool", best[0]
        elif to:
            # no DEX pool: a bonding curve holds the token and the HYPE itself
            bal = self._eth_call(deadline, dd.token, "balanceOf(address)", _pad(to))
            if bal and _hx(bal) > 0:
                dd.pool, dd.pool_kind = to, "curve"
                dd.liquidity_hype = _hx(self._call(deadline, "eth_getBalance",
                                                   [to, "latest"])) / WEI
        if to and to != dd.pool:
            dd.router = to

    def _flows(self, dd: DD, deadline: float) -> None:
        """Rebuild balances from recent Transfer logs: who sold into the pool, who holds."""
        if not dd.pool or not dd.decimals:
            return
        head = _hx(self._call(deadline, "eth_blockNumber", []))
        frm = max(0, head - self.log_blocks)
        logs = self._call(deadline, "eth_getLogs", [{
            "fromBlock": hex(frm), "toBlock": hex(head), "address": dd.token,
            "topics": [TOPIC_TRANSFER]}]) or []
        if len(logs) > self.max_logs:
            dd.partial = True
            return
        bal: dict[str, int] = defaultdict(int)
        sellers: set[str] = set()
        sold = 0
        minted = False
        for lg in logs:
            tp = lg.get("topics") or []
            if len(tp) < 3:
                continue
            f, t, v = "0x" + tp[1][-40:], "0x" + tp[2][-40:], _hx(lg.get("data"))
            if f == ZERO:
                minted = True
            else:
                bal[f] -= v
            if t not in DEAD:
                bal[t] += v
            if t == dd.pool and f not in (dd.pool, ZERO):
                sellers.add(f)
                sold += v
        dd.sellers = len(sellers)
        if dd.supply:
            dd.sold_pct = 100 * sold / dd.supply
            dd.complete = minted
            holders = [(a, v) for a, v in bal.items() if v > 0 and a != dd.pool and a not in DEAD]
            if holders and minted:
                a, v = max(holders, key=lambda kv: kv[1])
                dd.top_holder, dd.top_pct = a, 100 * v / dd.supply
                dd.top_is_owner = a == dd.owner


# ---- new pools from the chain ------------------------------------------------------------
def decode_pool_created(lg: dict) -> dict | None:
    tp = lg.get("topics") or []
    data = lg.get("data") or ""
    if len(tp) < 4 or len(data) < 130:
        return None
    pool = "0x" + data[-40:].lower()
    return {"token0": "0x" + tp[1][-40:].lower(), "token1": "0x" + tp[2][-40:].lower(),
            "fee": _hx(tp[3]), "pool": pool, "factory": str(lg.get("address", "")).lower(),
            "block": _hx(lg.get("blockNumber"))}


class LaunchWatcher:
    """Reports DEX pools for brand-new tokens, including venues Agora does not index.

    1. scan PoolCreated events since the stored cursor (never backfills on first start),
    2. remember the pair (WHYPE side, other token) in launch_venues,
    3. after `delay_s`, alert once for pools holding at least `min_liquidity_hype` of HYPE."""

    CURSOR = "launch_watch_cursor"

    def __init__(self, store: Any, rpc: Any, *, wrapped: str, factories: list[str],
                 min_liquidity_hype: float, delay_s: int, max_age_s: int = 6 * 3600,
                 max_blocks: int = 500, new_token_max_age_s: int = 86400,
                 now: Callable[[], float] = time.time):
        self.store, self.rpc, self.wrapped = store, rpc, wrapped.lower()
        self.factories = [f.lower() for f in factories]
        self.min_liq, self.delay_s, self.max_age_s = min_liquidity_hype, delay_s, max_age_s
        self.max_blocks, self.new_token_max_age_s, self.now = max_blocks, new_token_max_age_s, now

    def scan(self) -> int:
        head = _hx(self.rpc.call("eth_blockNumber", []))
        cur = self.store.get_kv(self.CURSOR)
        if cur is None:
            self.store.set_kv(self.CURSOR, str(head))
            return 0
        frm = int(cur) + 1
        if frm > head:
            return 0
        to = min(head, frm + self.max_blocks - 1)
        flt: dict[str, Any] = {"fromBlock": hex(frm), "toBlock": hex(to),
                               "topics": [TOPIC_POOL_CREATED]}
        if self.factories:
            flt["address"] = self.factories
        n = 0
        for lg in self.rpc.call("eth_getLogs", [flt]) or []:
            ev = decode_pool_created(lg)
            if not ev or self.wrapped not in (ev["token0"], ev["token1"]):
                continue
            token = ev["token1"] if ev["token0"] == self.wrapped else ev["token0"]
            if token == self.wrapped:
                continue
            ev["token"] = token
            ev["seen_ts"] = int(self.now())
            if self.store.launch_venue_add(f"pool:{ev['pool']}", "pool", ev):
                n += 1
        self.store.set_kv(self.CURSOR, str(to))   # only after the events are stored
        return n

    def evaluate(self) -> int:
        """Alert for pending pools that are old enough and funded. Returns alerts queued."""
        sent = 0
        for row in self.store.launch_pending_pools():
            d = row["details"]
            age = self.now() - d.get("seen_ts", self.now())
            if age < self.delay_s:
                continue
            if age > self.max_age_s:
                self.store.launch_venue_done(row["key"], {"skipped": "expired"})
                continue
            try:
                r = self.rpc.call("eth_call", [{"to": self.wrapped,
                                                "data": selector("balanceOf(address)")
                                                + _pad(d["pool"])}, "latest"])
            except Exception:  # noqa: BLE001 - try again next tick
                log.warning("launch watch: liquidity read failed for %s", d["pool"])
                continue
            liq = _hx(r) / WEI
            if liq < self.min_liq:
                continue                        # may be funded later, retried until expiry
            prof = self.store.token_profile_age_s(d["token"])
            if prof is not None and prof > self.new_token_max_age_s:
                self.store.launch_venue_done(row["key"], {"skipped": "old_token"})
                continue
            payload = {"type": "launch", "kind": "new_pool", "token": d["token"],
                       "symbol": self._symbol(d["token"]), "pool": d["pool"],
                       "factory": d.get("factory"), "fee": d.get("fee"),
                       "liquidity_hype": liq, "age_s": int(age)}
            self.store.launch_venue_done(row["key"], {"liquidity_hype": liq}, payload=payload)
            sent += 1
        return sent

    def _symbol(self, token: str) -> str | None:
        try:
            r = self.rpc.call("eth_call", [{"to": token, "data": selector("symbol()")}, "latest"])
            if not r or r == "0x":
                return None
            b = bytes.fromhex(r[2:])
            if len(b) == 32:
                return b.rstrip(b"\x00").decode("utf-8", "replace")[:24] or None
            n = int.from_bytes(b[32:64], "big")
            return b[64: 64 + n].decode("utf-8", "replace")[:24] or None
        except Exception:  # noqa: BLE001
            return None

    def tick(self) -> None:
        self.scan()
        self.evaluate()


# ---- paper trading report ----------------------------------------------------------------
HORIZONS_H = (1, 6, 24, 48)


def paper_outcome(tr: dict, now: float, slippage_pct: float = 10.0) -> dict:
    """Outcome of one paper trade. `tr` = Store.paper_trades row (entry, opened_ts, snapshots).

    The assumed fill is the signal buy's executed price plus slippage_pct. Prices after entry
    come from Agora market snapshots (a sample every ~10 minutes), so a peak can be missed
    or slightly stale; horizon values only count once the horizon has fully elapsed."""
    entry = tr.get("entry")
    out: dict[str, Any] = {"token": tr["token"], "opened_ts": tr["opened_ts"],
                           "n_independent": tr["n_independent"], "priced": False}
    snaps = [(ts, px) for ts, px in tr.get("snapshots", []) if px and px > 0]
    if not entry or entry <= 0 or not snaps:
        return out
    fill = entry * (1 + slippage_pct / 100)
    mult = [(ts, px / fill) for ts, px in snaps]
    out.update(priced=True, peak=max(m for _, m in mult), last=mult[-1][1])
    for h in HORIZONS_H:
        cutoff = tr["opened_ts"] + h * 3600
        if now >= cutoff:
            upto = [m for ts, m in mult if ts <= cutoff]
            out[f"h{h}"] = upto[-1] if upto else None
    return out


def summarize_paper(trades: list[dict], now: float, slippage_pct: float = 10.0) -> dict:
    res = [paper_outcome(t_, now, slippage_pct) for t_ in trades]
    priced = [r for r in res if r["priced"]]

    def med(xs: list[float]) -> float | None:
        xs = sorted(xs)
        return None if not xs else (xs[len(xs) // 2] if len(xs) % 2
                                    else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2)

    s: dict[str, Any] = {"signals": len(res), "priced": len(priced), "slippage_pct": slippage_pct}
    if priced:
        peaks = [r["peak"] for r in priced]
        s |= {"median_peak": med(peaks),
              "pct_ge_2x": 100 * sum(p >= 2 for p in peaks) / len(peaks),
              "pct_ge_10x": 100 * sum(p >= 10 for p in peaks) / len(peaks),
              "pct_below_entry_now": 100 * sum(r["last"] < 1 for r in priced) / len(priced)}
    for h in HORIZONS_H:
        xs = [r[f"h{h}"] for r in priced if r.get(f"h{h}") is not None]
        s[f"h{h}"] = {"n": len(xs), "median": med(xs),
                      "pct_up": (100 * sum(x > 1 for x in xs) / len(xs)) if xs else None}
    s["rows"] = res
    return s


def format_paper_report(s: dict) -> str:
    def x(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.2f}x"

    lines = [f"paper trades: {s['signals']} signals, {s['priced']} with market prices "
             f"(assumed fill = signal buy + {s['slippage_pct']:g}% slippage)"]
    if s["priced"]:
        lines.append(f"peak after entry: median {x(s['median_peak'])}, "
                     f">=2x {s['pct_ge_2x']:.0f}%, >=10x {s['pct_ge_10x']:.0f}%, "
                     f"below entry now {s['pct_below_entry_now']:.0f}%")
    for h in HORIZONS_H:
        d = s[f"h{h}"]
        if d["n"]:
            lines.append(f"  +{h}h: n={d['n']} median {x(d['median'])}, up {d['pct_up']:.0f}%")
    if s["priced"] < 20:
        lines.append("sample is small (<20 priced signals): treat as anecdote, not evidence")
    return "\n".join(lines)
