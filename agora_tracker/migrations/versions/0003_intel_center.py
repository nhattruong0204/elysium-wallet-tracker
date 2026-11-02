"""intelligence center: token profiles, market snapshots, UI sessions, report jobs

* token_profiles, token_market_snapshots: market history for returns / trending
* ui_sessions: Telegram menu state (selected period, pending input)
* report_jobs: export metadata (files are not stored)
* wallet_watchlist.source gains 'telegram' (wallets added from the bot)
* ix_trades_chain_time: every report filters trades by time

Additive only: nothing existing is rewritten, so the running tracker is unaffected.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

ADDR = sa.String(42)
TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _addr(col: str, table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{col} ~ '^0x[0-9a-f]{{40}}$'",
                              name=op.f(f"ck_{table}_{col}_lower_hex"))


def upgrade() -> None:
    op.drop_constraint(op.f("ck_wallet_watchlist_source"), "wallet_watchlist", type_="check")
    op.create_check_constraint(
        op.f("ck_wallet_watchlist_source"), "wallet_watchlist",
        "source IN ('leaderboard','manual','reverse_discovery','import','telegram')")
    op.create_index(op.f("ix_trades_chain_time"), "trades", ["chain_id", "block_time"])

    op.create_table(
        "token_profiles",
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("address", ADDR, nullable=False),
        sa.Column("symbol", sa.Text),
        sa.Column("name", sa.Text),
        sa.Column("launchpad", sa.Text),
        sa.Column("version", sa.Text),
        sa.Column("launched_at", TS),
        sa.Column("source", sa.Text, nullable=False, server_default="agora"),
        sa.Column("first_seen_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["chain_id"], ["chains.chain_id"],
                                name=op.f("fk_token_profiles_chain_id_chains")),
        sa.PrimaryKeyConstraint("chain_id", "address", name=op.f("pk_token_profiles")),
        _addr("address", "token_profiles"),
    )
    op.create_index(op.f("ix_token_profiles_symbol"), "token_profiles",
                    ["chain_id", sa.text("lower(symbol)")])

    op.create_table(
        "token_market_snapshots",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("chain_id", sa.BigInteger, nullable=False),
        sa.Column("token", ADDR, nullable=False),
        sa.Column("captured_at", TS, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_synced_at", TS),
        sa.Column("source_head", sa.BigInteger),
        sa.Column("price_hype", sa.Numeric),
        sa.Column("mcap_hype", sa.Numeric),
        sa.Column("vol_1h_hype", sa.Numeric),
        sa.Column("vol_24h_hype", sa.Numeric),
        sa.Column("change_1h", sa.Numeric),
        sa.Column("change_24h", sa.Numeric),
        sa.Column("buys_24h", sa.Integer),
        sa.Column("trades_24h", sa.Integer),
        sa.Column("last_trade_at", TS),
        sa.ForeignKeyConstraint(["chain_id"], ["chains.chain_id"],
                                name=op.f("fk_token_market_snapshots_chain_id_chains")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_token_market_snapshots")),
        sa.UniqueConstraint("chain_id", "token", "captured_at",
                            name=op.f("uq_token_market_snapshots_obs")),
        _addr("token", "token_market_snapshots"),
    )
    op.create_index(op.f("ix_token_market_snapshots_time"), "token_market_snapshots",
                    ["chain_id", "captured_at"])

    op.create_table(
        "ui_sessions",
        sa.Column("user_id", sa.BigInteger, autoincrement=False, nullable=False),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("range_preset", sa.Text),
        sa.Column("range_start", TS),
        sa.Column("range_end", TS),
        sa.Column("state", sa.Text),
        sa.Column("state_data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_ui_sessions")),
    )

    op.create_table(
        "report_jobs",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("fmt", sa.Text, nullable=False),
        sa.Column("range_start", TS, nullable=False),
        sa.Column("range_end", TS, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("rows", sa.Integer),
        sa.Column("bytes", sa.Integer),
        sa.Column("file_name", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", TS),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_jobs")),
        sa.CheckConstraint("status IN ('running','done','failed')", name=op.f("ck_report_jobs_status")),
        sa.CheckConstraint("fmt IN ('csv','xlsx')", name=op.f("ck_report_jobs_fmt")),
    )
    op.create_index(op.f("ix_report_jobs_user_created"), "report_jobs", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("report_jobs")
    op.drop_table("ui_sessions")
    op.drop_table("token_market_snapshots")
    op.drop_table("token_profiles")
    op.drop_index(op.f("ix_trades_chain_time"), table_name=op.f("trades"))
    op.execute("UPDATE wallet_watchlist SET source = 'manual' WHERE source = 'telegram'")
    op.drop_constraint(op.f("ck_wallet_watchlist_source"), "wallet_watchlist", type_="check")
    op.create_check_constraint(
        op.f("ck_wallet_watchlist_source"), "wallet_watchlist",
        "source IN ('leaderboard','manual','reverse_discovery','import')")
