"""explainable wallet scores: immutable versions, point-in-time score history

* `score_versions`  one row per scoring model version: the full configuration (weights and
  parameters) and its hash. A version is immutable: the tracker refuses to run a version whose
  configuration differs from the stored one, so a historical score always means what it meant.
* `wallet_scores`   one row per (wallet, version, as_of). `as_of` is the cut-off: every input of
  that row is strictly older than `as_of` (no look-ahead). `components` holds each component's
  value, weight, contribution and the inputs behind it; a missing input leaves the component
  NULL / unavailable, never zero. Hourly rows are pruned to daily after 48 h.

`public_api.wallet_scores` / `public_api.score_versions` expose both for public wallets only.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
ROLE = "dashboard_ro"


def upgrade() -> None:
    op.create_table(
        "score_versions",
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("config", postgresql.JSONB, nullable=False),
        sa.Column("config_hash", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_score_versions")),
    )
    op.create_table(
        "wallet_scores",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("as_of", TS, nullable=False),
        sa.Column("score", sa.Numeric(6, 2)),                 # 0-100, NULL = not enough evidence
        sa.Column("weight_available", sa.Numeric(5, 4), nullable=False),
        sa.Column("quality", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("components", postgresql.JSONB, nullable=False),
        sa.Column("computed_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("quality IN ('sufficient','limited','insufficient')",
                           name=op.f("ck_wallet_scores_quality")),
        sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)",
                           name=op.f("ck_wallet_scores_range")),
        sa.CheckConstraint("address ~ '^0x[0-9a-f]{40}$'",
                           name=op.f("ck_wallet_scores_address_lower_hex")),
        sa.ForeignKeyConstraint(["version"], ["score_versions.version"],
                                name=op.f("fk_wallet_scores_version_score_versions")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_scores")),
        sa.UniqueConstraint("chain_id", "address", "version", "as_of", name="uq_wallet_scores_point"),
    )
    op.create_index("ix_wallet_scores_version_asof", "wallet_scores", ["version", "as_of"])
    op.execute("""
        CREATE OR REPLACE VIEW public_api.wallet_scores AS
        SELECT s.chain_id, s.address, s.version, s.as_of, s.score, s.weight_available, s.quality,
               s.reason, s.components, s.computed_at
        FROM public.wallet_scores s
        WHERE s.address IN (SELECT address FROM public_api.public_wallets)""")
    op.execute("""
        CREATE OR REPLACE VIEW public_api.score_versions AS
        SELECT version, config, config_hash, created_at FROM public.score_versions""")
    op.execute(f"GRANT SELECT ON public_api.wallet_scores, public_api.score_versions TO {ROLE}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS public_api.score_versions")
    op.execute("DROP VIEW IF EXISTS public_api.wallet_scores")
    op.drop_index("ix_wallet_scores_version_asof", table_name="wallet_scores")
    op.drop_table("wallet_scores")
    op.drop_table("score_versions")
