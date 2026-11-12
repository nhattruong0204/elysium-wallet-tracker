"""Transfer bursts: one wallet sending the same token out many times in a row.

Pattern (2026-11-12, wallet 0xc57bf04f on JELLY and Catbalio): buy ~60% of a launch, then 50
equal transfers to 50 wallets within ~30 seconds. Each transfer used to be its own Telegram
alert (50 alerts in half a minute); the interesting fact is the split itself.

Rule: the first `min_n - 1` TRANSFER_OUT legs of a (wallet, token) alert as usual. When `min_n`
of them fall within `window_s`, a burst starts: one "split started" summary replaces the
individual alert, later transfers of the burst are counted silently, and when the wallet has
sent nothing for `quiet_s` one final summary reports the totals. In-memory only: after a
restart the next transfers simply count from zero again. Everything stays indexed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Burst:
    wallet: str
    token: str
    first_ts: float
    last_ts: float
    count: int = 0
    qty: int = 0
    start_qty: int | None = None          # the wallet's holding before the first transfer
    recipients: set[str] = field(default_factory=set)
    announced: int = 0                    # count reported by the "started" summary
    last_tx: str | None = None
    seen: set[str] = field(default_factory=set)

    def snapshot(self, stage: str) -> dict:
        return {"stage": stage, "wallet": self.wallet, "token": self.token, "count": self.count,
                "qty": str(self.qty), "recipients": len(self.recipients),
                "start_qty": None if self.start_qty is None else str(self.start_qty),
                "first_ts": int(self.first_ts), "last_ts": int(self.last_ts),
                "span_s": int(self.last_ts - self.first_ts), "tx": self.last_tx}


class BurstTracker:
    def __init__(self, min_n: int = 4, window_s: float = 120.0, quiet_s: float = 90.0):
        self.min_n, self.window_s, self.quiet_s = int(min_n), float(window_s), float(quiet_s)
        self._recent: dict[tuple[str, str], deque[tuple]] = {}
        self._active: dict[tuple[str, str], Burst] = {}

    def observe(self, wallet: str, token: str, ts: float, qty: int, *, key: str,
                counterparty: str | None = None, before_qty: int | None = None,
                tx: str | None = None) -> tuple[str, dict | None]:
        """Record one alertable TRANSFER_OUT leg. Returns (verdict, summary):
        ("single", None): alert it as usual; ("start", snapshot): send the summary instead;
        ("more", None): part of a running burst, send nothing."""
        k = (wallet, token)
        b = self._active.get(k)
        if b is not None:
            if key not in b.seen:
                self._add(b, ts, qty, counterparty, tx, key)
            return "more", None
        q = self._recent.setdefault(k, deque())
        if any(e[0] == key for e in q):
            return "single", None          # a replayed leg: do not count it twice
        while q and ts - q[0][1] > self.window_s:
            q.popleft()
        q.append((key, ts, qty, counterparty, before_qty, tx))
        if len(q) < self.min_n:
            return "single", None
        b = Burst(wallet, token, first_ts=q[0][1], last_ts=q[0][1], start_qty=q[0][4])
        for ekey, ets, eqty, ecp, _, etx in q:
            self._add(b, ets, eqty, ecp, etx, ekey)
        b.announced = b.count
        self._active[k] = b
        del self._recent[k]
        return "start", b.snapshot("started")

    @staticmethod
    def _add(b: Burst, ts: float, qty: int, cp: str | None, tx: str | None, key: str) -> None:
        b.count += 1
        b.qty += int(qty)
        b.last_ts = max(b.last_ts, ts)
        b.last_tx = tx or b.last_tx
        b.seen.add(key)
        if cp:
            b.recipients.add(cp)

    def finished(self, now_ts: float) -> list[dict]:
        """Bursts quiet for `quiet_s`: removed, and returned when they grew after the
        "started" summary (a burst that never grew has nothing more to report)."""
        out = []
        for k, b in list(self._active.items()):
            if now_ts - b.last_ts > self.quiet_s:
                del self._active[k]
                if b.count > b.announced:
                    out.append(b.snapshot("finished"))
        for k, q in list(self._recent.items()):        # bounded memory
            if not q or now_ts - q[-1][1] > self.window_s:
                del self._recent[k]
        return out
