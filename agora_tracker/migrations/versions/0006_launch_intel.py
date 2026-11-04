"""launch intelligence: venues seen so far, and the forward paper-trading log

* `launch_venues`  every launchpad / DEX pool the tracker has already reported, so a new one is
  alerted exactly once (key is unique, inserts are ON CONFLICT DO NOTHING).
* `paper_trades`   one row per token the "independent wallets" signal fired for. The entry is the
  executed price of the triggering on-chain buy; later prices come from token_market_snapshots
  at report time. Nothing here is ever executed; it only measures the signal forward.

Both tables are private: no `public_api` view is created, so the dashboard role cannot read them.
Additive and idempotent.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "launch_venues",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("first_seen_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("alerted_at", TS),
        sa.Column("details", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_launch_venues")),
        sa.ForeignKeyConstraint(["chain_id"], ["chains.chain_id"],
                                name=op.f("fk_launch_venues_chain_id_chains")),
        sa.UniqueConstraint("chain_id", "key", name=op.f("uq_launch_venues_key")),
    )
    op.create_index(op.f("ix_launch_venues_kind_seen"), "launch_venues",
                    ["chain_id", "kind", "first_seen_at"])
    op.create_table(
        "paper_trades",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("token", sa.String(42), nullable=False),
        sa.Column("signal_id", sa.BigInteger),
        sa.Column("opened_at", TS, nullable=False),
        sa.Column("block_number", sa.BigInteger),
        sa.Column("entry_price_hype", sa.Numeric(60, 30)),
        sa.Column("n_independent", sa.Integer, nullable=False),
        sa.Column("n_raw", sa.Integer, nullable=False),
        sa.Column("details", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_trades")),
        sa.ForeignKeyConstraint(["chain_id"], ["chains.chain_id"],
                                name=op.f("fk_paper_trades_chain_id_chains")),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"],
                                name=op.f("fk_paper_trades_signal_id_signals"),
                                ondelete="SET NULL"),
        sa.UniqueConstraint("chain_id", "token", name=op.f("uq_paper_trades_token")),
        sa.CheckConstraint("token ~ '^0x[0-9a-f]{40}$'", name=op.f("ck_paper_trades_token_lower_hex")),
    )
    op.create_index(op.f("ix_paper_trades_opened_at"), "paper_trades", ["opened_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_paper_trades_opened_at"), table_name="paper_trades")
    op.drop_table("paper_trades")
    op.drop_index(op.f("ix_launch_venues_kind_seen"), table_name="launch_venues")
    op.drop_table("launch_venues")
