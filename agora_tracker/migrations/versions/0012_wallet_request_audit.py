"""audit kinds and dedupe for wallet requests (Stage C2 / issue #92)

Additive to 0011. A wallet prompt (approval or swap) is a durable, auditable fact: the ledger
records *that* a request with a given binding digest was authorized and what the wallet answered,
once per request key. No signature, key or raw transaction is stored.

The widened CHECK is left in place on downgrade on purpose: order_events is append-only and
hash-chained, so rows of the new kinds cannot be removed, and a narrower CHECK would then fail.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-08
"""
from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

S = "trading_private"
KINDS = ("ORDER_CREATED", "TRANSITION", "TRANSITION_REFUSED", "ADMISSION_REFUSED",
         "ATTEMPT_RECORDED", "PAUSED", "RESUMED", "EXECUTION_FLAG_SET", "POLICY_SET", "RECOVERY",
         "WALLET_REQUEST", "WALLET_RESULT")


def upgrade() -> None:
    kinds = ", ".join(f"'{k}'" for k in KINDS)
    op.execute(f"ALTER TABLE {S}.order_events DROP CONSTRAINT IF EXISTS order_events_kind_check")
    op.execute(f"ALTER TABLE {S}.order_events ADD CONSTRAINT order_events_kind_check "
               f"CHECK (kind IN ({kinds}))")
    for kind, name in (("WALLET_REQUEST", "ux_events_wallet_request"),
                       ("WALLET_RESULT", "ux_events_wallet_result")):
        op.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {S}.order_events
                       (order_id, request_key) WHERE kind = '{kind}'""")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {S}.ux_events_wallet_result")
    op.execute(f"DROP INDEX IF EXISTS {S}.ux_events_wallet_request")
