"""Programmatic Alembic access (used by --migrate, the health check and tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config() -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade(engine: Engine, revision: str = "head") -> None:
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def downgrade(engine: Engine, revision: str) -> None:
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def set_dashboard_password(engine: Engine, password: str) -> None:
    """Give the read-only dashboard role a scram-sha-256 password (idempotent, rotatable).
    The role is created without one by migration 0004, so it cannot log in before this."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", password):
        raise ValueError("DASHBOARD_DB_PASSWORD must be 24-128 characters of [A-Za-z0-9_-] "
                         "(e.g. `openssl rand -hex 24`)")
    with engine.begin() as conn:
        # DDL cannot take bind parameters; the charset check above makes inlining safe
        conn.exec_driver_sql(f"ALTER ROLE dashboard_ro PASSWORD '{password}'")


def set_consensus_password(engine: Engine, password: str) -> None:
    """Provision the separate, narrowly privileged consensus_rw role.

    The role is created without LOGIN credentials by migration 0011; setting the
    password never grants it access to tracker wallets, outbox, or public raw data.
    """
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password):
        raise ValueError("CONSENSUS_DB_PASSWORD must be 32-128 safe characters")
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER ROLE consensus_rw PASSWORD '{password}'")
