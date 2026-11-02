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
    rpc_batch_size: int = 50
    wrapped_native: str | None = None   # WHYPE on Elysium; its transfers count as HYPE
    explorer_tx_url: str | None = None  # e.g. "https://explorer.example/tx/{tx}"
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
    top_n: int = 100
    refresh_s: float = 300.0
    timeout_s: float = 20.0


@dataclass
class AlertConfig:
    min_trade_hype: float = 0.5
    alert_transfers: bool = True        # wallet moving tokens to another address (rotation, OTC)
    leaderboard_alerts: bool = True
    rank_change: int = 10
    pnl_change_usd: float = 5000.0
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
class Config:
    chain: ChainConfig
    leaderboard: LeaderboardConfig
    alerts: AlertConfig
    telegram: TelegramConfig
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
    alerts = AlertConfig(**(raw.get("alerts") or {}))
    alerts.ignore_tokens = [_norm_addr(t) for t in alerts.ignore_tokens]

    tg_raw = raw.get("telegram") or {}
    tg = TelegramConfig(
        bot_token=os.environ.get(tg_raw.get("bot_token_env", "TG_BOT_TOKEN")),
        chat_id=os.environ.get(tg_raw.get("chat_id_env", "TG_CHAT_ID")),
        min_interval_s=tg_raw.get("min_interval_s", 1.1),
        timeout_s=tg_raw.get("timeout_s", 15.0),
        max_attempts=tg_raw.get("max_attempts", 12),
    )

    db_path = os.environ.get("TRACKER_DB_PATH") or raw.get("db_path", "tracker.db")
    if not os.path.isabs(db_path):
        db_path = str(base / db_path)
    wl = raw.get("watchlist_file")
    if wl and not os.path.isabs(wl):
        wl = str(base / wl)

    return Config(chain=chain, leaderboard=lb, alerts=alerts, telegram=tg,
                  db_path=db_path, watchlist_file=wl,
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
