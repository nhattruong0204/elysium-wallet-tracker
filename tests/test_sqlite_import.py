"""One-time SQLite -> PostgreSQL import: exactness, idempotency, dry-run, safety."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import CHAIN
from test_tracker import TOKEN, W1, W2, get_pos

from agora_tracker import sqlite_import
from agora_tracker.config import AlertConfig, ChainConfig, Config, LeaderboardConfig, TelegramConfig
from agora_tracker.notify import Notifier

FIXTURES = Path(__file__).parent / "fixtures"
BIG = 2**255 + 12345  # far beyond int64 / float precision
TOK2 = "0x" + "d0" * 20

# v0.2 = v0.1 + the columns P0 added in place
V0_2_ALTERS = """
ALTER TABLE events ADD COLUMN confidence TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE events ADD COLUMN hype_method TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE positions ADD COLUMN basis TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE positions ADD COLUMN realized_est_wei TEXT NOT NULL DEFAULT '0';
ALTER TABLE outbox ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE outbox ADD COLUMN next_attempt_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN last_error TEXT;
"""
# what P0's in-place upgrade ran right after the ALTERs (applied after rows exist)
V0_2_BACKFILL = """
UPDATE events SET confidence='estimated' WHERE approx=1;
UPDATE outbox SET status='sent' WHERE sent_at IS NOT NULL;
UPDATE outbox SET status='dead' WHERE sent_at IS NULL AND attempts >= 10;
"""


def make_sqlite(path: Path, version: str = "v0.1") -> Path:
    con = sqlite3.connect(path)
    con.executescript((FIXTURES / "schema_v0_1.sql").read_text())
    if version == "v0.2":
        con.executescript(V0_2_ALTERS)
    ev = "INSERT INTO events(key,block,ts,tx,wallet,kind,token,qty,hype_wei,approx) " \
         "VALUES(?,?,?,?,?,?,?,?,?,?)"
    con.execute(ev, (f"0xa:{W1}:{TOKEN}:BUY", 10, 1_800_000_600, "0xa", W1, "BUY", TOKEN,
                     str(BIG), str(10**18), 0))
    con.execute(ev, (f"0xb:{W1}:{TOKEN}:SELL", 20, 1_800_001_200, "0xb", W1, "SELL", TOKEN,
                     str(BIG // 2), str(8 * 10**17), 0))
    # v0.1 replay bug (T2): the same leg stored twice with different kinds
    con.execute(ev, (f"0xc:{W2}:{TOK2}:TRANSFER_OUT", 30, 1_800_001_800, "0xc", W2,
                     "TRANSFER_OUT", TOK2, "5", None, 1))
    con.execute(ev, (f"0xc:{W2}:{TOK2}:SELL", 30, 1_800_001_800, "0xc", W2, "SELL", TOK2,
                     "5", "7", 0))
    con.execute("INSERT INTO positions(wallet,token,qty,cost_wei,realized_wei,opened_at) "
                "VALUES(?,?,?,?,?,?)", (W1, TOKEN, str(BIG - BIG // 2), str(5 * 10**17),
                                        str(-3 * 10**17), 1_800_000_600))
    con.execute("INSERT INTO tokens VALUES(?,?,?)", (TOKEN, "KLEOS", 18))
    con.execute("INSERT INTO tokens VALUES(?,?,?)", (TOK2, "SIX", 6))
    con.execute("INSERT INTO contracts VALUES(?,?)", ("0x" + "b0" * 20, 1))
    con.execute("INSERT INTO traders VALUES(?,?,?,?,?,?,?,?)",
                (W1, 1, 63160.5, 24370.0, 89900.0, 165, 1, 1_800_002_000))
    con.execute("INSERT INTO traders VALUES(?,?,?,?,?,?,?,?)",
                (W2, 50, -10.25, 1.0, 2.0, 3, 0, 1_800_001_000))
    con.execute("INSERT INTO cluster_alerts VALUES(?,?)", (TOKEN, 1_800_000_700))
    con.execute("INSERT INTO outbox(key,text,created_at,sent_at,attempts) "
                "VALUES('delivered','sent text',1,2,1)")
    con.execute("INSERT INTO outbox(key,text,created_at,attempts) VALUES('gaveup','x',1,10)")
    con.execute("INSERT INTO outbox(key,text,created_at,attempts) VALUES('waiting','y',1,0)")
    con.execute("INSERT INTO kv VALUES('cursor','190000')")
    con.execute("INSERT INTO kv VALUES('heartbeat','1800002000')")
    if version == "v0.2":
        con.executescript(V0_2_BACKFILL)
        # worst case the importer must survive: a delivered alert whose status says pending
        con.execute("INSERT INTO outbox(key,text,created_at,sent_at,attempts,status) "
                    "VALUES('stale','s',1,2,1,'pending')")
    con.commit()
    con.close()
    return path


def cfg() -> Config:
    return Config(chain=ChainConfig(rpc_urls=["http://x"], chain_id=CHAIN),
                  leaderboard=LeaderboardConfig(enabled=False), alerts=AlertConfig(),
                  telegram=TelegramConfig())


def count(store, table: str) -> int:
    with store.engine.connect() as c:
        return c.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar()


@pytest.mark.parametrize("version", ["v0.1", "v0.2"])
def test_import_is_exact_and_complete(store, tmp_path, version, capsys):
    db = make_sqlite(tmp_path / "tracker.db", version)
    report = tmp_path / "report.json"
    rc = sqlite_import.run_import(str(db), cfg(), report_path=str(report), store=store)
    assert rc == 0, capsys.readouterr().out
    rep = json.loads(report.read_text())
    assert rep["result"] == "ok" and rep["errors"] == []
    assert rep["source_version"] == version
    assert rep["tables"]["trades"] == {"source_rows": 3, "already_present": 0, "inserted": 3}
    assert len(rep["duplicate_legs"]) == 1 and rep["duplicate_legs"][0]["kind"] == "SELL"

    p = get_pos(store, W1, TOKEN)
    assert (p.qty, p.cost_wei, p.realized_wei, p.basis) == \
        (BIG - BIG // 2, 5 * 10**17, -3 * 10**17, "legacy")
    with store.engine.connect() as c:
        qty = c.execute(sa.text("SELECT qty FROM trades WHERE tx_hash='0xa'")).scalar()
        confs = dict(c.execute(sa.text("SELECT tx_hash, confidence FROM trades")).all())
        statuses = dict(c.execute(sa.text(
            "SELECT dedup_key, status FROM notification_outbox")).all())
        decimals = c.execute(sa.text(
            "SELECT decimals FROM token_metadata WHERE address=:a"), {"a": TOK2}).scalar()
        hist = c.execute(sa.text("SELECT address, source, status, removed_at IS NOT NULL "
                                 "FROM wallet_watchlist ORDER BY address")).all()
    assert int(qty) == BIG
    assert confs == {"0xa": "legacy", "0xb": "legacy", "0xc": "estimated"}  # never 'high'
    want = {"delivered": "sent", "gaveup": "dead", "waiting": "pending"}
    if version == "v0.2":
        want["stale"] = "sent"
    assert statuses == want
    assert decimals == 6
    assert hist == [(W1, "leaderboard", "active", False), (W2, "import", "inactive", True)]
    assert store.get_cursor() == 190000
    assert set(store.top_traders()) == {W1}
    assert Path(rep["backup"]).exists()


def test_delivered_alerts_are_not_resent(store, tmp_path):
    db = make_sqlite(tmp_path / "tracker.db")
    assert sqlite_import.run_import(str(db), cfg(), store=store) == 0
    sent = []
    n = Notifier(store, TelegramConfig(), dry_run=True)
    n._send = lambda text, markup=None: (sent.append(text) or (True, "", False))
    n.drain(50)
    assert sent == ["y"]  # only the pending one


def test_dry_run_changes_nothing_but_validates(store, tmp_path):
    db = make_sqlite(tmp_path / "tracker.db")
    assert sqlite_import.run_import(str(db), cfg(), dry_run=True, store=store) == 0
    for table in ("trades", "wallet_positions", "notification_outbox", "token_metadata",
                  "leaderboard_snapshots", "indexer_checkpoints"):
        assert count(store, table) == 0, table


def test_rerun_is_idempotent(store, tmp_path, capsys):
    db = make_sqlite(tmp_path / "tracker.db")
    assert sqlite_import.run_import(str(db), cfg(), store=store) == 0
    before = {t: count(store, t) for t in ("trades", "transactions", "wallet_positions",
                                           "notification_outbox", "leaderboard_snapshots",
                                           "wallet_watchlist", "signals")}
    capsys.readouterr()
    assert sqlite_import.run_import(str(db), cfg(), store=store) == 0
    after = {t: count(store, t) for t in before}
    assert after == before
    assert "trades                 3        3         0" in capsys.readouterr().out


def test_validation_failure_rolls_back_everything(store, tmp_path, monkeypatch):
    db = make_sqlite(tmp_path / "tracker.db")
    real = sqlite_import.import_into

    def lossy(conn, st, src):
        src = {**src, "positions": src["positions"]}
        rep = real(conn, st, src)
        conn.execute(sa.text("UPDATE wallet_positions SET qty = qty - 1"))  # silent corruption
        return rep

    monkeypatch.setattr(sqlite_import, "import_into", lossy)
    assert sqlite_import.run_import(str(db), cfg(), store=store) == 1
    assert count(store, "trades") == 0 and count(store, "wallet_positions") == 0


def test_source_file_is_never_modified(store, tmp_path):
    db = make_sqlite(tmp_path / "tracker.db")
    before = db.read_bytes()
    sqlite_import.run_import(str(db), cfg(), store=store)
    assert db.read_bytes() == before


def test_refuses_non_tracker_file(store, tmp_path):
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x(a)").connection.commit()
    with pytest.raises(sqlite_import.ImportError_):
        sqlite_import.run_import(str(other), cfg(), store=store)
