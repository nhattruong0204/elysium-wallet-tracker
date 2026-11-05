"""Ascend launches indexed straight from the chain

Ascend (Uniswap v4 hook launchpad, docs: ascendlaunch.gitbook.io) emits `Launched` on its hook
and `EscrowLaunched` on its EscrowFactory for every coin. The tracker indexes both from block
422319 into `ascend_launches`, so a token's venue, pool, fee, pair asset and dev buy are known
at block time, hours before (or without) an Agora profile.

`public_api.ascend_launches` exposes the on-chain launch facts only (no creator / escrow /
recipient columns). Every row describes a public on-chain event, not a tracked wallet.

Additive and idempotent.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
ROLE = "dashboard_ro"


def upgrade() -> None:
    op.create_table(
        "ascend_launches",
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("token", sa.String(42), nullable=False),
        sa.Column("pool_id", sa.String(66), nullable=False),
        sa.Column("quote", sa.String(42), nullable=False),
        sa.Column("token_is0", sa.Boolean, nullable=False),
        sa.Column("fee_pips", sa.Integer, nullable=False),
        sa.Column("builder_fee_pips", sa.Integer, nullable=False),
        sa.Column("supply_seeded", sa.Numeric(78, 0), nullable=False),
        sa.Column("dev_buy_tokens", sa.Numeric(78, 0), nullable=False),
        sa.Column("creator", sa.String(42), nullable=False),
        sa.Column("escrow", sa.String(42)),
        sa.Column("recipient", sa.String(42)),
        sa.Column("block_number", sa.BigInteger, nullable=False),
        sa.Column("launched_at", TS),
        sa.Column("tx_hash", sa.String(66), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("chain_id", "token", name=op.f("pk_ascend_launches")),
        sa.ForeignKeyConstraint(["chain_id"], ["chains.chain_id"],
                                name=op.f("fk_ascend_launches_chain_id_chains")),
        sa.CheckConstraint("token ~ '^0x[0-9a-f]{40}$'", name=op.f("ck_ascend_launches_token_lower_hex")),
    )
    op.create_index(op.f("ix_ascend_launches_pool_id"), "ascend_launches", ["pool_id"])
    op.create_index(op.f("ix_ascend_launches_block"), "ascend_launches", ["chain_id", "block_number"])
    op.execute("""
        CREATE OR REPLACE VIEW public_api.ascend_launches AS
        SELECT a.chain_id, a.token, a.pool_id, a.quote, a.token_is0, a.fee_pips,
               a.builder_fee_pips, a.supply_seeded, a.dev_buy_tokens, a.block_number, a.launched_at
        FROM public.ascend_launches a""")
    op.execute(f"GRANT SELECT ON public_api.ascend_launches TO {ROLE}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS public_api.ascend_launches")
    op.drop_index(op.f("ix_ascend_launches_block"), table_name="ascend_launches")
    op.drop_index(op.f("ix_ascend_launches_pool_id"), table_name="ascend_launches")
    op.drop_table("ascend_launches")
