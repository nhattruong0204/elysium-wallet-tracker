"""Process wiring for `python -m agora_tracker --web`."""

from __future__ import annotations

import dataclasses
import logging
import os
import time

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from ..config import Config
from ..dburl import redact_db_url, resolve_database_url
from .app import create_app
from .reports import PublicReports, StoredMarket
from .service import Dashboard
from .usd import FxPolicy

log = logging.getLogger("agora_tracker.web")


def make_web_engine(url: str, cfg: Config) -> Engine:
    w = cfg.web
    return sa.create_engine(
        url, pool_size=w.db_pool_size, max_overflow=w.db_max_overflow, pool_pre_ping=True,
        pool_recycle=600, pool_timeout=6,
        connect_args={"application_name": "elysium-web", "connect_timeout": 5,
                      # belt and braces: the role already sets these
                      "options": ("-c search_path=public_api -c default_transaction_read_only=on "
                                  f"-c statement_timeout={w.query_timeout_ms}")})


def assert_least_privilege(engine: Engine) -> None:
    """Refuse to serve with a database role that could do more than read public_api.
    Catches the classic mistake of pointing the website at the tracker's admin URL."""
    with engine.connect() as c:
        who, sup, createdb, createrole = c.execute(sa.text(
            "SELECT current_user, r.rolsuper, r.rolcreatedb, r.rolcreaterole "
            "FROM pg_roles r WHERE r.rolname = current_user")).one()
        # any readable relation in schema public (by oid: needs no USAGE on the schema)
        raw_ok = c.execute(sa.text(
            "SELECT count(*) FROM pg_class r JOIN pg_namespace n ON n.oid = r.relnamespace "
            "WHERE n.nspname = 'public' AND r.relkind IN ('r', 'v', 'm', 'p') "
            "AND has_table_privilege(current_user, r.oid, 'SELECT')")).scalar()
        views = c.execute(sa.text("SELECT to_regclass('public_api.trades') IS NOT NULL")).scalar()
    if sup or createdb or createrole or raw_ok:
        raise SystemExit(f"refusing to start: database role {who!r} is not the read-only "
                         "dashboard role (superuser/createdb/createrole or can read raw tables)")
    if not views:
        raise SystemExit("refusing to start: schema public_api is missing (run --migrate)")


def build(cfg: Config, url: str | None = None) -> tuple[object, Engine]:
    url = resolve_database_url(url)
    engine = make_web_engine(url, cfg)
    for attempt in range(1, 31):             # the database may still be starting
        try:
            assert_least_privilege(engine)
            break
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            if attempt == 30:
                raise
            log.warning("database not ready (%s), retry %d/30", type(e).__name__, attempt)
            time.sleep(2)
    log.info("web: connected as %s", redact_db_url(url))
    ui = dataclasses.replace(cfg.ui, query_timeout_ms=cfg.web.query_timeout_ms)
    market = StoredMarket(engine, cfg.chain.chain_id, cfg.web.market_max_age_s)
    reports = PublicReports(engine, cfg.chain.chain_id, ui, value_unit="HYPE", market=market,
                            collector_interval_s=cfg.collector.interval_s)
    reports.fx_policy = FxPolicy.from_web(cfg.web)
    reports.hidden_tokens = tuple(cfg.alerts.mute_tokens)
    from ..consensus_delivery import ConsensusSettingsStore, make_settings_engine
    writer_pw = os.environ.get("CONSENSUS_DB_PASSWORD", "")
    admin_key = os.environ.get("CONSENSUS_ADMIN_KEY", "")
    if bool(writer_pw) != bool(admin_key):
        raise SystemExit("Both CONSENSUS_DB_PASSWORD and CONSENSUS_ADMIN_KEY are required")
    if admin_key and len(admin_key) < 32:
        raise SystemExit("CONSENSUS_ADMIN_KEY must contain at least 32 characters")
    writer_url = (str(sa.engine.make_url(url).set(
        username="consensus_rw", password=writer_pw)) if admin_key else "")
    writer = ConsensusSettingsStore(make_settings_engine(writer_url)) if admin_key else None
    return create_app(cfg, Dashboard(reports, cfg), engine, consensus_settings=writer), engine


def serve(cfg: Config, host: str | None = None, port: int | None = None) -> None:
    import uvicorn
    app, _ = build(cfg)
    uvicorn.run(app, host=host or cfg.web.host, port=port or cfg.web.port, workers=1,  # type: ignore[arg-type]
                log_level="info", access_log=True, server_header=False, date_header=True,
                timeout_keep_alive=15, limit_concurrency=100, proxy_headers=False)
