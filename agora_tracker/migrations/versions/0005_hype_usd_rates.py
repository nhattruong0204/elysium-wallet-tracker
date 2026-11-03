"""HYPE/USD rate history, so market caps can be shown in USD at the time they were observed

Agora quotes every price and market cap in HYPE and attaches the HYPE/USD rate it uses
(`hypeUsd`) to every leaderboard response. The tracker already stores that response in
`leaderboard_snapshots.raw_payload` every ~5 minutes, which is a time-stamped rate series
from the same provider and the same moment as the market data. This migration

* creates `hype_usd_rates` (one row per observation, source-tagged, unique per source+time),
* back-fills it from the stored payloads (rows without a valid positive number are skipped),
* keeps it current with a trigger on `leaderboard_snapshots`, so no application change and no
  container restart is needed for new observations. The trigger can never fail an insert:
  a problem is logged as a WARNING and the leaderboard row is written regardless,
* exposes only `(observed_at, hype_usd, source)` to the public role through
  `public_api.hype_usd_rates`. `raw_payload` stays private.

`token_market_snapshots.mcap_hype` is untouched: it remains the source evidence. USD is
derived at read time from the rate closest in time to each snapshot (see web/usd.py), so
history never changes when HYPE moves.

It also replaces the definition of the helper view `public_api.public_wallets` with an
equivalent that does not scan the whole leaderboard history. Every dashboard query joins to
that view; `SELECT DISTINCT address` over ~29,000 new entry rows per day (100 per 5-minute
snapshot) cost 160 ms at 30 days of history and grows linearly. The recursive "skip scan"
below walks the address index (one probe per distinct wallet, ~6 ms at the same size) and
returns exactly the same set: addresses with at least one entry in an `ok`/`imported`
snapshot. tests/test_usd.py proves the equivalence, privacy tests still guard the boundary.

Additive and idempotent; no data is rewritten.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

ROLE = "dashboard_ro"
SOURCE = "agora_leaderboard"
TS = sa.DateTime(timezone=True)

# rates outside (0, 1e6) are provider glitches, never prices
VALID = "r > 0 AND r < 1000000"

FUNCTION = f"""
CREATE OR REPLACE FUNCTION public.record_hype_usd_rate() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE r numeric;
BEGIN
  BEGIN
    IF NEW.raw_payload IS NOT NULL AND jsonb_typeof(NEW.raw_payload) = 'object'
       AND jsonb_typeof(NEW.raw_payload -> 'hypeUsd') = 'number' THEN
      r := (NEW.raw_payload ->> 'hypeUsd')::numeric;
      IF {VALID} THEN
        INSERT INTO public.hype_usd_rates (observed_at, hype_usd, source)
        VALUES (NEW.captured_at, round(r, 8), '{SOURCE}')
        ON CONFLICT (source, observed_at) DO NOTHING;
      END IF;
    END IF;
  EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'hype_usd_rates: could not record a rate (%): %', SQLSTATE, SQLERRM;
  END;
  RETURN NEW;
END
$fn$"""


PUBLIC_WALLETS_OLD = """
        SELECT DISTINCT e.address
        FROM public.leaderboard_snapshot_entries e
        JOIN public.leaderboard_snapshots s ON s.id = e.snapshot_id
        WHERE s.status IN ('ok', 'imported')"""

PUBLIC_WALLETS_NEW = """
        WITH RECURSIVE w(address) AS (
          (SELECT e.address FROM public.leaderboard_snapshot_entries e
            WHERE EXISTS (SELECT 1 FROM public.leaderboard_snapshots s
                          WHERE s.id = e.snapshot_id AND s.status IN ('ok', 'imported'))
            ORDER BY e.address LIMIT 1)
          UNION ALL
          SELECT (SELECT e.address FROM public.leaderboard_snapshot_entries e
                   WHERE e.address > w.address
                     AND EXISTS (SELECT 1 FROM public.leaderboard_snapshots s
                                 WHERE s.id = e.snapshot_id AND s.status IN ('ok', 'imported'))
                   ORDER BY e.address LIMIT 1)
          FROM w WHERE w.address IS NOT NULL)
        SELECT address FROM w WHERE address IS NOT NULL"""

PUBLIC_TOKENS_OLD = """
        SELECT DISTINCT chain_id, token FROM public_api.trades"""

# skip-scan over ix_trades_token_kind_time: same rows as the DISTINCT form, ~15 ms vs ~500 ms at 1M trades
PUBLIC_TOKENS_NEW = """
        WITH RECURSIVE w(chain_id, token) AS (
          (SELECT t.chain_id, t.token FROM public.trades t
            WHERE t.wallet IN (SELECT address FROM public_api.public_wallets)
            ORDER BY t.chain_id, t.token LIMIT 1)
          UNION ALL
          SELECT n.chain_id, n.token FROM w, LATERAL (
            SELECT t.chain_id, t.token FROM public.trades t
             WHERE (t.chain_id, t.token) > (w.chain_id, w.token)
               AND t.wallet IN (SELECT address FROM public_api.public_wallets)
             ORDER BY t.chain_id, t.token LIMIT 1) n)
        SELECT chain_id, token FROM w"""


def upgrade() -> None:
    op.create_table(
        "hype_usd_rates",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("hype_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hype_usd_rates")),
        sa.UniqueConstraint("source", "observed_at", name=op.f("uq_hype_usd_rates_obs")),
        sa.CheckConstraint("hype_usd > 0", name=op.f("ck_hype_usd_rates_positive")),
    )
    op.create_index(op.f("ix_hype_usd_rates_observed_at"), "hype_usd_rates", ["observed_at"])

    op.execute(f"""
        INSERT INTO public.hype_usd_rates (observed_at, hype_usd, source)
        SELECT captured_at, round(r, 8), '{SOURCE}'
        FROM (SELECT captured_at,
                     CASE WHEN jsonb_typeof(raw_payload) = 'object'
                           AND jsonb_typeof(raw_payload -> 'hypeUsd') = 'number'
                          THEN (raw_payload ->> 'hypeUsd')::numeric END AS r
              FROM public.leaderboard_snapshots WHERE raw_payload IS NOT NULL) x
        WHERE r IS NOT NULL AND {VALID}
        ON CONFLICT (source, observed_at) DO NOTHING""")

    op.execute(FUNCTION)
    op.execute("DROP TRIGGER IF EXISTS trg_leaderboard_hype_usd ON public.leaderboard_snapshots")
    op.execute("""
        CREATE TRIGGER trg_leaderboard_hype_usd
        AFTER INSERT OR UPDATE OF raw_payload ON public.leaderboard_snapshots
        FOR EACH ROW EXECUTE FUNCTION public.record_hype_usd_rate()""")

    op.execute("""
        CREATE OR REPLACE VIEW public_api.hype_usd_rates AS
        SELECT r.observed_at, r.hype_usd, r.source FROM public.hype_usd_rates r""")
    op.execute(f"GRANT SELECT ON public_api.hype_usd_rates TO {ROLE}")
    op.execute(f"CREATE OR REPLACE VIEW public_api.public_wallets AS {PUBLIC_WALLETS_NEW}")
    op.execute(f"CREATE OR REPLACE VIEW public_api.public_tokens AS {PUBLIC_TOKENS_NEW}")


def downgrade() -> None:
    op.execute(f"CREATE OR REPLACE VIEW public_api.public_tokens AS {PUBLIC_TOKENS_OLD}")
    op.execute(f"CREATE OR REPLACE VIEW public_api.public_wallets AS {PUBLIC_WALLETS_OLD}")
    op.execute("DROP VIEW IF EXISTS public_api.hype_usd_rates")
    op.execute("DROP TRIGGER IF EXISTS trg_leaderboard_hype_usd ON public.leaderboard_snapshots")
    op.execute("DROP FUNCTION IF EXISTS public.record_hype_usd_rate()")
    op.drop_index(op.f("ix_hype_usd_rates_observed_at"), table_name="hype_usd_rates")
    op.drop_table("hype_usd_rates")
