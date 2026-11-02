"""Periodic market snapshots -> token_market_snapshots + token_profiles.

Each round stores, under one capture timestamp:
  * the top `top_n` tokens by 24h volume, chain-wide, from Agora's token list, and
  * tokens traded by tracked wallets in the last `tracked_days` that are not in that list
    (fetched individually, at most `max_details_per_round` per round).
This is what makes returns over arbitrary past windows possible (Agora only exposes
rolling 1h/6h/24h/7d changes and limited candles). Coverage starts when the collector
starts; views say so instead of extrapolating.

Disk: ~150 tokens x 144 rounds/day ~ 22k rows/day; rows older than `retention_days` are
deleted once a day.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import schema as t
from .config import CollectorConfig
from .db import Store
from .market import AgoraMarket, TokenInfo

UTC = timezone.utc
log = logging.getLogger(__name__)


def _dec(v: float | None) -> Decimal | None:
    if v is None or v != v:
        return None
    return Decimal(repr(float(v)))


def _ts(v: int | None) -> datetime | None:
    return datetime.fromtimestamp(v, UTC) if v else None


class Collector:
    def __init__(self, store: Store, market: AgoraMarket, cfg: CollectorConfig):
        self.store, self.market, self.cfg = store, market, cfg
        self._last_prune = 0.0

    def _recent_tracked_tokens(self, since: datetime) -> list[str]:
        with self.store.engine.connect() as c:
            return [r[0] for r in c.execute(sa.text("""
                SELECT token FROM trades WHERE chain_id = :c AND block_time >= :s
                GROUP BY token ORDER BY max(block_time) DESC LIMIT 500"""),
                {"c": self.store.chain_id, "s": since})]

    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(UTC)).replace(microsecond=0)
        toks, meta = self.market.top_tokens(self.cfg.top_n, window="24h")
        if not toks:
            out = {"at": int(now.timestamp()), "ok": False, "error": "Agora token list unavailable"}
            self.store.set_kv("collector_last", json.dumps(out))
            log.warning("collector: Agora token list unavailable")
            return out
        have = {x.address for x in toks}
        extra = [a for a in self._recent_tracked_tokens(now - timedelta(days=self.cfg.tracked_days))
                 if a not in have][:self.cfg.max_details_per_round]
        for a in extra:
            info = self.market.token(a)
            if info is not None:
                toks.append(info)
        synced = meta.get("synced") or {}
        synced_at = datetime.fromtimestamp(synced["at"] / 1000, UTC) if synced.get("at") else None
        rows = [self._row(x, now, synced_at, synced.get("head")) for x in toks
                if x.address.startswith("0x") and len(x.address) == 42]
        cid = self.store.chain_id
        with self.store.tx() as c:
            if rows:
                c.execute(pg_insert(t.token_market_snapshots).values(rows)
                          .on_conflict_do_nothing(constraint="uq_token_market_snapshots_obs"))
            profiles = [{"chain_id": cid, "address": x.address, "symbol": x.symbol,
                         "name": x.name, "launchpad": x.launchpad, "version": x.version,
                         "launched_at": _ts(x.created_at), "source": "agora"} for x in toks]
            if profiles:
                stmt = pg_insert(t.token_profiles).values(profiles)
                c.execute(stmt.on_conflict_do_update(
                    index_elements=["chain_id", "address"],
                    set_={k: getattr(stmt.excluded, k) for k in
                          ("symbol", "name", "launchpad", "version", "launched_at")}
                    | {"updated_at": sa.func.now()}))
            if time.monotonic() - self._last_prune > 86400:
                n = c.execute(sa.delete(t.token_market_snapshots).where(
                    t.token_market_snapshots.c.chain_id == cid,
                    t.token_market_snapshots.c.captured_at
                    < now - timedelta(days=self.cfg.retention_days))).rowcount
                self._last_prune = time.monotonic()
                if n:
                    log.info("collector: pruned %d snapshots older than %d days", n,
                             self.cfg.retention_days)
        out = {"at": int(now.timestamp()), "ok": True, "tokens": len(rows),
               "extra": len(extra), "source_head": synced.get("head")}
        self.store.set_kv("collector_last", json.dumps(out))
        log.info("collector: %d token snapshots (%d tracked extras)", len(rows), len(extra))
        return out

    def _row(self, x: TokenInfo, now: datetime, synced_at: datetime | None,
             head: int | None) -> dict[str, Any]:
        return {"chain_id": self.store.chain_id, "token": x.address, "captured_at": now,
                "source": "agora", "source_synced_at": synced_at, "source_head": head,
                "price_hype": _dec(x.price_hype), "mcap_hype": _dec(x.mcap_hype),
                "vol_1h_hype": _dec(x.vol_1h_hype), "vol_24h_hype": _dec(x.vol_24h_hype),
                "change_1h": _dec(x.change_1h), "change_24h": _dec(x.change_24h),
                "buys_24h": x.buys_24h, "trades_24h": x.trades_24h,
                "last_trade_at": _ts(x.last_trade_at)}

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep sampling; the health page shows staleness
                log.exception("collector round failed")
            stop.wait(self.cfg.interval_s)
