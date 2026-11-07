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
    # How Transfer logs are fetched. "filtered": 2 eth_getLogs per wallet chunk (topic OR-lists,
    # cost grows with the wallet count). "full": ONE unfiltered Transfer query per range,
    # wallets filtered locally (cost independent of the wallet count, response grows with the
    # chain's Transfer volume). "auto": filtered until it would need more than
    # `full_scan_above_calls` calls per range, then full.
    # Measured 2026-11-06: the whole chain emits ~0.35 Transfer logs per block (about 460 KiB per
    # 2,000 blocks), so one unfiltered query is far cheaper than 2 x ceil(wallets / 50) filtered
    # ones, and the Kleos nodes only allow unfiltered queries up to 1,000 blocks.
    scan_mode: str = "auto"
    full_scan_above_calls: int = 2
    full_scan_max_span: int = 1000
    # auto: if the unfiltered scan sees more logs per block than this (a busier chain after
    # mainnet launch), use wallet filters for 10 minutes, then test again.
    full_scan_max_logs_per_block: float = 25.0
    min_block_range: int = 50           # floor when the span is shrunk after a node rejects it
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
    leaderboard_alerts: bool = True     # rank/PnL change alerts (snapshots are always recorded)
    cluster_alerts: bool = True         # cluster-buy alerts (signals are always recorded)
    token_image: bool = True            # show the token's picture above the alert card
    disk_free_pct: float = 0.15         # alert when the host disk has less free space than this
    disk_free_gb: float = 8.0           # ... or less than this many GB
    rank_change: int = 10
    pnl_change: float = 5000.0          # in leaderboard.value_unit
    cluster_window_s: int = 3600
    cluster_min_wallets: int = 3
    hype_dust: float = 0.0005           # |HYPE delta| below this = no HYPE leg (gas noise)
    ignore_tokens: list[str] = field(default_factory=list)   # dropped before indexing
    # still indexed (positions stay right) but never alerted, never part of a cluster, and
    # hidden from the website's Activity tab unless that exact token is filtered
    mute_tokens: list[str] = field(default_factory=list)


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
    kleos: str = "https://www.thekleos.pro"         # /token/<a>
    boxa: str = "https://boxa.club"                 # /token?address=<a> (any Elysium token)
    elyspump: str = "https://elysp.com"             # /#/token/<a>
    # "Trade in wallet" buttons: open the trade page inside the wallet app's browser.
    # rabby: https://go.rabby.io/mobile/?_cmd=open-dapp&dapp=<url> (Rabby Mobile universal link)
    # okx:   https://web3.okx.com/download?deeplink=okx://wallet/dapp/url?dappUrl=<url>
    wallet_buttons: list[str] = field(default_factory=lambda: ["rabby", "okx"])
    trade_site: str = "agora"                       # page opened: agora | boxa | kleos


@dataclass
class MarketConfig:
    """Token market data shown in alerts, from Agora's public API (values in HYPE)."""
    enabled: bool = True
    api: str = "https://api.agoradex.app"
    cache_s: float = 60.0
    timeout_s: float = 6.0


@dataclass
class UiConfig:
    """Telegram Intelligence Center (the `ui` service). See docs/TELEGRAM_UI.md."""
    enabled: bool = True
    timezone: str = "Asia/Ho_Chi_Minh"       # display only; storage and queries are UTC
    admin_ids_env: str = "TG_ADMIN_IDS"      # extra authorized Telegram user ids, comma separated
    admin_ids: list[int] = field(default_factory=list)   # resolved: TG_CHAT_ID + admin_ids_env
    page_size: int = 5
    # Pinned 10-button keyboard under the message box. Off: the menu lives in /menu and in
    # Telegram's Menu button, and the bot clears a keyboard an older version left behind.
    reply_keyboard: bool = False
    poll_timeout_s: int = 25                 # getUpdates long-poll
    workers: int = 4                         # concurrent interactive handlers
    report_workers: int = 1                  # concurrent exports (never starves the indexer)
    report_queue: int = 3                    # exports waiting beyond this are refused
    report_max_rows: int = 50000             # per sheet / CSV
    report_max_bytes: int = 20_000_000       # Telegram bots may send up to 50 MB
    query_timeout_ms: int = 15000            # PostgreSQL statement_timeout for reports
    qualified_min_pnl: float = 0.0           # Agora PnL above this = "qualified" wallet
    min_volume_hype: float = 25.0            # gainers/losers: minimum window volume
    min_mcap_hype: float = 100.0             # gainers/losers/trending: minimum market cap
    min_age_h: float = 1.0                   # gainers/losers: minimum token age
    min_observations: int = 2                # snapshot-based returns need this many points
    candle_fallback_tokens: int = 30         # custom ranges <=7d: tokens checked via candles
    disk_warn_pct: float = 0.15              # health page warns below this free share


@dataclass
class CollectorConfig:
    """Periodic market snapshots (token_market_snapshots) for historical returns."""
    enabled: bool = True
    interval_s: float = 600.0
    top_n: int = 150                         # by 24h volume, chain-wide (Agora)
    tracked_days: int = 7                    # plus tokens traded by tracked wallets lately
    max_details_per_round: int = 20          # per-token requests for tokens outside top_n
    retention_days: int = 30


@dataclass
class ScoringConfig:
    """Explainable wallet scores (agora_tracker/scoring.py). `model()` is what defines a score
    version; the other fields only schedule the job. A version is immutable: change a weight or
    a parameter and you must change `version`, or the job refuses to run."""
    enabled: bool = True
    version: str = "v1"
    interval_s: float = 3600.0
    retention_hourly_h: int = 48             # older rows are thinned to one per day
    lookback_days: int = 30
    min_closes: int = 5                      # measured closing trades needed for the PnL components
    min_snapshots: int = 12                  # accepted lists needed for list persistence
    wilson_z: float = 1.645                  # one-sided lower bound on the win rate
    return_cap: float = 1.0                  # measured return on cost mapped from [-cap, +cap]
    recency_half_life_days: float = 7.0
    top_n: int = 100
    min_weight_available: float = 0.5        # below this share of weight with data: no score
    sufficient_weight: float = 0.8           # ... and both measured components: "sufficient"
    limited_cap: float = 50.0                # a score without measured trade evidence is capped
    weights: dict[str, float] = field(default_factory=lambda: {
        "win_rate_lb": 0.25, "return_on_cost": 0.25, "recency": 0.15,
        "list_persistence": 0.15, "rank_strength": 0.20})

    def model(self) -> dict[str, Any]:
        return {"eligibility": "historical-top-membership-v2",
                "lookback_days": self.lookback_days, "min_closes": self.min_closes,
                "min_snapshots": self.min_snapshots, "wilson_z": self.wilson_z,
                "return_cap": self.return_cap,
                "recency_half_life_days": self.recency_half_life_days, "top_n": self.top_n,
                "min_weight_available": self.min_weight_available,
                "sufficient_weight": self.sufficient_weight, "limited_cap": self.limited_cap,
                "weights": dict(sorted(self.weights.items()))}


@dataclass
class WebConfig:
    """Public research dashboard (the `web` service). See docs/infra/ONCHAINFLOW_DASHBOARD.md."""
    host: str = "127.0.0.1"
    port: int = 8100
    site_name: str = "Elysium Research"
    public_url: str = "https://onchainflow.app"
    page_size: int = 25
    cache_ttl_s: float = 30.0
    cache_max_entries: int = 256
    cache_stale_s: float = 300.0             # serve a stale page this long if the database fails
    query_timeout_ms: int = 5000             # per statement; the role enforces the same default
    db_pool_size: int = 3
    db_max_overflow: int = 1                 # pool + overflow stays below the role's limit of 5
    max_concurrent_builds: int = 3           # page builds that may query at once
    rate_limit_per_ip: float = 10.0          # requests / second / client IP (0 disables)
    rate_burst: int = 40
    stale_after_s: int = 900                 # data older than this is flagged "stale"
    market_max_age_s: int = 1800             # stored market snapshots older than this are not "live"
    max_custom_days: int = 30
    # HYPE/USD rate alignment (see web/usd.py): a snapshot is converted with the stored rate
    # closest in time; the gap decides the provenance label and a hard cut-off.
    fx_exact_s: int = 60                     # |gap| <= this: "exact"
    fx_aligned_s: int = 600                  # <= this: "aligned" (one leaderboard cycle is 300 s)
    fx_max_gap_s: int = 3600                 # beyond this no USD value is produced
    activity_page_size: int = 50
    activity_max_page_size: int = 100
    activity_cache_ttl_s: float = 5.0        # shared by every client polling the same query
    activity_since_limit: int = 100          # most rows one incremental poll returns
    activity_poll_s: int = 20                # browser poll interval while the tab is visible
    activity_hidden_poll_s: int = 120        # ... and while it is hidden (0 = pause)


@dataclass
class TrackingConfig:
    """Which wallets the scanner follows. `cumulative` keeps every wallet that has ever been in
    an accepted, complete top list (wallet_enrollment); turn it off to go back to
    "latest snapshot + watchlists" without touching the database."""
    cumulative: bool = True
    # Wallet count above which a warning is raised (log + health). It is a *signal*, never a
    # cap: nothing is dropped. Set it from measured capacity (scripts/scan_bench.py).
    capacity_warn: int = 1000
    # Scanner lag (blocks behind head) that counts as "falling behind" in the health output.
    lag_warn_blocks: int = 3000


@dataclass
class IntelConfig:
    """Launch intelligence (alerts only; nothing here signs or sends a transaction)."""
    # independent-wallet scoring: bundle wallets (same-block multi-wallet buyers) count once
    independent_min: int = 3            # independent wallets that make the strong signal
    independent_window_s: int = 1800
    independent_cooldown_s: int = 86400  # at most one strong signal per token per day
    bundle_lookback_days: int = 14
    bundle_min_wallets: int = 3         # distinct wallets buying one token in one block
    bundle_min_hits: int = 5            # such blocks a wallet must take part in
    bundle_refresh_s: float = 1800.0
    # forward measurement of the strong signal (no orders are ever placed)
    paper_enabled: bool = True
    paper_slippage_pct: float = 10.0
    wrapped_native: str = "0xba876237b0f021a6c6d852d6c26e107997cc5e81"   # WHYPE (WETH9 of the DEX router)
    # read-only due diligence block in alerts
    dd_enabled: bool = True
    dd_budget_s: float = 6.0            # RPC time per token, hard cap
    dd_cache_s: float = 300.0
    dd_log_blocks: int = 18000          # ~1 h of Transfer logs at ~5 blocks/s, read in 5k chunks
    dd_block_time_s: float = 0.2        # for the "in the last N min" wording only
    dd_factories: list[str] = field(default_factory=list)   # DEX factories with getPool()
    # chain-level new pool watcher (venues Agora does not index)
    launch_watch: bool = True
    launch_watch_interval_s: float = 60.0
    launch_factories: list[str] = field(default_factory=list)  # empty = any PoolCreated
    launch_min_liquidity_hype: float = 300.0
    launch_delay_s: int = 120
    # alert when Agora reports a launchpad we have not seen before
    new_launchpad_alerts: bool = True
    # index Ascend launches from its own hook events (venue, pool, fee, pair, dev buy)
    ascend_index: bool = True
    ascend_interval_s: float = 60.0
    ascend_max_chunks: int = 2              # x 5,000 blocks per tick; backfill is gradual


@dataclass
class TradingConfig:
    """Stage B only. There is no live execution configuration switch."""
    paper_enabled: bool = False
    paper_order_path: str | None = None
    paper_rpc_url: str = 'https://rpc.agoradex.app'

    def __post_init__(self) -> None:
        from urllib.parse import urlsplit
        if not isinstance(self.paper_rpc_url, str) or urlsplit(self.paper_rpc_url).scheme != 'https':
            raise ValueError('paper_rpc_url must use HTTPS')
        if self.paper_order_path is not None and (not isinstance(self.paper_order_path, str) or not self.paper_order_path.strip()):
            raise ValueError('paper_order_path must be a local private file path')
        if type(self.paper_enabled) is not bool:
            raise ValueError("trading.paper_enabled must be a boolean")


@dataclass
class Config:
    chain: ChainConfig
    leaderboard: LeaderboardConfig
    alerts: AlertConfig
    telegram: TelegramConfig
    links: LinksConfig = field(default_factory=LinksConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    web: WebConfig = field(default_factory=WebConfig)
    intel: IntelConfig = field(default_factory=IntelConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
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
    alerts.mute_tokens = [_norm_addr(t) for t in alerts.mute_tokens]

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

    links_raw: dict[str, Any] = {k: v.rstrip("/") if isinstance(v, str) else v
                                 for k, v in (raw.get("links") or {}).items()}
    links = LinksConfig(**links_raw)
    market = MarketConfig(**(raw.get("market") or {}))
    ui = UiConfig(**{k: v for k, v in (raw.get("ui") or {}).items() if k != "admin_ids"})
    ui.admin_ids = _admin_ids(tg.chat_id, os.environ.get(ui.admin_ids_env, ""))
    collector = CollectorConfig(**(raw.get("collector") or {}))
    web = WebConfig(**(raw.get("web") or {}))
    intel = IntelConfig(**(raw.get("intel") or {}))
    tracking = TrackingConfig(**(raw.get("tracking") or {}))
    scoring = ScoringConfig(**(raw.get("scoring") or {}))
    intel.wrapped_native = _norm_addr(intel.wrapped_native)
    intel.dd_factories = [_norm_addr(a) for a in intel.dd_factories]
    intel.launch_factories = [_norm_addr(a) for a in intel.launch_factories]
    return Config(chain=chain, leaderboard=lb, alerts=alerts, telegram=tg, links=links,
                  market=market, ui=ui, collector=collector, web=web, intel=intel,
                  tracking=tracking, scoring=scoring,
                  trading=TradingConfig(**(raw.get("trading") or {})), db_path=db_path,
                  watchlist_file=wl,
                  exclude_contracts=raw.get("exclude_contracts", True))


def _admin_ids(chat_id: str | None, extra: str) -> list[int]:
    """Authorized users: the alert chat (a private chat id is the user's id) plus extras.
    Group/channel ids (negative) are ignored here: authorization is per user."""
    out: list[int] = []
    for part in [chat_id or "", *extra.replace(";", ",").split(",")]:
        part = part.strip()
        if part.lstrip("-").isdigit() and int(part) > 0 and int(part) not in out:
            out.append(int(part))
    return out


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
