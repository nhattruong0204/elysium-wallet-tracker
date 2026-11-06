"""public read model for Wallet Intelligence: scan coverage of public wallets

`wallet_scan_coverage` (migration 0008) holds the block intervals in which a wallet was really
scanned, and also holds intervals for private (manual / Telegram) wallets, so the table itself
stays out of the public schema. This adds `public_api.wallet_scan_coverage`, restricted to
wallets that appeared in an accepted leaderboard snapshot, so the website can label every
wallet's data quality (tracked since / gaps / derived-from-watchlist coverage) without ever
touching a private wallet.

Additive only: one view, one grant. Downgrade drops the view.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-06
"""
from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

ROLE = "dashboard_ro"


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW public_api.wallet_scan_coverage AS
        SELECT c.chain_id, c.address, c.from_block, c.to_block, c.basis, c.created_at, c.closed_at
        FROM public.wallet_scan_coverage c
        WHERE c.address IN (SELECT address FROM public_api.public_wallets)""")
    op.execute(f"GRANT SELECT ON public_api.wallet_scan_coverage TO {ROLE}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS public_api.wallet_scan_coverage")
