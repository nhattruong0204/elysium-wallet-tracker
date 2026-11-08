"""The order state machine, typed. The database enforces the same edges (migration 0011);
this table adds *who* may take each edge. A test keeps both lists identical."""

from __future__ import annotations

from enum import Enum


class OrderState(str, Enum):
    CREATED = "created"
    QUOTE_REVIEW = "quote_review"
    REQUIRES_APPROVAL = "requires_approval"
    AWAITING_WALLET = "awaiting_wallet"
    SUBMITTED = "submitted"
    AMBIGUOUS = "ambiguous"
    INCLUDED = "included"
    REVERTED = "reverted"
    FINALIZED = "finalized"
    ORPHANED = "orphaned"
    CANCELLED = "cancelled"


S = OrderState
USER, OWNER, SYSTEM = "user", "owner", "system"
ACTOR_KINDS = (USER, OWNER, SYSTEM)

# (from, to) -> who may take it. A user can only move an order up to the wallet prompt or
# cancel it before that; everything after the wallet is reconciliation (system). Nobody can
# fabricate inclusion or finality by asking for it as a user.
EDGES: dict[tuple[S, S], frozenset[str]] = {
    (S.CREATED, S.QUOTE_REVIEW): frozenset({USER, SYSTEM}),
    (S.CREATED, S.CANCELLED): frozenset({USER, OWNER, SYSTEM}),
    (S.QUOTE_REVIEW, S.REQUIRES_APPROVAL): frozenset({USER, SYSTEM}),
    (S.QUOTE_REVIEW, S.AWAITING_WALLET): frozenset({USER, SYSTEM}),
    (S.QUOTE_REVIEW, S.CANCELLED): frozenset({USER, OWNER, SYSTEM}),
    (S.REQUIRES_APPROVAL, S.QUOTE_REVIEW): frozenset({USER, SYSTEM}),
    (S.REQUIRES_APPROVAL, S.AWAITING_WALLET): frozenset({USER, SYSTEM}),
    (S.REQUIRES_APPROVAL, S.CANCELLED): frozenset({USER, OWNER, SYSTEM}),
    (S.AWAITING_WALLET, S.SUBMITTED): frozenset({SYSTEM}),
    (S.AWAITING_WALLET, S.AMBIGUOUS): frozenset({SYSTEM}),
    (S.AWAITING_WALLET, S.CANCELLED): frozenset({OWNER, SYSTEM}),
    (S.SUBMITTED, S.INCLUDED): frozenset({SYSTEM}),
    (S.SUBMITTED, S.REVERTED): frozenset({SYSTEM}),
    (S.SUBMITTED, S.AMBIGUOUS): frozenset({SYSTEM}),
    (S.AMBIGUOUS, S.SUBMITTED): frozenset({SYSTEM}),
    (S.AMBIGUOUS, S.INCLUDED): frozenset({SYSTEM}),
    (S.AMBIGUOUS, S.REVERTED): frozenset({SYSTEM}),
    (S.AMBIGUOUS, S.ORPHANED): frozenset({SYSTEM}),
    (S.AMBIGUOUS, S.CANCELLED): frozenset({OWNER, SYSTEM}),
    (S.INCLUDED, S.FINALIZED): frozenset({SYSTEM}),
    (S.INCLUDED, S.REVERTED): frozenset({SYSTEM}),
    (S.INCLUDED, S.ORPHANED): frozenset({SYSTEM}),
    (S.REVERTED, S.FINALIZED): frozenset({SYSTEM}),
    (S.REVERTED, S.INCLUDED): frozenset({SYSTEM}),
    (S.REVERTED, S.ORPHANED): frozenset({SYSTEM}),
    (S.ORPHANED, S.SUBMITTED): frozenset({SYSTEM}),
    (S.ORPHANED, S.INCLUDED): frozenset({SYSTEM}),
    (S.ORPHANED, S.REVERTED): frozenset({SYSTEM}),
    (S.ORPHANED, S.CANCELLED): frozenset({OWNER, SYSTEM}),
}

TERMINAL = frozenset({S.FINALIZED, S.CANCELLED})
# in flight: can still lead to (or already is) a wallet request or an unsettled transaction
ACTIVE = frozenset({S.CREATED, S.QUOTE_REVIEW, S.REQUIRES_APPROVAL, S.AWAITING_WALLET,
                    S.SUBMITTED, S.AMBIGUOUS, S.ORPHANED})
# before any wallet prompt: nothing can have been signed, so expiry simply cancels
PRE_WALLET = frozenset({S.CREATED, S.QUOTE_REVIEW, S.REQUIRES_APPROVAL})
# receipt reconciliation keeps working while trading is paused
RECONCILING = frozenset({S.AWAITING_WALLET, S.SUBMITTED, S.AMBIGUOUS, S.INCLUDED, S.REVERTED,
                         S.ORPHANED})
# an explanation is mandatory when an order leaves limbo without having settled
NEEDS_REASON = frozenset({(S.AMBIGUOUS, S.CANCELLED), (S.ORPHANED, S.CANCELLED),
                          (S.AWAITING_WALLET, S.CANCELLED)})
