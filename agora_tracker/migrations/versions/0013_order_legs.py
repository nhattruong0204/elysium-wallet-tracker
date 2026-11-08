"""durable wallet legs: idempotency, one-active-leg-per-wallet, nonce/hash identity (issue #93)

A *leg* is one thing a user's wallet was asked to do for an order (a zero-reset, a finite approval
or the swap). It is created in the same transaction as the WALLET_REQUEST audit event, so a
prompt that was authorized always has a durable row, and:

* `idempotency_key` (the request id) is unique: a duplicate callback cannot create a second leg;
* at most ONE unsettled leg per (wallet, chain) can exist (partial unique index). This is the
  concurrency guard that stops two orders from racing the same account's nonce;
* at most ONE swap leg per order, ever;
* `tx_hash`, `nonce` and `final_tx_hash` are set-once; state moves only along allowed edges;
* nothing secret is stored: only the public request payload the wallet was shown.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-08
"""
from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

S = "trading_private"
ROLE = "trading_rw"
STATES = ("prompted", "hash_reported", "ambiguous", "included", "reverted", "rejected",
          "superseded", "abandoned")
ACTIVE = ("prompted", "hash_reported", "ambiguous")


def upgrade() -> None:
    states = ", ".join(f"'{s}'" for s in STATES)
    active = ", ".join(f"'{s}'" for s in ACTIVE)
    op.execute(f"""
        CREATE TABLE {S}.order_legs (
          id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          order_id         uuid NOT NULL REFERENCES {S}.orders (id),
          user_id          bigint NOT NULL,
          leg_no           integer NOT NULL CHECK (leg_no >= 1),
          kind             text NOT NULL CHECK (kind IN ('reset', 'approve', 'swap')),
          wallet           char(42) NOT NULL CHECK (wallet ~ '^0x[0-9a-f]{{40}}$'),
          chain_id         bigint NOT NULL,
          idempotency_key  text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9:_.-]{{8,128}}$'),
          request_digest   char(64) NOT NULL CHECK (request_digest ~ '^[0-9a-f]{{64}}$'),
          expected_to      char(42) NOT NULL CHECK (expected_to ~ '^0x[0-9a-f]{{40}}$'),
          expected_value   numeric(78, 0) NOT NULL CHECK (expected_value >= 0),
          expected_data    text NOT NULL CHECK (expected_data ~ '^0x([0-9a-f]{{2}})+$'
                                                AND length(expected_data) <= 1024),
          gas_limit        numeric(20, 0),
          max_fee_wei      numeric(78, 0),
          nonce_hint       numeric(20, 0) CHECK (nonce_hint IS NULL OR nonce_hint >= 0),
          prompt_block     bigint,
          nonce            numeric(20, 0) CHECK (nonce IS NULL OR nonce >= 0),
          tx_hash          char(66) CHECK (tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{{64}}$'),
          final_tx_hash    char(66) CHECK (final_tx_hash IS NULL
                                            OR final_tx_hash ~ '^0x[0-9a-f]{{64}}$'),
          state            text NOT NULL DEFAULT 'prompted' CHECK (state IN ({states})),
          resolution       text CHECK (resolution IS NULL OR length(resolution) <= 200),
          version          integer NOT NULL DEFAULT 1,
          prompted_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
          answered_at      timestamptz,
          seen_at          timestamptz,
          resolved_at      timestamptz,
          CONSTRAINT uq_leg_no UNIQUE (order_id, leg_no),
          CONSTRAINT uq_leg_key UNIQUE (idempotency_key),
          CONSTRAINT uq_leg_tx UNIQUE (tx_hash)
        )""")
    op.execute(f"""CREATE UNIQUE INDEX ux_legs_one_swap ON {S}.order_legs (order_id)
                   WHERE kind = 'swap'""")
    op.execute(f"""CREATE UNIQUE INDEX ux_legs_wallet_active ON {S}.order_legs (wallet, chain_id)
                   WHERE state IN ({active})""")
    op.execute(f"CREATE INDEX ix_legs_active ON {S}.order_legs (state, prompted_at) "
               f"WHERE state IN ({active})")

    op.execute(f"""
        CREATE FUNCTION {S}.legs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
          IF NEW.state <> OLD.state AND NOT (
               (OLD.state = 'prompted'
                  AND NEW.state IN ('hash_reported', 'rejected', 'ambiguous', 'abandoned'))
            OR (OLD.state = 'hash_reported'
                  AND NEW.state IN ('ambiguous', 'included', 'reverted', 'superseded',
                                    'abandoned'))
            OR (OLD.state = 'ambiguous'
                  AND NEW.state IN ('hash_reported', 'included', 'reverted', 'superseded',
                                    'abandoned'))
            OR (OLD.state = 'included' AND NEW.state IN ('reverted', 'ambiguous'))
            OR (OLD.state = 'reverted' AND NEW.state IN ('included', 'ambiguous'))
          ) THEN
            RAISE EXCEPTION 'leg state % -> % is not allowed', OLD.state, NEW.state
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(f"""CREATE TRIGGER order_legs_guard BEFORE UPDATE OR DELETE ON {S}.order_legs
                   FOR EACH ROW EXECUTE FUNCTION {S}.legs_guard()""")
    op.execute(f"""CREATE TRIGGER order_legs_no_truncate BEFORE TRUNCATE ON {S}.order_legs
                   FOR EACH STATEMENT EXECUTE FUNCTION {S}.refuse_change()""")

    scope = ("(user_id = nullif(current_setting('trading.user_id', true), '')::bigint "
             "OR current_setting('trading.system', true) = 'on')")
    op.execute(f"ALTER TABLE {S}.order_legs ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY order_legs_scope ON {S}.order_legs FOR ALL TO {ROLE} "
               f"USING {scope} WITH CHECK {scope}")
    op.execute(f"GRANT SELECT, INSERT ON {S}.order_legs TO {ROLE}")
    op.execute(f"""GRANT UPDATE (state, version, nonce, tx_hash, final_tx_hash, resolution,
                   answered_at, seen_at, resolved_at) ON {S}.order_legs TO {ROLE}""")


def downgrade() -> None:
    # Legs are operational state for unsettled wallet requests: take a backup first.
    op.execute(f"DROP TABLE IF EXISTS {S}.order_legs")
    op.execute(f"DROP FUNCTION IF EXISTS {S}.legs_guard()")
