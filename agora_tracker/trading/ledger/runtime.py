"""Startup wiring: build the ledger from configuration, or nothing when the flag is off."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

import sqlalchemy as sa

from ...config import Config
from .core import Actor, OrderLedger, make_ledger_engine

log = logging.getLogger(__name__)
ROLE = "trading_rw"


class LedgerConfigError(RuntimeError):
    pass


def owner_ids(cfg: Config, environ: Mapping[str, str] = os.environ) -> frozenset[int]:
    """Owners may change settings and resume trading: the alert chat's owner (TG_CHAT_ID) and
    any ids in TRADING_OWNER_IDS. Being an allowed bot user (TG_ADMIN_IDS) is not enough."""
    out: set[int] = set()
    parts = [cfg.telegram.chat_id or "",
             *environ.get(cfg.trading.ledger_owner_ids_env, "").replace(";", ",").split(",")]
    for part in parts:
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            out.add(int(part))
    return frozenset(out)


def open_ledger(cfg: Config, environ: Mapping[str, str] = os.environ) -> OrderLedger | None:
    """None when `trading.ledger_enabled` is false. When it is true, any problem is fatal: a
    half-working ledger must never degrade into "no limits"."""
    if not cfg.trading.ledger_enabled:
        return None
    url = environ.get(cfg.trading.ledger_db_url_env, "")
    if not url:
        raise LedgerConfigError(f"{cfg.trading.ledger_db_url_env} is not set")
    user = sa.engine.make_url(url).username
    if user != ROLE:
        # never run the ledger with the owner's or the dashboard's credentials
        raise LedgerConfigError(f"the ledger must connect as {ROLE}, not {user!r}")
    owners = owner_ids(cfg, environ)
    if not owners:
        raise LedgerConfigError("no ledger owner: set TG_CHAT_ID or "
                                f"{cfg.trading.ledger_owner_ids_env}")
    ledger = OrderLedger(make_ledger_engine(url), owner_ids=owners,
                         require_finalized_tag=cfg.trading.finality_require_finalized_tag)
    ledger.bootstrap()
    report = ledger.recover()
    if not report.chain_ok:
        ledger.pause(Actor.system(), "audit chain verification failed at startup")
        log.error("ledger audit chain is INCONSISTENT; trading paused until an owner investigates")
    return ledger
