"""outbox payload for rich alert cards

Adds notification_outbox.payload (JSONB, nullable): the structured alert that the notifier
re-renders with fresh market data at send time. Existing rows keep payload NULL and are
sent as their stored text, unchanged.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_outbox",
                  sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("notification_outbox", "payload")
