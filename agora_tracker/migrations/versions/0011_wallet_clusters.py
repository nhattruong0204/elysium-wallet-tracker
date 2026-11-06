"""evidence-based wallet clusters: versioned model, edges with their evidence, lifecycle

A cluster is a set of public wallets joined by *observable* evidence (repeated same-block buys of
the same token, same-transaction token transfers between them, near-simultaneous first buys of
selective tokens). It says "these addresses behave as one operator would", never who the operator
is. Everything is versioned and point-in-time:

* `cluster_versions`        immutable model configuration + hash (as for score versions)
* `cluster_runs`            one row per evaluation, with its `as_of` cut-off
* `wallet_edges`            the pairwise evidence of a run (class counts, first/last seen, a few
                            example tokens) and its strength (strong / moderate / weak)
* `wallet_clusters`         identity and status (active / merged / invalidated), lineage
                            (`merged_into`, `split_from`)
* `wallet_cluster_members`  membership with validity intervals, so "who was in cluster X at time
                            T" is a plain range query and later knowledge never rewrites the past
* `cluster_events`          CREATED / EXTENDED / REDUCED / ABSORBED / SPLIT_FROM / INVALIDATED

Public views expose rows whose wallets are all public.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
ROLE = "dashboard_ro"
HEX = "address ~ '^0x[0-9a-f]{40}$'"


def upgrade() -> None:
    op.create_table(
        "cluster_versions",
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("config", postgresql.JSONB, nullable=False),
        sa.Column("config_hash", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_cluster_versions")))
    op.create_table(
        "cluster_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("as_of", TS, nullable=False),
        sa.Column("edges_n", sa.Integer, nullable=False),
        sa.Column("clusters_n", sa.Integer, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["version"], ["cluster_versions.version"],
                                name=op.f("fk_cluster_runs_version_cluster_versions")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cluster_runs")),
        sa.UniqueConstraint("version", "as_of", name="uq_cluster_runs_point"))
    op.create_table(
        "wallet_clusters",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_run_id", sa.BigInteger, nullable=False),
        sa.Column("ended_run_id", sa.BigInteger),
        sa.Column("merged_into", sa.BigInteger),
        sa.Column("split_from", sa.BigInteger),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("ended_at", TS),
        sa.CheckConstraint("status IN ('active','merged','invalidated')",
                           name=op.f("ck_wallet_clusters_status")),
        sa.ForeignKeyConstraint(["version"], ["cluster_versions.version"],
                                name=op.f("fk_wallet_clusters_version_cluster_versions")),
        sa.ForeignKeyConstraint(["created_run_id"], ["cluster_runs.id"],
                                name=op.f("fk_wallet_clusters_created_run_id_cluster_runs")),
        sa.ForeignKeyConstraint(["ended_run_id"], ["cluster_runs.id"],
                                name=op.f("fk_wallet_clusters_ended_run_id_cluster_runs")),
        sa.ForeignKeyConstraint(["merged_into"], ["wallet_clusters.id"],
                                name=op.f("fk_wallet_clusters_merged_into_wallet_clusters")),
        sa.ForeignKeyConstraint(["split_from"], ["wallet_clusters.id"],
                                name=op.f("fk_wallet_clusters_split_from_wallet_clusters")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_clusters")))
    op.create_index("ix_wallet_clusters_status", "wallet_clusters", ["version", "status"])
    op.create_table(
        "wallet_cluster_members",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("cluster_id", sa.BigInteger, nullable=False),
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("valid_from", TS, nullable=False),
        sa.Column("valid_to", TS),
        sa.Column("from_run_id", sa.BigInteger, nullable=False),
        sa.Column("to_run_id", sa.BigInteger),
        sa.CheckConstraint(HEX, name=op.f("ck_wallet_cluster_members_address_lower_hex")),
        sa.CheckConstraint("valid_to IS NULL OR valid_to >= valid_from",
                           name=op.f("ck_wallet_cluster_members_order")),
        sa.ForeignKeyConstraint(["cluster_id"], ["wallet_clusters.id"],
                                name=op.f("fk_wallet_cluster_members_cluster_id_wallet_clusters")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_cluster_members")))
    op.create_index("ux_wallet_cluster_members_open", "wallet_cluster_members",
                    ["cluster_id", "address"], unique=True,
                    postgresql_where=sa.text("valid_to IS NULL"))
    op.create_index("ix_wallet_cluster_members_address", "wallet_cluster_members",
                    ["chain_id", "address", "valid_from"])
    op.create_table(
        "wallet_edges",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("run_id", sa.BigInteger, nullable=False),
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("wallet_a", sa.String(42), nullable=False),
        sa.Column("wallet_b", sa.String(42), nullable=False),
        sa.Column("strength", sa.Text, nullable=False),
        sa.Column("evidence", postgresql.JSONB, nullable=False),
        sa.CheckConstraint("wallet_a < wallet_b", name=op.f("ck_wallet_edges_order")),
        sa.CheckConstraint("strength IN ('strong','moderate','weak')",
                           name=op.f("ck_wallet_edges_strength")),
        sa.ForeignKeyConstraint(["run_id"], ["cluster_runs.id"], ondelete="CASCADE",
                                name=op.f("fk_wallet_edges_run_id_cluster_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_edges")),
        sa.UniqueConstraint("run_id", "wallet_a", "wallet_b", name="uq_wallet_edges_pair"))
    op.create_index("ix_wallet_edges_a", "wallet_edges", ["wallet_a"])
    op.create_index("ix_wallet_edges_b", "wallet_edges", ["wallet_b"])
    op.create_table(
        "cluster_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("cluster_id", sa.BigInteger, nullable=False),
        sa.Column("run_id", sa.BigInteger, nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("detail", postgresql.JSONB, nullable=False),
        sa.CheckConstraint(
            "kind IN ('CREATED','EXTENDED','REDUCED','ABSORBED','MERGED_INTO','SPLIT_FROM',"
            "'INVALIDATED')", name=op.f("ck_cluster_events_kind")),
        sa.ForeignKeyConstraint(["cluster_id"], ["wallet_clusters.id"],
                                name=op.f("fk_cluster_events_cluster_id_wallet_clusters")),
        sa.ForeignKeyConstraint(["run_id"], ["cluster_runs.id"],
                                name=op.f("fk_cluster_events_run_id_cluster_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cluster_events")))
    op.create_index("ix_cluster_events_cluster", "cluster_events", ["cluster_id", "at"])

    pub = "(SELECT address FROM public_api.public_wallets)"
    op.execute("""CREATE OR REPLACE VIEW public_api.cluster_versions AS
        SELECT version, config, config_hash, created_at FROM public.cluster_versions""")
    op.execute("""CREATE OR REPLACE VIEW public_api.cluster_runs AS
        SELECT id, version, as_of, edges_n, clusters_n, created_at FROM public.cluster_runs""")
    op.execute(f"""CREATE OR REPLACE VIEW public_api.wallet_clusters AS
        SELECT c.id, c.chain_id, c.version, c.status, c.created_run_id, c.ended_run_id,
               c.merged_into, c.split_from, c.created_at, c.ended_at
        FROM public.wallet_clusters c
        WHERE NOT EXISTS (SELECT 1 FROM public.wallet_cluster_members m
                          WHERE m.cluster_id = c.id AND m.address NOT IN {pub})""")
    op.execute(f"""CREATE OR REPLACE VIEW public_api.wallet_cluster_members AS
        SELECT m.id, m.cluster_id, m.chain_id, m.address, m.valid_from, m.valid_to,
               m.from_run_id, m.to_run_id
        FROM public.wallet_cluster_members m
        WHERE m.address IN {pub}
          AND NOT EXISTS (SELECT 1 FROM public.wallet_cluster_members x
                          WHERE x.cluster_id = m.cluster_id AND x.address NOT IN {pub})""")
    op.execute(f"""CREATE OR REPLACE VIEW public_api.wallet_edges AS
        SELECT e.id, e.run_id, e.chain_id, e.wallet_a, e.wallet_b, e.strength, e.evidence
        FROM public.wallet_edges e
        WHERE e.wallet_a IN {pub} AND e.wallet_b IN {pub}""")
    op.execute("""CREATE OR REPLACE VIEW public_api.cluster_events AS
        SELECT v.id, v.cluster_id, v.run_id, v.at, v.kind, v.detail
        FROM public.cluster_events v
        WHERE v.cluster_id IN (SELECT id FROM public_api.wallet_clusters)""")
    op.execute(f"GRANT SELECT ON public_api.cluster_versions, public_api.cluster_runs, "
               f"public_api.wallet_clusters, public_api.wallet_cluster_members, "
               f"public_api.wallet_edges, public_api.cluster_events TO {ROLE}")


def downgrade() -> None:
    for v in ("cluster_events", "wallet_edges", "wallet_cluster_members", "wallet_clusters",
              "cluster_runs", "cluster_versions"):
        op.execute(f"DROP VIEW IF EXISTS public_api.{v}")
    op.drop_index("ix_cluster_events_cluster", table_name="cluster_events")
    op.drop_table("cluster_events")
    op.drop_index("ix_wallet_edges_b", table_name="wallet_edges")
    op.drop_index("ix_wallet_edges_a", table_name="wallet_edges")
    op.drop_table("wallet_edges")
    op.drop_index("ix_wallet_cluster_members_address", table_name="wallet_cluster_members")
    op.drop_index("ux_wallet_cluster_members_open", table_name="wallet_cluster_members",
                  postgresql_where=sa.text("valid_to IS NULL"))
    op.drop_table("wallet_cluster_members")
    op.drop_index("ix_wallet_clusters_status", table_name="wallet_clusters")
    op.drop_table("wallet_clusters")
    op.drop_table("cluster_runs")
    op.drop_table("cluster_versions")
