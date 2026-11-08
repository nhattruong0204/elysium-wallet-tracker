"""Durable wallet legs (migration 0013, issue #93): SQL helpers used by the ledger.

A leg is one thing the user's wallet was asked to do (reset / approve / swap). These functions run
inside the ledger's transactions so a leg, its audit event and any order transition commit or roll
back together. The database enforces the invariants that matter (unique idempotency key, one
unsettled leg per wallet+chain, one swap per order, set-once hash/nonce, legal state edges); the
Python layer only translates violations into stable refusal codes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

LEG_KINDS = ("reset", "approve", "swap")
ACTIVE_LEG_STATES = ("prompted", "hash_reported", "ambiguous")
HEX_ADDR = re.compile(r"^0x[0-9a-f]{40}$")

# Wallet-reported reasons are untrusted browser input: only these codes are ever stored.
WALLET_REASONS = frozenset({"user_rejected", "disconnected", "unsupported", "chain_mismatch",
                            "timeout", "insufficient_funds", "unspecified"})

COLS = ("id, order_id, user_id, leg_no, kind, wallet, chain_id, idempotency_key, request_digest, "
        "expected_to, expected_value, expected_data, gas_limit, max_fee_wei, nonce_hint, "
        "prompt_block, nonce, tx_hash, final_tx_hash, state, resolution, version, prompted_at, "
        "answered_at, seen_at, resolved_at")


@dataclass(frozen=True)
class LegSpec:
    """What the wallet was asked to do, exactly as shown to it. Public data only."""
    kind: str
    wallet: str
    chain_id: int
    to: str
    value_wei: int
    data: str
    gas_limit: int | None
    max_fee_wei: int | None
    nonce_hint: int | None
    prompt_block: int | None


def _ts(v) -> float | None:
    return v.timestamp() if v is not None else None


def public(row) -> dict[str, Any]:
    """JSON-safe leg. Wei and nonces are decimal strings; times are epoch seconds."""
    def s(v):
        return None if v is None else str(int(v))
    return {
        "id": int(row.id), "order_id": str(row.order_id), "leg_no": int(row.leg_no),
        "kind": row.kind, "wallet": row.wallet, "chain_id": int(row.chain_id),
        "request_id": row.idempotency_key, "request_digest": row.request_digest,
        "expected_to": row.expected_to, "expected_value_wei": s(row.expected_value),
        "expected_data": row.expected_data, "gas_limit": s(row.gas_limit),
        "max_fee_wei": s(row.max_fee_wei), "nonce_hint": s(row.nonce_hint),
        "prompt_block": None if row.prompt_block is None else int(row.prompt_block),
        "nonce": s(row.nonce), "tx_hash": row.tx_hash, "final_tx_hash": row.final_tx_hash,
        "state": row.state, "resolution": row.resolution, "version": int(row.version),
        "prompted_at": _ts(row.prompted_at), "answered_at": _ts(row.answered_at),
        "seen_at": _ts(row.seen_at), "resolved_at": _ts(row.resolved_at),
    }


def by_key(c: Connection, key: str, *, lock: bool = False):
    return c.execute(sa.text(f"SELECT {COLS} FROM order_legs WHERE idempotency_key = :k"
                             + (" FOR UPDATE" if lock else "")), {"k": key}).first()


def by_id(c: Connection, leg_id: int, *, lock: bool = False):
    return c.execute(sa.text(f"SELECT {COLS} FROM order_legs WHERE id = :i"
                             + (" FOR UPDATE" if lock else "")), {"i": leg_id}).first()


def for_order(c: Connection, order_id: UUID):
    return c.execute(sa.text(f"SELECT {COLS} FROM order_legs WHERE order_id = :o ORDER BY leg_no"),
                     {"o": order_id}).all()


def _constraint(exc: IntegrityError) -> str:
    diag = getattr(exc.orig, "diag", None)
    return str(getattr(diag, "constraint_name", "") or "")


def insert(c: Connection, *, order_id: UUID, user_id: int, key: str, digest: str,
           spec: LegSpec, prompted_at, refusal) -> int:
    """Insert the leg. `refusal(code, message)` builds the ledger's exception type."""
    if spec.kind not in LEG_KINDS or not HEX_ADDR.fullmatch(spec.wallet):
        raise refusal("invalid_request", "malformed wallet leg")
    n = c.execute(sa.text("SELECT coalesce(max(leg_no), 0) + 1 FROM order_legs "
                          "WHERE order_id = :o"), {"o": order_id}).scalar_one()
    try:
        with c.begin_nested():                      # a violation must not poison the outer tx
            c.execute(sa.text("""
                INSERT INTO order_legs (order_id, user_id, leg_no, kind, wallet, chain_id,
                    idempotency_key, request_digest, expected_to, expected_value, expected_data,
                    gas_limit, max_fee_wei, nonce_hint, prompt_block, prompted_at)
                VALUES (:o, :u, :n, :k, :w, :c, :key, :d, :to, :v, :data, :g, :f, :nh, :pb, :at)"""),
                {"o": order_id, "u": user_id, "n": n, "k": spec.kind, "w": spec.wallet,
                 "c": spec.chain_id, "key": key, "d": digest, "to": spec.to,
                 "v": spec.value_wei, "data": spec.data, "g": spec.gas_limit,
                 "f": spec.max_fee_wei, "nh": spec.nonce_hint, "pb": spec.prompt_block,
                 "at": prompted_at})
    except IntegrityError as exc:
        name = _constraint(exc)
        if name == "ux_legs_wallet_active":
            raise refusal("wallet_busy", "this wallet has an unsettled request; resolve it first") from None
        if name == "ux_legs_one_swap":
            raise refusal("already_requested", "a swap prompt was already issued for this order") from None
        if name in ("uq_leg_key", "uq_leg_no"):
            raise refusal("duplicate", "this wallet request already exists") from None
        raise refusal("invalid_request", "wallet leg rejected by the database") from None
    return int(n)
