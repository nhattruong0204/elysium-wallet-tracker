"""Dedicated consensus destination: private shared settings and durable send queue.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-11

Kept OUTSIDE public_api. The website's dashboard_ro role remains read-only and
cannot access either table. The independent consensus_rw role may access only
these objects, with no login password until the operator provisions it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

ROLE = "consensus_rw"
SCHEMA = "consensus_alerts"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                  NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5;
            END IF;
        END $$""")
    op.execute(f"ALTER ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
               "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5")
    op.execute(f"ALTER ROLE {ROLE} SET statement_timeout = '8s'")
    op.execute(f"ALTER ROLE {ROLE} SET lock_timeout = '2s'")
    op.execute(f"ALTER ROLE {ROLE} SET idle_in_transaction_session_timeout = '15s'")
    op.execute(f"ALTER ROLE {ROLE} SET search_path = {SCHEMA}")
    op.create_table(
        "settings", sa.Column("id", sa.SmallInteger, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("min_traders", sa.SmallInteger, nullable=False, server_default="3"),
        sa.Column("window_min", sa.SmallInteger, nullable=False, server_default="30"),
        sa.Column("min_net_hype", sa.Numeric(8, 2), nullable=False, server_default="1"),
        sa.Column("period", sa.Text, nullable=False, server_default="24h"),
        sa.Column("launchpad", sa.Text, nullable=False, server_default=""),
        sa.Column("independent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("id = 1", name="ck_cons_settings_singleton"),
        sa.CheckConstraint("min_traders IN (2,3,4,5,6,8,10)", name="ck_cons_traders"),
        sa.CheckConstraint("window_min IN (10,15,30,60,120,240)", name="ck_cons_window"),
        sa.CheckConstraint("min_net_hype IN (0,0.5,1,2,5,10,25)", name="ck_cons_min_net"),
        sa.CheckConstraint("period IN ('6h','24h','3d','7d')", name="ck_cons_period"),
        sa.CheckConstraint("launchpad ~ '^[a-z0-9]{0,24}$'", name="ck_cons_launchpad"),
        schema=SCHEMA,
    )
    op.execute(f"INSERT INTO {SCHEMA}.settings (id) VALUES (1) ON CONFLICT DO NOTHING")
    op.create_table(
        "settings_audit",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False, unique=True),
        sa.Column("saved_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("config", sa.JSON, nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "deliveries",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("event_key", sa.Text, nullable=False),
        sa.Column("token", sa.String(42), nullable=False),
        sa.Column("trigger_at", TS, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("buttons", sa.JSON, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("sent_at", TS),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('pending','sent','dead','cancelled')",
                           name="ck_cons_delivery_status"),
        sa.UniqueConstraint("version", "event_key", name="uq_cons_delivery_event"),
        schema=SCHEMA,
    )
    op.create_index("ix_cons_delivery_due", "deliveries", ["next_retry_at"],
                    postgresql_where=sa.text("status='pending'"), schema=SCHEMA)
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public_api FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public_api FROM {ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {ROLE}")
    op.execute(f"GRANT SELECT, UPDATE ON {SCHEMA}.settings TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON {SCHEMA}.settings_audit TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.deliveries TO {ROLE}")
    op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO {ROLE}")
    op.execute(f"DO $$ BEGIN EXECUTE format("
               f"'GRANT CONNECT ON DATABASE %I TO {ROLE}', current_database()); END $$")


def downgrade() -> None:
    # Destructive: delivery history is deleted. Take an offsite backup before downgrade.
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM {ROLE}")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{ROLE}') THEN
                DROP OWNED BY {ROLE};
                BEGIN
                    DROP ROLE {ROLE};
                EXCEPTION WHEN dependent_objects_still_exist THEN
                    ALTER ROLE {ROLE} NOLOGIN;
                END;
            END IF;
        END $$""")
