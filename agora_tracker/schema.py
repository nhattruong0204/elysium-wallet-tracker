"""PostgreSQL schema (SQLAlchemy Core). Mirrors the Alembic migrations exactly; a test
compares the two so they cannot drift.

Precision choices:
  NUMERIC(78,0)  every on-chain integer quantity (wei, raw token units, gas). uint256 max is
                 1.16e77, i.e. 78 digits, so any value a contract can emit fits exactly.
                 Signed PnL in wei uses the same type (|pnl| <= uint256 max).
  NUMERIC(24,6)  USD figures reported by Agora: up to 10^18 dollars at micro-dollar
                 resolution, far beyond any realistic value, no float rounding.
  TIMESTAMPTZ    all times, UTC.
Addresses are stored lowercase (CHECK enforced); a checksum form is kept where we have it.
Every chain-specific table carries chain_id.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})

U256 = sa.Numeric(78, 0)
USD = sa.Numeric(24, 6)
TS = sa.DateTime(timezone=True)
ADDR = sa.String(42)


def _addr_check(col: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{col} ~ '^0x[0-9a-f]{{40}}$'", name=f"{col}_lower_hex")


def _now() -> sa.TextClause:
    return sa.text("now()")


chains = sa.Table(
    "chains", metadata,
    sa.Column("chain_id", sa.BigInteger, primary_key=True, autoincrement=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("genesis_hash", sa.Text),
    sa.Column("created_at", TS, nullable=False, server_default=_now()),
)

service_state = sa.Table(
    "service_state", metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", JSONB, nullable=False),
    sa.Column("updated_at", TS, nullable=False, server_default=_now()),
)

indexer_checkpoints = sa.Table(
    "indexer_checkpoints", metadata,
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), primary_key=True),
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("block_number", sa.BigInteger, nullable=False),
    sa.Column("block_hash", sa.Text),
    sa.Column("updated_at", TS, nullable=False, server_default=_now()),
)

indexed_blocks = sa.Table(
    "indexed_blocks", metadata,
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), primary_key=True),
    sa.Column("block_number", sa.BigInteger, primary_key=True),
    sa.Column("block_hash", sa.Text),
    sa.Column("parent_hash", sa.Text),
    sa.Column("block_time", TS),
    sa.Column("indexed_at", TS, nullable=False, server_default=_now()),
)

wallets = sa.Table(
    "wallets", metadata,
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), primary_key=True),
    sa.Column("address", ADDR, primary_key=True),
    sa.Column("address_checksum", sa.String(42)),
    sa.Column("is_contract", sa.Boolean),
    sa.Column("first_seen_at", TS, nullable=False, server_default=_now()),
    _addr_check("address"),
)

wallet_watchlist = sa.Table(
    "wallet_watchlist", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, nullable=False),
    sa.Column("address", ADDR, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("label", sa.Text),
    sa.Column("reason", sa.Text),
    sa.Column("added_at", TS, nullable=False, server_default=_now()),
    sa.Column("removed_at", TS),
    sa.ForeignKeyConstraint(["chain_id", "address"], ["wallets.chain_id", "wallets.address"]),
    sa.CheckConstraint("source IN ('leaderboard','manual','reverse_discovery','import',"
                       "'telegram')", name="source"),
    sa.CheckConstraint("status IN ('active','inactive','excluded')", name="status"),
    sa.Index("ux_wallet_watchlist_current", "chain_id", "address", "source", unique=True,
             postgresql_where=sa.text("removed_at IS NULL")),
)

leaderboard_snapshots = sa.Table(
    "leaderboard_snapshots", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("captured_at", TS, nullable=False, server_default=_now()),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("complete", sa.Boolean, nullable=False),
    sa.Column("row_count", sa.Integer, nullable=False),
    sa.Column("accepted_count", sa.Integer, nullable=False),
    sa.Column("request_meta", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("raw_payload", JSONB),
    sa.CheckConstraint("status IN ('ok','degraded','failed','imported')", name="status"),
    sa.Index("ix_leaderboard_snapshots_chain_captured", "chain_id", "captured_at"),
)

leaderboard_snapshot_entries = sa.Table(
    "leaderboard_snapshot_entries", metadata,
    sa.Column("snapshot_id", sa.BigInteger,
              sa.ForeignKey("leaderboard_snapshots.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("address", ADDR, primary_key=True),
    sa.Column("rank", sa.Integer, nullable=False),
    sa.Column("pnl_usd", USD),
    sa.Column("worth_usd", USD),
    sa.Column("volume_usd", USD),
    sa.Column("trades", sa.Integer),
    sa.Column("extra", JSONB),
    _addr_check("address"),
    sa.Index("ix_leaderboard_snapshot_entries_address", "address"),
)

token_metadata = sa.Table(
    "token_metadata", metadata,
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), primary_key=True),
    sa.Column("address", ADDR, primary_key=True),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("decimals", sa.SmallInteger, nullable=False),
    sa.Column("source", sa.Text, nullable=False, server_default="rpc"),
    sa.Column("fetched_at", TS, nullable=False, server_default=_now()),
    _addr_check("address"),
)

# transactions + receipts are 1:1, so one table. Rows imported from v0.1 have only hash
# and block (the rest NULL): v0.1 never stored them.
transactions = sa.Table(
    "transactions", metadata,
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), primary_key=True),
    sa.Column("tx_hash", sa.String(66), primary_key=True),
    sa.Column("block_number", sa.BigInteger, nullable=False),
    sa.Column("from_address", ADDR),
    sa.Column("to_address", ADDR),
    sa.Column("value_wei", U256),
    sa.Column("nonce", sa.BigInteger),
    sa.Column("status", sa.SmallInteger),
    sa.Column("gas_used", U256),
    sa.Column("effective_gas_price", U256),
    sa.Column("fee_wei", U256),
    sa.Column("inserted_at", TS, nullable=False, server_default=_now()),
    sa.Index("ix_transactions_chain_block", "chain_id", "block_number"),
)

# Raw evidence: every Transfer log touching a tracked wallet, as returned by the node.
token_transfers = sa.Table(
    "token_transfers", metadata,
    sa.Column("chain_id", sa.BigInteger, primary_key=True),
    sa.Column("tx_hash", sa.String(66), primary_key=True),
    sa.Column("log_index", sa.Integer, primary_key=True),
    sa.Column("block_number", sa.BigInteger, nullable=False),
    sa.Column("token", ADDR, nullable=False),
    sa.Column("from_address", ADDR, nullable=False),
    sa.Column("to_address", ADDR, nullable=False),
    sa.Column("amount", U256, nullable=False),
    sa.ForeignKeyConstraint(["chain_id", "tx_hash"],
                            ["transactions.chain_id", "transactions.tx_hash"]),
    sa.CheckConstraint("amount >= 0", name="amount_nonneg"),
    _addr_check("token"), _addr_check("from_address"), _addr_check("to_address"),
)

trades = sa.Table(
    "trades", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, nullable=False),
    sa.Column("tx_hash", sa.String(66), nullable=False),
    sa.Column("wallet", ADDR, nullable=False),
    sa.Column("token", ADDR, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("confidence", sa.Text, nullable=False),
    sa.Column("hype_method", sa.Text, nullable=False),
    sa.Column("qty", U256, nullable=False),
    sa.Column("hype_wei", U256),
    sa.Column("counter_token", ADDR),
    sa.Column("counter_qty", U256),
    sa.Column("block_number", sa.BigInteger, nullable=False),
    sa.Column("block_time", TS),
    sa.Column("legacy_key", sa.Text, unique=True),
    sa.Column("created_at", TS, nullable=False, server_default=_now()),
    sa.ForeignKeyConstraint(["chain_id", "tx_hash"],
                            ["transactions.chain_id", "transactions.tx_hash"]),
    sa.UniqueConstraint("chain_id", "tx_hash", "wallet", "token", name="uq_trades_leg"),
    sa.CheckConstraint("kind IN ('BUY','SELL','SWAP','TRANSFER_IN','TRANSFER_OUT','MULTI')",
                       name="kind"),
    sa.CheckConstraint("confidence IN ('high','estimated','unknown','legacy')",
                       name="confidence"),
    sa.CheckConstraint("qty >= 0 AND (hype_wei IS NULL OR hype_wei >= 0)", name="nonneg"),
    _addr_check("wallet"), _addr_check("token"),
    sa.Index("ix_trades_token_kind_time", "chain_id", "token", "kind", "block_time"),
    sa.Index("ix_trades_wallet_block", "chain_id", "wallet", "block_number"),
    sa.Index("ix_trades_chain_time", "chain_id", "block_time"),
)

wallet_positions = sa.Table(
    "wallet_positions", metadata,
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), primary_key=True),
    sa.Column("wallet", ADDR, primary_key=True),
    sa.Column("token", ADDR, primary_key=True),
    sa.Column("qty", U256, nullable=False),
    sa.Column("cost_wei", U256, nullable=False),
    sa.Column("realized_wei", U256, nullable=False, server_default="0"),
    sa.Column("realized_est_wei", U256, nullable=False, server_default="0"),
    sa.Column("basis", sa.Text, nullable=False),
    sa.Column("opened_at", TS),
    sa.Column("updated_at", TS, nullable=False, server_default=_now()),
    sa.CheckConstraint("basis IN ('known','estimated','partial_unknown','legacy')",
                       name="basis"),
    sa.CheckConstraint("qty >= 0 AND cost_wei >= 0", name="nonneg"),
    _addr_check("wallet"), _addr_check("token"),
)

# One row per sell that touched a position: the audit trail behind realized PnL.
# (wallet_position_history is not stored separately: replaying trades + this table in
# id order reconstructs any past position exactly.)
realized_pnl = sa.Table(
    "realized_pnl", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), nullable=False),
    sa.Column("trade_id", sa.BigInteger, sa.ForeignKey("trades.id"), nullable=False, unique=True),
    sa.Column("wallet", ADDR, nullable=False),
    sa.Column("token", ADDR, nullable=False),
    sa.Column("qty_sold", U256, nullable=False),
    sa.Column("proceeds_wei", U256, nullable=False),
    sa.Column("cost_basis_wei", U256),
    sa.Column("pnl_wei", U256),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", TS, nullable=False, server_default=_now()),
    sa.CheckConstraint("status IN ('measured','estimated','unknown')", name="status"),
)

signals = sa.Table(
    "signals", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("token", ADDR, nullable=False),
    sa.Column("dedup_key", sa.Text, nullable=False, unique=True),
    sa.Column("window_start", TS),
    sa.Column("window_end", TS),
    sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", TS, nullable=False, server_default=_now()),
    sa.Index("ix_signals_token_created", "chain_id", "token", "created_at"),
)

signal_trades = sa.Table(
    "signal_trades", metadata,
    sa.Column("signal_id", sa.BigInteger, sa.ForeignKey("signals.id", ondelete="CASCADE"),
              primary_key=True),
    sa.Column("trade_id", sa.BigInteger, sa.ForeignKey("trades.id"), primary_key=True),
)

notification_outbox = sa.Table(
    "notification_outbox", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("dedup_key", sa.Text, nullable=False, unique=True),
    sa.Column("chat_id", sa.Text),
    sa.Column("severity", sa.Text, nullable=False, server_default="info"),
    sa.Column("text", sa.Text, nullable=False),
    # structured alert (cards.py) re-rendered at send time with fresh market data;
    # NULL for plain-text alerts, which are sent as `text`
    sa.Column("payload", JSONB),
    sa.Column("status", sa.Text, nullable=False, server_default="pending"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("next_attempt_at", TS, nullable=False, server_default=_now()),
    sa.Column("last_error", sa.Text),
    sa.Column("created_at", TS, nullable=False, server_default=_now()),
    sa.Column("sent_at", TS),
    sa.CheckConstraint("status IN ('pending','sent','dead')", name="status"),
    sa.Index("ix_notification_outbox_due", "next_attempt_at",
             postgresql_where=sa.text("status = 'pending'")),
)

notification_deliveries = sa.Table(
    "notification_deliveries", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("outbox_id", sa.BigInteger,
              sa.ForeignKey("notification_outbox.id", ondelete="CASCADE"), nullable=False),
    sa.Column("attempted_at", TS, nullable=False, server_default=_now()),
    sa.Column("ok", sa.Boolean, nullable=False),
    sa.Column("error", sa.Text),
    sa.Index("ix_notification_deliveries_outbox", "outbox_id"),
)


# --- Intelligence Center (migration 0003) ------------------------------------------------
# Token identity as reported by Agora (name, launchpad, launch time). Kept apart from
# token_metadata, whose symbol/decimals come from the chain and drive quantity math.
token_profiles = sa.Table(
    "token_profiles", metadata,
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), primary_key=True),
    sa.Column("address", ADDR, primary_key=True),
    sa.Column("symbol", sa.Text),
    sa.Column("name", sa.Text),
    sa.Column("launchpad", sa.Text),
    sa.Column("version", sa.Text),
    sa.Column("launched_at", TS),
    sa.Column("source", sa.Text, nullable=False, server_default="agora"),
    sa.Column("first_seen_at", TS, nullable=False, server_default=_now()),
    sa.Column("updated_at", TS, nullable=False, server_default=_now()),
    _addr_check("address"),
    sa.Index("ix_token_profiles_symbol", "chain_id", sa.text("lower(symbol)")),
)

# Timestamped market observations (Agora's last-trade price etc.), the basis for returns
# over arbitrary past windows. Prices are tiny (1e-8 HYPE), so NUMERIC without a fixed
# scale: the value Agora reported, as reported. These are indicative, not executable.
token_market_snapshots = sa.Table(
    "token_market_snapshots", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), nullable=False),
    sa.Column("token", ADDR, nullable=False),
    sa.Column("captured_at", TS, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("source_synced_at", TS),
    sa.Column("source_head", sa.BigInteger),
    sa.Column("price_hype", sa.Numeric),
    sa.Column("mcap_hype", sa.Numeric),
    sa.Column("vol_1h_hype", sa.Numeric),
    sa.Column("vol_24h_hype", sa.Numeric),
    sa.Column("change_1h", sa.Numeric),
    sa.Column("change_24h", sa.Numeric),
    sa.Column("buys_24h", sa.Integer),
    sa.Column("trades_24h", sa.Integer),
    sa.Column("last_trade_at", TS),
    _addr_check("token"),
    sa.UniqueConstraint("chain_id", "token", "captured_at", name="uq_token_market_snapshots_obs"),
    sa.Index("ix_token_market_snapshots_time", "chain_id", "captured_at"),
)

# HYPE/USD rate observations (migration 0005). Filled from the leaderboard payload by a
# trigger; read by the dashboard to turn the provider's HYPE market caps into USD at the time
# each snapshot was taken.
hype_usd_rates = sa.Table(
    "hype_usd_rates", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("observed_at", TS, nullable=False),
    sa.Column("hype_usd", sa.Numeric(24, 8), nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.UniqueConstraint("source", "observed_at", name="uq_hype_usd_rates_obs"),
    sa.CheckConstraint("hype_usd > 0", name="positive"),
    sa.Index("ix_hype_usd_rates_observed_at", "observed_at"),
)

# Launch intelligence (migration 0006). Private: no public_api view exposes these.
# launch_venues: every launchpad / DEX pool already reported, so a new one alerts once.
launch_venues = sa.Table(
    "launch_venues", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("first_seen_at", TS, nullable=False, server_default=_now()),
    sa.Column("alerted_at", TS),
    sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.UniqueConstraint("chain_id", "key", name="uq_launch_venues_key"),
    sa.Index("ix_launch_venues_kind_seen", "chain_id", "kind", "first_seen_at"),
)

# paper_trades: forward measurement of the independent-wallets signal. Entry = executed price
# of the triggering buy; later prices are read from token_market_snapshots at report time.
paper_trades = sa.Table(
    "paper_trades", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), nullable=False),
    sa.Column("token", ADDR, nullable=False),
    sa.Column("signal_id", sa.BigInteger, sa.ForeignKey("signals.id", ondelete="SET NULL")),
    sa.Column("opened_at", TS, nullable=False),
    sa.Column("block_number", sa.BigInteger),
    sa.Column("entry_price_hype", sa.Numeric(60, 30)),
    sa.Column("n_independent", sa.Integer, nullable=False),
    sa.Column("n_raw", sa.Integer, nullable=False),
    sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", TS, nullable=False, server_default=_now()),
    _addr_check("token"),
    sa.UniqueConstraint("chain_id", "token", name="uq_paper_trades_token"),
    sa.Index("ix_paper_trades_opened_at", "opened_at"),
)

# Ascend launches, indexed from the hook's `Launched` + EscrowFactory `EscrowLaunched` events
# (migration 0007). public_api.ascend_launches exposes the launch facts only.
ascend_launches = sa.Table(
    "ascend_launches", metadata,
    sa.Column("chain_id", sa.BigInteger, sa.ForeignKey("chains.chain_id"), primary_key=True),
    sa.Column("token", ADDR, primary_key=True),
    sa.Column("pool_id", sa.String(66), nullable=False),
    sa.Column("quote", ADDR, nullable=False),
    sa.Column("token_is0", sa.Boolean, nullable=False),
    sa.Column("fee_pips", sa.Integer, nullable=False),
    sa.Column("builder_fee_pips", sa.Integer, nullable=False),
    sa.Column("supply_seeded", sa.Numeric(78, 0), nullable=False),
    sa.Column("dev_buy_tokens", sa.Numeric(78, 0), nullable=False),
    sa.Column("creator", ADDR, nullable=False),
    sa.Column("escrow", ADDR),
    sa.Column("recipient", ADDR),
    sa.Column("block_number", sa.BigInteger, nullable=False),
    sa.Column("launched_at", TS),
    sa.Column("tx_hash", sa.String(66), nullable=False),
    sa.Column("first_seen_at", TS, nullable=False, server_default=_now()),
    _addr_check("token"),
    sa.Index("ix_ascend_launches_pool_id", "pool_id"),
    sa.Index("ix_ascend_launches_block", "chain_id", "block_number"),
)

# Cumulative Top-100 tracking (migration 0008). Leaderboard membership, tracking eligibility
# and scan coverage are different facts and live in different places:
#   wallet_enrollment                 who has ever been in an accepted, complete top list
#                                     (evidence) and whether the scanner may follow them
#   leaderboard_membership_events     ENTERED / EXITED against the previous accepted list
#   wallet_scan_coverage              block intervals in which a wallet was really scanned
wallet_enrollment = sa.Table(
    "wallet_enrollment", metadata,
    sa.Column("chain_id", sa.BigInteger, primary_key=True, autoincrement=False),
    sa.Column("address", ADDR, primary_key=True),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("excluded_reason", sa.Text),
    sa.Column("first_top_at", TS, nullable=False),
    sa.Column("last_top_at", TS, nullable=False),
    sa.Column("first_snapshot_id", sa.BigInteger, nullable=False),
    sa.Column("last_snapshot_id", sa.BigInteger, nullable=False),
    sa.Column("best_rank", sa.Integer, nullable=False),
    sa.Column("last_rank", sa.Integer, nullable=False),
    sa.Column("appearances", sa.Integer, nullable=False),
    sa.Column("entries", sa.Integer, nullable=False, server_default="1"),
    sa.Column("exits", sa.Integer, nullable=False, server_default="0"),
    sa.Column("in_top", sa.Boolean, nullable=False),
    sa.Column("enrolled_at", TS, nullable=False, server_default=_now()),
    sa.ForeignKeyConstraint(["chain_id", "address"], ["wallets.chain_id", "wallets.address"]),
    sa.CheckConstraint("status IN ('active','excluded')", name="status"),
    _addr_check("address"),
    sa.Index("ix_wallet_enrollment_in_top", "chain_id", "in_top"),
    sa.Index("ix_wallet_enrollment_status", "chain_id", "status"),
)

leaderboard_membership_events = sa.Table(
    "leaderboard_membership_events", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, nullable=False),
    sa.Column("address", ADDR, nullable=False),
    sa.Column("snapshot_id", sa.BigInteger, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("rank", sa.Integer, nullable=False),
    sa.Column("at", TS, nullable=False),
    sa.CheckConstraint("kind IN ('ENTERED','EXITED')", name="kind"),
    sa.UniqueConstraint("chain_id", "address", "snapshot_id", "kind", name="uq_membership_event"),
    _addr_check("address"),
    sa.Index("ix_leaderboard_membership_events_address_at", "chain_id", "address", "at"),
)

# One OPEN interval (to_block IS NULL) per wallet at most. 'live' intervals are written by the
# scanner at the exact cursor; 'derived' ones are reconstructed once from watchlist timestamps
# (nearest indexed block) and are approximate. Gaps between intervals are periods in which the
# wallet's trades were NOT recorded.
wallet_scan_coverage = sa.Table(
    "wallet_scan_coverage", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, nullable=False),
    sa.Column("address", ADDR, nullable=False),
    sa.Column("from_block", sa.BigInteger, nullable=False),
    sa.Column("to_block", sa.BigInteger),
    sa.Column("basis", sa.Text, nullable=False),
    sa.Column("created_at", TS, nullable=False, server_default=_now()),
    sa.Column("closed_at", TS),
    sa.CheckConstraint("basis IN ('live','derived')", name="basis"),
    sa.CheckConstraint("to_block IS NULL OR to_block >= from_block - 1", name="order"),
    sa.Index("ux_wallet_scan_coverage_open", "chain_id", "address", unique=True,
             postgresql_where=sa.text("to_block IS NULL")),
    sa.Index("ix_wallet_scan_coverage_address", "chain_id", "address", "from_block"),
    _addr_check("address"),
)

# Telegram UI state per user: selected period, pending conversational input. Survives
# restarts, so a half-finished custom-range or export flow resumes where it was.
ui_sessions = sa.Table(
    "ui_sessions", metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True, autoincrement=False),
    sa.Column("chat_id", sa.BigInteger, nullable=False),
    sa.Column("range_preset", sa.Text),
    sa.Column("range_start", TS),
    sa.Column("range_end", TS),
    sa.Column("state", sa.Text),
    sa.Column("state_data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("updated_at", TS, nullable=False, server_default=_now()),
)

# Export job metadata only; files are sent to Telegram and deleted, never stored here.
report_jobs = sa.Table(
    "report_jobs", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("user_id", sa.BigInteger, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("fmt", sa.Text, nullable=False),
    sa.Column("range_start", TS, nullable=False),
    sa.Column("range_end", TS, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("rows", sa.Integer),
    sa.Column("bytes", sa.Integer),
    sa.Column("file_name", sa.Text),
    sa.Column("error", sa.Text),
    sa.Column("created_at", TS, nullable=False, server_default=_now()),
    sa.Column("finished_at", TS),
    sa.CheckConstraint("status IN ('running','done','failed')", name="status"),
    sa.CheckConstraint("fmt IN ('csv','xlsx')", name="fmt"),
    sa.Index("ix_report_jobs_user_created", "user_id", "created_at"),
)

# Explainable wallet scores (migration 0010). A version row is immutable (configuration + hash);
# every score row is point-in-time: all of its inputs are strictly older than `as_of`.
score_versions = sa.Table(
    "score_versions", metadata,
    sa.Column("version", sa.Text, primary_key=True),
    sa.Column("config", JSONB, nullable=False),
    sa.Column("config_hash", sa.Text, nullable=False),
    sa.Column("created_at", TS, nullable=False, server_default=_now()),
)

wallet_scores = sa.Table(
    "wallet_scores", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("chain_id", sa.BigInteger, nullable=False),
    sa.Column("address", ADDR, nullable=False),
    sa.Column("version", sa.Text, sa.ForeignKey("score_versions.version"), nullable=False),
    sa.Column("as_of", TS, nullable=False),
    sa.Column("score", sa.Numeric(6, 2)),
    sa.Column("weight_available", sa.Numeric(5, 4), nullable=False),
    sa.Column("quality", sa.Text, nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("components", JSONB, nullable=False),
    sa.Column("computed_at", TS, nullable=False, server_default=_now()),
    sa.CheckConstraint("quality IN ('sufficient','limited','insufficient')", name="quality"),
    sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="range"),
    _addr_check("address"),
    sa.UniqueConstraint("chain_id", "address", "version", "as_of", name="uq_wallet_scores_point"),
    sa.Index("ix_wallet_scores_version_asof", "version", "as_of"),
)
