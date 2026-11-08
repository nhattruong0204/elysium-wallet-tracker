"""owner-audited recovery of stuck wallet legs (Stage C2 / issue #95 finding F-02)

`leg_recoveries` is the append-only record of an owner correcting a leg that reconciliation could
not settle (`ambiguous` / `orphaned`). A row exists only together with the leg change and the
hash-chained audit event, in one transaction. It stores the exact evidence the correction rested
on (transaction and block hashes, nonces, per-source heads) and a digest of it.

* append-only: UPDATE / DELETE / TRUNCATE are refused by trigger;
* at most ONE recovery per leg (`UNIQUE (leg_id)`): a corrected leg is terminal, so a duplicate or
  replayed request can never apply a second time;
* the request key is unique (idempotent retries);
* the reason is mandatory and bounded; the owner id is recorded;
* nothing here touches receipts, fills or transaction history: only the leg's own state moves,
  and only to `superseded` or `abandoned`.

`order_events.kind` is widened (as 0012 did; the wider CHECK stays on downgrade because
order_events is append-only and rows of the new kinds cannot be removed).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-08
"""
from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

S = "trading_private"
ROLE = "trading_rw"
KINDS = ("ORDER_CREATED", "TRANSITION", "TRANSITION_REFUSED", "ADMISSION_REFUSED",
         "ATTEMPT_RECORDED", "PAUSED", "RESUMED", "EXECUTION_FLAG_SET", "POLICY_SET", "RECOVERY",
         "WALLET_REQUEST", "WALLET_RESULT", "LEG_RECOVERY", "LEG_RECOVERY_REFUSED")


def upgrade() -> None:
    kinds = ", ".join(f"'{k}'" for k in KINDS)
    op.execute(f"ALTER TABLE {S}.order_events DROP CONSTRAINT IF EXISTS order_events_kind_check")
    op.execute(f"ALTER TABLE {S}.order_events ADD CONSTRAINT order_events_kind_check "
               f"CHECK (kind IN ({kinds}))")
    op.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS ux_events_leg_recovery ON {S}.order_events
                   (order_id, request_key) WHERE kind = 'LEG_RECOVERY'""")
    op.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS ux_events_leg_recovery_refused
                   ON {S}.order_events (order_id, request_key)
                   WHERE kind = 'LEG_RECOVERY_REFUSED'""")
    op.execute(f"""
        CREATE TABLE {S}.leg_recoveries (
          id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          leg_id           bigint NOT NULL REFERENCES {S}.order_legs (id),
          order_id         uuid NOT NULL REFERENCES {S}.orders (id),
          user_id          bigint NOT NULL,
          owner_id         bigint NOT NULL CHECK (owner_id > 0),
          request_key      text NOT NULL CHECK (request_key ~ '^[A-Za-z0-9:_.-]{{8,128}}$'),
          action           text NOT NULL CHECK (action IN ('superseded', 'abandoned')),
          category         text NOT NULL CHECK (length(category) BETWEEN 1 AND 60),
          from_state       text NOT NULL CHECK (from_state IN ('ambiguous', 'orphaned')),
          to_state         text NOT NULL CHECK (to_state IN ('superseded', 'abandoned')),
          leg_version      integer NOT NULL,
          reason           text NOT NULL CHECK (length(reason) BETWEEN 10 AND 200),
          evidence         jsonb NOT NULL,
          evidence_digest  char(64) NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{{64}}$'),
          event_chain_no   bigint,
          created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
          CONSTRAINT uq_recovery_leg UNIQUE (leg_id),
          CONSTRAINT uq_recovery_key UNIQUE (request_key),
          CONSTRAINT ck_recovery_edge CHECK (action = to_state)
        )""")
    op.execute(f"CREATE INDEX ix_recoveries_order ON {S}.leg_recoveries (order_id)")
    op.execute(f"""CREATE TRIGGER leg_recoveries_append_only BEFORE UPDATE OR DELETE
                   ON {S}.leg_recoveries FOR EACH ROW EXECUTE FUNCTION {S}.refuse_change()""")
    op.execute(f"""CREATE TRIGGER leg_recoveries_no_truncate BEFORE TRUNCATE ON {S}.leg_recoveries
                   FOR EACH STATEMENT EXECUTE FUNCTION {S}.refuse_change()""")
    scope = ("(user_id = nullif(current_setting('trading.user_id', true), '')::bigint "
             "OR current_setting('trading.system', true) = 'on')")
    op.execute(f"ALTER TABLE {S}.leg_recoveries ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY leg_recoveries_scope ON {S}.leg_recoveries FOR ALL TO {ROLE} "
               f"USING {scope} WITH CHECK {scope}")
    op.execute(f"GRANT SELECT, INSERT ON {S}.leg_recoveries TO {ROLE}")


def downgrade() -> None:
    # A recovery is an audit record: take a backup first. The widened event-kind CHECK stays.
    op.execute(f"DROP INDEX IF EXISTS {S}.ux_events_leg_recovery_refused")
    op.execute(f"DROP INDEX IF EXISTS {S}.ux_events_leg_recovery")
    op.execute(f"DROP TABLE IF EXISTS {S}.leg_recoveries")
