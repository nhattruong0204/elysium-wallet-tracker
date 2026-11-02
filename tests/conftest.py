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
from agora_tracker.migrate import upgrade

TEST_URL = os.environ.get("TEST_DATABASE_URL")
CHAIN = 99901


@pytest.fixture(scope="session")
def pg_engine():
    if not TEST_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    if "test" not in TEST_URL.rsplit("/", 1)[-1]:
        raise RuntimeError("refusing to wipe a database whose name does not contain 'test'")
    eng = make_engine(TEST_URL)
    with eng.begin() as c:
        c.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(sa.text("CREATE SCHEMA public"))
    upgrade(eng)
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
