"""receipts, reorg rollback and verified fills (Stage C2 / issue #94)

* `order_legs` gains receipt facts (block, receipt status, gas) and an `orphaned` state for a
  transaction whose block left the canonical chain. `orphaned` is NOT in the one-unsettled-leg
  index: after a reorg the transaction usually re-enters the mempool, and reconciliation (not a
  wallet block) decides what happened next.
* `order_fills`: what a swap *actually* moved, from on-chain evidence only. One row per inclusion
  (`UNIQUE (leg_id, block_hash)`), so re-reconciling the same inclusion can never double count,
  and a reorg marks the old row `rolled_back` instead of deleting it. `final` and `rolled_back`
  rows are immutable. A `verdict` of `unknown` is kept as a row so the gap is visible and PnL is
  reported as unknown rather than guessed.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-08
"""
from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

S = "trading_private"
ROLE = "trading_rw"
STATES = ("prompted", "hash_reported", "ambiguous", "included", "reverted", "rejected",
          "superseded", "abandoned", "orphaned")


def _guard(include_orphaned: bool) -> str:
    inc = "'reverted', 'ambiguous', 'orphaned'" if include_orphaned else "'reverted', 'ambiguous'"
    rev = "'included', 'ambiguous', 'orphaned'" if include_orphaned else "'included', 'ambiguous'"
    orphan = ("""
            OR (OLD.state = 'orphaned' AND NEW.state IN ('hash_reported', 'included', 'reverted',
                                    'ambiguous', 'superseded', 'abandoned'))"""
              if include_orphaned else "")
    final = ("""          IF OLD.finalized_at IS NOT NULL AND (
               NEW.state <> OLD.state OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at
               OR NEW.block_hash IS DISTINCT FROM OLD.block_hash
               OR NEW.block_number IS DISTINCT FROM OLD.block_number
               OR NEW.receipt_status IS DISTINCT FROM OLD.receipt_status) THEN
            RAISE EXCEPTION 'a finalized leg is immutable'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
""" if include_orphaned else "")
    return f"""
        CREATE OR REPLACE FUNCTION {S}.legs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP <> 'UPDATE' THEN
            RAISE EXCEPTION 'order_legs rows are never deleted'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF NEW.version <> OLD.version + 1 THEN
            RAISE EXCEPTION 'order_legs change by exactly one version'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF (OLD.tx_hash IS NOT NULL AND NEW.tx_hash IS DISTINCT FROM OLD.tx_hash)
             OR (OLD.nonce IS NOT NULL AND NEW.nonce IS DISTINCT FROM OLD.nonce)
             OR (OLD.final_tx_hash IS NOT NULL
                 AND NEW.final_tx_hash IS DISTINCT FROM OLD.final_tx_hash) THEN
            RAISE EXCEPTION 'hash and nonce of a leg are set once'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
{final}          IF NEW.state <> OLD.state AND NOT (
               (OLD.state = 'prompted'
                  AND NEW.state IN ('hash_reported', 'rejected', 'ambiguous', 'abandoned'))
            OR (OLD.state = 'hash_reported'
                  AND NEW.state IN ('ambiguous', 'included', 'reverted', 'superseded',
                                    'abandoned'))
            OR (OLD.state = 'ambiguous'
                  AND NEW.state IN ('hash_reported', 'included', 'reverted', 'superseded',
                                    'abandoned'))
            OR (OLD.state = 'included' AND NEW.state IN ({inc}))
            OR (OLD.state = 'reverted' AND NEW.state IN ({rev})){orphan}
          ) THEN
            RAISE EXCEPTION 'leg state % -> % is not allowed', OLD.state, NEW.state
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END $$"""


def upgrade() -> None:
    states = ", ".join(f"'{s}'" for s in STATES)
    op.execute(f"ALTER TABLE {S}.order_legs DROP CONSTRAINT IF EXISTS order_legs_state_check")
    op.execute(f"ALTER TABLE {S}.order_legs ADD CONSTRAINT order_legs_state_check "
               f"CHECK (state IN ({states}))")
    op.execute(f"""
        ALTER TABLE {S}.order_legs
          ADD COLUMN block_number    bigint,
          ADD COLUMN block_hash      char(66) CHECK (block_hash IS NULL
                                                     OR block_hash ~ '^0x[0-9a-f]{{64}}$'),
          ADD COLUMN receipt_status  smallint CHECK (receipt_status IS NULL
                                                     OR receipt_status IN (0, 1)),
          ADD COLUMN gas_used        numeric(20, 0),
          ADD COLUMN gas_cost_wei    numeric(78, 0),
          ADD COLUMN included_at     timestamptz,
          ADD COLUMN finalized_at    timestamptz""")
    op.execute(_guard(True))
    op.execute(f"""GRANT UPDATE (block_number, block_hash, receipt_status, gas_used, gas_cost_wei,
                   included_at, finalized_at) ON {S}.order_legs TO {ROLE}""")
    op.execute(f"""CREATE INDEX ix_legs_receipt ON {S}.order_legs (state, id)
                   WHERE finalized_at IS NULL
                     AND state IN ('hash_reported', 'included', 'reverted', 'orphaned')""")

    op.execute(f"""
        CREATE TABLE {S}.order_fills (
          id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          order_id      uuid NOT NULL REFERENCES {S}.orders (id),
          leg_id        bigint NOT NULL REFERENCES {S}.order_legs (id),
          user_id       bigint NOT NULL,
          wallet        char(42) NOT NULL,
          chain_id      bigint NOT NULL,
          token         char(42) NOT NULL,
          side          text NOT NULL CHECK (side IN ('buy', 'sell')),
          tx_hash       char(66) NOT NULL CHECK (tx_hash ~ '^0x[0-9a-f]{{64}}$'),
          block_number  bigint NOT NULL,
          block_hash    char(66) NOT NULL CHECK (block_hash ~ '^0x[0-9a-f]{{64}}$'),
          verdict       text NOT NULL CHECK (verdict IN ('verified', 'unknown', 'reverted')),
          native_wei    numeric(78, 0) CHECK (native_wei IS NULL OR native_wei >= 0),
          token_wei     numeric(78, 0) CHECK (token_wei IS NULL OR token_wei >= 0),
          gas_cost_wei  numeric(78, 0) NOT NULL CHECK (gas_cost_wei >= 0),
          status        text NOT NULL DEFAULT 'provisional'
                        CHECK (status IN ('provisional', 'final', 'rolled_back')),
          basis         text CHECK (basis IS NULL OR length(basis) <= 200),
          reason        text CHECK (reason IS NULL OR length(reason) <= 200),
          created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
          updated_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
          CONSTRAINT uq_fill_inclusion UNIQUE (leg_id, block_hash),
          CONSTRAINT ck_fill_verified CHECK (verdict <> 'verified'
                                             OR (native_wei IS NOT NULL AND token_wei IS NOT NULL)),
          CONSTRAINT ck_fill_empty CHECK (verdict = 'verified'
                                          OR (native_wei IS NULL AND token_wei IS NULL))
        )""")
    op.execute(f"CREATE INDEX ix_fills_user ON {S}.order_fills (user_id, wallet, token)")
    op.execute(f"""
        CREATE FUNCTION {S}.fills_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP <> 'UPDATE' THEN
            RAISE EXCEPTION 'order_fills rows are never deleted'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF NEW.id <> OLD.id OR NEW.order_id <> OLD.order_id OR NEW.leg_id <> OLD.leg_id
             OR NEW.user_id <> OLD.user_id OR NEW.wallet <> OLD.wallet OR NEW.token <> OLD.token
             OR NEW.side <> OLD.side OR NEW.tx_hash <> OLD.tx_hash
             OR NEW.block_number <> OLD.block_number OR NEW.block_hash <> OLD.block_hash
             OR NEW.verdict <> OLD.verdict OR NEW.native_wei IS DISTINCT FROM OLD.native_wei
             OR NEW.token_wei IS DISTINCT FROM OLD.token_wei
             OR NEW.gas_cost_wei <> OLD.gas_cost_wei THEN
            RAISE EXCEPTION 'a recorded fill changes only its status'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF NEW.status <> OLD.status AND NOT (OLD.status = 'provisional'
                                               AND NEW.status IN ('final', 'rolled_back')) THEN
            RAISE EXCEPTION 'fill status % -> % is not allowed', OLD.status, NEW.status
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          NEW.updated_at := clock_timestamp();
          RETURN NEW;
        END $$""")
    op.execute(f"""CREATE TRIGGER order_fills_guard BEFORE UPDATE OR DELETE ON {S}.order_fills
                   FOR EACH ROW EXECUTE FUNCTION {S}.fills_guard()""")
    op.execute(f"""CREATE TRIGGER order_fills_no_truncate BEFORE TRUNCATE ON {S}.order_fills
                   FOR EACH STATEMENT EXECUTE FUNCTION {S}.refuse_change()""")
    scope = ("(user_id = nullif(current_setting('trading.user_id', true), '')::bigint "
             "OR current_setting('trading.system', true) = 'on')")
    op.execute(f"ALTER TABLE {S}.order_fills ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY order_fills_scope ON {S}.order_fills FOR ALL TO {ROLE} "
               f"USING {scope} WITH CHECK {scope}")
    op.execute(f"GRANT SELECT, INSERT ON {S}.order_fills TO {ROLE}")
    op.execute(f"GRANT UPDATE (status, updated_at) ON {S}.order_fills TO {ROLE}")


def downgrade() -> None:
    # Fills are accounting records: take a backup first (docs/TRADING_RECEIPTS.md).
    op.execute(f"DROP TABLE IF EXISTS {S}.order_fills")
    op.execute(f"DROP FUNCTION IF EXISTS {S}.fills_guard()")
    op.execute(f"DROP INDEX IF EXISTS {S}.ix_legs_receipt")
    # Legs that reached a receipt state keep working under the 0013 edges; `orphaned` rows cannot
    # exist after the narrower CHECK, so fold them back into `ambiguous` first.
    op.execute(f"ALTER TABLE {S}.order_legs DISABLE TRIGGER order_legs_guard")
    op.execute(f"UPDATE {S}.order_legs SET state = 'ambiguous' WHERE state = 'orphaned'")
    op.execute(f"ALTER TABLE {S}.order_legs ENABLE TRIGGER order_legs_guard")
    op.execute(f"""
        ALTER TABLE {S}.order_legs
          DROP COLUMN IF EXISTS finalized_at, DROP COLUMN IF EXISTS included_at,
          DROP COLUMN IF EXISTS gas_cost_wei, DROP COLUMN IF EXISTS gas_used,
          DROP COLUMN IF EXISTS receipt_status, DROP COLUMN IF EXISTS block_hash,
          DROP COLUMN IF EXISTS block_number""")
    states = ", ".join(f"'{s}'" for s in STATES if s != "orphaned")
    op.execute(f"ALTER TABLE {S}.order_legs DROP CONSTRAINT IF EXISTS order_legs_state_check")
    op.execute(f"ALTER TABLE {S}.order_legs ADD CONSTRAINT order_legs_state_check "
               f"CHECK (state IN ({states}))")
    op.execute(_guard(False))
