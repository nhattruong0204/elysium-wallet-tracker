"""Structural gate for corroborated chain evidence (#95 F-15).

The ledger cannot know whether a chain fact is TRUE; `wallet/corroboration.py` establishes that
before any write. What the ledger CAN do, and does for every write that rests on chain facts
(receipts, finalization, verified fills, reorg/orphaning, automatic lock release, owner recovery),
is refuse the write unless it carries a provenance record showing the facts came from at least two
operationally distinct sources that agreed at one anchor block, on the expected chain, recently.
No caller can therefore finalize, certify or release on a single provider's say-so by forgetting
to ask for a second opinion."""

from __future__ import annotations

import json
import re
from typing import Any, NoReturn

TXHASH = re.compile(r"^0x[0-9a-f]{64}$")
MAX_EVIDENCE_BYTES = 24_000
MAX_AGE_S = 300.0
FUTURE_SKEW_S = 30.0


class EvidenceRefused(ValueError):
    """The evidence does not meet the gate; the ledger turns it into LedgerRefused."""


def canonical(evidence: dict) -> str:
    try:
        return json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError):
        raise EvidenceRefused("evidence must be plain JSON") from None


def check_chain_evidence(evidence: Any, *, chain_id: int, now: float) -> str:
    """Raise `EvidenceRefused` unless `evidence` is a corroborated provenance
    record. Returns the canonical JSON (for the digest)."""
    def refuse(why: str) -> NoReturn:
        raise EvidenceRefused(why)

    if not isinstance(evidence, dict):
        refuse("corroborated chain evidence is required")
    raw = canonical(evidence)
    if len(raw) > MAX_EVIDENCE_BYTES:
        refuse("evidence is too large")
    anchor, srcs = evidence.get("anchor"), evidence.get("sources")
    if evidence.get("schema") != 1:
        refuse("unsupported evidence schema")
    if evidence.get("chain_id") != chain_id:
        refuse("evidence is for another chain")
    if (not isinstance(anchor, dict) or type(anchor.get("number")) is not int
            or anchor["number"] < 0 or not TXHASH.fullmatch(str(anchor.get("hash", "")))):
        refuse("evidence has no anchor block")
    if not isinstance(srcs, list):
        refuse("evidence names no sources")
    names = [x.get("name") if isinstance(x, dict) else None for x in srcs]
    ops = [x.get("operator") if isinstance(x, dict) else None for x in srcs]
    if (len(srcs) < 2 or not all(isinstance(n, str) and n for n in names)
            or not all(isinstance(o, str) and o for o in ops)):
        refuse("evidence needs at least two named sources with operators")
    if len(set(names)) != len(names) or len(set(ops)) != len(ops):
        refuse("evidence sources are not distinct")
    if evidence.get("source_agreement") is not True:
        refuse("the sources did not agree")
    if evidence.get("failed") or evidence.get("problems"):
        refuse("evidence records unresolved source problems")
    taken = evidence.get("taken_at")
    if not isinstance(taken, (int, float)) or isinstance(taken, bool):
        refuse("evidence has no timestamp")
    if now - float(taken) > MAX_AGE_S or float(taken) - now > FUTURE_SKEW_S:
        refuse("evidence is stale")
    if type(evidence.get("finality")) is not int or evidence["finality"] < 1:
        refuse("evidence does not state the finality depth it used")
    return raw
