"""corroborated chain evidence (Stage C2 / issue #95 finding F-15)

`chain_evidence` is the append-only provenance of every ledger write that rests on chain facts:
which sources (name + operator), at which anchor block, how recently, with which reads, agreed.
A row exists only together with the leg change it justifies, in one transaction.

* receipts, finalization, verified fills, reorg/orphaning and automatic supersession/abandonment
  cannot be written without a row showing >= 2 distinct, agreeing sources (`ck_evidence_sources`);
* `kind = 'conflict'` rows record that sources DISAGREED (nothing was concluded); deduplicated per
  (leg, reason) so a persistent disagreement does not grow the table;
* fills point at the evidence that certified them; a `verified` fill must have one
  (`ck_fill_verified_evidence`, NOT VALID: rows written before this migration are not rechecked,
  and none can exist from a live deployment because execution has never been enabled);
* append-only: UPDATE / DELETE / TRUNCATE refused by trigger; `trading_rw` has SELECT, INSERT only.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-09
"""
from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

S = "trading_private"
ROLE = "trading_rw"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {S}.chain_evidence (
          id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          leg_id           bigint NOT NULL REFERENCES {S}.order_legs (id),
          order_id         uuid NOT NULL REFERENCES {S}.orders (id),
          user_id          bigint NOT NULL,
          kind             text NOT NULL
                           CHECK (kind IN ('inclusion', 'final', 'reorg', 'resolution', 'conflict')),
          chain_id         bigint NOT NULL,
          anchor_number    bigint,
          anchor_hash      char(66) CHECK (anchor_hash IS NULL OR anchor_hash ~ '^0x[0-9a-f]{{64}}$'),
          source_count     integer NOT NULL CHECK (source_count >= 0),
          source_agreement boolean NOT NULL,
          method_agreement boolean,
          code             text CHECK (code IS NULL OR length(code) <= 80),
          evidence         jsonb NOT NULL,
          evidence_digest  char(64) NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{{64}}$'),
          created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
          CONSTRAINT uq_evidence UNIQUE (leg_id, kind, evidence_digest),
          CONSTRAINT ck_evidence_sources CHECK (
            kind = 'conflict'
            OR (source_count >= 2 AND source_agreement AND anchor_number IS NOT NULL
                AND anchor_hash IS NOT NULL))
        )""")
    op.execute(f"CREATE INDEX ix_evidence_leg ON {S}.chain_evidence (leg_id, id)")
    op.execute(f"""CREATE TRIGGER chain_evidence_append_only BEFORE UPDATE OR DELETE
                   ON {S}.chain_evidence FOR EACH ROW EXECUTE FUNCTION {S}.refuse_change()""")
    op.execute(f"""CREATE TRIGGER chain_evidence_no_truncate BEFORE TRUNCATE ON {S}.chain_evidence
                   FOR EACH STATEMENT EXECUTE FUNCTION {S}.refuse_change()""")
    scope = ("(user_id = nullif(current_setting('trading.user_id', true), '')::bigint "
             "OR current_setting('trading.system', true) = 'on')")
    op.execute(f"ALTER TABLE {S}.chain_evidence ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY chain_evidence_scope ON {S}.chain_evidence FOR ALL TO {ROLE} "
               f"USING {scope} WITH CHECK {scope}")
    op.execute(f"GRANT SELECT, INSERT ON {S}.chain_evidence TO {ROLE}")
    op.execute(f"ALTER TABLE {S}.order_fills ADD COLUMN evidence_id bigint "
               f"REFERENCES {S}.chain_evidence (id)")
    op.execute(f"""ALTER TABLE {S}.order_fills ADD CONSTRAINT ck_fill_verified_evidence
                   CHECK (verdict <> 'verified' OR evidence_id IS NOT NULL) NOT VALID""")


def downgrade() -> None:
    # An evidence row is an audit record: take a backup first.
    op.execute(f"ALTER TABLE {S}.order_fills DROP CONSTRAINT IF EXISTS ck_fill_verified_evidence")
    op.execute(f"ALTER TABLE {S}.order_fills DROP COLUMN IF EXISTS evidence_id")
    op.execute(f"DROP TABLE IF EXISTS {S}.chain_evidence")
