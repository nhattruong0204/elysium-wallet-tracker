"""Config loading. Everything tunable lives in config.yaml; secrets come from env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ChainConfig:
    rpc_urls: list[str]
    chain_id: int = 99901
    poll_interval_s: float = 5.0
    max_block_range: int = 2000
    confirmations: int = 0
    backfill_blocks: int = 2000
    wallet_chunk: int = 50              # addresses per eth_getLogs topic OR-list
    rpc_timeout_s: float = 15.0
    rpc_max_retries: int = 4
    rpc_batch_size: int = 50           # upper bound; smaller per-endpoint limits are learned
    max_head_lag: int = 50              # skip endpoints this many blocks behind the best head
    head_check_s: float = 60.0          # how often endpoint heads are compared
    wrapped_native: str | None = None   # WHYPE on Elysium; its transfers count as HYPE
    explorer_tx_url: str | None = None  # deprecated: links.explorer is used instead
    explorer_address_url: str | None = None


@dataclass
class LeaderboardConfig:
    enabled: bool = True
    url: str = ""
    method: str = "GET"
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    items_path: str = ""                # dotted path to the trader list, "" = response is the list
    fields: dict[str, str] = field(default_factory=lambda: {
        "address": "address", "pnl": "pnl", "worth": "totalWorth",
        "volume": "volume", "trades": "trades",
    })
    sort_by: str = "pnl"                # re-sort client side, in case the endpoint doesn't
    value_unit: str = "USD"             # unit of pnl/worth/volume as the source reports them
    top_n: int = 100
    refresh_s: float = 300.0
    timeout_s: float = 20.0


@dataclass
class AlertConfig:
    min_trade_hype: float = 0.5
    alert_transfers: bool = True        # wallet moving tokens to another address (rotation, OTC)
    leaderboard_alerts: bool = True
    rank_change: int = 10
    pnl_change: float = 5000.0          # in leaderboard.value_unit
    cluster_window_s: int = 3600
    cluster_min_wallets: int = 3
    hype_dust: float = 0.0005           # |HYPE delta| below this = no HYPE leg (gas noise)
    ignore_tokens: list[str] = field(default_factory=list)


@dataclass
class TelegramConfig:
    bot_token: str | None = None
    chat_id: str | None = None
    min_interval_s: float = 1.1         # Telegram limit is ~1 msg/s per chat
    timeout_s: float = 15.0
    max_attempts: int = 12              # then the alert is dead-lettered (kept in the DB)


@dataclass
class LinksConfig:
    """Base URLs used to make every alert detail clickable (formats verified 2026-11-02)."""
    explorer: str = "https://elyscan.live"          # /tx/<hash>, /address/<a>, /token/<a>
    agora: str = "https://www.agoradex.app"         # /token/<a>, /trader/<a>
    kleos: str = "https://www.thekleos.xyz"         # /token/<a>
    boxa: str = "https://boxa.club"                 # /token?address=<a> (any Elysium token)
    elyspump: str = "https://elysp.com"             # /#/token/<a>


@dataclass
class MarketConfig:
    """Token market data shown in alerts, from Agora's public API (values in HYPE)."""
    enabled: bool = True
    api: str = "https://api.agoradex.app"
    cache_s: float = 60.0
    timeout_s: float = 6.0


@dataclass
class Config:
    chain: ChainConfig
    leaderboard: LeaderboardConfig
    alerts: AlertConfig
    telegram: TelegramConfig
    links: LinksConfig = field(default_factory=LinksConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    db_path: str = "tracker.db"
    watchlist_file: str | None = None
    exclude_contracts: bool = True


def _norm_addr(a: str) -> str:
    a = a.strip().lower()
    if not (a.startswith("0x") and len(a) == 42):
        raise ValueError(f"bad address: {a!r}")
    int(a, 16)
    return a


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    base = Path(path).resolve().parent

    chain_raw = raw.get("chain") or {}
    if not chain_raw.get("rpc_urls"):
        raise ValueError("chain.rpc_urls is required")
    chain = ChainConfig(**chain_raw)
    if chain.wrapped_native:
        chain.wrapped_native = _norm_addr(chain.wrapped_native)

    lb = LeaderboardConfig(**(raw.get("leaderboard") or {}))
    alerts_raw = dict(raw.get("alerts") or {})
    if "pnl_change_usd" in alerts_raw:  # pre-0.3 key
        alerts_raw.setdefault("pnl_change", alerts_raw.pop("pnl_change_usd"))
    alerts = AlertConfig(**alerts_raw)
    alerts.ignore_tokens = [_norm_addr(t) for t in alerts.ignore_tokens]

    tg_raw = raw.get("telegram") or {}
    tg = TelegramConfig(
        bot_token=os.environ.get(tg_raw.get("bot_token_env", "TG_BOT_TOKEN")),
        chat_id=os.environ.get(tg_raw.get("chat_id_env", "TG_CHAT_ID")),
        min_interval_s=tg_raw.get("min_interval_s", 1.1),
        timeout_s=tg_raw.get("timeout_s", 15.0),
        max_attempts=tg_raw.get("max_attempts", 12),
    )

    # PostgreSQL comes from DATABASE_URL. db_path only names a legacy SQLite file to import.
    db_path = raw.get("db_path", "tracker.db")
    if not os.path.isabs(db_path):
        db_path = str(base / db_path)
    wl = raw.get("watchlist_file")
    if wl and not os.path.isabs(wl):
        wl = str(base / wl)

    links = LinksConfig(**{k: v.rstrip("/") for k, v in (raw.get("links") or {}).items()})
    market = MarketConfig(**(raw.get("market") or {}))
    return Config(chain=chain, leaderboard=lb, alerts=alerts, telegram=tg, links=links,
                  market=market, db_path=db_path, watchlist_file=wl,
                  exclude_contracts=raw.get("exclude_contracts", True))


def load_watchlist(path: str | None) -> dict[str, str]:
    """`0xaddr  optional label` per line; '#' comments."""
    out: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return out
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        out[_norm_addr(parts[0])] = parts[1].strip() if len(parts) > 1 else ""
    return out
