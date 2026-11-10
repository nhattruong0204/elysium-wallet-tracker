"""Receipt reconciliation (issue #94): turn mined transactions into truthful ledger facts.

Per leg that has a transaction hash and is not final yet, each pass:

  1. fetches receipt + transaction (+ the canonical header at the receipt's height);
  2. validates them against the leg (hash, sender, target, value, calldata, status, block hash);
     a pair that does not match the request is NEVER accounted: it is left for the #93 reconciler;
  3. records the inclusion (`included` / `reverted`) with its fill, provisional until the block is
     `finality_confirmations` deep, then final;
  4. detects reorgs: a recorded block that is no longer canonical (or a receipt that vanished)
     rolls the provisional fill back and marks the leg/order `orphaned`; a receipt in a different
     block is a fresh inclusion (new fill row, the old one rolled back).

Rules that keep it honest:
  * the receipt worker does not consult the pause / execution flag: pausing stops NEW prompts, but
    what already happened on chain must still be recorded;
  * an approval leg changes nothing about its order and never has a fill; the swap is a new plan;
  * a hash reported by a wallet is never trusted over the chain: no receipt -> still `submitted`;
  * unreadable evidence => nothing recorded this pass (retry), and after `evidence_retries` passes the
    fill is recorded as `unknown` (never a guess) so PnL stays unknown rather than wrong;
  * every pass has a hard RPC-call budget so a backlog cannot starve the execution RPC."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..ledger.core import LedgerRefused, OrderLedger
from ..ledger.legs import FillRecord
from ..paper import TradeRefused
from .approvals import Head
from .fills import EvidenceUnavailable, ReceiptError, compute_fill, gas_cost, validate

log = logging.getLogger(__name__)

DEFAULT_FINALITY = 12
DEFAULT_RPC_BUDGET = 120
DEFAULT_EVIDENCE_RETRIES = 3


class ReceiptSource(Protocol):
    def head(self) -> Head: ...
    def transaction(self, tx_hash: str) -> dict | None: ...
    def receipt(self, tx_hash: str) -> dict | None: ...
    def block_header(self, number: int) -> dict | None: ...
    def block_transactions(self, number: int) -> list[dict]: ...
    def native_balance(self, address: str, block: int) -> int: ...
    def token_balance(self, token: str, address: str, block: int) -> int: ...


class BudgetExhausted(Exception):
    """This pass used its RPC budget; the remaining legs wait for the next pass."""


class _Metered:
    """Counts every chain read so one pass cannot exceed its budget."""

    def __init__(self, src: ReceiptSource, budget: int) -> None:
        self._src, self.left = src, budget

    def _spend(self) -> None:
        if self.left <= 0:
            raise BudgetExhausted
        self.left -= 1

    def head(self) -> Head:
        self._spend()
        return self._src.head()

    def transaction(self, h: str) -> dict | None:
        self._spend()
        return self._src.transaction(h)

    def receipt(self, h: str) -> dict | None:
        self._spend()
        return self._src.receipt(h)

    def block_header(self, n: int) -> dict | None:
        self._spend()
        return self._src.block_header(n)

    def block_transactions(self, n: int) -> list[dict]:
        self._spend()
        return self._src.block_transactions(n)

    def native_balance(self, a: str, b: int) -> int:
        self._spend()
        return self._src.native_balance(a, b)

    def token_balance(self, t: str, a: str, b: int) -> int:
        self._spend()
        return self._src.token_balance(t, a, b)


@dataclass
class PassReport:
    checked: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    budget_exhausted: bool = False

    def add(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1


class ReceiptReconciler:
    def __init__(self, ledger: OrderLedger, source: ReceiptSource, *,
                 finality_confirmations: int = DEFAULT_FINALITY,
                 rpc_budget: int = DEFAULT_RPC_BUDGET,
                 evidence_retries: int = DEFAULT_EVIDENCE_RETRIES,
                 clock: Callable[[], float] = time.time) -> None:
        if not 1 <= finality_confirmations <= 256:
            raise ValueError("finality_confirmations must be in 1..256")
        self.ledger, self.source, self.clock = ledger, source, clock
        self.finality = finality_confirmations
        self.rpc_budget, self.evidence_retries = rpc_budget, evidence_retries
        self._evidence_failures: dict[tuple[int, str], int] = {}

    # ------------------------------------------------------------------ one leg
    def process(self, leg: dict, src: ReceiptSource) -> str:
        """Returns a short outcome label (also the metric label)."""
        tx_hash = leg["final_tx_hash"] or leg["tx_hash"]
        if not tx_hash:
            return "no_hash"
        head = src.head()
        if leg["block_hash"] is not None and leg["state"] in ("included", "reverted"):
            return self._recheck(leg, tx_hash, head, src)
        return self._first_sight(leg, tx_hash, head, src)

    def _canonical(self, number: int, block_hash: str, src: ReceiptSource) -> bool | None:
        """True / False when the node answered; None when it has no header for that height yet."""
        h = src.block_header(number)
        if h is None:
            return None
        return str(h["hash"]).lower() == block_hash

    def _first_sight(self, leg: dict, tx_hash: str, head: Head, src: ReceiptSource) -> str:
        receipt = src.receipt(tx_hash)
        if receipt is None:
            return "pending"                    # a wallet-reported hash is only an acknowledgment
        tx = src.transaction(tx_hash)
        if tx is None:
            return "unverifiable"               # receipt without a transaction: node is inconsistent
        return self._record(leg, tx_hash, tx, receipt, head, src)

    def _record(self, leg: dict, tx_hash: str, tx: dict, receipt: dict, head: Head,
                src: ReceiptSource) -> str:
        problems = validate(leg, tx, receipt, tx_hash)
        if problems:
            log.error("leg %s receipt does not match the request: %s", leg["id"], problems)
            return "invalid"
        try:
            number = int(receipt["blockNumber"], 16)
        except (KeyError, TypeError, ValueError):
            return "invalid"
        block_hash = str(receipt["blockHash"]).lower()
        canon = self._canonical(number, block_hash, src)
        if canon is None:
            return "pending"                    # node has the receipt but not the header yet
        if not canon:
            return "not_canonical"              # receipt from a fork the node is leaving
        if number > head.number:
            return "pending"
        confirmations = head.number - number + 1
        final = confirmations >= self.finality
        status = 1 if receipt["status"] == "0x1" else 0
        state = "included" if status else "reverted"
        used, cost = gas_cost(tx, receipt)

        fill: FillRecord | None = None
        if leg["kind"] == "swap":
            key = (leg["id"], block_hash)
            fails = self._evidence_failures.get(key, 0)
            try:
                fill = compute_fill(leg, tx, receipt, tx_hash, src,
                                    strict=fails < self.evidence_retries)
            except EvidenceUnavailable:
                self._evidence_failures[key] = fails + 1
                return "evidence_unavailable"
            except ReceiptError as exc:
                log.error("leg %s receipt unusable: %s", leg["id"], exc)
                return "invalid"
            self._evidence_failures.pop(key, None)
        ok = self.ledger.apply_receipt(
            leg["id"], expected_version=leg["version"], state=state, block_number=number,
            block_hash=block_hash, receipt_status=status, gas_used=used, gas_cost_wei=cost,
            fill=fill, finalize=final)
        if not ok:
            return "stale"
        return ("final_" if final else "provisional_") + state

    def _recheck(self, leg: dict, tx_hash: str, head: Head, src: ReceiptSource) -> str:
        """The leg already has an inclusion recorded: is it still canonical, and is it final?"""
        number, block_hash = leg["block_number"], leg["block_hash"]
        canon = self._canonical(number, block_hash, src)
        if canon is None or canon is False:
            # The recorded block is not (or no longer) the canonical one at that height. Ask for the
            # receipt again: a different block = re-included (new fill row); none = orphaned.
            receipt = src.receipt(tx_hash)
            if receipt is not None and str(receipt.get("blockHash", "")).lower() != block_hash:
                tx = src.transaction(tx_hash)
                if tx is None:
                    return "unverifiable"
                return self._record(leg, tx_hash, tx, receipt, head, src)
            if receipt is not None:
                return "pending"                # same block again: header lag, not a reorg
            if canon is None:
                return "pending"                # no header AND no receipt: node is behind, not proof
            ok = self.ledger.apply_reorg(leg["id"], expected_version=leg["version"],
                                         reason="recorded block is no longer canonical")
            return "orphaned" if ok else "stale"
        if head.number - number + 1 < self.finality:
            return "provisional_unchanged"
        # canonical and deep enough: finalize. The fill is re-derived (strictly) and keyed by block
        # hash, so an existing provisional row is promoted, never duplicated or rewritten.
        receipt = src.receipt(tx_hash)
        tx = src.transaction(tx_hash)
        if receipt is None or tx is None:
            return "pending"
        return self._record(leg, tx_hash, tx, receipt, head, src)

    # ------------------------------------------------------------------ the pass
    def run_once(self, limit: int = 100,
                 should_continue: Callable[[], bool] | None = None) -> PassReport:
        """One pass. `should_continue` (optional) lets a supervisor bound the pass by wall time or
        a shutdown request; legs not reached wait for the next pass."""
        rep = PassReport()
        src = _Metered(self.source, self.rpc_budget)
        for leg in self.ledger.receipt_candidates(limit=limit):
            if should_continue is not None and not should_continue():
                break
            rep.checked += 1
            try:
                rep.add(self.process(leg, src))
            except BudgetExhausted:
                rep.budget_exhausted = True
                rep.add("budget_exhausted")
                break
            except (TradeRefused, LedgerRefused) as exc:
                rep.add("error")
                log.warning("receipt check of leg %s skipped: %s", leg.get("id"), exc)
        return rep


def serve(reconciler: ReceiptReconciler, stop: threading.Event, *, interval: float = 5.0,
          max_backoff: float = 60.0) -> int:
    """Run passes until `stop` is set (dormant: nothing calls this by default). A failing pass
    never ends the loop; consecutive failures back off exponentially up to `max_backoff`. Returns
    the number of passes made. Runs regardless of the pause flag by design."""
    passes, failures = 0, 0
    while not stop.is_set():
        try:
            reconciler.run_once()
            failures = 0
        except Exception:                                  # noqa: BLE001 - the loop must survive
            failures += 1
            log.exception("receipt pass failed (%d consecutive)", failures)
        passes += 1
        stop.wait(min(max_backoff, interval * (2 ** min(failures, 6))))
    return passes
