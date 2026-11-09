"""Shared RPC corroboration layer (#95 finding F-15).

One honest-looking endpoint is not evidence. Every fact that can finalize a transaction, certify a
fill, orphan an inclusion or release a (wallet, chain) lock is read from at least TWO independently
configured, operationally distinct sources and compared AT THE SAME BLOCK. The receipt worker
(`ReceiptReconciler`), the auto-reconciler (`LegReconciler`, terminal verdicts) and the owner
recovery tool (`LegRecovery`) all use this module; there is no second implementation.

What a `Session` guarantees (each point has an adversarial test):

* **Independence is a precondition.** Fewer than two sources, two sources with the same host or
  the same operator (`ExecutionRpc.operator`: an explicit label, else the registrable domain), or a
  source that is not on chain 99901 => `Unverifiable`. Operator distinctness is what the code can
  check; whether two brands share an upstream node cannot be proven here (see the docs).
* **One anchor.** Every source's head is read; the anchor is the LOWEST head. Heads must be fresh
  (<= MAX_HEAD_AGE_S on the sources' own block clock), within MAX_HEAD_SKEW blocks of each other,
  and every source must return the identical header hash at the anchor. All later facts are read
  at blocks <= the anchor. A provider that is a block or two behind therefore changes the anchor,
  never the facts: it is *not* "disagreement". A transaction reported mined above the anchor is
  `ahead` (retry later), not a conflict.
* **Presence must be unanimous at the anchor.** A transaction mined at or below the anchor must be
  seen, in the same block with the same hash and the same receipt (status, logs, gas), by EVERY
  source. One source missing it, or a different block/receipt, is a `Conflict`.
* **Fail closed.** A missing answer, a refusal, a rate limit or an exhausted call/time budget, a
  malformed response, a node without historical state, a stale head, a lagging source, a fork or any
  disagreement raises: `Unverifiable` (cannot establish) or `Conflict` (sources positively differ).
  Neither is ever turned into a verdict by guessing.
* **Bounded.** A session has a hard budget of source calls and wall time; calls are sequential;
  no source is called more than the budget allows. Cost and latency are therefore bounded per pass
  whatever the backlog or the number of providers.
* **Provenance.** `Session.provenance()` is the record attached to every ledger write: chain id,
  anchor, the sources (name, operator, head), when it was taken, the calls spent and every
  corroborated read (what, at which block, which value, agreed by whom).

What this does NOT prove: that the agreeing operators are truly independent of each other (same
upstream node, same cloud, a shared eclipse), or any cryptographic inclusion (no Merkle proof is
checked). It raises the cost of lying from "one endpoint" to "all configured operators".
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..paper import CHAIN, TradeRefused
from .approvals import Head
from .rpc import RpcBusy, RpcError

SCHEMA = 1
MIN_SOURCES = 2
MAX_HEAD_SKEW = 3
MAX_HEAD_AGE_S = 60
MAX_BISECT_SPAN = 200_000
SNAPSHOT_TTL_S = 10.0          # a pinned anchor older than this is re-read before a new unit of work
DEFAULT_CALLS = 120
DEFAULT_SECONDS = 20.0
MAX_READS_LOGGED = 64
MAX_NONCES_SCANNED = 10
HASH = re.compile(r"^0x[0-9a-f]{64}$")
ADDR = re.compile(r"^0x[0-9a-f]{40}$")

_TX_IDENT = ("hash", "from", "to", "value", "input", "nonce")
_RC_CORE = ("transactionHash", "from", "to", "status", "blockHash", "blockNumber", "gasUsed",
            "effectiveGasPrice", "contractAddress")


# ---------------------------------------------------------------------------------------- errors
class EvidenceError(TradeRefused):
    """Evidence could not be established. A `TradeRefused`, so every existing 'evidence unavailable
    right now' handler keeps working; `code` is stable and safe to log or show."""

    def __init__(self, code: str, message: str | None = None, *, source: str | None = None) -> None:
        super().__init__(message or code)
        self.code, self.source = code, source


class Unverifiable(EvidenceError):
    """Not enough healthy, fresh, in-step evidence: retry later. Nothing may be concluded."""


class Conflict(EvidenceError):
    """The sources positively disagree (or one contradicts itself). Nothing may be concluded and the
    disagreement is recorded for the operator."""


class BudgetExhausted(Unverifiable):
    """This session used its call or time budget; remaining work waits for the next pass."""

    def __init__(self, what: str = "calls") -> None:
        super().__init__("budget_exhausted", f"corroboration {what} budget exhausted")


# ---------------------------------------------------------------------------------------- values
@dataclass(frozen=True)
class Anchor:
    number: int
    hash: str
    timestamp: int


@dataclass(frozen=True)
class Live:
    name: str
    operator: str
    src: Any
    head: Head


@dataclass
class Snapshot:
    configured: int
    live: list[Live]
    failed: list[dict]
    anchor: Anchor | None
    problems: list[str]
    taken_at: float

    @property
    def ok(self) -> bool:
        return not self.problems and self.anchor is not None


@dataclass(frozen=True)
class Inclusion:
    """What every source agrees about one transaction, at the anchor."""
    status: str                    # absent | pending | ahead | not_canonical | mined
    tx: dict | None = None
    receipt: dict | None = None
    block_number: int | None = None
    block_hash: str | None = None
    confirmations: int | None = None     # anchor - block + 1 (conservative: lowest head)
    detail: str | None = None


@dataclass(frozen=True)
class Consuming:
    tx: dict
    block_number: int
    block_hash: str
    confirmations: int


@dataclass(frozen=True)
class Activity:
    nonce_at_start: int
    nonce_at_anchor: int
    txs: tuple[Consuming, ...]
    too_many: bool


def canon_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def digest_of(obj: Any) -> str:
    return hashlib.sha256(canon_json(obj).encode()).hexdigest()


def _qty(v: Any, what: str = "quantity") -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if not isinstance(v, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", v):
        raise Unverifiable("malformed_response", f"malformed {what}")
    return int(v, 16)


def _low(v: Any) -> str:
    return "" if v is None else str(v).lower()


def norm_tx(t: dict) -> dict:
    """The identity of a transaction as every honest source must report it (quantities as ints,
    addresses lower-case). Pending => blockNumber/blockHash are None."""
    if not isinstance(t, dict) or not HASH.fullmatch(_low(t.get("hash"))):
        raise Unverifiable("malformed_response", "malformed transaction")
    mined = t.get("blockNumber") is not None
    out = {
        "hash": _low(t["hash"]), "from": _low(t.get("from")), "to": _low(t.get("to")),
        "value": _qty(t.get("value", "0x0"), "value"), "input": _low(t.get("input", "0x")),
        "nonce": _qty(t.get("nonce"), "nonce"),
        "blockNumber": _qty(t["blockNumber"], "blockNumber") if mined else None,
        "blockHash": _low(t.get("blockHash")) if mined else None,
    }
    if mined and not HASH.fullmatch(str(out["blockHash"] or "")):
        raise Unverifiable("malformed_response", "malformed block hash")
    return out


def norm_receipt(r: dict) -> dict:
    if not isinstance(r, dict) or not isinstance(r.get("logs", []), list):
        raise Unverifiable("malformed_response", "malformed receipt")
    if r.get("status") not in ("0x0", "0x1"):
        raise Unverifiable("malformed_response", "malformed receipt status")
    bh = _low(r.get("blockHash"))
    if not HASH.fullmatch(bh):
        raise Unverifiable("malformed_response", "malformed receipt block hash")
    price = r.get("effectiveGasPrice")
    logs = []
    for lg in r.get("logs", []):
        if not isinstance(lg, dict) or not isinstance(lg.get("topics", []), list):
            raise Unverifiable("malformed_response", "malformed log")
        logs.append([_low(lg.get("address")), [_low(x) for x in lg["topics"]], _low(lg.get("data"))])
    return {
        "transactionHash": _low(r.get("transactionHash")), "from": _low(r.get("from")),
        "to": _low(r.get("to")), "status": r["status"], "blockHash": bh,
        "blockNumber": _qty(r.get("blockNumber"), "blockNumber"),
        "gasUsed": _qty(r.get("gasUsed"), "gasUsed"),
        "effectiveGasPrice": None if price is None else _qty(price, "gas price"),
        "contractAddress": _low(r.get("contractAddress")) or None, "logs": logs,
    }


# ---------------------------------------------------------------------------------------- service
class Corroborator:
    """Configuration + factory of sessions. Stateless between sessions, so it is safe to share
    between the worker thread, the owner command thread and the Mini App."""

    def __init__(self, sources: Sequence[Any], *, chain_id: int = CHAIN, finality: int = 12,
                 clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic,
                 max_skew: int = MAX_HEAD_SKEW, max_head_age_s: int = MAX_HEAD_AGE_S) -> None:
        if not 1 <= finality <= 256:
            raise ValueError("finality must be in 1..256")
        self.sources = [(self._name(s, i), s) for i, s in enumerate(sources)]
        self.chain_id, self.finality = chain_id, finality
        self.clock, self.mono = clock, mono
        self.max_skew, self.max_head_age_s = max_skew, max_head_age_s

    @staticmethod
    def _name(src: Any, i: int) -> str:
        return str(getattr(src, "host", None) or getattr(src, "name", None) or f"source{i + 1}")[:80]

    def session(self, *, calls: int = DEFAULT_CALLS, seconds: float = DEFAULT_SECONDS) -> Session:
        return Session(self, calls=calls, seconds=seconds)


class Session:
    """One bounded unit of corroboration (one worker pass, one owner diagnosis). Not thread-safe:
    each thread opens its own."""

    def __init__(self, owner: Corroborator, *, calls: int, seconds: float) -> None:
        self.o = owner
        self.left, self.spent = calls, 0
        self.deadline = owner.mono() + seconds
        self._snap: Snapshot | None = None
        self._reads: list[dict] = []

    # ------------------------------------------------------------------ metered source access
    def _call(self, name: str, src: Any, method: str, *args: Any, historical: bool = False) -> Any:
        if self.left <= 0:
            raise BudgetExhausted("calls")
        if self.o.mono() > self.deadline:
            raise BudgetExhausted("time")
        self.left -= 1
        self.spent += 1
        fn = getattr(src, method, None)
        if fn is None:
            raise Unverifiable("source_incapable", f"{method} not offered", source=name)
        try:
            return fn(*args)
        except EvidenceError:
            raise
        except RpcBusy:
            raise Unverifiable("rate_limited", "source is rate limited or busy", source=name) from None
        except RpcError:
            code = "historical_state_unavailable" if historical else "source_rejected"
            raise Unverifiable(code, "source refused the query", source=name) from None
        except TradeRefused:
            raise Unverifiable("source_unavailable", "source unavailable", source=name) from None
        except Exception:                                           # noqa: BLE001 - external I/O
            code = "historical_state_unavailable" if historical else "source_error"
            raise Unverifiable(code, "source misbehaved", source=name) from None

    def _each(self, snap: Snapshot, method: str, *args: Any, historical: bool = False) -> list[Any]:
        """The same read from EVERY live source (any failure fails the whole read)."""
        return [self._call(x.name, x.src, method, *args, historical=historical) for x in snap.live]

    def map_sources(self, fn: Callable[[Any], Any]) -> list[tuple[str, Any]]:
        """Run `fn(src)` against every live source of the pinned snapshot, metered. For reads
        that have no anchored form (e.g. nonce at `latest`/`pending`)."""
        snap = self.snapshot()
        out = []
        for x in snap.live:
            if self.left <= 0:
                raise BudgetExhausted("calls")
            if self.o.mono() > self.deadline:
                raise BudgetExhausted("time")
            self.left -= 1
            self.spent += 1
            try:
                out.append((x.name, fn(x.src)))
            except EvidenceError:
                raise
            except Exception:                                       # noqa: BLE001
                raise Unverifiable("source_unavailable", "source unavailable", source=x.name) from None
        return out

    # ------------------------------------------------------------------ the anchor
    def snapshot(self, *, strict: bool = False, refresh: bool = False) -> Snapshot:
        """Heads of every source and the anchor they share. `strict` raises on any problem;
        otherwise the problems are returned (owner diagnosis shows them)."""
        stale = (self._snap is not None
                 and float(self.o.clock()) - self._snap.taken_at > SNAPSHOT_TTL_S)
        if self._snap is None or refresh or stale:
            self._snap = self._take()
        snap = self._snap
        if strict and not snap.ok:
            code = snap.problems[0] if snap.problems else "source_unavailable"
            err = Conflict if code == "sources_disagree_on_chain" else Unverifiable
            raise err(code, f"no usable anchor: {code}")
        return snap

    def _take(self) -> Snapshot:
        o = self.o
        now = float(o.clock())
        snap = Snapshot(configured=len(o.sources), live=[], failed=[], anchor=None, problems=[],
                        taken_at=now)
        names = [n for n, _ in o.sources]
        ops = [str(getattr(s, "operator", n)).lower() for n, s in o.sources]
        if len(set(names)) != len(names) or len(set(ops)) != len(ops):
            snap.problems.append("sources_not_independent")
        if len(o.sources) < MIN_SOURCES:
            snap.problems.append("needs_two_independent_sources")
        for (name, src), op in zip(o.sources, ops, strict=True):
            if getattr(src, "chain_id", None) != o.chain_id:
                snap.failed.append({"name": name, "error": "wrong_chain"})
                if "wrong_chain" not in snap.problems:
                    snap.problems.append("wrong_chain")
                continue
            try:
                h = self._call(name, src, "head")
                if not isinstance(h, Head) or not HASH.fullmatch(str(h.hash)):
                    raise Unverifiable("malformed_response", "malformed head", source=name)
                if getattr(src, "chain_verified", False) is not True:
                    raise Unverifiable("chain_unverified", "chain id not verified", source=name)
            except BudgetExhausted:
                raise                                   # a spent budget is not a provider outage
            except EvidenceError as exc:
                snap.failed.append({"name": name, "error": exc.code})
                continue
            snap.live.append(Live(name, op, src, h))
        if any(f["error"] == "chain_unverified" for f in snap.failed):
            snap.problems.append("chain_unverified")
        if any(f["error"] not in ("wrong_chain", "chain_unverified") for f in snap.failed):
            snap.problems.append("source_unavailable")
        if not snap.live:
            return snap
        lo = min(snap.live, key=lambda x: x.head.number)
        snap.anchor = Anchor(lo.head.number, lo.head.hash, lo.head.timestamp)
        if max(x.head.number for x in snap.live) - lo.head.number > o.max_skew:
            snap.problems.append("heads_diverge")
        if any(not -5 <= now - x.head.timestamp <= o.max_head_age_s for x in snap.live):
            snap.problems.append("stale_head")
        if not {"stale_head", "heads_diverge"} & set(snap.problems):
            try:
                hashes = {self._header_hash(x, snap.anchor.number) for x in snap.live}
            except BudgetExhausted:
                raise
            except EvidenceError:
                snap.problems.append("source_unavailable")
            else:
                if len(hashes) != 1 or None in hashes:
                    snap.problems.append("sources_disagree_on_chain")
        return snap

    def _header_hash(self, x: Live, number: int) -> str | None:
        h = self._call(x.name, x.src, "block_header", number)
        if h is None:
            return None
        got = _low(h.get("hash")) if isinstance(h, dict) else ""
        if not HASH.fullmatch(got):
            raise Unverifiable("malformed_response", "malformed header", source=x.name)
        return got

    def anchor(self) -> Anchor:
        a = self.snapshot(strict=True).anchor
        assert a is not None
        return a

    def pin(self) -> Snapshot:
        """Start a unit of work: a strict, fresh-enough snapshot. Everything the unit reads is
        then compared at this one anchor."""
        return self.snapshot(strict=True)

    # ------------------------------------------------------------------ reads at the anchor
    def _at_or_below_anchor(self, snap: Snapshot, block: int) -> None:
        assert snap.anchor is not None
        if block > snap.anchor.number:
            raise Unverifiable("block_beyond_anchor", "block is above the shared anchor")

    def _note(self, what: str, args: list, block: int | None, value: Any, snap: Snapshot) -> None:
        if len(self._reads) < MAX_READS_LOGGED:
            self._reads.append({"read": what, "args": args, "block": block, "value": value,
                                "agreed_by": [x.name for x in snap.live]})

    def header_hash(self, number: int) -> str:
        """The canonical block hash at `number` (<= anchor), identical on every source."""
        snap = self.snapshot(strict=True)
        self._at_or_below_anchor(snap, number)
        got = {self._header_hash(x, number) for x in snap.live}
        if None in got:
            raise Unverifiable("header_missing", "a source has no header at that height")
        if len(got) != 1:
            raise Conflict("sources_disagree_on_block", "sources disagree on the block hash")
        (h,) = got
        assert h is not None
        return h

    def nonce_at(self, address: str, block: int) -> int:
        snap = self.snapshot(strict=True)
        self._at_or_below_anchor(snap, block)
        vals = self._each(snap, "tx_count", address, hex(block))
        if len(set(vals)) != 1:
            raise Conflict("sources_disagree_on_wallet_nonce", "sources disagree on the wallet nonce")
        self._note("nonce", [address], block, vals[0], snap)
        return int(vals[0])

    def queued(self, address: str) -> bool:
        """True if ANY source sees a pending transaction beyond `latest` for the wallet (the
        conservative reading: one queued tx is enough to keep the wallet blocked)."""
        snap = self.snapshot(strict=True)
        flags = []
        for x in snap.live:
            a = self._call(x.name, x.src, "tx_count", address, "latest")
            b = self._call(x.name, x.src, "tx_count", address, "pending")
            flags.append(b > a)
        return any(flags)

    def native_balance(self, address: str, block: int) -> int:
        snap = self.snapshot(strict=True)
        self._at_or_below_anchor(snap, block)
        vals = self._each(snap, "native_balance", address, block, historical=True)
        if len(set(vals)) != 1:
            raise Conflict("sources_disagree_on_balance", "sources disagree on a historical balance")
        self._note("native_balance", [address], block, str(vals[0]), snap)
        return int(vals[0])

    def token_balance(self, token: str, address: str, block: int) -> int:
        snap = self.snapshot(strict=True)
        self._at_or_below_anchor(snap, block)
        vals = self._each(snap, "token_balance", token, address, block, historical=True)
        if len(set(vals)) != 1:
            raise Conflict("sources_disagree_on_balance", "sources disagree on a historical balance")
        self._note("token_balance", [token, address], block, str(vals[0]), snap)
        return int(vals[0])

    def block_transactions(self, number: int) -> list[dict]:
        snap = self.snapshot(strict=True)
        self._at_or_below_anchor(snap, number)
        lists = self._each(snap, "block_transactions", number)
        keys = []
        for lst in lists:
            if not isinstance(lst, list):
                raise Unverifiable("malformed_response", "malformed block")
            keys.append(sorted((_low(t.get("hash")), _low(t.get("from")), _low(t.get("to")),
                                str(t.get("nonce"))) for t in lst if isinstance(t, dict)))
        if any(k != keys[0] for k in keys):
            raise Conflict("sources_disagree_on_block_contents",
                           "sources disagree on a block's transactions")
        self._note("block_transactions", [], number, digest_of(keys[0]), snap)
        return [t for t in lists[0] if isinstance(t, dict)]

    # ------------------------------------------------------------------ transaction identity
    def inclusion(self, tx_hash: str) -> Inclusion:
        """The one answer every source agrees on for `tx_hash`, compared at the anchor."""
        snap = self.snapshot(strict=True)
        assert snap.anchor is not None
        a = snap.anchor.number
        views: list[tuple[Live, dict, dict | None]] = []
        raw: dict[str, tuple[dict | None, dict | None]] = {}     # what each source literally said
        for x in snap.live:
            tx = self._call(x.name, x.src, "transaction", tx_hash)
            rc = self._call(x.name, x.src, "receipt", tx_hash)
            if rc is not None and tx is None:
                raise Conflict("source_contradicts_itself", "receipt without a transaction",
                               source=x.name)
            raw[x.name] = (tx, rc)
            ntx = None if tx is None else norm_tx(tx)
            if ntx is not None and ntx["hash"] != tx_hash:
                raise Conflict("sources_disagree_on_transaction", "wrong transaction returned",
                               source=x.name)
            nrc = None if rc is None else norm_receipt(rc)
            if nrc is not None:
                if ntx is None or ntx["blockNumber"] is None:
                    raise Conflict("source_contradicts_itself", "receipt for an unmined tx",
                                   source=x.name)
                if (nrc["blockHash"], nrc["blockNumber"]) != (ntx["blockHash"], ntx["blockNumber"]):
                    raise Conflict("source_contradicts_itself", "receipt and tx differ on block",
                                   source=x.name)
                if nrc["blockNumber"] > x.head.number:
                    raise Conflict("source_contradicts_itself", "inclusion above its own head",
                                   source=x.name)
            views.append((x, ntx, nrc))  # type: ignore[arg-type]
        present = [v[1] for v in views if v[1] is not None]
        if len({canon_json({k: t[k] for k in _TX_IDENT}) for t in present}) > 1:
            raise Conflict("sources_disagree_on_transaction",
                           "sources report different transactions under one hash")
        mined = [v for v in views if v[2] is not None]
        if not mined:
            if all(v[1] is None for v in views):
                return Inclusion("absent")
            first = next(v for v in views if v[1] is not None)
            return Inclusion("pending", tx=raw[first[0].name][0])
        blocks = {(v[2]["blockNumber"], v[2]["blockHash"]) for v in mined if v[2] is not None}
        if len(blocks) != 1:
            raise Conflict("sources_disagree_on_receipt", "sources report different inclusions")
        (number, bhash), = blocks
        if number > a:
            return Inclusion("ahead", tx=raw[mined[0][0].name][0], block_number=number,
                             block_hash=bhash,
                             detail="mined above the shared anchor; retry when the anchor advances")
        if len(mined) != len(views):
            raise Conflict("sources_disagree_on_transaction",
                           "a source that must have the block does not report the transaction")
        if len({canon_json(v[1]) for v in mined}) != 1:
            raise Conflict("sources_disagree_on_transaction", "sources disagree on the transaction")
        if len({canon_json(v[2]) for v in mined}) != 1:
            raise Conflict("sources_disagree_on_receipt", "sources disagree on the receipt")
        hdr = {self._header_hash(x, number) for x in snap.live}
        if None in hdr:
            raise Unverifiable("header_missing", "a source has no header for the inclusion block")
        if len(hdr) != 1:
            raise Conflict("sources_disagree_on_block", "sources disagree on the block hash")
        canonical = next(iter(hdr)) == bhash
        first_tx, first_rc = raw[mined[0][0].name]
        conf = a - number + 1
        self._note("inclusion", [tx_hash], number, digest_of(mined[0][2]), snap)
        return Inclusion("mined" if canonical else "not_canonical", tx=first_tx, receipt=first_rc,
                         block_number=number, block_hash=bhash, confirmations=conf)

    def consuming_tx(self, wallet: str, nonce: int, lo: int | None) -> Consuming:
        """The transaction of `wallet` that consumed `nonce`, found on EVERY source by bisecting
        the wallet's nonce by block (~log2(span) reads each) and required to be identical."""
        snap = self.snapshot(strict=True)
        assert snap.anchor is not None
        a = snap.anchor.number
        lo = max(0, a - MAX_BISECT_SPAN) if lo is None else max(lo, a - MAX_BISECT_SPAN)
        found = []
        for x in snap.live:
            found.append(self._locate_one(x, wallet, nonce, lo, a))
        if len({(str(t["hash"]).lower(), b, bh) for t, b, bh in found}) != 1:
            raise Conflict("sources_disagree_on_consuming_transaction",
                           "sources disagree on which transaction used the nonce")
        tx, number, bh = found[0]
        self._note("consuming_tx", [wallet, nonce], number, _low(tx["hash"]), snap)
        return Consuming(tx, number, bh, a - number + 1)

    def wallet_activity(self, wallet: str, lo: int, *,
                        max_nonces: int = MAX_NONCES_SCANNED) -> Activity:
        """Every transaction `wallet` sent between block `lo` and the anchor, each found on
        EVERY source (nonce at `lo` and at the anchor must agree). More than `max_nonces` is
        reported, not scanned (bounded cost)."""
        a = self.anchor().number
        c0 = self.nonce_at(wallet, lo)
        cn = self.nonce_at(wallet, a)
        if cn < c0:
            raise Conflict("wallet_nonce_went_backwards", "wallet nonce decreased since the prompt")
        if cn - c0 > max_nonces:
            return Activity(c0, cn, (), True)
        return Activity(c0, cn, tuple(self.consuming_tx(wallet, k, lo) for k in range(c0, cn)),
                        False)

    def _locate_one(self, x: Live, wallet: str, n: int, lo: int, hi: int) -> tuple[dict, int, str]:
        def cnt(b: int) -> int:
            return self._call(x.name, x.src, "tx_count", wallet, hex(b))
        if cnt(lo) > n:
            raise Conflict("nonce_consumed_before_the_request", "nonce used before the prompt")
        if cnt(hi) <= n:
            raise Conflict("nonce_not_consumed", "nonce is not consumed at the anchor")
        left, right = lo, hi
        while right - left > 1:
            mid = (left + right) // 2
            if cnt(mid) > n:
                right = mid
            else:
                left = mid
        hdr = self._call(x.name, x.src, "block_header", right)
        if hdr is None:
            raise Unverifiable("header_missing", "no header for the consuming block", source=x.name)
        bh = _low(hdr.get("hash"))
        for tx in self._call(x.name, x.src, "block_transactions", right):
            try:
                same = (_low(tx.get("from")) == wallet and _qty(tx.get("nonce")) == n
                        and HASH.fullmatch(_low(tx.get("hash"))))
            except (EvidenceError, AttributeError):
                continue
            if same:
                if _low(tx.get("blockHash", bh)) != bh:
                    raise Conflict("block_hash_mismatch", "transaction and header disagree",
                                   source=x.name)
                return tx, right, bh
        raise Conflict("consuming_transaction_not_in_block", "nonce consumed but tx not in block",
                       source=x.name)

    # ------------------------------------------------------------------ provenance
    corroborated = True        # marker: reads on this object are agreed by >= 2 operators

    def mark(self) -> tuple[int, int]:
        """A bookmark so one unit of work (one leg) records only ITS reads and call cost."""
        return len(self._reads), self.spent

    def provenance(self, *, since: tuple[int, int] | None = None,
                   strict: bool = True) -> dict[str, Any]:
        """The record attached to every ledger write that rests on corroborated chain facts.
        `strict=False` is for diagnostics and conflict records: it never raises, states the
        problems, and sets `source_agreement` False when the sources were not in step (the
        ledger refuses such a record for any chain-fact write)."""
        snap = self.snapshot(strict=strict)
        r0, c0 = since if since is not None else (0, 0)
        anchor = snap.anchor
        return {
            "schema": SCHEMA, "chain_id": self.o.chain_id, "taken_at": snap.taken_at,
            "max_head_age_s": self.o.max_head_age_s, "finality": self.o.finality,
            "anchor": None if anchor is None else {"number": anchor.number, "hash": anchor.hash,
                                                    "timestamp": anchor.timestamp},
            "sources": [{"name": x.name, "operator": x.operator, "head_number": x.head.number,
                         "head_hash": x.head.hash, "head_timestamp": x.head.timestamp}
                        for x in snap.live],
            "failed": list(snap.failed), "problems": list(snap.problems),
            "calls": self.spent - c0, "reads": list(self._reads[r0:]),
            "source_agreement": bool(snap.ok),
        }
