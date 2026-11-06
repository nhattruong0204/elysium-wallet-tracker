"""cumulative Top-100 tracking: wallet registry, membership events, scan coverage

Until now the scanner followed `latest leaderboard snapshot + watchlists`, so a wallet that fell
out of the Top 100 stopped being scanned. This migration adds the records for the new policy
(every wallet that has ever been in an accepted, complete top list stays enrolled) without
touching anything that exists:

* `wallet_enrollment`  one row per wallet with leaderboard evidence: first/last time in the list,
  best/last rank, snapshot appearances, entries/exits, current membership, tracking status;
* `leaderboard_membership_events`  ENTERED / EXITED against the previous accepted list;
* `wallet_scan_coverage`  block intervals in which a wallet was really scanned (gaps = trades
  not recorded). Private (manual/Telegram) wallets get intervals too, so this table stays out
  of the public views.

`public_api.wallet_enrollment` and `public_api.leaderboard_membership_events` expose the
registry for public wallets only. `public_api.service_state` additionally exposes the
`scan_stats` key (aggregate scanner cost: requests, duration, lag; no wallet identities).

Rows are filled by the tracker (`--tracking-backfill`, also run at start-up): idempotent.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
ROLE = "dashboard_ro"
STATE_KEYS_OLD = "'rpc_heads', 'heartbeat', 'ui_heartbeat', 'collector_last'"
STATE_KEYS_NEW = STATE_KEYS_OLD + ", 'scan_stats'"


def upgrade() -> None:
    op.create_table(
        "wallet_enrollment",
        sa.Column("chain_id", sa.BigInteger, nullable=False, autoincrement=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("excluded_reason", sa.Text),
        sa.Column("first_top_at", TS, nullable=False),
        sa.Column("last_top_at", TS, nullable=False),
        sa.Column("first_snapshot_id", sa.BigInteger, nullable=False),
        sa.Column("last_snapshot_id", sa.BigInteger, nullable=False),
        sa.Column("best_rank", sa.Integer, nullable=False),
        sa.Column("last_rank", sa.Integer, nullable=False),
        sa.Column("appearances", sa.Integer, nullable=False),
        sa.Column("entries", sa.Integer, nullable=False, server_default="1"),
        sa.Column("exits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("in_top", sa.Boolean, nullable=False),
        sa.Column("enrolled_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('active','excluded')",
                           name=op.f("ck_wallet_enrollment_status")),
        sa.CheckConstraint("address ~ '^0x[0-9a-f]{40}$'",
                           name=op.f("ck_wallet_enrollment_address_lower_hex")),
        sa.ForeignKeyConstraint(["chain_id", "address"], ["wallets.chain_id", "wallets.address"],
                                name=op.f("fk_wallet_enrollment_chain_id_wallets")),
        sa.PrimaryKeyConstraint("chain_id", "address", name=op.f("pk_wallet_enrollment")),
    )
    op.create_index("ix_wallet_enrollment_in_top", "wallet_enrollment", ["chain_id", "in_top"])
    op.create_index("ix_wallet_enrollment_status", "wallet_enrollment", ["chain_id", "status"])

    op.create_table(
        "leaderboard_membership_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), nullable=False),
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.CheckConstraint("kind IN ('ENTERED','EXITED')",
                           name=op.f("ck_leaderboard_membership_events_kind")),
        sa.CheckConstraint("address ~ '^0x[0-9a-f]{40}$'",
                           name=op.f("ck_leaderboard_membership_events_address_lower_hex")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leaderboard_membership_events")),
        sa.UniqueConstraint("chain_id", "address", "snapshot_id", "kind",
                            name="uq_membership_event"),
    )
    op.create_index("ix_leaderboard_membership_events_address_at",
                    "leaderboard_membership_events", ["chain_id", "address", "at"])

    op.create_table(
        "wallet_scan_coverage",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), nullable=False),
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("from_block", sa.BigInteger, nullable=False),
        sa.Column("to_block", sa.BigInteger),
        sa.Column("basis", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("closed_at", TS),
        sa.CheckConstraint("basis IN ('live','derived')",
                           name=op.f("ck_wallet_scan_coverage_basis")),
        sa.CheckConstraint("to_block IS NULL OR to_block >= from_block - 1",
                           name=op.f("ck_wallet_scan_coverage_order")),
        sa.CheckConstraint("address ~ '^0x[0-9a-f]{40}$'",
                           name=op.f("ck_wallet_scan_coverage_address_lower_hex")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_scan_coverage")),
    )
    op.create_index("ux_wallet_scan_coverage_open", "wallet_scan_coverage",
                    ["chain_id", "address"], unique=True,
                    postgresql_where=sa.text("to_block IS NULL"))
    op.create_index("ix_wallet_scan_coverage_address", "wallet_scan_coverage",
                    ["chain_id", "address", "from_block"])

    op.execute("""
        CREATE OR REPLACE VIEW public_api.wallet_enrollment AS
        SELECT e.chain_id, e.address, e.status, e.first_top_at, e.last_top_at, e.best_rank,
               e.last_rank, e.appearances, e.entries, e.exits, e.in_top, e.enrolled_at
        FROM public.wallet_enrollment e
        WHERE e.address IN (SELECT address FROM public_api.public_wallets)""")
    op.execute("""
        CREATE OR REPLACE VIEW public_api.leaderboard_membership_events AS
        SELECT v.chain_id, v.address, v.snapshot_id, v.kind, v.rank, v.at
        FROM public.leaderboard_membership_events v
        WHERE v.address IN (SELECT address FROM public_api.public_wallets)""")
    op.execute(f"GRANT SELECT ON public_api.wallet_enrollment, "
               f"public_api.leaderboard_membership_events TO {ROLE}")
    op.execute(f"""
        CREATE OR REPLACE VIEW public_api.service_state AS
        SELECT key, value, updated_at FROM public.service_state
        WHERE key IN ({STATE_KEYS_NEW})""")


def downgrade() -> None:
    op.execute(f"""
        CREATE OR REPLACE VIEW public_api.service_state AS
        SELECT key, value, updated_at FROM public.service_state
        WHERE key IN ({STATE_KEYS_OLD})""")
    op.execute("DROP VIEW IF EXISTS public_api.leaderboard_membership_events")
    op.execute("DROP VIEW IF EXISTS public_api.wallet_enrollment")
    op.drop_index("ix_wallet_scan_coverage_address", table_name="wallet_scan_coverage")
    op.drop_index("ux_wallet_scan_coverage_open", table_name="wallet_scan_coverage",
                  postgresql_where=sa.text("to_block IS NULL"))
    op.drop_table("wallet_scan_coverage")
    op.drop_index("ix_leaderboard_membership_events_address_at",
                  table_name="leaderboard_membership_events")
    op.drop_table("leaderboard_membership_events")
    op.drop_index("ix_wallet_enrollment_status", table_name="wallet_enrollment")
    op.drop_index("ix_wallet_enrollment_in_top", table_name="wallet_enrollment")
    op.drop_table("wallet_enrollment")
