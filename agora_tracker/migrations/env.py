"""Alembic environment. The database URL comes from DATABASE_URL (or -x url=...), never
from a file, so credentials stay out of the repository."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from agora_tracker.dburl import resolve_database_url
from agora_tracker.schema import metadata

config = context.config
target_metadata = metadata


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True)
    return resolve_database_url(x.get("url") or config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:  # programmatic use (tests, --migrate) passes a connection
        context.configure(connection=connectable, target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
