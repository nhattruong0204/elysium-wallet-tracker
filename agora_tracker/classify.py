"""Turn one wallet's net flows inside one transaction into trade events.

Inputs are *net* deltas from the wallet's point of view, so router hops, fee
transfers and refunds inside the same tx cancel out naturally.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeEvent:
    kind: str                 # BUY | SELL | SWAP | TRANSFER_IN | TRANSFER_OUT | MULTI
    tx: str
    block: int
    wallet: str
    token: str
    qty: int                  # raw token units, always positive
    hype_wei: int | None      # HYPE paid (BUY) / received (SELL), positive; None if unknown
    counter_token: str | None = None
    counter_qty: int | None = None
    approx: bool = False      # HYPE leg inferred under ambiguity (several wallet txs in one block)

    @property
    def key(self) -> str:
        return f"{self.tx}:{self.wallet}:{self.token}:{self.kind}"


def classify(*, tx: str, block: int, wallet: str, token_deltas: dict[str, int],
             trade_hype_wei: int | None, dust_wei: int, approx: bool = False) -> list[TradeEvent]:
    """trade_hype_wei: wallet's HYPE change attributable to the trade, gas already added back.
    Negative = HYPE spent, positive = HYPE received, None = unknown."""
    moved = {t: d for t, d in token_deltas.items() if d != 0}
    if not moved:
        return []
    h = trade_hype_wei
    hype_moved = h is not None and abs(h) > dust_wei
    pos = [(t, d) for t, d in moved.items() if d > 0]
    neg = [(t, -d) for t, d in moved.items() if d < 0]

    def ev(kind, token, qty, hype=None, ct=None, cq=None):
        return TradeEvent(kind, tx, block, wallet, token, qty, hype, ct, cq, approx)

    if len(moved) == 1:
        (token, d), = moved.items()
        if hype_moved and d > 0 and h < 0:
            return [ev("BUY", token, d, -h)]
        if hype_moved and d < 0 and h > 0:
            return [ev("SELL", token, -d, h)]
        if d > 0:
            return [ev("TRANSFER_IN", token, d)]
        return [ev("TRANSFER_OUT", token, -d)]

    if len(pos) == 1 and len(neg) == 1 and not hype_moved:
        (tin, qin), (tout, qout) = pos[0], neg[0]
        return [ev("SWAP", tin, qin, None, tout, qout)]

    # Several tokens moved (batch buys, LP adds, aggregator splits): report each leg,
    # HYPE attribution unknown.
    return [ev("MULTI", t, abs(d)) for t, d in moved.items()]
