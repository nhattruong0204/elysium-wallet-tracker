# Private order ledger, risk budgets and durable pause (Stage C2, issue #91)

**Status: implemented behind two OFF-by-default switches; not reviewed independently; not
deployed.** The ledger records and limits orders. It does **not** sign, approve, broadcast or hold
a key, and it moves no funds. `PaperFlow.broadcast` still always refuses. Stacked on #100 (Mini
App), #101 (authentication) and #102 (immutable unsigned V2 plans); it consumes the plan object
from #102 and does not change it.

## What it is

A PostgreSQL schema `trading_private`, owned by the migrating role and used at runtime only by
the least-privilege role `trading_rw`, plus `agora_tracker/trading/ledger/`:

| Piece | Responsibility |
|---|---|
| `states.py` | `OrderState`, the allowed edges **and who may take each one**, terminal/active sets |
| `policy.py` | `RiskPolicy`: integer-wei caps, bps limits, token allowlist, hard ceilings in code |
| `core.py` | `OrderLedger`: idempotent create, transitions, pause, execution flag, recovery, audit verification |
| `runtime.py` | startup wiring; refuses to start misconfigured; pauses if the audit chain is broken |
| migration `0011` | schema, role, triggers, row level security, grants (backward compatible) |

Nothing here is reachable from the website: `dashboard_ro` and `public_api` have no privilege on
the schema (tested), no `public_api` view references it, and the HTTP/Mini App layers are not
changed by this PR.

## Switches (both default OFF)

| Switch | Where | Effect |
|---|---|---|
| `trading.ledger_enabled` | config, default `false` | Builds the ledger at `--ui` startup, makes `/pause_trading` durable, adds `/resume_trading`. |
| `execution_enabled` | `controls` row, default `false`, owner-only to turn on | Without it no order can enter `awaiting_wallet`, i.e. no wallet request, ever. |

Enabling the first never enables the second. If the first is on and anything is wrong (missing
URL, wrong role, no owner, database unreachable) the UI service **exits** instead of running
without limits.

## State machine

| From | To (who may take the edge) |
|---|---|
| `created` | `quote_review` (system/user), `cancelled` (owner/system/user) |
| `quote_review` | `requires_approval` (system/user), `awaiting_wallet` (system/user), `cancelled` (owner/system/user) |
| `requires_approval` | `quote_review` (system/user), `awaiting_wallet` (system/user), `cancelled` (owner/system/user) |
| `awaiting_wallet` | `submitted` (system), `ambiguous` (system), `cancelled` (owner/system) |
| `submitted` | `included` (system), `reverted` (system), `ambiguous` (system) |
| `ambiguous` | `submitted` (system), `included` (system), `reverted` (system), `orphaned` (system), `cancelled` (owner/system) |
| `included` | `finalized` (system), `reverted` (system), `orphaned` (system) |
| `reverted` | `finalized` (system), `included` (system), `orphaned` (system) |
| `orphaned` | `submitted` (system), `included` (system), `reverted` (system), `cancelled` (owner/system) |
| `finalized` | — terminal |
| `cancelled` | — terminal |

The table above is generated from `EDGES` in `states.py`, which is **identical** to the rows of
`trading_private.order_state_edges` (a test asserts equality; changing the machine needs a new
migration and review).

* Invalid and repeated transitions are rejected (`invalid_transition`); the database trigger
  rejects them again if the application is bypassed.
* `version` increases by exactly one per change (trigger-enforced); `update_seq` comes from a
  sequence, so consumers can read "everything changed since N" in order.
* `finalized` and `cancelled` are frozen; no UPDATE or DELETE can change them, for any role.
* Who may move an order: a **user** can only advance an order up to the wallet prompt or cancel it
  before that. Everything after the wallet (`submitted`, `ambiguous`, `included`, `reverted`,
  `finalized`, `orphaned`) is `system` only. Leaving `awaiting_wallet`/`ambiguous`/`orphaned` for
  `cancelled` needs an owner or the system **and a written reason**.
* **No fabricated fills.** `submitted` requires a recorded nonce or hash, `included`/`reverted`
  require a recorded transaction hash (`missing_evidence`). The ledger stores references the
  reconciler reports; it has no field for a fill price, an amount received or PnL, so none can be
  invented. `signature_status` can only go `unsigned -> wallet_signed` (on `submitted`).

## What is persisted per order

`user_id`, `wallet`, `chain_id` (99901 only), `token`, `side`, `amount_in_wei`, `min_out_wei`,
`notional_wei`, `gas_reserve_wei`, `slippage_bps`, `price_impact_bps`, `deadline`, `route_hash`
(`fingerprint` of the plan's verified route), `plan_id`, `intent_id`, `plan_digest` (`fingerprint`
of the whole immutable plan from #102, which already binds user and session), `request_digest`,
`policy_version`, `signature_status`, timestamps, and in `execution_attempts` the nonces and
transaction hashes. These columns are **immutable** after insert (trigger, every role). There is no
column for a private key, mnemonic, signature, raw transaction or provider credential (a test scans
the catalog for such names). Amounts are `numeric(78,0)` / Python `int`; no float anywhere.

`order_events` is the immutable audit log: who (`user`/`owner`/`system`), what, from/to state,
request key, and a small JSON detail. Refusals are audited too (`ADMISSION_REFUSED`,
`TRANSITION_REFUSED`), once per (key, reason), so retries cannot flood it.

## Authorization boundaries

1. **Application:** `create_order` takes `user_id` and `wallet` from the authenticated session
   (#101) and refuses a plan whose user or wallet differs (`unauthorized_user`, `wallet_mismatch`)
   or an expired session. Reads and writes are owner-bound: another user's order is
   indistinguishable from a missing one (`not_found`, never "forbidden").
2. **Database (row level security):** every statement runs with `trading.user_id` set to one user
   (or `trading.system = on` for reconciliation and operator paths). `trading_rw` sees and writes
   only that user's rows; with no scope it sees nothing (tested with raw SQL).
3. **Actors:** `Actor.user(id)` is the only thing request-handling code may construct.
   `Actor.owner(id)` must be in the configured owner set (`TG_CHAT_ID` plus `TRADING_OWNER_IDS`;
   merely being in `TG_ADMIN_IDS` is not owner). `Actor.system()` is for in-process components.
4. **Privileges:** `trading_rw` is not superuser, has no `BYPASSRLS`, 5 connections, 5 s
   `statement_timeout`, 2 s `lock_timeout`, 15 s idle-in-transaction timeout, and an empty
   `public` (it cannot read `trades`, `wallets`, the outbox, etc.). Column-level `UPDATE` only on
   `state`, `version`, `signature_status`, `last_reason`. No `DELETE`/`TRUNCATE` anywhere. The
   engine factory caps the pool at 4 and never reads `DATABASE_URL`; startup refuses a URL whose
   user is not `trading_rw`.
5. **Budgets without leaking rows:** daily usage comes from one `SECURITY DEFINER` function that
   returns six numbers; the role cannot read other users' orders to compute it.

The role has **no password** until `--migrate` sets one from `TRADING_DB_PASSWORD` (24-128 chars of
`[A-Za-z0-9_-]`), the same pattern as `dashboard_ro`.

## Risk policy

Stored as immutable, versioned rows (`policy_versions`); each order records the version it was
admitted under. Only an owner can set one, and values are bounded by **ceilings in code** that no
configuration can exceed: per-trade cap <= 1 HYPE (the PAPER engine's own cap), daily user
notional <= 10 HYPE, global <= 20 HYPE, slippage and impact <= 100 bps, gas caps, at most 5 open
orders per user / per wallet, order TTL <= 15 min. The default policy has an **empty allowlist**
(deny everything) and conservative values; bootstrap installs it, never anything looser.

Checked at admission, in this order: identity -> idempotent replay -> pause -> plan expiry and
deadline margin -> allowlist -> per-trade cap -> slippage -> price impact -> open-order and
conflicting-order limits -> daily notional (user, then global) -> daily gas (user, then global).

* **Notional** is the HYPE leg: the input for a buy, the quoted output for a sell. **Gas** is the
  plan's `max_gas_fee_wei` or the policy default. "Day" is the UTC day of the order's `created_at`;
  budgets of still-open orders from earlier days keep counting.
* **Released** when an order is `cancelled` (notional and gas) or `reverted` (notional); **kept**
  once an order is submitted, because gas is spent and a mined revert still burns it.
* Creates are serialized by one advisory lock so two concurrent requests cannot both fit under a
  cap that only one fits under (tested with threads).
* **Tightening applies to open orders:** entering `awaiting_wallet` re-checks the allowlist,
  per-trade cap, slippage and impact against the *current* policy.
* Expiry: orders expire after `order_ttl_s`; plans must have `min_deadline_margin_s` left.

## Durable emergency pause

`controls.paused` is a database row, not a process flag.

* **Blocks:** new orders, and every pre-wallet forward transition (so no new wallet request),
  for every actor. **Allows:** cancellation, idempotent replays, and all reconciliation of orders
  already past the wallet (`submitted`, `ambiguous`, `included`, `reverted`, `orphaned`).
  `pending_reconciliation()` keeps working while paused.
* **Race-free:** transitions hold `controls FOR SHARE`, pause takes it exclusively, so a wallet
  request cannot slip between the check and the commit (tested).
* **Who:** an owner, or the system (e.g. an automatic breaker), may pause; only an owner may
  resume. In Telegram any authorized operator can `/pause_trading` in an emergency (recorded as
  system with the operator id in the reason); `/resume_trading` is owner-only.
* **Fails closed:** if the flag cannot be read, trading is treated as paused. The UI re-reads it
  before every trade command/callback, so a pause set by any process is honored by all.

## Idempotency

* Create: `UNIQUE(user_id, idempotency_key)` + `request_digest`. Same key and same request returns
  the existing order (`created = False`), even while paused. Same key, different request ->
  `idempotency_conflict`. Same plan under a new key -> `duplicate_plan` (`UNIQUE(plan_digest)`).
* Transition: the request key (use the Telegram `callback_query.id`, or `update_id` + action)
  is recorded with the `TRANSITION` event under a partial unique index. A duplicate callback
  returns the current order with `applied = False` and changes nothing; the same key for a
  different target is a conflict. A *different* key for an edge that was already taken is an
  `invalid_transition`, not a silent no-op.
* Lock order is fixed (controls -> admission lock -> order row -> audit lock) so concurrent
  requests queue instead of deadlocking; a deadlock is retried, a lock/statement timeout surfaces
  as `busy` and is safe to retry with the same key.

## Crash consistency and recovery

* Every operation is one transaction: an order, its state change and its audit event commit
  together or not at all. There is no in-memory state to lose.
* `recover()` runs at startup. It applies **time-based expiry only**: pre-wallet orders past their
  expiry are cancelled (nothing can have been signed), and an order still `awaiting_wallet` past
  its on-chain deadline becomes **`ambiguous`, not cancelled** — a wallet may have signed and
  broadcast just before the process died. Orders in `submitted`/`ambiguous`/`orphaned` keep their
  state across restarts for the reconciler to resolve from chain evidence; the pause flag and the
  execution flag persist. It then verifies the audit chain and writes a `RECOVERY` event. If the
  chain is broken, startup pauses trading and logs an error.
* It is idempotent: running it twice changes nothing the second time.

## Audit log integrity

Chain position, timestamp, `prev_hash` and `hash` are assigned **by a trigger** under an advisory
lock, so callers cannot choose or forge them (a test inserts a forged row and sees it overwritten).
UPDATE, DELETE and TRUNCATE are rejected for every role. `verify_chain()` recomputes every hash in
the database and checks contiguity and linkage. This detects after-the-fact edits or deletions by
a privileged user; it cannot prevent a superuser from rewriting the whole chain, so ship the nightly
off-site backup (which contains the ledger) and compare `RECOVERY` event hashes across copies.

## Backup, restore, rollback, retention

* **Backup:** `trading_private` lives in the `tracker` database, so the existing nightly
  `pg_dump -Fc` (docs/BACKUP_AND_RECOVERY.md) already includes it. For a ledger-only dump:
  `pg_dump -Fc -n trading_private -f ledger.dump "$DATABASE_URL"`.
* **Restore:** use a full `pg_restore` of the schema (never `--data-only` into a live ledger):
  `pg_restore` creates the triggers *after* loading data, so the stored hashes and timestamps are
  restored verbatim. A data-only load with triggers enabled would re-stamp the audit chain.
  Roles are cluster-wide and not in the dump: on a new cluster run `--migrate` against an empty
  database first (it creates `trading_rw` and `dashboard_ro`), drop `trading_private`, then restore,
  then run `--migrate` once more to set the password. After any restore run `verify_chain()`
  (startup does) and check `controls` — a restored pause stays paused. Tested by a
  dump -> wipe -> restore round trip that checks state, attempts, pause, privileges and the chain.
* **Rollback:** `alembic downgrade 0010` drops the schema and the role. It is **destructive for
  ledger data on purpose** (the schema is private, nothing else depends on it): take a backup first.
  It does not touch scanner state, `indexer_checkpoints`, the outbox, positions, `paper_trades` or
  any `public`/`public_api` object (tested: an existing row survives down and up).
  Operationally, prefer `trading.ledger_enabled: false` + pause, which keeps the data.
* **Compatibility:** the migration is additive; old code ignores the schema, and the new code does
  nothing with it unless `ledger_enabled` is true.
* **Retention:** ledger and audit rows are kept indefinitely (they are the audit trail; volume is
  one order and a handful of events). There is no automatic purge and no `DELETE` privilege.
  Purging, if ever required, is a manual owner procedure on a restored copy, never in place.
  Do not paste order contents into logs; the application logs codes and ids only.

## Operator set-up (nothing here has been run in production)

```bash
# 1. migrate and provision the role password (24-128 chars of [A-Za-z0-9_-])
TRADING_DB_PASSWORD=$(openssl rand -hex 24) python -m agora_tracker --migrate
# 2. ui service environment: TRADING_DB_URL=postgresql+psycopg://trading_rw:<pw>@postgres:5432/tracker
#    optional TRADING_OWNER_IDS=<telegram ids>; config: trading.ledger_enabled: true
# 3. an owner installs a real policy (allowlist, caps) via OrderLedger.set_policy
#    (an owner settings screen is future work); execution_enabled stays off.
```

## Tests (`tests/test_trading_ledger*.py`, `tests/test_ledger_states.py`)

State machine + Python/DB edge equality; idempotent create and callbacks incl. 8-thread races;
concurrent cap enforcement; every admission refusal and its audit event; budget release and day
rollover; user/global/gas caps; pause (blocks, allows reconciliation, durable across a new engine,
fails closed, races with wallet requests); execution flag; policy tightening; restart recovery and
expiry; attempts; privacy isolation (API, raw SQL under RLS, `dashboard_ro`, `public_api`);
least-privilege role (a list of forbidden statements); DB guards even for the owner; audit tamper and
deletion detection; migration down/up preserving data; dump/restore round trip; configuration and
Telegram command behaviour. All against an isolated PostgreSQL. **No live transactions, no RPC,
no signing.**

## Security assumptions (what a reviewer must confirm)

1. The caller supplies the authenticated `user_id`/`wallet`/session expiry (#101) and an unmodified
   `UnsignedTradePlan` (#102). The ledger checks types and consistency, not signatures.
2. Only trusted in-process code constructs `Actor.system()`. Request handlers must never accept an
   actor kind from the client.
3. The database owner and the host are trusted; the chain protects against tampering by `trading_rw`
   or by application bugs, not against a superuser (see audit log integrity).
4. The system clock is correct (it drives expiry and the daily window; DB time stamps audit events).
5. Per-wallet limits cannot see external wallet transactions; reconciliation (C3) must handle
   those. A `submitted` order only means a transaction reference was recorded.
6. `execution_enabled` and the allowlist are the only gates between this ledger and a wallet
   prompt; both default closed and are owner-controlled.

## Remaining dependencies (not in this PR)

* #100 / #101 / #102 must merge first; this PR is stacked on #102 and must be retargeted when they do.
* An authenticated owner **settings UI/API** for policy and the execution flag (today: Python API).
* Wiring the Mini App/HTTP order endpoints to `create_order`/`transition` (C2 follow-ups).
* Receipt reconciler, nonce leases, approval lifecycle, submission (C2/C3 issues) — they will call
  `record_attempt`/`transition` as `Actor.system()`.
* Independent security review, and a separately authorized production migration and rollout.
