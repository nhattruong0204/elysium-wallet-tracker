"""public dashboard: read-only role `dashboard_ro` and the `public_api` view layer

The public website (onchainflow.app) connects as `dashboard_ro`. That role

* is LOGIN only: no superuser / createdb / createrole / replication, 5 connections max,
  read-only transactions, 5 s statement timeout, `search_path = public_api`;
* has no privilege on anything in schema `public` (the raw tables), only SELECT on the
  views listed below;
* has no password until `--migrate` sets one from DASHBOARD_DB_PASSWORD (scram-sha-256),
  so it cannot log in over the network before the operator provisions it.

The views are owned by the migrating role, run with its rights and are the ONLY place
where the public/private boundary is decided:

  public wallet   a wallet that appears in an accepted Agora leaderboard snapshot (Agora's
                  top list is public). Wallets followed only through watchlist.txt or the
                  Telegram menu are private: no trade, position, PnL, signal, token or
                  count derived from them is visible.
  public token    a token with at least one trade by a public wallet.

Columns that could carry unreviewed content (raw_payload, request_meta, extra, legacy_key,
notification text / chat ids, watchlist labels and reasons) are not exposed. New columns on
the base tables stay invisible until a later migration exposes them.

Additive: nothing existing is touched except removing the PUBLIC TEMPORARY privilege on the
database (defence in depth: a session cannot create temp tables even if it flips its
read-only flag).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ROLE = "dashboard_ro"
SCHEMA = "public_api"

PUBLIC_WALLETS = "SELECT address FROM public_api.public_wallets"

# (view name, SQL). Order matters: helpers first.
VIEWS: list[tuple[str, str]] = [
    ("public_wallets", """
        SELECT DISTINCT e.address
        FROM public.leaderboard_snapshot_entries e
        JOIN public.leaderboard_snapshots s ON s.id = e.snapshot_id
        WHERE s.status IN ('ok', 'imported')"""),
    ("trades", f"""
        SELECT t.id, t.chain_id, t.tx_hash, t.wallet, t.token, t.kind, t.confidence,
               t.hype_method, t.qty, t.hype_wei, t.counter_token, t.counter_qty,
               t.block_number, t.block_time
        FROM public.trades t
        WHERE t.wallet IN ({PUBLIC_WALLETS})"""),
    ("public_tokens", """
        SELECT DISTINCT chain_id, token FROM public_api.trades"""),
    ("wallets", f"""
        SELECT w.chain_id, w.address, w.first_seen_at
        FROM public.wallets w
        WHERE w.address IN ({PUBLIC_WALLETS})"""),
    # only the rows the tracker itself creates from the public leaderboard; label and reason
    # exist for query compatibility but are always NULL
    ("wallet_watchlist", f"""
        SELECT w.id, w.chain_id, w.address, w.source, w.status, NULL::text AS label,
               NULL::text AS reason, w.added_at, w.removed_at
        FROM public.wallet_watchlist w
        WHERE w.source = 'leaderboard' AND w.address IN ({PUBLIC_WALLETS})"""),
    ("wallet_positions", f"""
        SELECT p.chain_id, p.wallet, p.token, p.qty, p.cost_wei, p.realized_wei,
               p.realized_est_wei, p.basis, p.opened_at, p.updated_at
        FROM public.wallet_positions p
        WHERE p.wallet IN ({PUBLIC_WALLETS})"""),
    ("realized_pnl", f"""
        SELECT r.id, r.chain_id, r.trade_id, r.wallet, r.token, r.qty_sold, r.proceeds_wei,
               r.cost_basis_wei, r.pnl_wei, r.status, r.created_at
        FROM public.realized_pnl r
        WHERE r.wallet IN ({PUBLIC_WALLETS})"""),
    # a signal is public only if it has trades and every wallet involved is public
    ("signals", f"""
        SELECT s.id, s.chain_id, s.kind, s.token, s.window_start, s.window_end, s.details,
               s.created_at
        FROM public.signals s
        WHERE EXISTS (SELECT 1 FROM public.signal_trades st WHERE st.signal_id = s.id)
          AND NOT EXISTS (
            SELECT 1 FROM public.signal_trades st
            JOIN public.trades t ON t.id = st.trade_id
            WHERE st.signal_id = s.id AND t.wallet NOT IN ({PUBLIC_WALLETS}))
          AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(
              CASE WHEN jsonb_typeof(s.details -> 'wallets') = 'array'
                   THEN s.details -> 'wallets' ELSE '[]'::jsonb END) AS w(a)
            WHERE w.a NOT IN ({PUBLIC_WALLETS}))"""),
    ("leaderboard_snapshots", """
        SELECT id, chain_id, source, captured_at, status, complete, row_count, accepted_count
        FROM public.leaderboard_snapshots"""),
    ("leaderboard_snapshot_entries", """
        SELECT e.snapshot_id, e.address, e.rank, e.pnl_usd, e.worth_usd, e.volume_usd, e.trades
        FROM public.leaderboard_snapshot_entries e"""),
    ("token_metadata", """
        SELECT m.chain_id, m.address, m.symbol, m.decimals, m.fetched_at
        FROM public.token_metadata m
        WHERE (m.chain_id, m.address) IN (SELECT chain_id, token FROM public_api.public_tokens)"""),
    ("token_profiles", """
        SELECT p.chain_id, p.address, p.symbol, p.name, p.launchpad, p.version, p.launched_at,
               p.first_seen_at, p.updated_at
        FROM public.token_profiles p
        WHERE (p.chain_id, p.address) IN (SELECT chain_id, token FROM public_api.public_tokens)"""),
    ("token_market_snapshots", """
        SELECT m.id, m.chain_id, m.token, m.captured_at, m.source, m.source_synced_at,
               m.source_head, m.price_hype, m.mcap_hype, m.vol_1h_hype, m.vol_24h_hype,
               m.change_1h, m.change_24h, m.buys_24h, m.trades_24h, m.last_trade_at
        FROM public.token_market_snapshots m
        WHERE (m.chain_id, m.token) IN (SELECT chain_id, token FROM public_api.public_tokens)"""),
    ("indexer_checkpoints", """
        SELECT chain_id, name, block_number, updated_at FROM public.indexer_checkpoints"""),
    ("indexed_blocks", """
        SELECT chain_id, block_number, block_time FROM public.indexed_blocks"""),
    # health keys only; rpc_heads holds provider host names (never URLs / keys)
    ("service_state", """
        SELECT key, value, updated_at FROM public.service_state
        WHERE key IN ('rpc_heads', 'heartbeat', 'ui_heartbeat', 'collector_last')"""),
    # alert queue health without any message text, chat id or payload
    ("outbox_status", """
        SELECT status, count(*)::bigint AS n,
               min(created_at) FILTER (WHERE status = 'pending') AS oldest_pending_at,
               max(sent_at) AS last_sent_at
        FROM public.notification_outbox GROUP BY status"""),
]

# What the role may read: everything except the two helper views.
GRANTED = [name for name, _ in VIEWS if name not in ("public_wallets", "public_tokens")]


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.execute(f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
              NOBYPASSRLS CONNECTION LIMIT 5;
          END IF;
        END $$""")
    op.execute(f"ALTER ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
               "NOBYPASSRLS CONNECTION LIMIT 5")
    op.execute(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on")
    op.execute(f"ALTER ROLE {ROLE} SET statement_timeout = '5s'")
    op.execute(f"ALTER ROLE {ROLE} SET lock_timeout = '2s'")
    op.execute(f"ALTER ROLE {ROLE} SET idle_in_transaction_session_timeout = '15s'")
    op.execute(f"ALTER ROLE {ROLE} SET search_path = {SCHEMA}")
    for name, sql in VIEWS:
        op.execute(f"CREATE OR REPLACE VIEW {SCHEMA}.{name} AS {sql}")
    # explicit allow-list: nothing is granted by default or by future default privileges
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM {ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {ROLE}")
    for name in GRANTED:
        op.execute(f"GRANT SELECT ON {SCHEMA}.{name} TO {ROLE}")
    op.execute("DO $$ BEGIN EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', "
               "current_database()); END $$")
    op.execute(f"DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO {ROLE}', "
               "current_database()); END $$")


def downgrade() -> None:
    op.execute("DO $$ BEGIN EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO PUBLIC', "
               "current_database()); END $$")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            DROP OWNED BY {ROLE};
            BEGIN
              DROP ROLE {ROLE};
            EXCEPTION WHEN dependent_objects_still_exist THEN
              -- roles are cluster-wide: another database may still hold grants.
              -- Leave the role in place but make it unusable.
              ALTER ROLE {ROLE} NOLOGIN;
            END;
          END IF;
        END $$""")
