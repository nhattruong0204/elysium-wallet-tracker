"""PostgreSQL test fixtures.

Set TEST_DATABASE_URL to a *dedicated, disposable* database whose name contains "test",
e.g. postgresql+psycopg://tracker:tracker@127.0.0.1/tracker_test. The schema is dropped and
re-migrated once per session, and every table is truncated before each test.
Tests that need PostgreSQL are skipped when the variable is not set.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

from agora_tracker import schema
from agora_tracker.db import Store, make_engine
from agora_tracker.migrate import set_dashboard_password, upgrade

TEST_URL = os.environ.get("TEST_DATABASE_URL")
# The role is created WITHOUT a password (migration 0004); like production, the tests provision
# one. CI's Postgres uses scram-sha-256 over TCP, so a passwordless login cannot work there.
ROLE_PASSWORD = "test-dashboard-password-0123456789"


def role_url() -> str:
    """URL that logs in as the read-only dashboard role."""
    return sa.engine.make_url(TEST_URL).set(
        username="dashboard_ro", password=ROLE_PASSWORD).render_as_string(hide_password=False)
CHAIN = 99901


@pytest.fixture(scope="session")
def pg_engine():
    if not TEST_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    if "test" not in TEST_URL.rsplit("/", 1)[-1]:
        raise RuntimeError("refusing to wipe a database whose name does not contain 'test'")
    eng = make_engine(TEST_URL)
    with eng.begin() as c:
        c.execute(sa.text("DROP SCHEMA IF EXISTS public_api CASCADE"))
        c.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(sa.text("CREATE SCHEMA public"))
        c.execute(sa.text("GRANT USAGE ON SCHEMA public TO PUBLIC"))   # as in a fresh PG16
    upgrade(eng)
    set_dashboard_password(eng, ROLE_PASSWORD)
    yield eng
    eng.dispose()


def truncate_all(engine) -> None:
    names = ", ".join(tbl.name for tbl in schema.metadata.sorted_tables)
    with engine.begin() as c:
        c.execute(sa.text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def store(pg_engine, monkeypatch) -> Store:
    truncate_all(pg_engine)
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    s = Store(pg_engine, chain_id=CHAIN)
    yield s
    s.release_indexer_lock()
