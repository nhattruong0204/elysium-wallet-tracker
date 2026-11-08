"""private trading order ledger: schema `trading_private`, role `trading_rw`

Stage C2 / issue #91. Everything lives in its own schema that nobody except the migrating owner
and the least-privilege `trading_rw` role can even see; `dashboard_ro` and `public_api` have no
path to it. Existing tables, the scanner checkpoint and the outbox are not touched (additive).

Defence in depth, so a bug in the application cannot silently corrupt the ledger:

* the order state machine is enforced by a trigger over `order_state_edges` (invalid or repeated
  transitions raise), versions must increase by exactly one, and the reviewed intent columns are
  immutable for every role; a terminal order cannot change;
* `order_events` is an append-only, hash-chained audit log: the chain position, timestamp and
  hashes are assigned by a trigger under an advisory lock (callers cannot choose them), UPDATE,
  DELETE and TRUNCATE are rejected, and `trading_rw` has no such privileges anyway;
* row level security scopes `trading_rw` to one Telegram user (`trading.user_id`) unless the
  application explicitly enters system scope (reconciliation / operator paths);
* aggregates needed for risk budgets come from one narrow SECURITY DEFINER function that returns
  numbers only, never rows;
* `trading_rw` has no password until `--migrate` sets one from TRADING_DB_PASSWORD, so it cannot
  log in over the network before the operator provisions it.

No wallet secret, signed transaction or provider credential has a column here.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-08
"""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

ROLE = "trading_rw"
S = "trading_private"
HEX40 = "'^0x[0-9a-f]{40}$'"
HEX64 = "'^[0-9a-f]{64}$'"

STATES = ("created", "quote_review", "requires_approval", "awaiting_wallet", "submitted",
          "ambiguous", "included", "reverted", "finalized", "orphaned", "cancelled")
TERMINAL = ("finalized", "cancelled")

# A snapshot, deliberately not imported from the application: changing the state machine later
# needs a new migration (and a review), and tests assert both lists are identical.
EDGES = (
    ("created", "quote_review"), ("created", "cancelled"),
    ("quote_review", "requires_approval"), ("quote_review", "awaiting_wallet"),
    ("quote_review", "cancelled"),
    ("requires_approval", "quote_review"), ("requires_approval", "awaiting_wallet"),
    ("requires_approval", "cancelled"),
    ("awaiting_wallet", "submitted"), ("awaiting_wallet", "ambiguous"),
    ("awaiting_wallet", "cancelled"),
    ("submitted", "included"), ("submitted", "reverted"), ("submitted", "ambiguous"),
    ("ambiguous", "submitted"), ("ambiguous", "included"), ("ambiguous", "reverted"),
    ("ambiguous", "orphaned"), ("ambiguous", "cancelled"),
    ("included", "finalized"), ("included", "reverted"), ("included", "orphaned"),
    ("reverted", "finalized"), ("reverted", "included"), ("reverted", "orphaned"),
    ("orphaned", "submitted"), ("orphaned", "included"), ("orphaned", "reverted"),
    ("orphaned", "cancelled"),
)
IMMUTABLE = ("id", "user_id", "wallet", "chain_id", "idempotency_key", "request_digest",
             "intent_id", "plan_id", "plan_digest", "route_hash", "token", "side",
             "amount_in_wei", "min_out_wei", "notional_wei", "gas_reserve_wei", "slippage_bps",
             "price_impact_bps", "deadline", "policy_version", "created_at", "expires_at")
EVENT_KINDS = ("ORDER_CREATED", "TRANSITION", "TRANSITION_REFUSED", "ADMISSION_REFUSED",
               "ATTEMPT_RECORDED", "PAUSED", "RESUMED", "EXECUTION_FLAG_SET", "POLICY_SET",
               "RECOVERY")


def q(items: tuple[str, ...]) -> str:
    return ", ".join(f"'{i}'" for i in items)


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA {S}")
    op.execute(f"REVOKE ALL ON SCHEMA {S} FROM PUBLIC")
    op.execute(f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
              NOBYPASSRLS CONNECTION LIMIT 5;
          END IF;
        END $$""")
    op.execute(f"ALTER ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
               "NOBYPASSRLS CONNECTION LIMIT 5")
    op.execute(f"ALTER ROLE {ROLE} SET statement_timeout = '5s'")
    op.execute(f"ALTER ROLE {ROLE} SET lock_timeout = '2s'")
    op.execute(f"ALTER ROLE {ROLE} SET idle_in_transaction_session_timeout = '15s'")
    op.execute(f"ALTER ROLE {ROLE} SET search_path = {S}, pg_catalog")

    # ---- global controls: durable pause, execution flag (default OFF), active policy -------
    op.execute(f"""
        CREATE TABLE {S}.policy_versions (
          version    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          params     jsonb NOT NULL,
          digest     char(64) NOT NULL CHECK (digest ~ {HEX64}),
          created_by bigint,
          created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute(f"""
        CREATE TABLE {S}.controls (
          id                smallint PRIMARY KEY CHECK (id = 1),
          paused            boolean NOT NULL DEFAULT false,
          pause_reason      text CHECK (pause_reason IS NULL OR length(pause_reason) <= 200),
          paused_at         timestamptz,
          paused_by         bigint,
          execution_enabled boolean NOT NULL DEFAULT false,
          policy_version    bigint REFERENCES {S}.policy_versions (version),
          version           bigint NOT NULL DEFAULT 1,
          updated_at        timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute(f"INSERT INTO {S}.controls (id) VALUES (1)")

    # ---- state machine ---------------------------------------------------------------------
    op.execute(f"""
        CREATE TABLE {S}.order_state_edges (
          from_state text NOT NULL CHECK (from_state IN ({q(STATES)})),
          to_state   text NOT NULL CHECK (to_state IN ({q(STATES)})),
          PRIMARY KEY (from_state, to_state)
        )""")
    values = ", ".join(f"('{a}', '{b}')" for a, b in EDGES)
    op.execute(f"INSERT INTO {S}.order_state_edges (from_state, to_state) VALUES {values}")

    # ---- orders ----------------------------------------------------------------------------
    op.execute(f"CREATE SEQUENCE {S}.order_update_seq")
    op.execute(f"""
        CREATE TABLE {S}.orders (
          id               uuid PRIMARY KEY,
          user_id          bigint NOT NULL CHECK (user_id > 0),
          wallet           varchar(42) NOT NULL CHECK (wallet ~ {HEX40}),
          chain_id         bigint NOT NULL CHECK (chain_id = 99901),
          idempotency_key  text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9:_.-]{{8,128}}$'),
          request_digest   char(64) NOT NULL CHECK (request_digest ~ {HEX64}),
          intent_id        text NOT NULL CHECK (length(intent_id) BETWEEN 1 AND 128),
          plan_id          text NOT NULL CHECK (length(plan_id) BETWEEN 1 AND 128),
          plan_digest      char(64) NOT NULL CHECK (plan_digest ~ {HEX64}),
          route_hash       char(64) NOT NULL CHECK (route_hash ~ {HEX64}),
          token            varchar(42) NOT NULL CHECK (token ~ {HEX40}),
          side             text NOT NULL CHECK (side IN ('buy', 'sell')),
          amount_in_wei    numeric(78, 0) NOT NULL CHECK (amount_in_wei > 0),
          min_out_wei      numeric(78, 0) NOT NULL CHECK (min_out_wei >= 0),
          notional_wei     numeric(78, 0) NOT NULL CHECK (notional_wei > 0),
          gas_reserve_wei  numeric(78, 0) NOT NULL CHECK (gas_reserve_wei >= 0),
          slippage_bps     integer NOT NULL CHECK (slippage_bps BETWEEN 0 AND 10000),
          price_impact_bps integer NOT NULL CHECK (price_impact_bps BETWEEN 0 AND 10000),
          deadline         bigint NOT NULL CHECK (deadline > 0),
          signature_status text NOT NULL DEFAULT 'unsigned'
                           CHECK (signature_status IN ('unsigned', 'wallet_signed')),
          state            text NOT NULL DEFAULT 'created' CHECK (state IN ({q(STATES)})),
          version          bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
          update_seq       bigint NOT NULL DEFAULT nextval('{S}.order_update_seq'),
          policy_version   bigint NOT NULL REFERENCES {S}.policy_versions (version),
          created_at       timestamptz NOT NULL DEFAULT now(),
          updated_at       timestamptz NOT NULL DEFAULT now(),
          expires_at       timestamptz NOT NULL,
          terminal_at      timestamptz,
          last_reason      text CHECK (last_reason IS NULL OR length(last_reason) <= 200),
          CONSTRAINT uq_orders_user_key UNIQUE (user_id, idempotency_key),
          CONSTRAINT uq_orders_plan_digest UNIQUE (plan_digest),
          CONSTRAINT ck_orders_expiry CHECK (expires_at > created_at - interval '1 minute')
        )""")
    op.execute(f"CREATE INDEX ix_orders_user ON {S}.orders (user_id, created_at DESC)")
    op.execute(f"CREATE INDEX ix_orders_day ON {S}.orders (created_at)")
    op.execute(f"CREATE INDEX ix_orders_seq ON {S}.orders (update_seq)")
    op.execute(f"""CREATE INDEX ix_orders_active ON {S}.orders (chain_id, wallet)
                   WHERE state NOT IN ('finalized', 'cancelled')""")

    op.execute(f"""
        CREATE FUNCTION {S}.orders_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'orders are never deleted' USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF ROW({", ".join("NEW." + c for c in IMMUTABLE)})
             IS DISTINCT FROM ROW({", ".join("OLD." + c for c in IMMUTABLE)}) THEN
            RAISE EXCEPTION 'the reviewed order intent is immutable'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF OLD.state IN ({q(TERMINAL)}) THEN
            RAISE EXCEPTION 'order % is terminal (%)', OLD.id, OLD.state
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF NEW.version <> OLD.version + 1 THEN
            RAISE EXCEPTION 'order version must increase by exactly one'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF OLD.signature_status = 'wallet_signed' AND NEW.signature_status = 'unsigned' THEN
            RAISE EXCEPTION 'signature status cannot be reverted'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF NEW.state IS DISTINCT FROM OLD.state AND NOT EXISTS (
               SELECT 1 FROM {S}.order_state_edges e
               WHERE e.from_state = OLD.state AND e.to_state = NEW.state) THEN
            RAISE EXCEPTION 'invalid order transition % -> %', OLD.state, NEW.state
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          NEW.update_seq := nextval('{S}.order_update_seq');
          NEW.updated_at := clock_timestamp();
          IF NEW.state IN ({q(TERMINAL)}) THEN
            NEW.terminal_at := clock_timestamp();
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(f"""CREATE TRIGGER orders_guard BEFORE UPDATE OR DELETE ON {S}.orders
                   FOR EACH ROW EXECUTE FUNCTION {S}.orders_guard()""")

    op.execute(f"""
        CREATE FUNCTION {S}.refuse_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
              USING ERRCODE = 'integrity_constraint_violation'; END $$""")
    op.execute(f"""CREATE TRIGGER orders_no_truncate BEFORE TRUNCATE ON {S}.orders
                   FOR EACH STATEMENT EXECUTE FUNCTION {S}.refuse_truncate()""")

    # ---- execution attempts (tx hashes / nonces): append-only ---------------------------------
    op.execute(f"""
        CREATE TABLE {S}.execution_attempts (
          id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          order_id   uuid NOT NULL REFERENCES {S}.orders (id),
          user_id    bigint NOT NULL,
          attempt_no integer NOT NULL CHECK (attempt_no >= 1),
          kind       text NOT NULL CHECK (kind IN ('approval', 'swap')),
          nonce      numeric(20, 0) CHECK (nonce IS NULL OR nonce >= 0),
          tx_hash    char(66) CHECK (tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{{64}}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_attempt_no UNIQUE (order_id, attempt_no),
          CONSTRAINT uq_attempt_tx UNIQUE (tx_hash),
          CONSTRAINT ck_attempt_has_ref CHECK (nonce IS NOT NULL OR tx_hash IS NOT NULL)
        )""")

    # ---- audit log: append-only, hash chained -----------------------------------------------
    op.execute(f"""
        CREATE TABLE {S}.order_events (
          chain_no    bigint PRIMARY KEY,
          at          timestamptz NOT NULL,
          order_id    uuid REFERENCES {S}.orders (id),
          user_id     bigint,
          actor_kind  text NOT NULL CHECK (actor_kind IN ('user', 'owner', 'system')),
          actor_id    bigint,
          kind        text NOT NULL CHECK (kind IN ({q(EVENT_KINDS)})),
          from_state  text,
          to_state    text,
          request_key text CHECK (request_key IS NULL OR length(request_key) <= 200),
          detail      jsonb NOT NULL DEFAULT '{{}}'::jsonb,
          prev_hash   char(64) NOT NULL,
          hash        char(64) NOT NULL UNIQUE
        )""")
    op.execute(f"""CREATE UNIQUE INDEX ux_events_transition_key ON {S}.order_events
                   (order_id, request_key) WHERE kind = 'TRANSITION' AND request_key IS NOT NULL""")
    op.execute(f"""CREATE UNIQUE INDEX ux_events_refused_transition ON {S}.order_events
                   (order_id, request_key) WHERE kind = 'TRANSITION_REFUSED'""")
    op.execute(f"""CREATE UNIQUE INDEX ux_events_refused_admission ON {S}.order_events
                   (user_id, request_key) WHERE kind = 'ADMISSION_REFUSED'""")
    op.execute(f"CREATE INDEX ix_events_order ON {S}.order_events (order_id, chain_no)")
    op.execute(f"""
        CREATE FUNCTION {S}.event_digest(e {S}.order_events) RETURNS text
        LANGUAGE sql STABLE AS $$
          SELECT encode(sha256(convert_to(concat_ws('|', e.chain_no::text, e.prev_hash,
                 to_char(e.at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
                 coalesce(e.order_id::text, ''), coalesce(e.user_id::text, ''), e.actor_kind,
                 coalesce(e.actor_id::text, ''), e.kind, coalesce(e.from_state, ''),
                 coalesce(e.to_state, ''), coalesce(e.request_key, ''), e.detail::text),
                 'UTF8')), 'hex') $$""")
    # SECURITY DEFINER on purpose: the chain head is global, but trading_rw only sees its own
    # user's rows through RLS. The trigger runs as the owner so the chain stays linear.
    op.execute(f"""
        CREATE FUNCTION {S}.events_chain() RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER SET search_path = pg_catalog, {S} AS $$
        DECLARE last_no bigint; last_hash text;
        BEGIN
          PERFORM pg_advisory_xact_lock(hashtext('trading_private.order_events'));
          SELECT chain_no, hash INTO last_no, last_hash
            FROM {S}.order_events ORDER BY chain_no DESC LIMIT 1;
          NEW.chain_no  := coalesce(last_no, 0) + 1;
          NEW.at        := clock_timestamp();
          NEW.prev_hash := coalesce(last_hash, repeat('0', 64));
          NEW.hash      := {S}.event_digest(NEW);
          RETURN NEW;
        END $$""")
    op.execute(f"""CREATE TRIGGER events_chain BEFORE INSERT ON {S}.order_events
                   FOR EACH ROW EXECUTE FUNCTION {S}.events_chain()""")
    op.execute(f"""
        CREATE FUNCTION {S}.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
              USING ERRCODE = 'integrity_constraint_violation'; END $$""")
    for t in ("order_events", "execution_attempts", "policy_versions"):
        op.execute(f"""CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {S}.{t}
                       FOR EACH ROW EXECUTE FUNCTION {S}.refuse_change()""")
        op.execute(f"""CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {S}.{t}
                       FOR EACH STATEMENT EXECUTE FUNCTION {S}.refuse_change()""")
    op.execute(f"""
        CREATE FUNCTION {S}.controls_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP <> 'UPDATE' OR NEW.id <> OLD.id OR NEW.version <> OLD.version + 1 THEN
            RAISE EXCEPTION 'controls change by exactly one version and are never deleted'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          NEW.updated_at := clock_timestamp();
          RETURN NEW;
        END $$""")
    op.execute(f"""CREATE TRIGGER controls_guard BEFORE UPDATE OR DELETE ON {S}.controls
                   FOR EACH ROW EXECUTE FUNCTION {S}.controls_guard()""")
    op.execute(f"""CREATE TRIGGER controls_no_truncate BEFORE TRUNCATE ON {S}.controls
                   FOR EACH STATEMENT EXECUTE FUNCTION {S}.refuse_truncate()""")

    # ---- row level security: one Telegram user at a time ------------------------------------
    scope = ("(user_id = nullif(current_setting('trading.user_id', true), '')::bigint "
             "OR current_setting('trading.system', true) = 'on')")
    for t in ("orders", "execution_attempts", "order_events"):
        op.execute(f"ALTER TABLE {S}.{t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {t}_scope ON {S}.{t} FOR ALL TO {ROLE} "
                   f"USING {scope} WITH CHECK {scope}")

    # ---- numbers, never rows: what a new order needs to know about today's usage --------------
    op.execute(f"""
        CREATE FUNCTION {S}.admission_snapshot(p_user bigint, p_wallet text, p_chain bigint,
                                               p_day_start timestamptz, p_day_end timestamptz)
        RETURNS TABLE (user_notional numeric, user_gas numeric, global_notional numeric,
                       global_gas numeric, user_active bigint, wallet_active bigint)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, {S} AS $$
          SELECT
            coalesce(sum(notional_wei) FILTER (WHERE user_id = p_user
                     AND state NOT IN ('cancelled', 'reverted')), 0),
            coalesce(sum(gas_reserve_wei) FILTER (WHERE user_id = p_user
                     AND state <> 'cancelled'), 0),
            coalesce(sum(notional_wei) FILTER (WHERE state NOT IN ('cancelled', 'reverted')), 0),
            coalesce(sum(gas_reserve_wei) FILTER (WHERE state <> 'cancelled'), 0),
            count(*) FILTER (WHERE user_id = p_user AND state IN
                     ('created', 'quote_review', 'requires_approval', 'awaiting_wallet',
                      'submitted', 'ambiguous', 'orphaned')),
            count(*) FILTER (WHERE wallet = p_wallet AND chain_id = p_chain AND state IN
                     ('created', 'quote_review', 'requires_approval', 'awaiting_wallet',
                      'submitted', 'ambiguous', 'orphaned'))
          FROM {S}.orders
          WHERE (created_at >= p_day_start AND created_at < p_day_end)
             OR state IN ('created', 'quote_review', 'requires_approval', 'awaiting_wallet',
                          'submitted', 'ambiguous', 'orphaned') $$""")
    op.execute(f"REVOKE ALL ON FUNCTION {S}.admission_snapshot(bigint, text, bigint, "
               "timestamptz, timestamptz) FROM PUBLIC")

    # ---- privileges: the explicit allow-list ---------------------------------------------------
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA {S} TO {ROLE}")
    op.execute(f"GRANT SELECT ON {S}.order_state_edges TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON {S}.policy_versions TO {ROLE}")
    op.execute(f"GRANT SELECT ON {S}.controls TO {ROLE}")
    op.execute(f"""GRANT UPDATE (paused, pause_reason, paused_at, paused_by, execution_enabled,
                   policy_version, version) ON {S}.controls TO {ROLE}""")
    op.execute(f"GRANT SELECT, INSERT ON {S}.orders TO {ROLE}")
    op.execute(f"""GRANT UPDATE (state, version, signature_status, last_reason)
                   ON {S}.orders TO {ROLE}""")
    op.execute(f"GRANT SELECT, INSERT ON {S}.execution_attempts TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON {S}.order_events TO {ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE {S}.order_update_seq TO {ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {S}.admission_snapshot(bigint, text, bigint, "
               f"timestamptz, timestamptz) TO {ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {S}.event_digest({S}.order_events) TO {ROLE}")
    op.execute("DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO "
               f"{ROLE}', current_database()); END $$")


def downgrade() -> None:
    # Rolling back drops the private ledger (and its audit log) on purpose: take a backup first
    # (docs/TRADING_ORDER_LEDGER.md). Nothing outside the schema depends on it.
    op.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
    op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM {ROLE}', current_database());
            REVOKE ALL ON SCHEMA public FROM {ROLE};
            DROP ROLE {ROLE};
          END IF;
        END $$""")
