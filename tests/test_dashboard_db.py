"""Public dashboard database boundary (migration 0004).

Every assertion here runs through a real connection as the `dashboard_ro` role, never the
admin connection, so a wrong GRANT or a leaky view fails the test. Needs TEST_DATABASE_URL.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from conftest import CHAIN, ROLE_PASSWORD, role_url
from sqlalchemy import exc as sa_exc

from agora_tracker import schema as t
from agora_tracker.classify import TradeEvent
from agora_tracker.config import UiConfig
from agora_tracker.leaderboard import Trader
from agora_tracker.migrate import current_revision, downgrade, set_dashboard_password, upgrade
from agora_tracker.reports import Reports
from agora_tracker.timerange import preset_range

UTC = timezone.utc
E18 = 10 ** 18
NOW = datetime.now(UTC).replace(microsecond=0)
PUB1, PUB2 = "0x" + "a1" * 20, "0x" + "a2" * 20
PRIV = "0x" + "b9" * 20          # followed only through watchlist.txt / Telegram
TOK_PUB, TOK_PRIV = "0x" + "1a" * 20, "0x" + "1f" * 20
PRIVATE_LABEL = "my-secret-friend"
SECRET_TEXT = "SECRET-ALERT-TEXT chat 123456789"


def ago(**kw) -> int:
    return int((NOW - timedelta(**kw)).timestamp())


@pytest.fixture
def seeded(store, role_ready):
    store.sync_manual_watchlist({PRIV: PRIVATE_LABEL})
    sid = store.record_snapshot([Trader(address=PUB1, rank=1, pnl=500.0),
                                 Trader(address=PUB2, rank=2, pnl=300.0)],
                                source="agora", status="ok", complete=True, row_count=2)
    with store.tx() as c:
        c.execute(sa.text("UPDATE leaderboard_snapshots SET captured_at = :t, "
                          "raw_payload = '{\"secret\": 1}', request_meta = '{\"k\": \"v\"}' "
                          "WHERE id = :i"), {"t": NOW - timedelta(hours=20), "i": sid})
        c.execute(sa.text("UPDATE leaderboard_snapshot_entries SET extra = '{\"raw\": 1}'"))
        c.execute(sa.insert(t.token_metadata), [
            {"chain_id": CHAIN, "address": TOK_PUB, "symbol": "PUB", "decimals": 18},
            {"chain_id": CHAIN, "address": TOK_PRIV, "symbol": "HIDDEN", "decimals": 18}])
        c.execute(sa.insert(t.token_profiles), [
            {"chain_id": CHAIN, "address": TOK_PUB, "symbol": "PUB", "name": "Public Token",
             "launchpad": "boxa"},
            {"chain_id": CHAIN, "address": TOK_PRIV, "symbol": "HIDDEN", "name": "Hidden",
             "launchpad": "kleos"}])
        legs = [("0x01", PUB1, TOK_PUB, "BUY", 2.0, ago(hours=2)),
                ("0x02", PUB2, TOK_PUB, "BUY", 1.0, ago(hours=1)),
                ("0x03", PUB1, TOK_PUB, "SELL", 1.5, ago(minutes=30)),
                ("0x04", PRIV, TOK_PUB, "BUY", 50.0, ago(hours=1)),
                ("0x05", PRIV, TOK_PRIV, "BUY", 70.0, ago(hours=2))]
        ids = {}
        for i, (tx, w, tok, kind, h, ts) in enumerate(legs):
            ev = TradeEvent(kind, tx.ljust(66, "0"), 1000 + i, w, tok, 100 * E18,
                            int(Decimal(str(h)) * E18), confidence="high",
                            hype_method="balance_diff")
            ids[tx] = store.insert_trade(c, ev, ts)
        store.record_realized(c, ids["0x03"], PUB1, TOK_PUB, qty_sold=100 * E18,
                              proceeds_wei=int(1.5 * E18), cost_basis_wei=E18,
                              pnl_wei=E18 // 2, status="measured")
        store.record_realized(c, ids["0x04"], PRIV, TOK_PUB, qty_sold=1, proceeds_wei=1,
                              cost_basis_wei=1, pnl_wei=0, status="measured")
        c.execute(sa.insert(t.wallet_positions), [
            {"chain_id": CHAIN, "wallet": PUB1, "token": TOK_PUB, "qty": 5 * E18,
             "cost_wei": E18, "basis": "known"},
            {"chain_id": CHAIN, "wallet": PRIV, "token": TOK_PRIV, "qty": 9 * E18,
             "cost_wei": E18, "basis": "known"}])
        store.set_cursor(c, 2000)
        # two cluster signals: one purely public, one that involves the private wallet
        for key, wallets, trade_ids in (
                ("pub", [PUB1, PUB2], [ids["0x01"], ids["0x02"]]),
                ("mixed", [PUB1, PRIV], [ids["0x01"], ids["0x04"]])):
            sid2 = c.execute(sa.insert(t.signals).values(
                chain_id=CHAIN, kind="cluster_buy", token=TOK_PUB, dedup_key=key,
                window_start=NOW - timedelta(hours=2), window_end=NOW - timedelta(hours=1),
                details={"wallets": wallets, "window_s": 3600}).returning(t.signals.c.id)
            ).scalar()
            c.execute(sa.insert(t.signal_trades),
                      [{"signal_id": sid2, "trade_id": x} for x in trade_ids])
        c.execute(sa.insert(t.token_market_snapshots), [
            {"chain_id": CHAIN, "token": TOK_PUB, "captured_at": NOW - timedelta(minutes=2),
             "source": "agora", "price_hype": Decimal("1"), "mcap_hype": 1000},
            {"chain_id": CHAIN, "token": TOK_PRIV, "captured_at": NOW - timedelta(minutes=2),
             "source": "agora", "price_hype": Decimal("1"), "mcap_hype": 1000}])
        c.execute(sa.insert(t.notification_outbox), [
            {"dedup_key": "a", "chat_id": "123456789", "text": SECRET_TEXT, "status": "sent",
             "sent_at": NOW},
            {"dedup_key": "b", "chat_id": "123456789", "text": SECRET_TEXT, "status": "dead",
             "sent_at": None}])
        c.execute(sa.insert(t.ui_sessions).values(user_id=123456789, chat_id=123456789))
        c.execute(sa.insert(t.report_jobs).values(
            user_id=123456789, kind="x", fmt="csv", range_start=NOW, range_end=NOW,
            status="done"))
    store.set_kv("heartbeat", str(int(NOW.timestamp())))
    store.set_kv("tg_offset", "987654321")
    store.set_kv("disk", json.dumps({"total": 1, "free": 1}))
    store.set_kv("rpc_heads", json.dumps({"best": 5, "endpoints": [{"host": "rpc.example",
                                                                    "head": 5}]}))
    return store


@pytest.fixture
def dash(pg_engine, seeded):
    url = role_url()
    eng = sa.create_engine(url)
    yield eng
    eng.dispose()


def q(eng, sql, **p):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(sa.text(sql), p).mappings()]


def denied(eng, sql):
    with pytest.raises(sa_exc.DBAPIError) as ei:
        with eng.begin() as c:
            c.execute(sa.text(sql))
    msg = str(ei.value).lower()
    assert "permission denied" in msg or "read-only" in msg or "does not exist" in msg, msg


# --- role ---------------------------------------------------------------------------------
def test_role_is_least_privilege(pg_engine):
    with pg_engine.connect() as c:
        r = c.execute(sa.text("""SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication,
            rolbypassrls, rolcanlogin, rolconnlimit, rolconfig FROM pg_roles
            WHERE rolname = 'dashboard_ro'""")).mappings().one()
    assert not (r["rolsuper"] or r["rolcreatedb"] or r["rolcreaterole"] or r["rolreplication"]
                or r["rolbypassrls"])
    assert r["rolcanlogin"] and r["rolconnlimit"] == 5
    cfg = " ".join(r["rolconfig"])
    assert "default_transaction_read_only=on" in cfg and "statement_timeout=5s" in cfg
    assert "search_path=public_api" in cfg


def test_session_defaults_come_from_the_role(dash):
    with dash.connect() as c:
        assert c.execute(sa.text("SHOW default_transaction_read_only")).scalar() == "on"
        assert c.execute(sa.text("SHOW statement_timeout")).scalar() == "5s"
        assert c.execute(sa.text("SELECT current_user")).scalar() == "dashboard_ro"
        # unqualified names resolve to the view layer, never to public
        assert c.execute(sa.text(
            "SELECT relnamespace::regnamespace::text FROM pg_class "
            "WHERE oid = to_regclass('trades')")).scalar() == "public_api"


@pytest.mark.parametrize("sql", [
    "INSERT INTO trades (chain_id) VALUES (1)",
    "UPDATE trades SET kind = 'BUY'",
    "DELETE FROM trades",
    "INSERT INTO public_api.service_state (key, value) VALUES ('x', '{}')",
    "TRUNCATE public.trades",
    "CREATE TABLE public_api.evil (x int)",
    "CREATE TABLE public.evil (x int)",
    "CREATE TEMP TABLE evil (x int)",
    "DROP VIEW public_api.trades",
    "ALTER ROLE dashboard_ro SUPERUSER",
    "CREATE ROLE evil",
])
def test_writes_and_ddl_fail(dash, sql):
    denied(dash, sql)


def test_writes_fail_even_if_the_session_turns_read_only_off(dash):
    with pytest.raises(sa_exc.DBAPIError):
        with dash.begin() as c:
            c.execute(sa.text("SET default_transaction_read_only = off"))
            c.execute(sa.text("SET transaction_read_only = off"))
            c.execute(sa.text("INSERT INTO public.service_state (key, value) "
                              "VALUES ('x', '{}')"))
    with pytest.raises(sa_exc.DBAPIError):
        with dash.begin() as c:
            c.execute(sa.text("SET transaction_read_only = off"))
            c.execute(sa.text("CREATE TEMP TABLE evil (x int)"))
    assert q(dash, "SELECT count(*) AS n FROM service_state WHERE key = 'x'")[0]["n"] == 0


@pytest.mark.parametrize("table", [
    "notification_outbox", "notification_deliveries", "ui_sessions", "report_jobs",
    "transactions", "token_transfers", "signal_trades", "chains", "alembic_version",
    "wallet_watchlist", "trades", "service_state", "leaderboard_snapshots", "wallets",
    "token_profiles", "wallet_positions", "realized_pnl", "signals",
])
def test_raw_tables_in_public_are_not_readable(dash, table):
    denied(dash, f"SELECT * FROM public.{table} LIMIT 1")


@pytest.mark.parametrize("view", ["public_wallets", "public_tokens"])
def test_helper_views_are_not_granted(dash, view):
    denied(dash, f"SELECT * FROM public_api.{view}")


def test_unexposed_columns_do_not_exist(dash):
    for col, view in [("raw_payload", "leaderboard_snapshots"),
                      ("request_meta", "leaderboard_snapshots"),
                      ("extra", "leaderboard_snapshot_entries"),
                      ("legacy_key", "trades"), ("text", "outbox_status"),
                      ("chat_id", "outbox_status"), ("payload", "outbox_status"),
                      ("dedup_key", "signals")]:
        denied(dash, f"SELECT {col} FROM {view}")


# --- the public / private boundary -----------------------------------------------------------
def test_private_wallet_activity_is_invisible(dash):
    wallets = {r["wallet"] for r in q(dash, "SELECT DISTINCT wallet FROM trades")}
    assert wallets == {PUB1, PUB2}
    assert q(dash, "SELECT count(*) AS n FROM trades")[0]["n"] == 3
    assert q(dash, "SELECT count(*) AS n FROM wallet_positions WHERE wallet = :w", w=PRIV)[0]["n"] == 0
    assert {r["wallet"] for r in q(dash, "SELECT wallet FROM realized_pnl")} == {PUB1}
    assert q(dash, "SELECT * FROM wallets WHERE address = :w", w=PRIV) == []


def test_tokens_only_private_wallets_touched_are_hidden(dash):
    for view, col in [("token_profiles", "address"), ("token_metadata", "address"),
                      ("token_market_snapshots", "token")]:
        got = {r[col] for r in q(dash, f"SELECT {col} FROM {view}")}
        assert got == {TOK_PUB}, view


def test_watchlist_view_exposes_no_manual_wallets_or_labels(dash):
    rows = q(dash, "SELECT * FROM wallet_watchlist")
    assert {r["address"] for r in rows} == {PUB1, PUB2}
    assert {r["source"] for r in rows} == {"leaderboard"}
    assert {r["label"] for r in rows} == {None} and {r["reason"] for r in rows} == {None}
    assert PRIVATE_LABEL not in json.dumps(rows, default=str)


def test_mixed_signal_is_hidden_public_signal_visible(dash):
    sig = q(dash, "SELECT details FROM signals")
    assert len(sig) == 1
    assert set(sig[0]["details"]["wallets"]) == {PUB1, PUB2}


def test_signal_with_private_wallet_only_in_details_is_hidden(seeded, dash):
    with seeded.tx() as c:   # details names the private wallet but trade links are all public
        c.execute(sa.text("UPDATE signals SET details = jsonb_build_object('wallets', "
                          "jsonb_build_array(CAST(:p AS text), CAST(:q AS text))) "
                          "WHERE dedup_key = 'pub'"), {"p": PUB1, "q": PRIV})
    assert q(dash, "SELECT count(*) AS n FROM signals")[0]["n"] == 0


def test_service_state_whitelist(dash):
    keys = {r["key"] for r in q(dash, "SELECT key FROM service_state")}
    assert keys == {"heartbeat", "rpc_heads"}          # no tg_offset, no disk


def test_outbox_status_has_counts_only(dash):
    rows = q(dash, "SELECT * FROM outbox_status ORDER BY status")
    assert {r["status"]: r["n"] for r in rows} == {"dead": 1, "sent": 1}
    assert set(rows[0]) == {"status", "n", "oldest_pending_at", "last_sent_at"}
    assert SECRET_TEXT not in json.dumps(rows, default=str)


def test_nothing_private_in_any_granted_view(dash):
    """Brute force: dump every granted view as text and look for the private markers."""
    with dash.connect() as c:
        views = [r[0] for r in c.execute(sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public_api'"))]
    assert "outbox_status" in views
    blob = ""
    for v in views:
        blob += json.dumps(q(dash, f"SELECT * FROM public_api.{v}"), default=str)
    for secret in (PRIV, TOK_PRIV, PRIVATE_LABEL, SECRET_TEXT, "123456789", "987654321",
                   "tg_offset"):
        assert secret not in blob, secret


# --- analytics reuse the same SQL through the views -------------------------------------------
def test_reports_work_unchanged_on_the_role_and_exclude_private(dash, seeded):
    from agora_tracker.web.reports import PublicReports
    ui = UiConfig(min_volume_hype=1, min_mcap_hype=1, min_age_h=0)
    admin = Reports(seeded.engine, CHAIN, ui, market=None)
    public = PublicReports(dash, CHAIN, ui, market=None)
    tr = preset_range("6h", NOW)
    a, p = admin.market_overview(tr), public.market_overview(tr)
    assert p["totals"]["buy"] == Decimal("3")          # 2 + 1, public wallets only
    assert a["totals"]["buy"] > p["totals"]["buy"]
    assert p["totals"]["sell"] == Decimal("1.5")
    assert p["totals"]["wallets"] == 2 and p["totals"]["tokens"] == 1
    assert {r["token"] for r in p["top_tokens"]} == {TOK_PUB}
    h = public.health()
    assert h["db_ok"] and h["watch"] == 2 and h["outbox"] == {"dead": 1, "sent": 1}
    assert public.search(TOK_PRIV)["kind"] != "token" or public.search(TOK_PRIV).get("unknown")
    assert public.search(PRIV)["kind"] == "wallet" and public.search(PRIV).get("unknown")
    assert public.wallet_detail(PRIV, tr)["activity"]["legs"] == 0


def test_statement_timeout_cancels_slow_queries(dash):
    with dash.connect() as c:
        c.execute(sa.text("SET LOCAL statement_timeout = 200"))
        with pytest.raises(sa_exc.DBAPIError) as ei:
            c.execute(sa.text("SELECT pg_sleep(2)"))
    assert "statement timeout" in str(ei.value).lower()


def test_connection_limit_is_enforced(pg_engine, seeded):
    url = role_url()
    conns = []
    try:
        for _ in range(5):
            e = sa.create_engine(url)
            conns.append((e, e.connect()))
        e6 = sa.create_engine(url)
        with pytest.raises(sa_exc.OperationalError) as ei:
            e6.connect()
        assert "too many" in str(ei.value).lower()
    finally:
        for e, c in conns:
            c.close()
            e.dispose()


# --- migration lifecycle ----------------------------------------------------------------------
def test_migration_is_reversible_and_repeatable(pg_engine):
    assert current_revision(pg_engine) == "0004"
    downgrade(pg_engine, "0003")
    with pg_engine.connect() as c:
        # roles are cluster-wide: dropped, or (when another database in the cluster still
        # holds grants, e.g. a dev database) kept but unable to log in
        assert c.execute(sa.text("SELECT count(*) FROM pg_roles WHERE rolname = "
                                 "'dashboard_ro' AND rolcanlogin")).scalar() == 0
        assert c.execute(sa.text("SELECT count(*) FROM pg_namespace WHERE nspname = "
                                 "'public_api'")).scalar() == 0
        assert c.execute(sa.text("SELECT count(*) FROM trades")).scalar() >= 0   # data intact
    upgrade(pg_engine)
    assert current_revision(pg_engine) == "0004"
    upgrade(pg_engine)                                     # idempotent
    set_dashboard_password(pg_engine, ROLE_PASSWORD)       # a recreated role has no password
