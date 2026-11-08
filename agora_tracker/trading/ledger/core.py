"""Private order ledger over PostgreSQL schema `trading_private` (migration 0011).

What this module is NOT: it never signs, approves, broadcasts or holds a key. It records what a
user reviewed (an immutable unsigned plan from #102), decides whether the risk policy admits it,
and tracks the order's lifecycle as other components report it.

Concurrency model (READ COMMITTED + explicit locks, always in this order, so no deadlocks):

    controls FOR SHARE  ->  admission advisory lock (creates only)  ->  order row FOR UPDATE
                        ->  audit-chain advisory lock (taken by the insert trigger, last)

* A Telegram callback delivered twice, or two tabs racing, carry the same idempotency/request
  key and produce exactly one ledger entry / one transition; the second call gets the first result.
* Pause takes the exclusive side of `controls`, so a wallet request cannot slip through between
  "check paused" and "move to awaiting_wallet".
* Every statement runs under row level security scoped to one Telegram user (`trading.user_id`)
  unless the code explicitly enters system scope (reconciliation, operator paths). A user can
  therefore never read or change another user's order, even through a bug in a query here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from ..paper import CHAIN
from ..plans import UnsignedTradePlan, fingerprint
from . import legs as _legs
from .legs import WALLET_REASONS, FillRecord, LegSpec
from .policy import RiskPolicy
from .states import (
    ACTIVE,
    ACTOR_KINDS,
    EDGES,
    NEEDS_REASON,
    OWNER,
    PRE_WALLET,
    RECONCILING,
    SYSTEM,
    USER,
    OrderState,
)

log = logging.getLogger(__name__)

KEY = re.compile(r"^[A-Za-z0-9:_.-]{8,128}$")
TXHASH = re.compile(r"^0x[0-9a-f]{64}$")
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
MAX_REASON = 200
DAY_S = 86400
# An authenticated user could otherwise grow the audit log by sending refused requests under
# endless fresh keys. Beyond these caps a refusal is still returned and logged, just not stored.
MAX_ADMISSION_REFUSALS_PER_DAY = 100
MAX_TRANSITION_REFUSALS_PER_ORDER = 50


# `trading_rw` is capped at 5 connections in the database (migration 0011); the pool never asks
# for more, so a burst of callbacks queues here instead of failing at login.
MAX_POOL = 4


def make_ledger_engine(url: str) -> Engine:
    """Engine for the `trading_rw` role. Never reads DATABASE_URL: the ledger must not fall
    back to the tracker's owner credentials by accident."""
    if not url:
        raise ValueError("a trading_rw database URL is required")
    return sa.create_engine(
        url, pool_size=MAX_POOL, max_overflow=0, pool_pre_ping=True, pool_recycle=1800,
        pool_timeout=10,
        connect_args={"application_name": "elysium-trading-ledger", "connect_timeout": 10})


class LedgerRefused(Exception):
    """A request the ledger declined, with a stable machine code and a safe message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class Actor:
    kind: str
    id: int | None = None

    @classmethod
    def user(cls, user_id: int) -> Actor:
        return cls(USER, user_id)

    @classmethod
    def owner(cls, user_id: int) -> Actor:
        return cls(OWNER, user_id)

    @classmethod
    def system(cls) -> Actor:
        """In-process components only (reconciler, startup recovery). Never build one from a
        request: the HTTP and Telegram layers only ever construct `Actor.user(...)`."""
        return cls(SYSTEM, None)


@dataclass(frozen=True)
class OrderView:
    """What the owner of an order may see. Wei amounts are strings (JSON-safe, no float)."""
    id: str
    state: str
    version: int
    wallet: str
    chain_id: int
    token: str
    side: str
    amount_in_wei: str
    min_out_wei: str
    notional_wei: str
    gas_reserve_wei: str
    slippage_bps: int
    price_impact_bps: int
    deadline: int
    route_hash: str
    plan_id: str
    plan_digest: str
    signature_status: str
    policy_version: int
    created_at: str
    expires_at: str
    last_reason: str | None
    attempts: tuple[dict, ...] = field(default=())

    def public(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class CreateResult:
    order: OrderView
    created: bool          # False: an identical request already existed (idempotent replay)


@dataclass(frozen=True)
class TransitionResult:
    order: OrderView
    applied: bool          # False: this request key was already applied (duplicate callback)


@dataclass(frozen=True)
class AnswerResult:
    leg: dict
    order: OrderView
    applied: bool          # False: the same answer was already recorded (duplicate callback)
    order_conflict: bool = False   # a hash was recorded but the order could not follow (see log)


@dataclass(frozen=True)
class Controls:
    paused: bool
    pause_reason: str | None
    paused_by: int | None
    execution_enabled: bool
    policy_version: int | None
    version: int


@dataclass(frozen=True)
class RecoveryReport:
    expired_cancelled: int
    wallet_deadline_ambiguous: int
    pending_reconciliation: dict[str, int]
    paused: bool
    execution_enabled: bool
    chain_ok: bool
    events_checked: int


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    checked: int
    first_bad: int | None = None
    reason: str | None = None


def _wei(value) -> int:
    return int(value) if value is not None else 0


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


def _check_key(value: str, what: str) -> str:
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise LedgerRefused("invalid_request", f"{what} must be 8-128 chars of [A-Za-z0-9:_.-]")
    return value


def _check_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise LedgerRefused("invalid_request", f"reason must be 1-{MAX_REASON} characters")
    return reason.strip()


class OrderLedger:
    def __init__(self, engine: Engine, *, owner_ids: frozenset[int] | set[int] = frozenset(),
                 clock: Callable[[], float] = time.time, chain_id: int = CHAIN):
        self.engine = engine
        self.owner_ids = frozenset(owner_ids)
        self.clock = clock
        self.chain_id = chain_id

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _tx(self, scope: str | int) -> Iterator[Connection]:
        """One transaction in a row-level-security scope: a user id, or "system"."""
        with self.engine.begin() as c:
            if scope == "system":
                c.execute(sa.text("SELECT set_config('trading.system', 'on', true), "
                                  "set_config('trading.user_id', '', true)"))
            else:
                if type(scope) is not int or scope <= 0:
                    raise LedgerRefused("unauthorized_user", "invalid user")
                c.execute(sa.text("SELECT set_config('trading.system', 'off', true), "
                                  "set_config('trading.user_id', :u, true)"), {"u": str(scope)})
            yield c

    def _run(self, fn: Callable[[], Any]) -> Any:
        """Retry only what the database rolled back for us (deadlock); map lock/statement
        timeouts to a retryable `busy`. Everything here is idempotent by key, so retry is safe."""
        for attempt in range(3):
            try:
                return fn()
            except DBAPIError as exc:
                state = _sqlstate(exc)
                if state == "40P01" and attempt < 2:
                    time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
                    continue
                if state in ("55P03", "57014", "40P01", "40001"):
                    log.warning("ledger busy (%s)", state)
                    raise LedgerRefused("busy", "the ledger is busy; retry with the same key") from exc
                if state == "23000":
                    log.error("ledger integrity guard fired: %s", exc.orig)
                    raise LedgerRefused("integrity", "the database refused an inconsistent change") from exc
                raise
        raise AssertionError("unreachable")

    def _actor_scope(self, actor: Actor) -> str | int:
        if actor.kind not in ACTOR_KINDS:
            raise LedgerRefused("actor_not_permitted", "unknown actor")
        if actor.kind == USER:
            if type(actor.id) is not int or actor.id <= 0:
                raise LedgerRefused("unauthorized_user", "invalid user")
            return actor.id
        if actor.kind == OWNER:
            if actor.id not in self.owner_ids:
                raise LedgerRefused("actor_not_permitted", "owner privileges required")
            return "system"
        if actor.id is not None:
            raise LedgerRefused("actor_not_permitted", "system actor carries no user id")
        return "system"

    def _require_owner(self, actor: Actor, *, allow_system: bool = False) -> None:
        if actor.kind == OWNER and actor.id in self.owner_ids:
            return
        if allow_system and actor.kind == SYSTEM and actor.id is None:
            return
        raise LedgerRefused("actor_not_permitted", "owner privileges required")

    # ------------------------------------------------------------------ audit

    @staticmethod
    def _event(c: Connection, *, kind: str, actor: Actor, order_id: UUID | None = None,
               user_id: int | None = None, from_state: str | None = None,
               to_state: str | None = None, request_key: str | None = None,
               detail: dict | None = None) -> bool:
        """Append an audit event. The chain position, time and hashes are assigned by the
        database trigger. Returns False when a duplicate refusal was already recorded."""
        row = c.execute(sa.text("""
            INSERT INTO order_events (order_id, user_id, actor_kind, actor_id, kind, from_state,
                                      to_state, request_key, detail, prev_hash, hash)
            VALUES (:oid, :uid, :ak, :aid, :kind, :fs, :ts, :rk, CAST(:detail AS jsonb), '', '')
            ON CONFLICT DO NOTHING RETURNING chain_no"""),
            {"oid": order_id, "uid": user_id, "ak": actor.kind, "aid": actor.id, "kind": kind,
             "fs": from_state, "ts": to_state, "rk": request_key,
             "detail": json.dumps(detail or {}, sort_keys=True, separators=(",", ":"))}).first()
        return row is not None

    @staticmethod
    def _refusal_budget(c: Connection, *, user_id: int | None = None,
                        order_id: UUID | None = None) -> bool:
        if order_id is not None:
            n = c.execute(sa.text("SELECT count(*) FROM order_events WHERE order_id = :o "
                                  "AND kind = 'TRANSITION_REFUSED'"), {"o": order_id}).scalar_one()
            ok = n < MAX_TRANSITION_REFUSALS_PER_ORDER
        else:
            n = c.execute(sa.text("SELECT count(*) FROM order_events WHERE user_id = :u "
                                  "AND kind = 'ADMISSION_REFUSED' AND at > now() - interval '1 day'"),
                          {"u": user_id}).scalar_one()
            ok = n < MAX_ADMISSION_REFUSALS_PER_DAY
        if not ok:
            log.warning("refusal audit cap reached (user=%s order=%s); not storing more", user_id, order_id)
        return ok

    # ------------------------------------------------------------------ controls / policy

    def _controls(self, c: Connection, *, lock: bool) -> sa.Row:
        row = c.execute(sa.text(
            "SELECT paused, pause_reason, paused_by, execution_enabled, policy_version, version "
            "FROM controls WHERE id = 1" + (" FOR SHARE" if lock else ""))).first()
        if row is None:
            raise LedgerRefused("integrity", "ledger controls row is missing")
        return row

    def _policy(self, c: Connection, version: int | None) -> RiskPolicy:
        if version is None:
            raise LedgerRefused("no_policy", "no risk policy has been set by the owner")
        row = c.execute(sa.text("SELECT params FROM policy_versions WHERE version = :v"),
                        {"v": version}).first()
        if row is None:
            raise LedgerRefused("no_policy", "risk policy version is missing")
        return RiskPolicy.from_params(row[0])

    def controls(self) -> Controls:
        def go():
            with self._tx("system") as c:
                r = self._controls(c, lock=False)
                return Controls(bool(r.paused), r.pause_reason, r.paused_by,
                                bool(r.execution_enabled), r.policy_version, r.version)
        return self._run(go)

    def is_paused(self) -> bool:
        """Durable pause flag. Fails closed: if the ledger cannot be read, trading is paused."""
        try:
            return self.controls().paused
        except Exception:   # noqa: BLE001 - deliberate: unreadable state must not allow trading
            log.exception("could not read the durable pause flag; treating trading as paused")
            return True

    def active_policy(self) -> RiskPolicy:
        def go():
            with self._tx("system") as c:
                return self._policy(c, self._controls(c, lock=False).policy_version)
        return self._run(go)

    def bootstrap(self) -> Controls:
        """Idempotent startup step: make sure a (deny-everything) default policy exists so
        orders can reference one. It never enables execution and never changes the pause flag."""
        def go():
            with self._tx("system") as c:
                row = c.execute(sa.text(
                    "SELECT policy_version FROM controls WHERE id = 1 FOR UPDATE")).first()
                if row is not None and row[0] is None:
                    self._store_policy(c, RiskPolicy(), Actor.system(), initial=True)
        self._run(go)
        return self.controls()

    def _store_policy(self, c: Connection, policy: RiskPolicy, actor: Actor, *,
                      initial: bool = False) -> int:
        version = c.execute(sa.text(
            "INSERT INTO policy_versions (params, digest, created_by) "
            "VALUES (CAST(:p AS jsonb), :d, :by) RETURNING version"),
            {"p": json.dumps(policy.params(), sort_keys=True), "d": policy.digest(),
             "by": actor.id}).scalar_one()
        c.execute(sa.text("UPDATE controls SET policy_version = :v, version = version + 1 "
                          "WHERE id = 1"), {"v": version})
        self._event(c, kind="POLICY_SET", actor=actor,
                    detail={"policy_version": version, "digest": policy.digest(),
                            "initial": initial})
        return version

    def set_policy(self, actor: Actor, policy: RiskPolicy) -> int:
        """Owner-only. Stores a new immutable policy version; existing orders keep theirs."""
        self._require_owner(actor)
        if not isinstance(policy, RiskPolicy):
            raise LedgerRefused("invalid_request", "policy must be a RiskPolicy")

        def go():
            with self._tx("system") as c:
                self._controls(c, lock=False)
                c.execute(sa.text("SELECT 1 FROM controls WHERE id = 1 FOR UPDATE"))
                return self._store_policy(c, policy, actor)
        return self._run(go)

    def pause(self, actor: Actor, reason: str) -> Controls:
        """Durable emergency pause. Owner or the in-process system (e.g. an automatic breaker)
        may pause; only an owner may resume. Idempotent. Never blocks reconciliation."""
        self._require_owner(actor, allow_system=True)
        why = _check_reason(reason)
        if why is None:
            raise LedgerRefused("invalid_request", "a pause needs a reason")

        def go():
            with self._tx("system") as c:
                r = c.execute(sa.text("SELECT paused FROM controls WHERE id = 1 FOR UPDATE")).first()
                if r is None:
                    raise LedgerRefused("integrity", "ledger controls row is missing")
                if not r.paused:
                    c.execute(sa.text(
                        "UPDATE controls SET paused = true, pause_reason = :r, paused_at = now(), "
                        "paused_by = :by, version = version + 1 WHERE id = 1"),
                        {"r": why, "by": actor.id})
                    self._event(c, kind="PAUSED", actor=actor, detail={"reason": why})
        self._run(go)
        log.warning("trading ledger PAUSED by %s (%s)", actor.kind, why)
        return self.controls()

    def resume(self, actor: Actor, reason: str) -> Controls:
        self._require_owner(actor)
        why = _check_reason(reason)
        if why is None:
            raise LedgerRefused("invalid_request", "a resume needs a reason")

        def go():
            with self._tx("system") as c:
                r = c.execute(sa.text("SELECT paused FROM controls WHERE id = 1 FOR UPDATE")).first()
                if r is not None and r.paused:
                    c.execute(sa.text(
                        "UPDATE controls SET paused = false, pause_reason = NULL, "
                        "paused_at = NULL, paused_by = NULL, version = version + 1 WHERE id = 1"))
                    self._event(c, kind="RESUMED", actor=actor, detail={"reason": why})
        self._run(go)
        log.warning("trading ledger RESUMED by owner %s", actor.id)
        return self.controls()

    def set_execution_enabled(self, actor: Actor, enabled: bool, reason: str) -> Controls:
        """The feature flag for wallet requests. Defaults OFF; turning it ON is owner-only,
        turning it OFF may also be done by the system (fail-safe)."""
        why = _check_reason(reason)
        if why is None or type(enabled) is not bool:
            raise LedgerRefused("invalid_request", "enabled must be a bool and a reason is required")
        self._require_owner(actor, allow_system=not enabled)

        def go():
            with self._tx("system") as c:
                r = c.execute(sa.text(
                    "SELECT execution_enabled FROM controls WHERE id = 1 FOR UPDATE")).first()
                if r is not None and r.execution_enabled != enabled:
                    c.execute(sa.text("UPDATE controls SET execution_enabled = :e, "
                                      "version = version + 1 WHERE id = 1"), {"e": enabled})
                    self._event(c, kind="EXECUTION_FLAG_SET", actor=actor,
                                detail={"enabled": enabled, "reason": why})
        self._run(go)
        return self.controls()

    # ------------------------------------------------------------------ views

    @staticmethod
    def _view(row, attempts: tuple[dict, ...] = ()) -> OrderView:
        return OrderView(
            id=str(row.id), state=row.state, version=row.version, wallet=row.wallet,
            chain_id=row.chain_id, token=row.token, side=row.side,
            amount_in_wei=str(_wei(row.amount_in_wei)), min_out_wei=str(_wei(row.min_out_wei)),
            notional_wei=str(_wei(row.notional_wei)),
            gas_reserve_wei=str(_wei(row.gas_reserve_wei)), slippage_bps=row.slippage_bps,
            price_impact_bps=row.price_impact_bps, deadline=row.deadline,
            route_hash=row.route_hash, plan_id=row.plan_id, plan_digest=row.plan_digest,
            signature_status=row.signature_status, policy_version=row.policy_version,
            created_at=row.created_at.astimezone(timezone.utc).isoformat(),
            expires_at=row.expires_at.astimezone(timezone.utc).isoformat(),
            last_reason=row.last_reason, attempts=attempts)

    _COLS = ("id, user_id, wallet, chain_id, token, side, amount_in_wei, min_out_wei, "
             "notional_wei, gas_reserve_wei, slippage_bps, price_impact_bps, deadline, route_hash, "
             "plan_id, plan_digest, signature_status, policy_version, created_at, expires_at, "
             "last_reason, state, version, update_seq")

    def _attempts(self, c: Connection, order_id) -> tuple[dict, ...]:
        rows = c.execute(sa.text(
            "SELECT attempt_no, kind, nonce, tx_hash, created_at FROM execution_attempts "
            "WHERE order_id = :o ORDER BY attempt_no"), {"o": order_id}).all()
        return tuple({"attempt_no": r.attempt_no, "kind": r.kind,
                      "nonce": None if r.nonce is None else int(r.nonce), "tx_hash": r.tx_hash,
                      "at": r.created_at.astimezone(timezone.utc).isoformat()} for r in rows)

    def get_order(self, user_id: int, order_id: str | UUID) -> OrderView:
        """Owner-bound read: another user's order is indistinguishable from a missing one."""
        oid = self._uuid(order_id)

        def go():
            with self._tx(user_id) as c:
                row = c.execute(sa.text(f"SELECT {self._COLS} FROM orders WHERE id = :i"),
                                {"i": oid}).first()
                if row is None:
                    raise LedgerRefused("not_found", "order not found")
                return self._view(row, self._attempts(c, oid))
        return self._run(go)

    def list_orders(self, user_id: int, *, limit: int = 20,
                    states: tuple[str, ...] | None = None) -> list[OrderView]:
        limit = max(1, min(int(limit), 100))
        wanted = [OrderState(s).value for s in states] if states else None

        def go():
            with self._tx(user_id) as c:
                q = f"SELECT {self._COLS} FROM orders WHERE user_id = :u"
                params: dict[str, Any] = {"u": user_id, "n": limit}
                if wanted:
                    q += " AND state = ANY(:st)"
                    params["st"] = wanted
                rows = c.execute(sa.text(q + " ORDER BY created_at DESC, id LIMIT :n"),
                                 params).all()
                return [self._view(r) for r in rows]
        return self._run(go)

    def get_events(self, user_id: int, order_id: str | UUID, *, limit: int = 100) -> list[dict]:
        oid = self._uuid(order_id)

        def go():
            with self._tx(user_id) as c:
                owned = c.execute(sa.text("SELECT 1 FROM orders WHERE id = :i"), {"i": oid}).first()
                if owned is None:
                    raise LedgerRefused("not_found", "order not found")
                rows = c.execute(sa.text(
                    "SELECT chain_no, at, actor_kind, kind, from_state, to_state, detail "
                    "FROM order_events WHERE order_id = :i ORDER BY chain_no LIMIT :n"),
                    {"i": oid, "n": max(1, min(int(limit), 500))}).all()
                return [{"chain_no": r.chain_no, "at": r.at.astimezone(timezone.utc).isoformat(),
                         "actor": r.actor_kind, "kind": r.kind, "from": r.from_state,
                         "to": r.to_state, "detail": r.detail} for r in rows]
        return self._run(go)

    @staticmethod
    def _uuid(value) -> UUID:
        try:
            return value if isinstance(value, UUID) else UUID(str(value))
        except ValueError:
            raise LedgerRefused("not_found", "order not found") from None

    # ------------------------------------------------------------------ create

    def create_order(self, *, user_id: int, wallet: str, plan: UnsignedTradePlan,
                     idempotency_key: str, session_expires_at: float | None = None
                     ) -> CreateResult:
        """Admit one reviewed unsigned plan into the ledger, exactly once per idempotency key.

        `user_id` and `wallet` come from the authenticated session (#101), never from the plan:
        the plan must agree with them. Refusals are audited (one event per key and reason)."""
        _check_key(idempotency_key, "idempotency_key")
        if type(user_id) is not int or user_id <= 0:
            raise LedgerRefused("unauthorized_user", "invalid user")
        if not isinstance(plan, UnsignedTradePlan):
            raise LedgerRefused("invalid_plan", "an unsigned plan is required")
        wallet = str(wallet).lower()
        now = float(self.clock())
        refusal: LedgerRefused | None = None
        result: CreateResult | None = None

        def go():
            nonlocal refusal, result
            refusal = result = None
            with self._tx(user_id) as c:
                try:
                    result = self._create_in_tx(c, user_id, wallet, plan, idempotency_key,
                                                session_expires_at, now)
                except LedgerRefused as r:
                    refusal = r
                    if r.code not in ("busy", "invalid_request", "not_found") \
                            and self._refusal_budget(c, user_id=user_id):
                        self._event(c, kind="ADMISSION_REFUSED", actor=Actor.user(user_id),
                                    user_id=user_id, request_key=f"{idempotency_key}:{r.code}",
                                    detail={"code": r.code,
                                            "plan_digest": _safe_digest(plan)})
        try:
            self._run(go)
        except IntegrityError as exc:
            # backstop for the UNIQUE(plan_digest) / (user_id, idempotency_key) races
            if "plan_digest" in str(exc.orig):
                raise LedgerRefused("duplicate_plan", "this plan already has an order") from exc
            raise LedgerRefused("idempotency_conflict", "conflicting order request") from exc
        if refusal is not None:
            raise refusal
        assert result is not None
        return result

    def _create_in_tx(self, c: Connection, user_id: int, wallet: str, plan: UnsignedTradePlan,
                      key: str, session_expires_at: float | None, now: float) -> CreateResult:
        ctl = self._controls(c, lock=True)
        c.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext('trading_private.admission'))"))

        # -- identity binding: everything the plan says about who must match the session
        if plan.user_id != user_id:
            raise LedgerRefused("unauthorized_user", "plan belongs to another user")
        if str(plan.wallet).lower() != wallet:
            raise LedgerRefused("wallet_mismatch", "plan wallet differs from the session wallet")
        if plan.chain_id != self.chain_id:
            raise LedgerRefused("invalid_plan", "unsupported chain")
        plan_digest = fingerprint(plan)
        request_digest = hashlib.sha256(json.dumps(
            {"u": user_id, "w": wallet, "p": plan_digest}, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

        # -- idempotency first: a replay must succeed (even while paused) and a reused key
        #    with a different request must fail loudly
        existing = c.execute(sa.text(
            f"SELECT {self._COLS}, request_digest FROM orders "
            "WHERE user_id = :u AND idempotency_key = :k"), {"u": user_id, "k": key}).first()
        if existing is not None:
            if existing.request_digest != request_digest:
                raise LedgerRefused("idempotency_conflict",
                                    "this key was used for a different request")
            return CreateResult(self._view(existing, self._attempts(c, existing.id)), False)
        dup = c.execute(sa.text("SELECT 1 FROM orders WHERE plan_digest = :d"),
                        {"d": plan_digest}).first()
        if dup is not None:
            raise LedgerRefused("duplicate_plan", "this plan already has an order")

        # -- gates
        if ctl.paused:
            raise LedgerRefused("paused", "trading is paused")
        policy = self._policy(c, ctl.policy_version)
        if session_expires_at is not None and now >= session_expires_at:
            raise LedgerRefused("session_expired", "the authenticated session has expired")
        if not (now < plan.expires_at and plan.deadline >= now + policy.min_deadline_margin_s):
            raise LedgerRefused("expired", "the reviewed plan is expired or too close to its deadline")
        if plan.side not in ("buy", "sell"):
            raise LedgerRefused("invalid_plan", "unknown side")
        token = str(plan.token).lower()
        if token not in policy.allowed_tokens:
            raise LedgerRefused("token_not_allowed", "token is not on the allowlist")
        # HYPE leg: what the user pays when buying, what they receive when selling
        notional = plan.amount_in_wei if plan.side == "buy" else plan.amount_out_wei
        if type(notional) is not int or notional <= 0 or plan.min_out_wei < 0 \
                or plan.amount_in_wei <= 0:
            raise LedgerRefused("invalid_plan", "plan amounts are not positive integers")
        gas = plan.max_gas_fee_wei if plan.max_gas_fee_wei is not None \
            else policy.default_gas_reserve_wei
        if type(gas) is not int or gas < 0:
            raise LedgerRefused("invalid_plan", "invalid gas estimate")
        if notional > policy.per_trade_cap_wei:
            raise LedgerRefused("per_trade_cap", "above the per-trade HYPE cap")
        if plan.slippage_bps > policy.max_slippage_bps:
            raise LedgerRefused("slippage", "slippage above the policy maximum")
        if plan.price_impact_bps > policy.max_price_impact_bps \
                or plan.price_impact_bps > plan.max_price_impact_bps:
            raise LedgerRefused("impact", "price impact above the policy maximum")

        day_start = now - (now % DAY_S)
        snap = c.execute(sa.text(
            "SELECT user_notional, user_gas, global_notional, global_gas, user_active, "
            "wallet_active FROM admission_snapshot(:u, :w, :ch, :a, :b)"),
            {"u": user_id, "w": wallet, "ch": self.chain_id, "a": _utc(day_start),
             "b": _utc(day_start + DAY_S)}).one()
        if snap.user_active >= policy.max_active_orders_per_user:
            raise LedgerRefused("active_orders", "too many open orders")
        if snap.wallet_active >= policy.max_conflicting_orders_per_wallet:
            raise LedgerRefused("conflicting_order", "this wallet already has an open order")
        if _wei(snap.user_notional) + notional > policy.daily_notional_cap_wei:
            raise LedgerRefused("daily_notional_cap", "daily HYPE notional cap reached")
        if _wei(snap.global_notional) + notional > policy.global_daily_notional_cap_wei:
            raise LedgerRefused("global_daily_cap", "global daily notional cap reached")
        if _wei(snap.user_gas) + gas > policy.daily_gas_cap_wei \
                or _wei(snap.global_gas) + gas > policy.global_daily_gas_cap_wei:
            raise LedgerRefused("daily_gas_cap", "daily gas cap reached")

        order_id = uuid4()
        expires = min(now + policy.order_ttl_s, plan.expires_at)
        c.execute(sa.text("""
            INSERT INTO orders (id, user_id, wallet, chain_id, idempotency_key, request_digest,
                intent_id, plan_id, plan_digest, route_hash, token, side, amount_in_wei,
                min_out_wei, notional_wei, gas_reserve_wei, slippage_bps, price_impact_bps,
                deadline, policy_version, created_at, expires_at)
            VALUES (:id, :u, :w, :ch, :k, :rd, :intent, :pid, :pd, :rh, :tok, :side, :ain, :mo,
                :nt, :gas, :sl, :pi, :dl, :pv, :cr, :ex)"""),
            {"id": order_id, "u": user_id, "w": wallet, "ch": plan.chain_id, "k": key,
             "rd": request_digest, "intent": plan.intent_id, "pid": plan.id, "pd": plan_digest,
             "rh": fingerprint(plan.snapshot.route), "tok": token, "side": plan.side,
             "ain": plan.amount_in_wei, "mo": plan.min_out_wei, "nt": notional, "gas": gas,
             "sl": plan.slippage_bps, "pi": plan.price_impact_bps, "dl": plan.deadline,
             "pv": ctl.policy_version, "cr": _utc(now), "ex": _utc(expires)})
        self._event(c, kind="ORDER_CREATED", actor=Actor.user(user_id), order_id=order_id,
                    user_id=user_id, to_state="created", request_key=key,
                    detail={"plan_digest": plan_digest, "route_hash": fingerprint(plan.snapshot.route),
                            "notional_wei": str(notional), "gas_reserve_wei": str(gas),
                            "policy_version": ctl.policy_version, "policy_digest": policy.digest()})
        row = c.execute(sa.text(f"SELECT {self._COLS} FROM orders WHERE id = :i"),
                        {"i": order_id}).one()
        return CreateResult(self._view(row), True)

    # ------------------------------------------------------------------ transitions

    def transition(self, order_id: str | UUID, to_state: OrderState | str, *, actor: Actor,
                   request_key: str, expected_version: int | None = None,
                   reason: str | None = None) -> TransitionResult:
        """Move an order along an allowed edge, exactly once per request key."""
        _check_key(request_key, "request_key")
        oid = self._uuid(order_id)
        try:
            target = OrderState(to_state)
        except ValueError:
            raise LedgerRefused("invalid_transition", "unknown state") from None
        reason = _check_reason(reason)
        scope = self._actor_scope(actor)
        now = float(self.clock())
        out: TransitionResult | None = None
        refusal: LedgerRefused | None = None

        def go():
            nonlocal out, refusal
            out = refusal = None
            with self._tx(scope) as c:
                try:
                    out = self._transition_in_tx(c, oid, target, actor, request_key,
                                                 expected_version, reason, now)
                except LedgerRefused as r:
                    refusal = r
                    if r.code not in ("busy", "not_found", "invalid_request"):
                        row = c.execute(sa.text("SELECT user_id, state FROM orders WHERE id = :i"),
                                        {"i": oid}).first()
                        if row is not None and self._refusal_budget(c, order_id=oid):
                            self._event(c, kind="TRANSITION_REFUSED", actor=actor, order_id=oid,
                                        user_id=row.user_id, from_state=row.state,
                                        to_state=target.value, request_key=request_key,
                                        detail={"code": r.code})
        self._run(go)
        if refusal is not None:
            raise refusal
        assert out is not None
        return out

    def _transition_in_tx(self, c: Connection, oid: UUID, target: OrderState, actor: Actor,
                          key: str, expected_version: int | None, reason: str | None,
                          now: float) -> TransitionResult:
        ctl = self._controls(c, lock=True)
        row = c.execute(sa.text(f"SELECT {self._COLS} FROM orders WHERE id = :i FOR UPDATE"),
                        {"i": oid}).first()
        if row is None:
            raise LedgerRefused("not_found", "order not found")

        prior = c.execute(sa.text(
            "SELECT to_state FROM order_events WHERE order_id = :i AND kind = 'TRANSITION' "
            "AND request_key = :k"), {"i": oid, "k": key}).first()
        if prior is not None:      # duplicate callback: report the current truth, change nothing
            if prior.to_state != target.value:
                raise LedgerRefused("idempotency_conflict",
                                    "this request key was used for a different transition")
            return TransitionResult(self._view(row, self._attempts(c, oid)), False)

        current = OrderState(row.state)
        if expected_version is not None and expected_version != row.version:
            raise LedgerRefused("stale_version", "the order changed; reload and retry")
        allowed = EDGES.get((current, target))
        if allowed is None:
            raise LedgerRefused("invalid_transition", f"{current.value} -> {target.value} is not allowed")
        if actor.kind not in allowed:
            raise LedgerRefused("actor_not_permitted", "this actor cannot take that transition")
        if (current, target) in NEEDS_REASON and reason is None:
            raise LedgerRefused("invalid_request", "a reason is required for this transition")

        forward_pre_wallet = current in PRE_WALLET and target != OrderState.CANCELLED
        if forward_pre_wallet:
            if ctl.paused:
                raise LedgerRefused("paused", "trading is paused")
            if now >= row.expires_at.timestamp():
                raise LedgerRefused("expired", "the order has expired")
        if target == OrderState.AWAITING_WALLET:
            if not ctl.execution_enabled:
                raise LedgerRefused("execution_disabled", "wallet requests are disabled")
            if now >= row.deadline:
                raise LedgerRefused("expired", "the transaction deadline has passed")
            # re-check against the *current* policy: tightening it must stop open orders too
            self._recheck_policy(row, self._policy(c, ctl.policy_version))
        if target in (OrderState.SUBMITTED, OrderState.INCLUDED, OrderState.REVERTED):
            need_hash = target != OrderState.SUBMITTED
            n = c.execute(sa.text(
                "SELECT count(*) FROM execution_attempts WHERE order_id = :i"
                + (" AND tx_hash IS NOT NULL" if need_hash else "")), {"i": oid}).scalar_one()
            if n == 0:   # no fabricated fills: a result needs a recorded transaction reference
                raise LedgerRefused("missing_evidence",
                                    "no recorded transaction for this order yet")

        sig = "wallet_signed" if target == OrderState.SUBMITTED else row.signature_status
        c.execute(sa.text(
            "UPDATE orders SET state = :s, version = version + 1, signature_status = :g, "
            "last_reason = :r WHERE id = :i"),
            {"s": target.value, "g": sig, "r": reason, "i": oid})
        self._event(c, kind="TRANSITION", actor=actor, order_id=oid, user_id=row.user_id,
                    from_state=current.value, to_state=target.value, request_key=key,
                    detail={"version": row.version + 1, "reason": reason})
        fresh = c.execute(sa.text(f"SELECT {self._COLS} FROM orders WHERE id = :i"),
                          {"i": oid}).one()
        return TransitionResult(self._view(fresh, self._attempts(c, oid)), True)

    @staticmethod
    def _recheck_policy(row, policy: RiskPolicy) -> None:
        if row.token not in policy.allowed_tokens:
            raise LedgerRefused("token_not_allowed", "token is no longer allowed")
        if _wei(row.notional_wei) > policy.per_trade_cap_wei:
            raise LedgerRefused("per_trade_cap", "above the current per-trade cap")
        if row.slippage_bps > policy.max_slippage_bps:
            raise LedgerRefused("slippage", "slippage above the current maximum")
        if row.price_impact_bps > policy.max_price_impact_bps:
            raise LedgerRefused("impact", "price impact above the current maximum")

    # ------------------------------------------------------------------ wallet requests

    def authorize_wallet_request(self, user_id: int, order_id: str | UUID, *, kind: str,
                                 request_key: str, digest: str, leg: LegSpec | None = None
                                 ) -> bool:
        """The gate in front of EVERY wallet prompt (approval or swap), and its audit record.

        Refuses while paused or while the execution flag is off, re-applies the current policy,
        and requires the order to be in the state that matches the prompt: `awaiting_wallet` for
        a swap, `requires_approval` for an approval (before any wallet exists nothing is signed).
        Returns False when this request key was already authorized (duplicate click/callback)."""
        _check_key(request_key, "request_key")
        if kind not in ("approval", "swap") or not HEX_DIGEST.fullmatch(digest or ""):
            raise LedgerRefused("invalid_request", "malformed wallet request")
        oid = self._uuid(order_id)
        want = OrderState.AWAITING_WALLET if kind == "swap" else OrderState.REQUIRES_APPROVAL
        now = float(self.clock())
        refusal: LedgerRefused | None = None
        fresh = False

        def go():
            nonlocal refusal, fresh
            refusal, fresh = None, False
            with self._tx(user_id) as c:
                try:
                    ctl = self._controls(c, lock=True)
                    row = c.execute(sa.text(f"SELECT {self._COLS} FROM orders WHERE id = :i "
                                            "FOR UPDATE"), {"i": oid}).first()
                    if row is None:
                        raise LedgerRefused("not_found", "order not found")
                    if ctl.paused:
                        raise LedgerRefused("paused", "trading is paused")
                    if not ctl.execution_enabled:
                        raise LedgerRefused("execution_disabled", "wallet requests are disabled")
                    if OrderState(row.state) != want:
                        raise LedgerRefused("invalid_transition",
                                            f"a {kind} prompt needs state {want.value}, not {row.state}")
                    if now >= row.deadline or (kind == "approval"
                                               and now >= row.expires_at.timestamp()):
                        raise LedgerRefused("expired", "the order has expired")
                    self._recheck_policy(row, self._policy(c, ctl.policy_version))
                    if kind == "swap" and c.execute(sa.text(
                            "SELECT 1 FROM order_events WHERE order_id = :i AND "
                            "kind = 'WALLET_REQUEST' AND detail->>'leg' = 'swap' AND "
                            "request_key IS DISTINCT FROM :k LIMIT 1"),
                            {"i": oid, "k": request_key}).first() is not None:
                        # one swap prompt per order, ever: a lost answer is resolved by chain
                        # reconciliation, never by asking the wallet to send the trade again
                        raise LedgerRefused("already_requested",
                                            "a swap prompt was already issued for this order")
                    # The leg goes in FIRST: if the database refuses it (wallet busy, second swap)
                    # nothing of this request, not even its audit event, is committed.
                    if leg is not None and _legs.by_key(c, request_key) is None:
                        if (leg.kind == "swap") != (kind == "swap"):
                            raise LedgerRefused("invalid_request", "leg kind does not match prompt")
                        _legs.insert(c, order_id=oid, user_id=user_id, key=request_key,
                                     digest=digest, spec=leg, prompted_at=_utc(now),
                                     refusal=LedgerRefused)
                    fresh = self._event(c, kind="WALLET_REQUEST", actor=Actor.user(user_id),
                                        order_id=oid, user_id=user_id, request_key=request_key,
                                        detail={"leg": kind, "digest": digest})
                except LedgerRefused as r:
                    refusal = r
                    if r.code not in ("busy", "not_found", "invalid_request"):
                        row2 = c.execute(sa.text("SELECT state FROM orders WHERE id = :i"),
                                         {"i": oid}).first()
                        if row2 is not None and self._refusal_budget(c, order_id=oid):
                            self._event(c, kind="TRANSITION_REFUSED", actor=Actor.user(user_id),
                                        order_id=oid, user_id=user_id, from_state=row2.state,
                                        request_key=f"{request_key}:{r.code}"[:200],
                                        detail={"code": r.code, "leg": kind})
        self._run(go)
        if refusal is not None:
            raise refusal
        return fresh

    def record_wallet_result(self, user_id: int, order_id: str | UUID, *, request_key: str,
                             outcome: str, tx_hash: str | None = None,
                             reason: str | None = None) -> bool:
        """Audit what the wallet answered (hash, rejection, error). Once per request key; it
        changes no order state — transitions and attempts are separate, explicit calls."""
        _check_key(request_key, "request_key")
        if outcome not in ("sent", "rejected", "error", "mismatch", "confirmed", "reverted"):
            raise LedgerRefused("invalid_request", "unknown wallet outcome")
        if tx_hash is not None and not TXHASH.fullmatch(tx_hash):
            raise LedgerRefused("invalid_request", "malformed transaction hash")
        why = _check_reason(reason)
        if why is not None and why not in WALLET_REASONS:
            raise LedgerRefused("invalid_request", "unknown wallet reason code")
        oid = self._uuid(order_id)

        def go():
            with self._tx(user_id) as c:
                row = c.execute(sa.text("SELECT 1 FROM orders WHERE id = :i"), {"i": oid}).first()
                if row is None:
                    raise LedgerRefused("not_found", "order not found")
                return self._event(c, kind="WALLET_RESULT", actor=Actor.user(user_id),
                                   order_id=oid, user_id=user_id, request_key=request_key,
                                   detail={"outcome": outcome, "tx_hash": tx_hash, "reason": why})
        return bool(self._run(go))


    # ------------------------------------------------------------------ wallet legs (#93)

    def get_legs(self, user_id: int, order_id: str | UUID) -> list[dict]:
        oid = self._uuid(order_id)

        def go():
            with self._tx(user_id) as c:
                return [_legs.public(r) for r in _legs.for_order(c, oid)]
        return self._run(go)

    def active_legs(self, *, limit: int = 200) -> list[dict]:
        """Unsettled legs for the reconciler (system scope), oldest first."""
        def go():
            with self._tx("system") as c:
                rows = c.execute(sa.text(
                    f"SELECT {_legs.COLS} FROM order_legs WHERE state = ANY(:s) "
                    "ORDER BY prompted_at, id LIMIT :l"),
                    {"s": list(_legs.EVIDENCE_LEG_STATES), "l": limit}).all()
                return [_legs.public(r) for r in rows]
        return self._run(go)

    def report_wallet_answer(self, user_id: int, request_key: str, *, outcome: str,
                             tx_hash: str | None = None, reason: str | None = None
                             ) -> AnswerResult:
        """Record, in ONE transaction, what the user's wallet answered for a prompt: the audit
        event, the leg, the attempt and the order's move. Exactly-once per request key.

        * hash      -> leg `hash_reported`, attempt recorded, swap order `submitted`
                       (an acknowledgment, never a fill);
        * rejected  -> leg `rejected`; a swap order is cancelled (nothing was sent);
        * error     -> leg `ambiguous`; a swap order becomes `ambiguous`: the wallet may have sent
                       it, so only chain evidence (hash / nonce) can resolve it, never a re-prompt.
        A repeat of the same answer is a no-op; a different second answer is a conflict."""
        _check_key(request_key, "request_key")
        if outcome not in ("hash", "rejected", "error"):
            raise LedgerRefused("invalid_request", "unknown wallet outcome")
        if outcome == "hash" and (not isinstance(tx_hash, str) or not TXHASH.fullmatch(tx_hash)):
            raise LedgerRefused("invalid_request", "malformed transaction hash")
        if outcome != "hash":
            tx_hash = None
        why = _check_reason(reason)
        if why is not None and why not in WALLET_REASONS:
            raise LedgerRefused("invalid_request", "unknown wallet reason code")
        now = float(self.clock())
        system = Actor.system()
        out: AnswerResult | None = None

        def go():
            nonlocal out
            with self._tx(user_id) as c:
                head = _legs.by_key(c, request_key)
                if head is None:
                    raise LedgerRefused("not_found", "unknown wallet request")
                oid = head.order_id
                self._controls(c, lock=True)
                order = c.execute(sa.text(f"SELECT {self._COLS} FROM orders WHERE id = :i "
                                          "FOR UPDATE"), {"i": oid}).first()
                leg = _legs.by_key(c, request_key, lock=True)
                if order is None or leg is None:
                    raise LedgerRefused("not_found", "unknown wallet request")
                is_swap = leg.kind == "swap"
                state = leg.state

                if state != "prompted" and not (
                        state == "ambiguous" and leg.tx_hash is None and outcome == "hash"):
                    same = ((outcome == "hash" and leg.tx_hash == tx_hash)
                            or (outcome == "rejected" and state == "rejected")
                            or (outcome == "error" and state == "ambiguous"
                                and leg.tx_hash is None))
                    if not same:
                        raise LedgerRefused("conflict", "this wallet request was already answered")
                    out = AnswerResult(_legs.public(leg), self._view(order, self._attempts(c, oid)),
                                       False)
                    return

                new_state = {"hash": "hash_reported", "rejected": "rejected",
                             "error": "ambiguous"}[outcome]
                try:
                    with c.begin_nested():
                        c.execute(sa.text(
                            "UPDATE order_legs SET state = :s, tx_hash = :h, version = version + 1, "
                            "answered_at = :t, resolution = :r WHERE id = :i"),
                            {"s": new_state, "h": tx_hash, "t": _utc(now), "i": leg.id,
                             "r": "wallet_error" if outcome == "error" else None})
                except IntegrityError:
                    raise LedgerRefused("conflict", "that transaction hash belongs to another request") from None
                self._event(c, kind="WALLET_RESULT", actor=Actor.user(user_id), order_id=oid,
                            user_id=user_id, request_key=request_key,
                            detail={"outcome": {"hash": "sent"}.get(outcome, outcome),
                                    "tx_hash": tx_hash, "reason": why, "leg_no": int(leg.leg_no)})
                conflict = False
                ostate = OrderState(order.state)
                if outcome == "hash":
                    allowed = ({OrderState.AWAITING_WALLET, OrderState.AMBIGUOUS,
                                OrderState.SUBMITTED} if is_swap else {OrderState.REQUIRES_APPROVAL})
                    if ostate in allowed:
                        self._record_attempt_in_tx(c, oid, user_id, "swap" if is_swap else "approval",
                                                   tx_hash, system)
                        if is_swap and ostate != OrderState.SUBMITTED:
                            self._transition_in_tx(c, oid, OrderState.SUBMITTED, system,
                                                   f"submitted:{request_key}"[:128], None, None, now)
                    else:
                        conflict = True
                        log.error("wallet hash for order %s arrived in state %s; leg recorded, "
                                  "order left as is", oid, ostate.value)
                elif outcome == "rejected":
                    if is_swap and ostate == OrderState.AWAITING_WALLET:
                        self._transition_in_tx(c, oid, OrderState.CANCELLED, system,
                                               f"rejected:{request_key}"[:128], None,
                                               "wallet_rejected", now)
                else:
                    if is_swap and ostate == OrderState.AWAITING_WALLET:
                        self._transition_in_tx(c, oid, OrderState.AMBIGUOUS, system,
                                               f"walleterr:{request_key}"[:128], None,
                                               "wallet error; verify on chain", now)
                fresh_leg = _legs.by_key(c, request_key)
                fresh_order = c.execute(sa.text(f"SELECT {self._COLS} FROM orders WHERE id = :i"),
                                        {"i": oid}).first()
                out = AnswerResult(_legs.public(fresh_leg), self._view(fresh_order,
                                   self._attempts(c, oid)), True, conflict)
        self._run(go)
        assert out is not None
        return out

    def _record_attempt_in_tx(self, c: Connection, oid: UUID, user_id: int, kind: str,
                              tx_hash: str | None, actor: Actor) -> None:
        if c.execute(sa.text("SELECT 1 FROM execution_attempts WHERE tx_hash = :h"),
                     {"h": tx_hash}).first() is not None:
            return
        n = c.execute(sa.text("SELECT coalesce(max(attempt_no), 0) + 1 FROM execution_attempts "
                              "WHERE order_id = :i"), {"i": oid}).scalar_one()
        c.execute(sa.text("INSERT INTO execution_attempts (order_id, user_id, attempt_no, kind, "
                          "tx_hash) VALUES (:o, :u, :n, :k, :h)"),
                  {"o": oid, "u": user_id, "n": n, "k": kind, "h": tx_hash})
        self._event(c, kind="ATTEMPT_RECORDED", actor=actor, order_id=oid, user_id=user_id,
                    detail={"attempt_no": n, "attempt_kind": kind, "nonce": None,
                            "tx_hash": tx_hash})

    def resolve_leg(self, leg_id: int, *, expected_version: int, to_state: str, resolution: str,
                    nonce: int | None = None, final_tx_hash: str | None = None,
                    seen: bool = False, order_path: tuple[OrderState | str, ...] = (),
                    order_reason: str | None = None) -> bool:
        """Reconciler write path (system scope): move a leg on chain evidence, optionally with the
        order (stepping through `order_path`, skipping states it is already in), in one
        transaction. Optimistic: returns False if the leg changed since it was read
        (another worker or a late wallet answer won), so evidence is never applied to stale state."""
        now = float(self.clock())
        system = Actor.system()
        if final_tx_hash is not None and not TXHASH.fullmatch(final_tx_hash):
            raise LedgerRefused("invalid_request", "malformed transaction hash")
        path = tuple(OrderState(x) for x in order_path)
        resolution = (_check_reason(resolution) or "")[:200]

        def go() -> bool:
            with self._tx("system") as c:
                head = _legs.by_id(c, leg_id)
                if head is None:
                    raise LedgerRefused("not_found", "unknown leg")
                self._controls(c, lock=True)
                c.execute(sa.text("SELECT 1 FROM orders WHERE id = :i FOR UPDATE"),
                          {"i": head.order_id})
                leg = _legs.by_id(c, leg_id, lock=True)
                if leg.version != expected_version:
                    return False
                terminal = to_state in ("rejected", "superseded", "abandoned")
                try:
                    with c.begin_nested():
                        c.execute(sa.text(
                            "UPDATE order_legs SET state = :s, version = version + 1, "
                            "resolution = :r, nonce = coalesce(nonce, :n), "
                            "final_tx_hash = coalesce(final_tx_hash, :f), "
                            "seen_at = CASE WHEN :seen THEN coalesce(seen_at, :t) ELSE seen_at END, "
                            "resolved_at = CASE WHEN :term THEN :t ELSE resolved_at END "
                            "WHERE id = :i"),
                            {"s": to_state, "r": resolution, "n": nonce, "f": final_tx_hash,
                             "seen": seen, "t": _utc(now), "term": terminal, "i": leg_id})
                except IntegrityError as exc:
                    raise LedgerRefused("invalid_transition",
                                        f"leg {leg.state} -> {to_state} is not allowed") from exc
                version = expected_version + 1
                evidence_hash = final_tx_hash or leg.tx_hash
                if OrderState.SUBMITTED in path and evidence_hash is not None:
                    # "submitted" needs a recorded transaction reference (database rule)
                    self._record_attempt_in_tx(c, leg.order_id, int(leg.user_id), leg.kind
                                               if leg.kind == "swap" else "approval",
                                               evidence_hash, system)
                self._event(c, kind="WALLET_RESULT", actor=system, order_id=leg.order_id,
                            user_id=int(leg.user_id), request_key=f"recon-{leg_id}-v{version}",
                            detail={"outcome": f"leg_{to_state}", "tx_hash": leg.tx_hash,
                                    "reason": resolution, "leg_no": int(leg.leg_no),
                                    "final_tx_hash": final_tx_hash, "nonce": nonce})
                current = OrderState(c.execute(sa.text("SELECT state FROM orders WHERE id = :i"),
                                               {"i": leg.order_id}).scalar_one())
                for i, step_to in enumerate(path):       # e.g. submitted -> ambiguous -> cancelled
                    if current == step_to:
                        continue
                    self._transition_in_tx(c, leg.order_id, step_to, system,
                                           f"recon-{leg_id}-v{version}-{i}", None, order_reason, now)
                    current = step_to
                return True
        return bool(self._run(go))


    # ------------------------------------------------------------------ receipts and fills (#94)

    def receipt_candidates(self, *, limit: int = 200) -> list[dict]:
        """Legs the receipt worker must keep checking until final (system scope). Oldest first so
        a flood of new legs cannot starve an old one; it works while trading is paused."""
        def go():
            with self._tx("system") as c:
                rows = c.execute(sa.text(
                    f"SELECT {_legs.COLS} FROM order_legs WHERE finalized_at IS NULL "
                    "AND state = ANY(:s) AND (tx_hash IS NOT NULL OR final_tx_hash IS NOT NULL) "
                    "ORDER BY id LIMIT :l"),
                    {"s": list(_legs.RECEIPT_LEG_STATES), "l": limit}).all()
                return [_legs.public(r) for r in rows]
        return self._run(go)

    def get_fills(self, user_id: int, order_id: str | UUID | None = None, *,
                  include_rolled_back: bool = False) -> list[dict]:
        oid = self._uuid(order_id) if order_id is not None else None

        def go():
            with self._tx(user_id) as c:
                rows = c.execute(sa.text(
                    f"SELECT {_legs.FILL_COLS} FROM order_fills WHERE "
                    "(CAST(:o AS uuid) IS NULL OR order_id = CAST(:o AS uuid)) "
                    "AND (:rb OR status <> 'rolled_back') ORDER BY id"),
                    {"o": oid, "rb": include_rolled_back}).all()
                return [_legs.fill_public(r) for r in rows]
        return self._run(go)

    def apply_receipt(self, leg_id: int, *, expected_version: int, state: str,
                      block_number: int, block_hash: str, receipt_status: int, gas_used: int,
                      gas_cost_wei: int, fill: FillRecord | None, finalize: bool) -> bool:
        """Record a validated receipt (system scope), atomically: the leg, the fill (one row per
        inclusion, so re-applying the same inclusion changes nothing), and the swap order's move
        (submitted/ambiguous/orphaned -> included|reverted [-> finalized]). An approval leg never
        touches the order and never produces a fill. Optimistic on `expected_version`."""
        if state not in ("included", "reverted") or receipt_status not in (0, 1) or (
                state == "included") != (receipt_status == 1):
            raise LedgerRefused("invalid_request", "receipt state does not match its status")
        if not TXHASH.fullmatch(block_hash or ""):
            raise LedgerRefused("invalid_request", "malformed block hash")
        now = float(self.clock())
        system = Actor.system()
        S_ = OrderState

        def go() -> bool:
            with self._tx("system") as c:
                head = _legs.by_id(c, leg_id)
                if head is None:
                    raise LedgerRefused("not_found", "unknown leg")
                self._controls(c, lock=True)
                c.execute(sa.text("SELECT 1 FROM orders WHERE id = :i FOR UPDATE"),
                          {"i": head.order_id})
                leg = _legs.by_id(c, leg_id, lock=True)
                if leg.version != expected_version or leg.finalized_at is not None:
                    return False
                is_swap = leg.kind == "swap"
                try:
                    with c.begin_nested():
                        c.execute(sa.text(
                            "UPDATE order_legs SET state = :s, version = version + 1, "
                            "block_number = :bn, block_hash = :bh, receipt_status = :rs, "
                            "gas_used = :gu, gas_cost_wei = :gc, "
                            "included_at = coalesce(included_at, :t), "
                            "finalized_at = CASE WHEN :fin THEN :t ELSE NULL END, "
                            "seen_at = coalesce(seen_at, :t), "
                            "resolution = :r WHERE id = :i"),
                            {"s": state, "bn": block_number, "bh": block_hash, "rs": receipt_status,
                             "gu": gas_used, "gc": gas_cost_wei, "t": _utc(now), "fin": finalize,
                             "r": "receipt_final" if finalize else "receipt_provisional",
                             "i": leg_id})
                except IntegrityError as exc:
                    raise LedgerRefused("invalid_transition",
                                        f"leg {leg.state} -> {state} is not allowed") from exc
                if is_swap and fill is not None:
                    # an older provisional row for a different block is a rolled-back inclusion
                    c.execute(sa.text(
                        "UPDATE order_fills SET status = 'rolled_back' WHERE leg_id = :l "
                        "AND status = 'provisional' AND block_hash <> :bh"),
                        {"l": leg_id, "bh": block_hash})
                    c.execute(sa.text("""
                        INSERT INTO order_fills (order_id, leg_id, user_id, wallet, chain_id, token,
                            side, tx_hash, block_number, block_hash, verdict, native_wei, token_wei,
                            gas_cost_wei, basis, reason)
                        VALUES (:o, :l, :u, :w, :c, :tk, :sd, :tx, :bn, :bh, :v, :nw, :tw, :gc,
                                :b, :r)
                        ON CONFLICT (leg_id, block_hash) DO NOTHING"""),
                        {"o": leg.order_id, "l": leg_id, "u": leg.user_id, "w": leg.wallet,
                         "c": leg.chain_id, "tk": fill.token, "sd": fill.side,
                         "tx": leg.final_tx_hash or leg.tx_hash, "bn": block_number,
                         "bh": block_hash, "v": fill.verdict, "nw": fill.native_wei,
                         "tw": fill.token_wei, "gc": fill.gas_cost_wei, "b": fill.basis,
                         "r": fill.reason})
                    if finalize:
                        c.execute(sa.text(
                            "UPDATE order_fills SET status = 'final' WHERE leg_id = :l "
                            "AND block_hash = :bh AND status = 'provisional'"),
                            {"l": leg_id, "bh": block_hash})
                version = expected_version + 1
                self._event(c, kind="WALLET_RESULT", actor=system, order_id=leg.order_id,
                            user_id=int(leg.user_id), request_key=f"recon-{leg_id}-v{version}",
                            detail={"outcome": f"receipt_{state}", "tx_hash": leg.tx_hash,
                                    "block_number": block_number, "block_hash": block_hash,
                                    "final": finalize, "leg_no": int(leg.leg_no),
                                    "verdict": fill.verdict if fill else None})
                if is_swap:
                    cur = OrderState(c.execute(sa.text("SELECT state FROM orders WHERE id = :i"),
                                               {"i": leg.order_id}).scalar_one())
                    target = S_.INCLUDED if state == "included" else S_.REVERTED
                    path = (target, S_.FINALIZED) if finalize else (target,)
                    for i, step_to in enumerate(path):
                        if cur == step_to:
                            continue
                        self._transition_in_tx(c, leg.order_id, step_to, system,
                                               f"recon-{leg_id}-v{version}-{i}", None,
                                               "receipt " + state, now)
                        cur = step_to
                return True
        return bool(self._run(go))

    def apply_reorg(self, leg_id: int, *, expected_version: int, reason: str) -> bool:
        """The block that held this leg's transaction is no longer canonical (or the receipt is
        gone): undo the provisional fill, mark the leg and its swap order `orphaned`, and let
        reconciliation find the canonical outcome. Never deletes anything; refuses to touch a
        finalized order."""
        now = float(self.clock())
        system = Actor.system()
        why = (_check_reason(reason) or "")[:200]

        def go() -> bool:
            with self._tx("system") as c:
                head = _legs.by_id(c, leg_id)
                if head is None:
                    raise LedgerRefused("not_found", "unknown leg")
                self._controls(c, lock=True)
                c.execute(sa.text("SELECT 1 FROM orders WHERE id = :i FOR UPDATE"),
                          {"i": head.order_id})
                leg = _legs.by_id(c, leg_id, lock=True)
                if (leg.version != expected_version or leg.finalized_at is not None
                        or leg.state not in ("included", "reverted")):
                    return False
                try:
                    with c.begin_nested():
                        c.execute(sa.text(
                            "UPDATE order_legs SET state = 'orphaned', version = version + 1, "
                            "resolution = :r WHERE id = :i"), {"r": why, "i": leg_id})
                except IntegrityError as exc:
                    raise LedgerRefused("invalid_transition", "leg cannot be orphaned") from exc
                c.execute(sa.text("UPDATE order_fills SET status = 'rolled_back' WHERE leg_id = :l "
                                  "AND status = 'provisional'"), {"l": leg_id})
                version = expected_version + 1
                self._event(c, kind="WALLET_RESULT", actor=system, order_id=leg.order_id,
                            user_id=int(leg.user_id), request_key=f"recon-{leg_id}-v{version}",
                            detail={"outcome": "reorg", "tx_hash": leg.tx_hash, "reason": why,
                                    "block_hash": leg.block_hash, "leg_no": int(leg.leg_no)})
                if leg.kind == "swap":
                    cur = OrderState(c.execute(sa.text("SELECT state FROM orders WHERE id = :i"),
                                               {"i": leg.order_id}).scalar_one())
                    if cur in (OrderState.INCLUDED, OrderState.REVERTED):
                        self._transition_in_tx(c, leg.order_id, OrderState.ORPHANED, system,
                                               f"recon-{leg_id}-v{version}-0", None,
                                               "reorg: " + why[:150], now)
                return True
        return bool(self._run(go))

    # ------------------------------------------------------------------ execution attempts

    def record_attempt(self, order_id: str | UUID, *, kind: str, nonce: int | None = None,
                       tx_hash: str | None = None, actor: Actor | None = None) -> dict:
        """Record a nonce / transaction hash reported by the (future) submission component.
        The ledger stores references only; it never sees a signature or a key. Idempotent per
        tx hash."""
        actor = actor or Actor.system()
        if actor.kind != SYSTEM:
            raise LedgerRefused("actor_not_permitted", "only the system records attempts")
        if kind not in ("approval", "swap"):
            raise LedgerRefused("invalid_request", "unknown attempt kind")
        if tx_hash is not None and (not isinstance(tx_hash, str) or not TXHASH.fullmatch(tx_hash)):
            raise LedgerRefused("invalid_request", "malformed transaction hash")
        if nonce is not None and (type(nonce) is not int or not 0 <= nonce < 2**63):
            raise LedgerRefused("invalid_request", "malformed nonce")
        if nonce is None and tx_hash is None:
            raise LedgerRefused("invalid_request", "a nonce or a transaction hash is required")
        oid = self._uuid(order_id)

        def go():
            with self._tx("system") as c:
                row = c.execute(sa.text("SELECT user_id, state FROM orders WHERE id = :i "
                                        "FOR UPDATE"), {"i": oid}).first()
                if row is None:
                    raise LedgerRefused("not_found", "order not found")
                allowed = {OrderState.REQUIRES_APPROVAL} if kind == "approval" else RECONCILING
                if OrderState(row.state) not in allowed:
                    raise LedgerRefused("invalid_transition",
                                        f"cannot record a {kind} attempt in state {row.state}")
                if tx_hash is not None:
                    dup = c.execute(sa.text(
                        "SELECT order_id, attempt_no FROM execution_attempts WHERE tx_hash = :h"),
                        {"h": tx_hash}).first()
                    if dup is not None:
                        if dup.order_id != oid:
                            raise LedgerRefused("idempotency_conflict",
                                                "transaction hash belongs to another order")
                        return {"attempt_no": dup.attempt_no, "recorded": False}
                n = c.execute(sa.text(
                    "SELECT coalesce(max(attempt_no), 0) + 1 FROM execution_attempts "
                    "WHERE order_id = :i"), {"i": oid}).scalar_one()
                c.execute(sa.text(
                    "INSERT INTO execution_attempts (order_id, user_id, attempt_no, kind, nonce, "
                    "tx_hash) VALUES (:o, :u, :n, :k, :nc, :h)"),
                    {"o": oid, "u": row.user_id, "n": n, "k": kind, "nc": nonce, "h": tx_hash})
                self._event(c, kind="ATTEMPT_RECORDED", actor=actor, order_id=oid,
                            user_id=row.user_id, detail={"attempt_no": n, "attempt_kind": kind,
                                                         "nonce": nonce, "tx_hash": tx_hash})
                return {"attempt_no": n, "recorded": True}
        return self._run(go)

    # ------------------------------------------------------------------ recovery / reconcile

    def pending_reconciliation(self, *, limit: int = 100) -> list[dict]:
        """Orders whose on-chain outcome the receipt reconciler must still resolve. This keeps
        working while trading is paused: pause stops new wallet requests, not bookkeeping."""
        states = [s.value for s in RECONCILING]

        def go():
            with self._tx("system") as c:
                rows = c.execute(sa.text(
                    "SELECT id, state, version, update_seq FROM orders WHERE state = ANY(:s) "
                    "ORDER BY update_seq LIMIT :n"), {"s": states, "n": max(1, min(limit, 500))}).all()
                return [{"id": str(r.id), "state": r.state, "version": r.version} for r in rows]
        return self._run(go)

    def expire_due(self, *, limit: int = 200) -> tuple[int, int]:
        """Pre-wallet orders past their expiry are cancelled (nothing can have been signed).
        An order still awaiting a wallet past its on-chain deadline becomes AMBIGUOUS, not
        cancelled: a wallet may have signed and broadcast before the process stopped."""
        now = float(self.clock())
        pre = [s.value for s in PRE_WALLET]

        def due():
            with self._tx("system") as c:
                a = c.execute(sa.text(
                    "SELECT id, version FROM orders WHERE state = ANY(:s) AND expires_at <= :n "
                    "ORDER BY update_seq LIMIT :l"),
                    {"s": pre, "n": _utc(now), "l": limit}).all()
                b = c.execute(sa.text(
                    "SELECT id, version FROM orders WHERE state = 'awaiting_wallet' "
                    "AND deadline <= :n ORDER BY update_seq LIMIT :l"),
                    {"n": int(now), "l": limit}).all()
                return a, b
        expired, wallet = self._run(due)
        cancelled = ambiguous = 0
        system = Actor.system()
        for r in expired:
            try:
                res = self.transition(r.id, OrderState.CANCELLED, actor=system,
                                      request_key=f"expire:{r.id}:{r.version}", reason="expired")
                cancelled += int(res.applied)
            except LedgerRefused as exc:
                log.info("expire skipped for %s: %s", r.id, exc)
        for r in wallet:
            try:
                res = self.transition(r.id, OrderState.AMBIGUOUS, actor=system,
                                      request_key=f"walletdl:{r.id}:{r.version}",
                                      reason="wallet deadline passed without a result")
                ambiguous += int(res.applied)
            except LedgerRefused as exc:
                log.info("deadline sweep skipped for %s: %s", r.id, exc)
        return cancelled, ambiguous

    def recover(self) -> RecoveryReport:
        """Run once at process start. It does not "fix" in-flight orders: pending and ambiguous
        orders keep their state across a restart so the reconciler can resolve them from chain
        evidence. It only applies time-based expiry, and records what it found."""
        cancelled, ambiguous = self.expire_due()
        chain = self.verify_chain()
        ctl = self.controls()

        def census():
            with self._tx("system") as c:
                rows = c.execute(sa.text(
                    "SELECT state, count(*) AS n FROM orders WHERE state = ANY(:s) GROUP BY state"),
                    {"s": [s.value for s in ACTIVE | RECONCILING]}).all()
                counts = {r.state: int(r.n) for r in rows}
                self._event(c, kind="RECOVERY", actor=Actor.system(),
                            detail={"expired_cancelled": cancelled,
                                    "wallet_deadline_ambiguous": ambiguous, "open": counts,
                                    "paused": ctl.paused, "chain_ok": chain.ok})
                return counts
        counts = self._run(census)
        report = RecoveryReport(cancelled, ambiguous, counts, ctl.paused,
                                ctl.execution_enabled, chain.ok, chain.checked)
        log.info("ledger recovery: %s", report)
        return report

    # ------------------------------------------------------------------ integrity / usage

    def verify_chain(self, *, batch: int = 2000) -> ChainCheck:
        """Recompute the audit hash chain in the database and check linkage and contiguity."""
        last_no, last_hash, checked = 0, "0" * 64, 0
        while True:
            rows = self._chain_page(last_no, batch)
            if not rows:
                return ChainCheck(True, checked)
            for r in rows:
                if r.chain_no != last_no + 1:
                    return ChainCheck(False, checked, r.chain_no, "gap in chain numbers")
                if r.prev_hash != last_hash:
                    return ChainCheck(False, checked, r.chain_no, "broken link")
                if r.hash != r.calc:
                    return ChainCheck(False, checked, r.chain_no, "hash mismatch")
                last_no, last_hash, checked = r.chain_no, r.hash, checked + 1

    def _chain_page(self, after: int, batch: int) -> list:
        def go() -> list:
            with self._tx("system") as c:
                return list(c.execute(sa.text(
                    "SELECT e.chain_no, e.prev_hash, e.hash, event_digest(e) AS calc "
                    "FROM order_events e WHERE e.chain_no > :a ORDER BY e.chain_no LIMIT :n"),
                    {"a": after, "n": batch}).all())
        return self._run(go)

    def usage(self, *, now: float | None = None) -> dict[str, int]:
        """Aggregate budget usage for operators and the owner's settings screen: numbers only."""
        t = float(self.clock() if now is None else now)
        start = t - (t % DAY_S)

        def go():
            with self._tx("system") as c:
                s = c.execute(sa.text(
                    "SELECT user_notional, user_gas, global_notional, global_gas, user_active, "
                    "wallet_active FROM admission_snapshot(0, '', :ch, :a, :b)"),
                    {"ch": self.chain_id, "a": _utc(start), "b": _utc(start + DAY_S)}).one()
                return {"global_notional_wei": _wei(s.global_notional),
                        "global_gas_wei": _wei(s.global_gas)}
        return self._run(go)


def _safe_digest(plan) -> str | None:
    try:
        return fingerprint(plan)
    except Exception:   # noqa: BLE001 - a malformed plan must still produce an audit event
        return None
