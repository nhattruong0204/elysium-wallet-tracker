"""PostgreSQL-specific behaviour: migrations, precision, locking, outbox claiming, backup."""

from __future__ import annotations

import os
import shutil
import subprocess
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from conftest import CHAIN, TEST_URL, truncate_all
from test_tracker import E18, TOKEN, W1, FakeChain, build, get_pos

from agora_tracker import schema
from agora_tracker.classify import TradeEvent
from agora_tracker.db import Position, Store
from agora_tracker.migrate import current_revision, downgrade, head_revision, upgrade

U256_MAX = 2**256 - 1


def test_migrations_up_down_up_and_no_drift(pg_engine):
    assert current_revision(pg_engine) == head_revision()
    downgrade(pg_engine, "base")
    with pg_engine.connect() as c:
        left = sa.inspect(c).get_table_names()
    assert left == ["alembic_version"] or left == []
    upgrade(pg_engine)
    assert current_revision(pg_engine) == head_revision()
    with pg_engine.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), schema.metadata)
    assert diff == [], f"schema.py and migrations differ: {diff}"


def test_uint256_quantities_round_trip_exactly(store):
    c = FakeChain()
    _, f, e = build(store, c)
    ev = TradeEvent("BUY", "0x" + "ab" * 32, 7, W1, TOKEN, U256_MAX, U256_MAX - 1)
    with store.tx() as conn:
        tid = store.insert_trade(conn, ev, 1_800_000_000)
        store.put_position(conn, W1, TOKEN, Position(qty=U256_MAX, cost_wei=U256_MAX,
                                                     realized_wei=-(U256_MAX - 5),
                                                     realized_est_wei=U256_MAX))
        store.record_realized(conn, tid, W1, TOKEN, qty_sold=U256_MAX, proceeds_wei=U256_MAX,
                              cost_basis_wei=1, pnl_wei=-(U256_MAX - 1), status="measured")
    p = get_pos(store, W1, TOKEN)
    assert (p.qty, p.cost_wei, p.realized_wei, p.realized_est_wei) == \
        (U256_MAX, U256_MAX, -(U256_MAX - 5), U256_MAX)
    with store.engine.connect() as conn:
        qty, hype = conn.execute(sa.text("SELECT qty, hype_wei FROM trades")).one()
        pnl = conn.execute(sa.text("SELECT pnl_wei FROM realized_pnl")).scalar()
    assert (qty, hype, pnl) == (Decimal(U256_MAX), Decimal(U256_MAX - 1), Decimal(-(U256_MAX - 1)))


def test_usd_values_are_decimal_not_float(store):
    from agora_tracker.leaderboard import Trader
    store.record_snapshot([Trader(address=W1, rank=1, pnl=0.1, worth=12345678.123456,
                                  volume=None)], source="t", status="ok", complete=True,
                          row_count=1)
    with store.engine.connect() as c:
        pnl, worth = c.execute(sa.text(
            "SELECT pnl_usd, worth_usd FROM leaderboard_snapshot_entries")).one()
    assert pnl == Decimal("0.100000") and worth == Decimal("12345678.123456")


def test_address_and_chain_constraints(store):
    with pytest.raises(sa.exc.IntegrityError):
        with store.engine.begin() as c:
            c.execute(sa.text("INSERT INTO wallets(chain_id, address) VALUES (:c, :a)"),
                      {"c": CHAIN, "a": "0xABCDEF" + "0" * 34})  # uppercase rejected
    with pytest.raises(sa.exc.IntegrityError):
        with store.engine.begin() as c:
            c.execute(sa.text("INSERT INTO wallets(chain_id, address) VALUES (1, :a)"),
                      {"a": W1})  # unknown chain rejected
    with pytest.raises(sa.exc.IntegrityError):
        with store.tx() as c:  # negative quantity rejected
            store.insert_trade(c, TradeEvent("BUY", "0x01", 1, W1, TOKEN, -1, 1), None)


def test_only_one_indexer_per_chain(store, pg_engine):
    other = Store(pg_engine, chain_id=CHAIN)
    assert store.acquire_indexer_lock() is True
    assert other.acquire_indexer_lock() is False
    assert store.indexer_lock_alive()
    store.release_indexer_lock()
    assert other.acquire_indexer_lock() is True
    other.release_indexer_lock()


def test_concurrent_notifiers_never_claim_the_same_alert(store):
    store.enqueue_now("a", "first")
    store.enqueue_now("b", "second")
    with store.tx() as c1, store.tx() as c2:
        r1 = store.claim_due_alert(c1)
        r2 = store.claim_due_alert(c2)  # SKIP LOCKED: gets the next row, does not wait
        assert r1 is not None and r2 is not None and r1["id"] != r2["id"]
        store.mark_sent(c1, r1["id"])
        store.mark_sent(c2, r2["id"])
    assert store.outbox_counts() == {"sent": 2}


def test_snapshot_history_is_kept_and_watchlist_tracks_membership(store):
    from agora_tracker.leaderboard import Trader
    a, b = W1, "0x" + "b1" * 20
    store.record_snapshot([Trader(a, 1, 10.0), Trader(b, 2, 5.0)], source="agora",
                          status="ok", complete=True, row_count=2)
    store.record_snapshot([Trader(a, 1, 12.0)], source="agora", status="ok",
                          complete=True, row_count=1)
    with store.engine.connect() as c:
        snaps = c.execute(sa.text("SELECT COUNT(*) FROM leaderboard_snapshots")).scalar()
        rows = c.execute(sa.text("SELECT address, status, removed_at IS NOT NULL "
                                 "FROM wallet_watchlist ORDER BY address")).all()
    assert snaps == 2
    assert rows == [(a, "active", False), (b, "inactive", True)]  # b kept, marked inactive
    assert set(store.top_traders()) == {a}


def test_evidence_is_stored_with_trades(store):
    c = FakeChain()
    c.add_tx("0xa", 10, W1, value=E18, transfers=[("0x" + "b0" * 20, W1, 1000 * E18)],
             bal={W1: (10 * E18, 9 * E18 - 10**15)})
    _, f, e = build(store, c)
    res = f.scan(1, 40, {W1})
    e.apply_range(res, 40)
    with store.engine.connect() as conn:
        tx = conn.execute(sa.text("SELECT from_address, value_wei, fee_wei, status "
                                  "FROM transactions")).one()
        tr = conn.execute(sa.text("SELECT log_index, amount FROM token_transfers")).one()
        blk = conn.execute(sa.text("SELECT block_number, block_time FROM indexed_blocks")).one()
    assert tx == (W1, Decimal(E18), Decimal(10**15), 1)
    assert tr == (0, Decimal(1000 * E18))
    assert blk[0] == 10 and blk[1].tzinfo is not None


def _pg_tools_usable() -> bool:
    if not (shutil.which("pg_dump") and shutil.which("pg_restore") and TEST_URL):
        return False
    out = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True).stdout
    client = int(out.split()[-1].split(".")[0])
    eng = sa.create_engine(TEST_URL)
    with eng.connect() as c:
        server = int(c.execute(sa.text("SHOW server_version_num")).scalar()) // 10000
    eng.dispose()
    return client >= server


@pytest.mark.skipif(not _pg_tools_usable(), reason="pg_dump/pg_restore missing or older "
                                                   "than the server")
def test_backup_and_restore_round_trip(store, tmp_path):
    """The documented backup procedure (pg_dump -Fc, pg_restore --clean) restores data."""
    c = FakeChain()
    c.add_tx("0xa", 10, W1, value=E18, transfers=[("0x" + "b0" * 20, W1, 7 * E18)],
             bal={W1: (10 * E18, 9 * E18 - 10**15)})
    _, f, e = build(store, c)
    e.apply_range(f.scan(1, 40, {W1}), 40)
    libpq = sa.engine.make_url(TEST_URL).set(drivername="postgresql").render_as_string(
        hide_password=False)
    dump = tmp_path / "tracker.dump"
    env = {**os.environ}
    subprocess.run(["pg_dump", "-Fc", "-f", str(dump), libpq], check=True, env=env)
    truncate_all(store.engine)
    assert store.get_cursor() is None
    subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", libpq,
                    str(dump)], check=True, env=env)
    assert store.get_cursor() == 40
    assert get_pos(store, W1, TOKEN).qty == 7 * E18


def test_mixed_full_and_stub_transactions_keep_all_columns(store):
    """Regression: a multi-row INSERT must not drop columns missing from the first row."""
    with store.tx() as c:
        store.insert_evidence(c, [{"tx_hash": "0x01", "block_number": 1},
                                  {"tx_hash": "0x02", "block_number": 2, "from_address": W1,
                                   "value_wei": 5, "status": 1}], [])
    with store.engine.connect() as conn:
        row = conn.execute(sa.text("SELECT from_address, value_wei, status FROM transactions "
                                   "WHERE tx_hash='0x02'")).one()
    assert row == (W1, Decimal(5), 1)
