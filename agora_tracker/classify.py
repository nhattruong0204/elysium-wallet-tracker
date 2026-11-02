"""Turn one wallet's net flows inside one transaction into trade events.

Inputs are *net* deltas from the wallet's point of view, so router hops, fee
transfers and refunds inside the same tx cancel out naturally.

Confidence (how sure we are about the HYPE leg, never upgraded later):
  high      HYPE leg measured: balance change across the block with the wallet's nonce
            confirming this was its only tx there, or paid/received in wrapped HYPE.
  estimated HYPE leg inferred, e.g. from tx.value when the balance change is ambiguous.
  unknown   HYPE leg not measurable; only token movement is known.
Only the wallet's own transactions can spend its HYPE, so a tx sent by someone else
can never be a BUY/SELL for this wallet: it is a transfer (airdrop, escrow fill, gift).
"""

from __future__ import annotations

from dataclasses import dataclass

CONFIDENCE = ("high", "estimated", "unknown")


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
    confidence: str = "high"
    hype_method: str = "none"  # balance_diff | wrapped | tx_value | none
    inbound: bool | None = None  # direction, only needed for MULTI legs (not persisted)

    @property
    def approx(self) -> bool:
        """Back-compat: anything not measured is approximate."""
        return self.confidence != "high"

    @property
    def key(self) -> str:
        # One row per (tx, wallet, token) leg. `kind` is deliberately NOT part of the key:
        # a replay with better data must not create a second row for the same leg.
        return f"{self.tx}:{self.wallet}:{self.token}"


def classify(*, tx: str, block: int, wallet: str, token_deltas: dict[str, int],
             trade_hype_wei: int | None, dust_wei: int, is_sender: bool = True,
             confidence: str = "high", hype_method: str = "none") -> list[TradeEvent]:
    """trade_hype_wei: wallet's HYPE change attributable to the trade, gas already added back.
    Negative = HYPE spent, positive = HYPE received, None = unknown."""
    if confidence not in CONFIDENCE:
        raise ValueError(f"bad confidence {confidence!r}")
    moved = {t: d for t, d in token_deltas.items() if d != 0}
    if not moved:
        return []

    def ev(kind, token, qty, hype=None, ct=None, cq=None, conf=confidence, method=hype_method):
        return TradeEvent(kind, tx, block, wallet, token, qty, hype, ct, cq, conf,
                          method if hype is not None else "none")

    if not is_sender:
        # Someone else's tx moved tokens to/from this wallet: a transfer, whatever the HYPE did.
        return [ev("TRANSFER_IN" if d > 0 else "TRANSFER_OUT", t, abs(d), conf="high")
                for t, d in moved.items()]

    hype_known = trade_hype_wei is not None and confidence != "unknown"
    h = trade_hype_wei if trade_hype_wei is not None else 0
    hype_moved = hype_known and abs(h) > dust_wei
    pos = [(t, d) for t, d in moved.items() if d > 0]
    neg = [(t, -d) for t, d in moved.items() if d < 0]

    if len(moved) == 1:
        (token, d), = moved.items()
        if hype_moved and d > 0 and h < 0:
            return [ev("BUY", token, d, -h)]
        if hype_moved and d < 0 and h > 0:
            return [ev("SELL", token, -d, h)]
        if hype_known:
            # HYPE measured and it did not move against the token: a plain transfer.
            return [ev("TRANSFER_IN" if d > 0 else "TRANSFER_OUT", token, abs(d))]
        # HYPE leg unknown: could be a sell or a transfer. Record movement, claim nothing.
        return [ev("TRANSFER_IN" if d > 0 else "TRANSFER_OUT", token, abs(d), conf="unknown")]

    if len(pos) == 1 and len(neg) == 1 and hype_known and not hype_moved:
        (tin, qin), (tout, qout) = pos[0], neg[0]
        return [ev("SWAP", tin, qin, None, tout, qout)]

    # Several tokens moved (batch buys, LP adds, aggregator splits): report each leg,
    # HYPE attribution unknown.
    legs = []
    for t, d in moved.items():
        e = ev("MULTI", t, abs(d), conf="unknown")
        e.inbound = d > 0
        legs.append(e)
    return legs
