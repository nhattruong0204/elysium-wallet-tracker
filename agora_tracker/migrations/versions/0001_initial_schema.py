"""initial schema

PostgreSQL foundation: chains, checkpoints, wallets/watchlist history, leaderboard
snapshots, raw transactions and Transfer logs, confidence-tagged trades, positions,
realized PnL audit trail, signals and the notification outbox. See agora_tracker/schema.py
for the precision rationale. Downgrade drops everything (take a pg_dump first).

Revision ID: 0001
Revises: (none)
Create Date: 2026-11-02 14:54:30.664564
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('chains',
    sa.Column('chain_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('genesis_hash', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('chain_id', name=op.f('pk_chains'))
    )
    op.create_table('notification_outbox',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('dedup_key', sa.Text(), nullable=False),
    sa.Column('chat_id', sa.Text(), nullable=True),
    sa.Column('severity', sa.Text(), server_default='info', nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('pending','sent','dead')", name=op.f('ck_notification_outbox_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notification_outbox')),
    sa.UniqueConstraint('dedup_key', name=op.f('uq_notification_outbox_dedup_key'))
    )
    op.create_index('ix_notification_outbox_due', 'notification_outbox', ['next_attempt_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_table('service_state',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_service_state'))
    )
    op.create_table('indexed_blocks',
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('block_number', sa.BigInteger(), nullable=False),
    sa.Column('block_hash', sa.Text(), nullable=True),
    sa.Column('parent_hash', sa.Text(), nullable=True),
    sa.Column('block_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('indexed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chain_id'], ['chains.chain_id'], name=op.f('fk_indexed_blocks_chain_id_chains')),
    sa.PrimaryKeyConstraint('chain_id', 'block_number', name=op.f('pk_indexed_blocks'))
    )
    op.create_table('indexer_checkpoints',
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('block_number', sa.BigInteger(), nullable=False),
    sa.Column('block_hash', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chain_id'], ['chains.chain_id'], name=op.f('fk_indexer_checkpoints_chain_id_chains')),
    sa.PrimaryKeyConstraint('chain_id', 'name', name=op.f('pk_indexer_checkpoints'))
    )
    op.create_table('leaderboard_snapshots',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('complete', sa.Boolean(), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('accepted_count', sa.Integer(), nullable=False),
    sa.Column('request_meta', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.CheckConstraint("status IN ('ok','degraded','failed','imported')", name=op.f('ck_leaderboard_snapshots_status')),
    sa.ForeignKeyConstraint(['chain_id'], ['chains.chain_id'], name=op.f('fk_leaderboard_snapshots_chain_id_chains')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_leaderboard_snapshots'))
    )
    op.create_index('ix_leaderboard_snapshots_chain_captured', 'leaderboard_snapshots', ['chain_id', 'captured_at'], unique=False)
    op.create_table('notification_deliveries',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('outbox_id', sa.BigInteger(), nullable=False),
    sa.Column('attempted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['outbox_id'], ['notification_outbox.id'], name=op.f('fk_notification_deliveries_outbox_id_notification_outbox'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notification_deliveries'))
    )
    op.create_index('ix_notification_deliveries_outbox', 'notification_deliveries', ['outbox_id'], unique=False)
    op.create_table('signals',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('token', sa.String(length=42), nullable=False),
    sa.Column('dedup_key', sa.Text(), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('window_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chain_id'], ['chains.chain_id'], name=op.f('fk_signals_chain_id_chains')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_signals')),
    sa.UniqueConstraint('dedup_key', name=op.f('uq_signals_dedup_key'))
    )
    op.create_index('ix_signals_token_created', 'signals', ['chain_id', 'token', 'created_at'], unique=False)
    op.create_table('token_metadata',
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('address', sa.String(length=42), nullable=False),
    sa.Column('symbol', sa.Text(), nullable=False),
    sa.Column('decimals', sa.SmallInteger(), nullable=False),
    sa.Column('source', sa.Text(), server_default='rpc', nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("address ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_token_metadata_address_lower_hex')),
    sa.ForeignKeyConstraint(['chain_id'], ['chains.chain_id'], name=op.f('fk_token_metadata_chain_id_chains')),
    sa.PrimaryKeyConstraint('chain_id', 'address', name=op.f('pk_token_metadata'))
    )
    op.create_table('transactions',
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('tx_hash', sa.String(length=66), nullable=False),
    sa.Column('block_number', sa.BigInteger(), nullable=False),
    sa.Column('from_address', sa.String(length=42), nullable=True),
    sa.Column('to_address', sa.String(length=42), nullable=True),
    sa.Column('value_wei', sa.Numeric(precision=78, scale=0), nullable=True),
    sa.Column('nonce', sa.BigInteger(), nullable=True),
    sa.Column('status', sa.SmallInteger(), nullable=True),
    sa.Column('gas_used', sa.Numeric(precision=78, scale=0), nullable=True),
    sa.Column('effective_gas_price', sa.Numeric(precision=78, scale=0), nullable=True),
    sa.Column('fee_wei', sa.Numeric(precision=78, scale=0), nullable=True),
    sa.Column('inserted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chain_id'], ['chains.chain_id'], name=op.f('fk_transactions_chain_id_chains')),
    sa.PrimaryKeyConstraint('chain_id', 'tx_hash', name=op.f('pk_transactions'))
    )
    op.create_index('ix_transactions_chain_block', 'transactions', ['chain_id', 'block_number'], unique=False)
    op.create_table('wallet_positions',
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('wallet', sa.String(length=42), nullable=False),
    sa.Column('token', sa.String(length=42), nullable=False),
    sa.Column('qty', sa.Numeric(precision=78, scale=0), nullable=False),
    sa.Column('cost_wei', sa.Numeric(precision=78, scale=0), nullable=False),
    sa.Column('realized_wei', sa.Numeric(precision=78, scale=0), server_default='0', nullable=False),
    sa.Column('realized_est_wei', sa.Numeric(precision=78, scale=0), server_default='0', nullable=False),
    sa.Column('basis', sa.Text(), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("basis IN ('known','estimated','partial_unknown','legacy')", name=op.f('ck_wallet_positions_basis')),
    sa.CheckConstraint("token ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_wallet_positions_token_lower_hex')),
    sa.CheckConstraint("wallet ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_wallet_positions_wallet_lower_hex')),
    sa.CheckConstraint('qty >= 0 AND cost_wei >= 0', name=op.f('ck_wallet_positions_nonneg')),
    sa.ForeignKeyConstraint(['chain_id'], ['chains.chain_id'], name=op.f('fk_wallet_positions_chain_id_chains')),
    sa.PrimaryKeyConstraint('chain_id', 'wallet', 'token', name=op.f('pk_wallet_positions'))
    )
    op.create_table('wallets',
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('address', sa.String(length=42), nullable=False),
    sa.Column('address_checksum', sa.String(length=42), nullable=True),
    sa.Column('is_contract', sa.Boolean(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("address ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_wallets_address_lower_hex')),
    sa.ForeignKeyConstraint(['chain_id'], ['chains.chain_id'], name=op.f('fk_wallets_chain_id_chains')),
    sa.PrimaryKeyConstraint('chain_id', 'address', name=op.f('pk_wallets'))
    )
    op.create_table('leaderboard_snapshot_entries',
    sa.Column('snapshot_id', sa.BigInteger(), nullable=False),
    sa.Column('address', sa.String(length=42), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('pnl_usd', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('worth_usd', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('volume_usd', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('trades', sa.Integer(), nullable=True),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.CheckConstraint("address ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_leaderboard_snapshot_entries_address_lower_hex')),
    sa.ForeignKeyConstraint(['snapshot_id'], ['leaderboard_snapshots.id'], name=op.f('fk_leaderboard_snapshot_entries_snapshot_id_leaderboard_snapshots'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('snapshot_id', 'address', name=op.f('pk_leaderboard_snapshot_entries'))
    )
    op.create_index('ix_leaderboard_snapshot_entries_address', 'leaderboard_snapshot_entries', ['address'], unique=False)
    op.create_table('token_transfers',
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('tx_hash', sa.String(length=66), nullable=False),
    sa.Column('log_index', sa.Integer(), nullable=False),
    sa.Column('block_number', sa.BigInteger(), nullable=False),
    sa.Column('token', sa.String(length=42), nullable=False),
    sa.Column('from_address', sa.String(length=42), nullable=False),
    sa.Column('to_address', sa.String(length=42), nullable=False),
    sa.Column('amount', sa.Numeric(precision=78, scale=0), nullable=False),
    sa.CheckConstraint("from_address ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_token_transfers_from_address_lower_hex')),
    sa.CheckConstraint("to_address ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_token_transfers_to_address_lower_hex')),
    sa.CheckConstraint("token ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_token_transfers_token_lower_hex')),
    sa.CheckConstraint('amount >= 0', name=op.f('ck_token_transfers_amount_nonneg')),
    sa.ForeignKeyConstraint(['chain_id', 'tx_hash'], ['transactions.chain_id', 'transactions.tx_hash'], name=op.f('fk_token_transfers_chain_id_transactions')),
    sa.PrimaryKeyConstraint('chain_id', 'tx_hash', 'log_index', name=op.f('pk_token_transfers'))
    )
    op.create_table('trades',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('tx_hash', sa.String(length=66), nullable=False),
    sa.Column('wallet', sa.String(length=42), nullable=False),
    sa.Column('token', sa.String(length=42), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Text(), nullable=False),
    sa.Column('hype_method', sa.Text(), nullable=False),
    sa.Column('qty', sa.Numeric(precision=78, scale=0), nullable=False),
    sa.Column('hype_wei', sa.Numeric(precision=78, scale=0), nullable=True),
    sa.Column('counter_token', sa.String(length=42), nullable=True),
    sa.Column('counter_qty', sa.Numeric(precision=78, scale=0), nullable=True),
    sa.Column('block_number', sa.BigInteger(), nullable=False),
    sa.Column('block_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('legacy_key', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("confidence IN ('high','estimated','unknown','legacy')", name=op.f('ck_trades_confidence')),
    sa.CheckConstraint("kind IN ('BUY','SELL','SWAP','TRANSFER_IN','TRANSFER_OUT','MULTI')", name=op.f('ck_trades_kind')),
    sa.CheckConstraint("token ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_trades_token_lower_hex')),
    sa.CheckConstraint("wallet ~ '^0x[0-9a-f]{40}$'", name=op.f('ck_trades_wallet_lower_hex')),
    sa.CheckConstraint('qty >= 0 AND (hype_wei IS NULL OR hype_wei >= 0)', name=op.f('ck_trades_nonneg')),
    sa.ForeignKeyConstraint(['chain_id', 'tx_hash'], ['transactions.chain_id', 'transactions.tx_hash'], name=op.f('fk_trades_chain_id_transactions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trades')),
    sa.UniqueConstraint('chain_id', 'tx_hash', 'wallet', 'token', name='uq_trades_leg'),
    sa.UniqueConstraint('legacy_key', name=op.f('uq_trades_legacy_key'))
    )
    op.create_index('ix_trades_token_kind_time', 'trades', ['chain_id', 'token', 'kind', 'block_time'], unique=False)
    op.create_index('ix_trades_wallet_block', 'trades', ['chain_id', 'wallet', 'block_number'], unique=False)
    op.create_table('wallet_watchlist',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('address', sa.String(length=42), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='active', nullable=False),
    sa.Column('label', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("source IN ('leaderboard','manual','reverse_discovery','import')", name=op.f('ck_wallet_watchlist_source')),
    sa.CheckConstraint("status IN ('active','inactive','excluded')", name=op.f('ck_wallet_watchlist_status')),
    sa.ForeignKeyConstraint(['chain_id', 'address'], ['wallets.chain_id', 'wallets.address'], name=op.f('fk_wallet_watchlist_chain_id_wallets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_wallet_watchlist'))
    )
    op.create_index('ux_wallet_watchlist_current', 'wallet_watchlist', ['chain_id', 'address', 'source'], unique=True, postgresql_where=sa.text('removed_at IS NULL'))
    op.create_table('realized_pnl',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('chain_id', sa.BigInteger(), nullable=False),
    sa.Column('trade_id', sa.BigInteger(), nullable=False),
    sa.Column('wallet', sa.String(length=42), nullable=False),
    sa.Column('token', sa.String(length=42), nullable=False),
    sa.Column('qty_sold', sa.Numeric(precision=78, scale=0), nullable=False),
    sa.Column('proceeds_wei', sa.Numeric(precision=78, scale=0), nullable=False),
    sa.Column('cost_basis_wei', sa.Numeric(precision=78, scale=0), nullable=True),
    sa.Column('pnl_wei', sa.Numeric(precision=78, scale=0), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('measured','estimated','unknown')", name=op.f('ck_realized_pnl_status')),
    sa.ForeignKeyConstraint(['chain_id'], ['chains.chain_id'], name=op.f('fk_realized_pnl_chain_id_chains')),
    sa.ForeignKeyConstraint(['trade_id'], ['trades.id'], name=op.f('fk_realized_pnl_trade_id_trades')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_realized_pnl')),
    sa.UniqueConstraint('trade_id', name=op.f('uq_realized_pnl_trade_id'))
    )
    op.create_table('signal_trades',
    sa.Column('signal_id', sa.BigInteger(), nullable=False),
    sa.Column('trade_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['signal_id'], ['signals.id'], name=op.f('fk_signal_trades_signal_id_signals'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['trade_id'], ['trades.id'], name=op.f('fk_signal_trades_trade_id_trades')),
    sa.PrimaryKeyConstraint('signal_id', 'trade_id', name=op.f('pk_signal_trades'))
    )


def downgrade() -> None:
    op.drop_table('signal_trades')
    op.drop_table('realized_pnl')
    op.drop_index('ux_wallet_watchlist_current', table_name='wallet_watchlist', postgresql_where=sa.text('removed_at IS NULL'))
    op.drop_table('wallet_watchlist')
    op.drop_index('ix_trades_wallet_block', table_name='trades')
    op.drop_index('ix_trades_token_kind_time', table_name='trades')
    op.drop_table('trades')
    op.drop_table('token_transfers')
    op.drop_index('ix_leaderboard_snapshot_entries_address', table_name='leaderboard_snapshot_entries')
    op.drop_table('leaderboard_snapshot_entries')
    op.drop_table('wallets')
    op.drop_table('wallet_positions')
    op.drop_index('ix_transactions_chain_block', table_name='transactions')
    op.drop_table('transactions')
    op.drop_table('token_metadata')
    op.drop_index('ix_signals_token_created', table_name='signals')
    op.drop_table('signals')
    op.drop_index('ix_notification_deliveries_outbox', table_name='notification_deliveries')
    op.drop_table('notification_deliveries')
    op.drop_index('ix_leaderboard_snapshots_chain_captured', table_name='leaderboard_snapshots')
    op.drop_table('leaderboard_snapshots')
    op.drop_table('indexer_checkpoints')
    op.drop_table('indexed_blocks')
    op.drop_table('service_state')
    op.drop_index('ix_notification_outbox_due', table_name='notification_outbox', postgresql_where=sa.text("status = 'pending'"))
    op.drop_table('notification_outbox')
    op.drop_table('chains')
