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
    sa.CheckConstraint("source IN ('leaderboard','manual','reverse_discovery','import')",
                       name="source"),
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
