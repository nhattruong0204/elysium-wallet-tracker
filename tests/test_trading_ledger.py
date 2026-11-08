"""Private order ledger (issue #91) against a real, isolated PostgreSQL.

The ledger runs as the real least-privilege `trading_rw` role, so row level security and column
grants are exercised for real (the migrating owner would bypass them). Nothing here signs,
approves, broadcasts or touches funds: plans are synthetic frozen dataclasses.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa
from conftest import ROLE_PASSWORD, TEST_URL  # noqa: F401  (ROLE_PASSWORD: dashboard role)

from agora_tracker.db import make_engine
from agora_tracker.migrate import downgrade, set_trading_password, upgrade
from agora_tracker.trading.ledger import (
    EDGES,
    Actor,
    LedgerRefused,
    OrderLedger,
    OrderState,
    RiskPolicy,
    make_ledger_engine,
)
from agora_tracker.trading.ledger.policy import HYPE
from agora_tracker.trading.plans import (
    PlanSnapshot,
    UnsignedTradePlan,
    UnsignedTransaction,
    VerifiedRoute,
    fingerprint,
)

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")

S = OrderState
TRADING_PASSWORD = "test-trading-password-0123456789"
TOKEN = "0x" + "11" * 20
TOKEN2 = "0x" + "22" * 20
W1 = "0x" + "a1" * 20
W2 = "0x" + "a2" * 20
U1, U2, OWNER = 1001, 1002, 9000
NOW0 = 1_800_000_000.0           # a fixed instant (2027-01-15 08:00 UTC)
T = "ledger-test-"


def trading_url() -> str:
    return sa.engine.make_url(TEST_URL).set(
        username="trading_rw", password=TRADING_PASSWORD).render_as_string(hide_password=False)


def make_plan(*, user=U1, wallet=W1, token=TOKEN, side="buy", amount_in=HYPE // 2,
              amount_out=10**21, slippage=50, impact=20, gas=10**15, now=NOW0, nonce="a",
              expires_in=300, deadline_in=600) -> UnsignedTradePlan:
    route = VerifiedRoute("0x" + "01" * 20, "0x" + "02" * 20, "0x" + "03" * 20, (token, token),
                          "h1", "h2", "h3", "h4", "h5")
    snap = PlanSnapshot(10, "0x" + "12" * 32, int(now), 18, 0, 0, None, 1, 1, route)
    tx = UnsignedTransaction(wallet, "0x" + "01" * 20, amount_in if side == "buy" else 0,
                             "0x" + "00" * 4, None, None)
    return UnsignedTradePlan(
        id=f"plan-{nonce}", user_id=user, session_binding="sess-" + nonce,
        intent_id=f"intent-{nonce}", wallet=wallet, chain_id=99901, token=token, side=side,
        path=(token, token), amount_in_wei=amount_in, amount_out_wei=amount_out,
        min_out_wei=amount_out // 2, slippage_bps=slippage, price_impact_bps=impact,
        max_price_impact_bps=100, pool_fee_bps=30, recipient=wallet, spender=None,
        finite_approval_wei=0, snapshot=snap, transaction=tx, max_gas_fee_wei=gas,
        simulation_status="ok", created_at=now, expires_at=now + expires_in,
        deadline=int(now + deadline_in))


def permissive(**kw) -> RiskPolicy:
    base = dict(per_trade_cap_wei=HYPE, daily_notional_cap_wei=4 * HYPE,
                global_daily_notional_cap_wei=8 * HYPE, max_active_orders_per_user=5,
                max_conflicting_orders_per_wallet=5, allowed_tokens=tuple(sorted({TOKEN, TOKEN2})))
    base.update(kw)
    return RiskPolicy(**base)


LEDGER_TABLES = ("leg_recoveries", "order_events", "execution_attempts", "order_fills", "order_legs", "orders", "controls",
                 "policy_versions", "order_state_edges")


def reset_ledger(engine) -> None:
    """Test-only wipe, as the schema owner: the append-only guards are user triggers, so they are
    switched off for the wipe and back on. This is the only way rows ever disappear."""
    with engine.begin() as c:
        for t in LEDGER_TABLES:
            c.execute(sa.text(f"ALTER TABLE trading_private.{t} DISABLE TRIGGER USER"))
        c.execute(sa.text("TRUNCATE trading_private.leg_recoveries, trading_private.order_events, trading_private.execution_attempts, trading_private.order_fills, "
                          "trading_private.order_legs, trading_private.orders, trading_private.controls, "
                          "trading_private.policy_versions RESTART IDENTITY CASCADE"))
        c.execute(sa.text("INSERT INTO trading_private.controls (id) VALUES (1)"))
        c.execute(sa.text("ALTER SEQUENCE trading_private.order_update_seq RESTART"))
        for t in LEDGER_TABLES:
            c.execute(sa.text(f"ALTER TABLE trading_private.{t} ENABLE TRIGGER USER"))


class Clock:
    def __init__(self, t=NOW0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def owner_engine(pg_engine):
    reset_ledger(pg_engine)
    set_trading_password(pg_engine, TRADING_PASSWORD)
    return pg_engine


@pytest.fixture
def rw_engine(owner_engine):
    eng = make_ledger_engine(trading_url())
    yield eng
    eng.dispose()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ledger(rw_engine, clock):
    lg = OrderLedger(rw_engine, owner_ids={OWNER}, clock=clock)
    lg.bootstrap()
    lg.set_policy(Actor.owner(OWNER), permissive())
    return lg


@pytest.fixture
def live(ledger):
    """Ledger with the execution flag ON (it defaults OFF) so wallet requests can be tested."""
    ledger.set_execution_enabled(Actor.owner(OWNER), True, "test")
    return ledger


def create(lg, n="a", **kw):
    user = kw.pop("user", U1)
    wallet = kw.pop("wallet", W1)
    plan = make_plan(user=user, wallet=wallet, nonce=n, now=lg.clock(), **kw)
    return lg.create_order(user_id=user, wallet=wallet, plan=plan, idempotency_key=f"{T}{n}-key"), plan


def step(lg, oid, to, n, actor=None, **kw):
    return lg.transition(oid, to, actor=actor or Actor.user(U1), request_key=f"{T}{n}-step", **kw)


def refused(code):
    return pytest.raises(LedgerRefused, match=f"^{code}:")


def to_awaiting(lg, oid, tag="x"):
    step(lg, oid, S.QUOTE_REVIEW, f"{tag}1")
    return step(lg, oid, S.AWAITING_WALLET, f"{tag}2")


def to_submitted(lg, oid, tag="x", tx="0x" + "ab" * 32):
    to_awaiting(lg, oid, tag)
    lg.record_attempt(oid, kind="swap", nonce=7, tx_hash=tx)
    return step(lg, oid, S.SUBMITTED, f"{tag}3", actor=Actor.system())


def raw(engine, sql, **params):
    with engine.begin() as c:
        res = c.execute(sa.text(sql), params)
        return res.all() if res.returns_rows else []


def n_orders(engine) -> int:
    return raw(engine, "SELECT count(*) FROM trading_private.orders")[0][0]


# =============================================================== schema / state machine

def test_python_edges_equal_database_edges(owner_engine):
    db = {(r[0], r[1]) for r in raw(owner_engine,
          "SELECT from_state, to_state FROM trading_private.order_state_edges")}
    assert db == {(a.value, b.value) for a, b in EDGES}


def test_flag_and_pause_defaults(ledger):
    c = ledger.controls()
    assert (c.paused, c.execution_enabled) == (False, False)
    assert c.policy_version is not None


def test_bootstrap_is_idempotent_and_default_policy_denies_everything(rw_engine, clock):
    lg = OrderLedger(rw_engine, owner_ids={OWNER}, clock=clock)
    first = lg.bootstrap()
    again = lg.bootstrap()
    assert first.policy_version == again.policy_version
    with refused("token_not_allowed"):
        create(lg)


def test_no_policy_fails_closed(rw_engine, clock):
    lg = OrderLedger(rw_engine, owner_ids={OWNER}, clock=clock)    # never bootstrapped
    with refused("no_policy"):
        create(lg)


# =============================================================== create + persistence

def test_create_persists_the_exact_reviewed_intent(ledger, owner_engine):
    res, plan = create(ledger)
    o = res.order
    assert res.created and o.state == "created" and o.version == 1
    assert o.signature_status == "unsigned"
    row = raw(owner_engine, "SELECT user_id, wallet, chain_id, token, side, amount_in_wei, "
              "min_out_wei, deadline, route_hash, plan_digest, plan_id, intent_id, notional_wei, "
              "gas_reserve_wei FROM trading_private.orders")[0]
    assert row.user_id == U1 and row.wallet == W1 and row.chain_id == 99901
    assert row.token == TOKEN and row.side == "buy"
    assert int(row.amount_in_wei) == plan.amount_in_wei and int(row.min_out_wei) == plan.min_out_wei
    assert row.deadline == plan.deadline
    assert row.plan_digest == fingerprint(plan) == o.plan_digest
    assert row.route_hash == fingerprint(plan.snapshot.route)
    assert (row.plan_id, row.intent_id) == (plan.id, plan.intent_id)
    assert int(row.notional_wei) == plan.amount_in_wei and int(row.gas_reserve_wei) == plan.max_gas_fee_wei


def test_sell_notional_is_the_hype_leg(ledger):
    res, plan = create(ledger, side="sell", amount_in=10**21, amount_out=HYPE // 4)
    assert res.order.notional_wei == str(HYPE // 4)


def test_ledger_has_no_secret_columns(owner_engine):
    cols = {r[0] for r in raw(owner_engine, "SELECT column_name FROM information_schema.columns "
                              "WHERE table_schema = 'trading_private'")}
    assert not [c for c in cols if any(w in c for w in
                ("secret", "private", "seed", "mnemonic", "raw_tx", "signed_tx", "signature_hex",
                 "api_key", "credential", "password"))]


def test_idempotent_replay_is_one_logical_entry(ledger, owner_engine):
    a, _ = create(ledger)
    b, _ = create(ledger)
    assert (a.created, b.created) == (True, False)
    assert a.order.id == b.order.id
    assert n_orders(owner_engine) == 1
    kinds = [r[0] for r in raw(owner_engine, "SELECT kind FROM trading_private.order_events")]
    assert kinds.count("ORDER_CREATED") == 1


def test_same_key_different_plan_is_a_conflict(ledger):
    create(ledger)
    other = make_plan(nonce="b", now=ledger.clock())
    with refused("idempotency_conflict"):
        ledger.create_order(user_id=U1, wallet=W1, plan=other, idempotency_key=f"{T}a-key")


def test_same_plan_new_key_is_a_duplicate(ledger):
    _, plan = create(ledger)
    with refused("duplicate_plan"):
        ledger.create_order(user_id=U1, wallet=W1, plan=plan, idempotency_key=f"{T}other-key")


def test_concurrent_identical_creates_yield_one_order(ledger, owner_engine):
    plan = make_plan(nonce="c", now=ledger.clock())
    barrier = threading.Barrier(8)

    def go(_):
        barrier.wait()
        return ledger.create_order(user_id=U1, wallet=W1, plan=plan, idempotency_key=f"{T}c-key")
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(go, range(8)))
    assert sum(r.created for r in results) == 1
    assert len({r.order.id for r in results}) == 1
    assert n_orders(owner_engine) == 1
    kinds = [r[0] for r in raw(owner_engine, "SELECT kind FROM trading_private.order_events")]
    assert kinds.count("ORDER_CREATED") == 1
    assert ledger.verify_chain().ok


def test_concurrent_creates_cannot_exceed_the_daily_cap(rw_engine, clock):
    lg = OrderLedger(rw_engine, owner_ids={OWNER}, clock=clock)
    lg.bootstrap()
    lg.set_policy(Actor.owner(OWNER), permissive(daily_notional_cap_wei=2 * HYPE,
                                                 global_daily_notional_cap_wei=2 * HYPE))
    barrier = threading.Barrier(6)

    def go(i):
        barrier.wait()
        try:
            return create(lg, n=f"r{i}", amount_in=HYPE)[0]
        except LedgerRefused as e:
            return e.code
    with ThreadPoolExecutor(6) as ex:
        out = list(ex.map(go, range(6)))
    assert sum(1 for r in out if not isinstance(r, str)) == 2
    assert sorted(r for r in out if isinstance(r, str)) == ["daily_notional_cap"] * 4


@pytest.mark.parametrize("kw,code", [
    ({"amount_in": HYPE + 1}, "per_trade_cap"),
    ({"token": "0x" + "33" * 20}, "token_not_allowed"),
    ({"slippage": 101}, "slippage"),
    ({"impact": 101}, "impact"),
    ({"gas": 10**17}, "daily_gas_cap"),
    ({"expires_in": -1}, "expired"),
    ({"deadline_in": 30}, "expired"),                  # inside min_deadline_margin_s
    ({"amount_in": 0}, "invalid_plan"),
])
def test_admission_refusals_are_explained_and_audited(ledger, owner_engine, kw, code):
    with refused(code):
        create(ledger, **kw)
    ev = raw(owner_engine, "SELECT kind, detail->>'code' AS code FROM trading_private.order_events "
             "WHERE kind = 'ADMISSION_REFUSED'")
    assert [(e.kind, e.code) for e in ev] == [("ADMISSION_REFUSED", code)]
    assert n_orders(owner_engine) == 0


def test_repeated_refusal_does_not_spam_the_audit_log(ledger, owner_engine):
    for _ in range(3):
        with refused("per_trade_cap"):
            create(ledger, amount_in=HYPE + 1)
    assert raw(owner_engine, "SELECT count(*) FROM trading_private.order_events "
               "WHERE kind='ADMISSION_REFUSED'")[0][0] == 1


def test_refusal_audit_cannot_be_used_to_flood_the_log(ledger, owner_engine):
    from agora_tracker.trading.ledger import core
    for i in range(core.MAX_ADMISSION_REFUSALS_PER_DAY + 20):
        with refused("per_trade_cap"):
            create(ledger, f"f{i:04d}", amount_in=HYPE + 1)
    assert raw(owner_engine, "SELECT count(*) FROM trading_private.order_events "
               "WHERE kind='ADMISSION_REFUSED'")[0][0] == core.MAX_ADMISSION_REFUSALS_PER_DAY
    oid = create(ledger, "okay")[0].order.id
    for i in range(core.MAX_TRANSITION_REFUSALS_PER_ORDER + 10):
        with refused("invalid_transition"):
            step(ledger, oid, S.FINALIZED, f"x{i:04d}", actor=Actor.system())
    assert raw(owner_engine, "SELECT count(*) FROM trading_private.order_events "
               "WHERE kind='TRANSITION_REFUSED'")[0][0] == core.MAX_TRANSITION_REFUSALS_PER_ORDER
    assert ledger.verify_chain().ok


def test_identity_binding(ledger):
    plan = make_plan(user=U2, nonce="m", now=ledger.clock())
    with refused("unauthorized_user"):
        ledger.create_order(user_id=U1, wallet=W1, plan=plan, idempotency_key=f"{T}m-key")
    plan = make_plan(wallet=W2, nonce="n", now=ledger.clock())
    with refused("wallet_mismatch"):
        ledger.create_order(user_id=U1, wallet=W1, plan=plan, idempotency_key=f"{T}n-key")
    with refused("session_expired"):
        ledger.create_order(user_id=U1, wallet=W1, plan=make_plan(nonce="o", now=ledger.clock()),
                            idempotency_key=f"{T}o-key", session_expires_at=NOW0 - 1)
    with refused("invalid_plan"):
        ledger.create_order(user_id=U1, wallet=W1, plan={"id": "x"},  # type: ignore[arg-type]
                            idempotency_key=f"{T}p-key")
    with refused("invalid_request"):
        ledger.create_order(user_id=U1, wallet=W1, plan=make_plan(now=NOW0), idempotency_key="short")


def test_daily_user_and_global_budgets_release_on_cancel_and_reset_next_day(rw_engine, clock):
    lg = OrderLedger(rw_engine, owner_ids={OWNER}, clock=clock)
    lg.bootstrap()
    lg.set_policy(Actor.owner(OWNER), permissive(daily_notional_cap_wei=HYPE,
                                                 global_daily_notional_cap_wei=int(1.5 * HYPE)))
    a, _ = create(lg, "a", amount_in=HYPE)
    with refused("daily_notional_cap"):
        create(lg, "b", amount_in=HYPE // 2)
    # a different user is limited by the *global* cap, not by user 1's budget
    create(lg, "c", user=U2, wallet=W2, amount_in=HYPE // 2)
    with refused("global_daily_cap"):
        create(lg, "d", user=U2, wallet=W2, amount_in=1)
    lg.transition(a.order.id, S.CANCELLED, actor=Actor.user(U1), request_key=f"{T}cancel-a1")
    create(lg, "e", amount_in=HYPE // 2)                       # budget released
    clock.t += 86400                                           # next UTC day
    create(lg, "f", amount_in=HYPE // 2)


def test_gas_budget_and_open_order_limits(rw_engine, clock):
    lg = OrderLedger(rw_engine, owner_ids={OWNER}, clock=clock)
    lg.bootstrap()
    lg.set_policy(Actor.owner(OWNER), permissive(
        daily_gas_cap_wei=3 * 10**15, global_daily_gas_cap_wei=10**16,
        max_active_orders_per_user=2, max_conflicting_orders_per_wallet=1))
    create(lg, "a")
    with refused("conflicting_order"):                         # same wallet, one open order max
        create(lg, "b")
    create(lg, "c", wallet=W2)
    with refused("active_orders"):                             # user limit of 2 reached
        create(lg, "d", wallet="0x" + "a3" * 20)
    z = create(lg, "z", user=U2, wallet="0x" + "a4" * 20, gas=2 * 10**15)[0].order.id
    with refused("daily_gas_cap"):                             # 2e15 + (2e15 + 1) > 3e15
        create(lg, "g", user=U2, wallet="0x" + "a5" * 20, gas=2 * 10**15 + 1)
    lg.transition(z, S.CANCELLED, actor=Actor.user(U2), request_key=f"{T}cancel-z1")
    create(lg, "h", user=U2, wallet="0x" + "a5" * 20, gas=2 * 10**15 + 1)   # gas released


# =============================================================== transitions

def test_full_lifecycle_requires_system_evidence(live, owner_engine):
    oid = create(live)[0].order.id
    to_submitted(live, oid)
    included = step(live, oid, S.INCLUDED, "inc", actor=Actor.system())
    fin = step(live, oid, S.FINALIZED, "fin", actor=Actor.system())
    assert included.order.state == "included" and fin.order.state == "finalized"
    assert fin.order.signature_status == "wallet_signed"
    assert fin.order.version == 6
    assert fin.order.attempts[0]["tx_hash"] == "0x" + "ab" * 32 and fin.order.attempts[0]["nonce"] == 7
    ev = live.get_events(U1, oid)
    assert [e["to"] for e in ev if e["kind"] == "TRANSITION"] == \
        ["quote_review", "awaiting_wallet", "submitted", "included", "finalized"]
    assert live.verify_chain().ok


def test_invalid_and_duplicate_transitions_are_rejected(live):
    oid = create(live)[0].order.id
    with refused("invalid_transition"):
        step(live, oid, S.INCLUDED, "bad1", actor=Actor.system())
    with refused("invalid_transition"):
        step(live, oid, S.FINALIZED, "bad2", actor=Actor.system())
    step(live, oid, S.QUOTE_REVIEW, "ok1")
    with refused("invalid_transition"):                        # duplicate under a NEW key
        step(live, oid, S.QUOTE_REVIEW, "ok2")
    with refused("invalid_transition"):
        step(live, oid, "no_such_state", "bad3")


def test_duplicate_callback_with_same_key_applies_once(live, owner_engine):
    oid = create(live)[0].order.id
    r1 = step(live, oid, S.QUOTE_REVIEW, "cb")
    r2 = step(live, oid, S.QUOTE_REVIEW, "cb")
    assert (r1.applied, r2.applied) == (True, False)
    assert r2.order.version == r1.order.version == 2
    with refused("idempotency_conflict"):                      # same key, different target
        step(live, oid, S.CANCELLED, "cb")
    assert raw(owner_engine, "SELECT count(*) FROM trading_private.order_events "
               "WHERE kind='TRANSITION'")[0][0] == 1


def test_concurrent_duplicate_callbacks_apply_once(live):
    oid = create(live)[0].order.id
    barrier = threading.Barrier(8)

    def go(_):
        barrier.wait()
        return step(live, oid, S.QUOTE_REVIEW, "race").applied
    with ThreadPoolExecutor(8) as ex:
        applied = list(ex.map(go, range(8)))
    assert applied.count(True) == 1
    assert live.get_order(U1, oid).version == 2


def test_concurrent_conflicting_transitions_have_one_winner(live):
    oid = create(live)[0].order.id
    step(live, oid, S.QUOTE_REVIEW, "q")
    barrier = threading.Barrier(2)

    def go(i):
        barrier.wait()
        to = [S.CANCELLED, S.REQUIRES_APPROVAL][i]
        try:
            return step(live, oid, to, f"w{i}").order.state
        except LedgerRefused as e:
            return e.code
    with ThreadPoolExecutor(2) as ex:
        out = list(ex.map(go, range(2)))
    final = live.get_order(U1, oid)
    # both interleavings end cancelled: approval-then-cancel applies both edges, cancel-then-
    # approval refuses the second one because the order is terminal
    assert final.state == "cancelled"
    assert (final.version, out.count("invalid_transition")) in ((4, 0), (3, 1))
    assert live.verify_chain().ok


def test_users_cannot_take_system_edges_or_skip_evidence(live):
    oid = create(live)[0].order.id
    to_awaiting(live, oid)
    with refused("actor_not_permitted"):
        step(live, oid, S.SUBMITTED, "u1", actor=Actor.user(U1))
    with refused("missing_evidence"):
        step(live, oid, S.SUBMITTED, "u2", actor=Actor.system())
    live.record_attempt(oid, kind="swap", nonce=3)             # nonce only, no tx hash yet
    step(live, oid, S.SUBMITTED, "u3", actor=Actor.system())
    with refused("missing_evidence"):                          # a result needs a tx hash
        step(live, oid, S.INCLUDED, "u4", actor=Actor.system())


def test_owner_actor_needs_to_be_a_configured_owner(live):
    oid = create(live)[0].order.id
    with refused("actor_not_permitted"):
        step(live, oid, S.CANCELLED, "o1", actor=Actor.owner(U1))
    with refused("actor_not_permitted"):
        step(live, oid, S.CANCELLED, "o2", actor=Actor(kind="system", id=5))
    with refused("actor_not_permitted"):
        step(live, oid, S.CANCELLED, "o3", actor=Actor(kind="root"))


def test_limbo_exits_need_a_reason_and_terminal_orders_freeze(live, owner_engine):
    oid = create(live)[0].order.id
    to_awaiting(live, oid)
    with refused("invalid_request"):
        step(live, oid, S.CANCELLED, "r1", actor=Actor.owner(OWNER))
    done = step(live, oid, S.CANCELLED, "r2", actor=Actor.owner(OWNER), reason="operator cancelled")
    assert done.order.last_reason == "operator cancelled"
    with refused("invalid_transition"):
        step(live, oid, S.QUOTE_REVIEW, "r3")
    # ... and the database itself refuses, whatever the application says
    with pytest.raises(sa.exc.DBAPIError, match="terminal"):
        raw(owner_engine, "UPDATE trading_private.orders SET state='created', version=version+1")


def test_stale_version_is_rejected(live):
    oid = create(live)[0].order.id
    step(live, oid, S.QUOTE_REVIEW, "s1")
    with refused("stale_version"):
        step(live, oid, S.REQUIRES_APPROVAL, "s2", expected_version=1)
    assert step(live, oid, S.REQUIRES_APPROVAL, "s3", expected_version=2).applied


def test_expired_pre_wallet_orders_cannot_advance_but_can_cancel(live, clock):
    oid = create(live)[0].order.id
    clock.t += 181                                             # past the 180 s order ttl
    with refused("expired"):
        step(live, oid, S.QUOTE_REVIEW, "e1")
    assert step(live, oid, S.CANCELLED, "e2").applied


def test_update_sequence_is_monotonic(live, owner_engine):
    a = create(live, "a")[0].order.id
    b = create(live, "b", wallet=W2)[0].order.id
    seq = lambda i: raw(owner_engine, "SELECT update_seq FROM trading_private.orders WHERE id=:i", i=i)[0][0]  # noqa: E731
    s0 = (seq(a), seq(b))
    step(live, a, S.QUOTE_REVIEW, "m1")
    step(live, b, S.QUOTE_REVIEW, "m2")
    step(live, a, S.REQUIRES_APPROVAL, "m3")
    s1 = (seq(a), seq(b))
    assert s0[0] < s0[1] < s1[1] < s1[0]


# =============================================================== execution flag / pause

def test_execution_flag_defaults_off_and_blocks_wallet_requests(ledger):
    oid = create(ledger)[0].order.id
    step(ledger, oid, S.QUOTE_REVIEW, "f1")
    with refused("execution_disabled"):
        step(ledger, oid, S.AWAITING_WALLET, "f2")
    with refused("actor_not_permitted"):
        ledger.set_execution_enabled(Actor.user(U1), True, "me")
    with refused("actor_not_permitted"):                       # the system may only switch it OFF
        ledger.set_execution_enabled(Actor.system(), True, "auto")
    ledger.set_execution_enabled(Actor.owner(OWNER), True, "enable for test")
    assert step(ledger, oid, S.AWAITING_WALLET, "f3").order.state == "awaiting_wallet"
    ledger.set_execution_enabled(Actor.system(), False, "breaker")
    assert ledger.controls().execution_enabled is False


def test_pause_blocks_new_work_but_not_cancel_or_reconciliation(live):
    flying = create(live, "a")[0].order.id
    to_submitted(live, flying, "a")
    pending = create(live, "b", wallet=W2)[0].order.id
    live.pause(Actor.owner(OWNER), "incident drill")
    with refused("paused"):
        create(live, "c", wallet="0x" + "a3" * 20)
    with refused("paused"):
        step(live, pending, S.QUOTE_REVIEW, "p1")
    assert step(live, pending, S.CANCELLED, "p2").applied                      # cancel still works
    assert step(live, flying, S.INCLUDED, "p3", actor=Actor.system()).applied  # reconciliation too
    assert [o["id"] for o in live.pending_reconciliation()] == [flying]
    # a replay of an already-created order succeeds even while paused
    assert create(live, "a")[0].created is False


def test_pause_is_durable_across_a_process_restart(live, owner_engine, clock):
    live.pause(Actor.owner(OWNER), "stay down")
    new_engine = make_ledger_engine(trading_url())       # a fresh process, a fresh pool
    try:
        reborn = OrderLedger(new_engine, owner_ids={OWNER}, clock=clock)
        assert reborn.is_paused() is True
        assert reborn.controls().pause_reason == "stay down"
        with refused("paused"):
            create(reborn)
        with refused("actor_not_permitted"):
            reborn.resume(Actor.user(U1), "let me")
        reborn.resume(Actor.owner(OWNER), "all clear")
        assert reborn.is_paused() is False
    finally:
        new_engine.dispose()


def test_pause_fails_closed_when_the_database_is_unreachable(clock):
    dead = sa.create_engine("postgresql+psycopg://nobody:x@127.0.0.1:1/none",
                            connect_args={"connect_timeout": 1})
    assert OrderLedger(dead, clock=clock).is_paused() is True
    dead.dispose()


def test_pause_resume_are_owner_gated_and_idempotent(ledger, owner_engine):
    with refused("actor_not_permitted"):
        ledger.pause(Actor.user(U1), "nope")
    ledger.pause(Actor.system(), "automatic breaker")           # the system may pause...
    v = ledger.controls().version
    ledger.pause(Actor.owner(OWNER), "again")                   # ...repeat pauses change nothing
    assert ledger.controls().version == v
    with refused("actor_not_permitted"):
        ledger.resume(Actor.system(), "auto resume must not exist")
    with refused("invalid_request"):
        ledger.pause(Actor.owner(OWNER), "  ")
    kinds = [r[0] for r in raw(owner_engine, "SELECT kind FROM trading_private.order_events")]
    assert kinds.count("PAUSED") == 1


def test_pause_races_with_wallet_requests_without_leaking_one_through(live):
    ids = [create(live, f"r{i}", wallet="0x" + f"{0xb0 + i:02x}" * 20)[0].order.id for i in range(4)]
    for i, oid in enumerate(ids):
        step(live, oid, S.QUOTE_REVIEW, f"rq{i}")
    barrier = threading.Barrier(5)
    results = {}

    def request(i):
        barrier.wait()
        try:
            results[i] = step(live, ids[i], S.AWAITING_WALLET, f"rw{i}").order.state
        except LedgerRefused as e:
            results[i] = e.code

    def pause():
        barrier.wait()
        live.pause(Actor.owner(OWNER), "race")
    threads = [threading.Thread(target=request, args=(i,)) for i in range(4)] + \
              [threading.Thread(target=pause)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert live.controls().paused
    # each request either committed strictly before the pause or was refused by it
    assert set(results.values()) <= {"awaiting_wallet", "paused"}
    with refused("paused"):
        create(live, "late", wallet="0x" + "c1" * 20)
    for i, outcome in results.items():
        if outcome == "paused":
            with refused("paused"):
                step(live, ids[i], S.AWAITING_WALLET, f"again{i}")


def test_tightened_policy_stops_open_orders_at_the_wallet_prompt(live):
    oid = create(live)[0].order.id
    step(live, oid, S.QUOTE_REVIEW, "t1")
    live.set_policy(Actor.owner(OWNER), permissive(allowed_tokens=(TOKEN2,)))
    with refused("token_not_allowed"):
        step(live, oid, S.AWAITING_WALLET, "t2")
    live.set_policy(Actor.owner(OWNER), permissive(per_trade_cap_wei=HYPE // 4))
    with refused("per_trade_cap"):
        step(live, oid, S.AWAITING_WALLET, "t3")
    assert live.get_order(U1, oid).policy_version < live.controls().policy_version  # orders keep theirs


def test_policy_changes_are_owner_only_and_versioned(ledger, owner_engine):
    with refused("actor_not_permitted"):
        ledger.set_policy(Actor.user(U1), permissive())
    v1 = ledger.controls().policy_version
    v2 = ledger.set_policy(Actor.owner(OWNER), permissive(max_slippage_bps=10))
    assert v2 > v1 and ledger.active_policy().max_slippage_bps == 10
    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        raw(owner_engine, "UPDATE trading_private.policy_versions SET digest = repeat('0', 64)")


# =============================================================== recovery

def test_pending_and_ambiguous_orders_survive_a_restart(live, rw_engine, clock):
    a = create(live, "a")[0].order.id
    to_awaiting(live, a, "a")
    b = create(live, "b", wallet=W2)[0].order.id
    to_submitted(live, b, "b")
    step(live, b, S.AMBIGUOUS, "b4", actor=Actor.system())
    live.pause(Actor.owner(OWNER), "pre-restart")
    eng = make_ledger_engine(trading_url())
    try:
        reborn = OrderLedger(eng, owner_ids={OWNER}, clock=clock)
        report = reborn.recover()
        assert reborn.get_order(U1, a).state == "awaiting_wallet"
        assert reborn.get_order(U1, b).state == "ambiguous"
        assert report.paused and report.chain_ok
        assert report.pending_reconciliation == {"awaiting_wallet": 1, "ambiguous": 1}
        assert {o["id"] for o in reborn.pending_reconciliation()} == {a, b}
        assert reborn.get_order(U1, b).attempts[0]["tx_hash"] == "0x" + "ab" * 32
    finally:
        eng.dispose()


def test_recovery_applies_time_based_expiry_only(live, clock):
    pre = create(live, "p")[0].order.id
    gone = create(live, "g", wallet=W2)[0].order.id
    to_awaiting(live, gone, "g")
    sub = create(live, "s", wallet="0x" + "a3" * 20)[0].order.id
    to_submitted(live, sub, "s", tx="0x" + "cd" * 32)
    clock.t += 700                                             # past ttl and past the deadline
    report = live.recover()
    assert (report.expired_cancelled, report.wallet_deadline_ambiguous) == (1, 1)
    assert live.get_order(U1, pre).state == "cancelled"
    # a wallet might have signed before we died: ambiguous, never a silent cancel
    assert live.get_order(U1, gone).state == "ambiguous"
    assert live.get_order(U1, sub).state == "submitted"          # in-flight tx: untouched
    again = live.recover()                                       # idempotent
    assert (again.expired_cancelled, again.wallet_deadline_ambiguous) == (0, 0)
    kinds = [e["kind"] for e in live.get_events(U1, gone)]
    assert "TRANSITION" in kinds
    assert live.verify_chain().ok


# =============================================================== attempts

def test_record_attempt_is_idempotent_and_validated(live):
    a = create(live, "a")[0].order.id
    b = create(live, "b", wallet=W2)[0].order.id
    with refused("invalid_transition"):                          # nothing to record before the wallet
        live.record_attempt(a, kind="swap", nonce=1)
    to_awaiting(live, a, "a")
    to_awaiting(live, b, "b")
    tx = "0x" + "ee" * 32
    first = live.record_attempt(a, kind="swap", nonce=9, tx_hash=tx)
    again = live.record_attempt(a, kind="swap", nonce=9, tx_hash=tx)
    assert first == {"attempt_no": 1, "recorded": True}
    assert again == {"attempt_no": 1, "recorded": False}
    with refused("idempotency_conflict"):                        # a hash belongs to one order
        live.record_attempt(b, kind="swap", tx_hash=tx)
    for kw in ({"kind": "swap"}, {"kind": "x", "nonce": 1}, {"kind": "swap", "tx_hash": "0xabc"},
               {"kind": "swap", "nonce": -1}, {"kind": "swap", "nonce": True}):
        with refused("invalid_request"):
            live.record_attempt(a, **kw)
    with refused("actor_not_permitted"):
        live.record_attempt(a, kind="swap", nonce=2, actor=Actor.user(U1))


# =============================================================== privacy / authorization

def test_other_users_see_nothing(live, owner_engine):
    mine = create(live, "a")[0].order.id
    step(live, mine, S.QUOTE_REVIEW, "x1")
    with refused("not_found"):
        live.get_order(U2, mine)
    with refused("not_found"):
        live.get_events(U2, mine)
    assert live.list_orders(U2) == []
    assert [o.id for o in live.list_orders(U1)] == [mine]
    with refused("not_found"):                                   # cannot move someone else's order
        live.transition(mine, S.CANCELLED, actor=Actor.user(U2), request_key=f"{T}steal-1")
    with refused("not_found"):
        live.get_order(U1, "not-a-uuid")
    assert live.get_order(U1, mine).state == "quote_review"


def test_owner_privilege_cannot_be_claimed_by_a_user_id(live):
    mine = create(live, "a")[0].order.id
    with refused("actor_not_permitted"):
        live.transition(mine, S.CANCELLED, actor=Actor.owner(U2), request_key=f"{T}fake-own1")


def test_row_level_security_scopes_raw_sql(live, rw_engine):
    a = create(live, "a")[0].order.id
    create(live, "b", user=U2, wallet=W2)
    create(live, "c", user=U2, wallet="0x" + "a3" * 20)

    def seen(scope):
        with rw_engine.begin() as c:
            if scope:
                c.execute(sa.text("SELECT set_config('trading.user_id', :u, true)"), {"u": scope})
            return (c.execute(sa.text("SELECT count(*) FROM orders")).scalar(),
                    c.execute(sa.text("SELECT count(*) FROM order_events")).scalar())
    assert seen(None) == (0, 0)                                  # no scope: nothing
    assert seen("1001")[0] == 1 and seen("1002")[0] == 2
    assert seen("999999") == (0, 0)
    with rw_engine.begin() as c:                                 # cannot write as someone else
        c.execute(sa.text("SELECT set_config('trading.user_id', '1002', true)"))
        with pytest.raises(sa.exc.DBAPIError, match="row-level security"):
            c.execute(sa.text("INSERT INTO order_events (user_id, actor_kind, kind, prev_hash, hash) "
                              "VALUES (1001, 'user', 'PAUSED', '', '')"))
    assert live.get_order(U1, a).state == "created"


def test_budget_function_returns_numbers_only(live, rw_engine):
    create(live, "a", amount_in=HYPE // 2)
    create(live, "b", user=U2, wallet=W2, amount_in=HYPE // 4)
    with rw_engine.begin() as c:                                 # no scope at all
        row = c.execute(sa.text("SELECT * FROM admission_snapshot(1001, :w, 99901, :a, :b)"),
                        {"w": W1, "a": "2026-01-01", "b": "2030-01-01"}).one()
    assert int(row.user_notional) == HYPE // 2
    assert int(row.global_notional) == HYPE // 2 + HYPE // 4
    assert row._fields == ("user_notional", "user_gas", "global_notional", "global_gas",
                           "user_active", "wallet_active")


# =============================================================== database privileges

def as_role(owner_engine, user, password):
    return make_engine(sa.engine.make_url(TEST_URL).set(username=user, password=password)
                       .render_as_string(hide_password=False), pool_size=1)


def test_public_dashboard_roles_cannot_touch_the_ledger(live, owner_engine, role_ready):
    create(live, "a")
    dash = as_role(owner_engine, "dashboard_ro", ROLE_PASSWORD)
    try:
        for sql in ("SELECT * FROM trading_private.orders", "SELECT * FROM trading_private.order_events",
                    "SELECT * FROM trading_private.controls",
                    "SELECT * FROM trading_private.admission_snapshot(1, 'x', 99901, now(), now())"):
            with pytest.raises(sa.exc.DBAPIError, match="permission denied"):
                with dash.begin() as c:
                    c.execute(sa.text(sql))
        with dash.begin() as c:                                  # nor is it visible as a name
            assert c.execute(sa.text("SELECT count(*) FROM information_schema.tables "
                                     "WHERE table_schema = 'trading_private'")).scalar() == 0
            assert c.execute(sa.text("SELECT has_schema_privilege('public_api', 'USAGE')")).scalar()
            assert not c.execute(sa.text(
                "SELECT has_schema_privilege('trading_private', 'USAGE')")).scalar()
        # the public view layer exposes no ledger object
        assert raw(owner_engine, "SELECT count(*) FROM information_schema.view_table_usage "
                   "WHERE view_schema = 'public_api' AND table_schema = 'trading_private'")[0][0] == 0
    finally:
        dash.dispose()


def test_trading_role_is_least_privilege(live, rw_engine):
    role = raw(live.engine, "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, "
               "rolreplication, rolconnlimit, rolconfig FROM pg_roles WHERE rolname='trading_rw'")[0]
    assert (role.rolsuper, role.rolcreatedb, role.rolcreaterole, role.rolbypassrls,
            role.rolreplication) == (False, False, False, False, False)
    assert role.rolconnlimit == 5
    cfg = " ".join(role.rolconfig)
    assert "statement_timeout=5s" in cfg and "lock_timeout=2s" in cfg and "search_path" in cfg
    forbidden = [
        "SELECT * FROM public.trades", "SELECT * FROM public.wallets",
        "SELECT * FROM public.notification_outbox", "SELECT * FROM public_api.trades",
        "CREATE TABLE public.evil (x int)", "CREATE TABLE trading_private.evil (x int)",
        "DELETE FROM trading_private.orders", "TRUNCATE trading_private.orders",
        "UPDATE trading_private.orders SET amount_in_wei = 1",
        "UPDATE trading_private.orders SET user_id = 1002",
        "UPDATE trading_private.order_events SET detail = '{}'::jsonb",
        "DELETE FROM trading_private.order_events", "TRUNCATE trading_private.order_events",
        "DELETE FROM trading_private.execution_attempts",
        "UPDATE trading_private.policy_versions SET digest = repeat('1', 64)",
        "DELETE FROM trading_private.controls", "TRUNCATE trading_private.controls",
        "INSERT INTO trading_private.order_state_edges VALUES ('created', 'finalized')",
        "UPDATE trading_private.controls SET id = 2", "DROP SCHEMA trading_private CASCADE",
        "ALTER TABLE trading_private.orders DISABLE TRIGGER USER",
        "ALTER TABLE trading_private.orders DISABLE ROW LEVEL SECURITY",
    ]
    for sql in forbidden:
        with pytest.raises(sa.exc.DBAPIError), rw_engine.begin() as c:
            c.execute(sa.text(sql))


def test_audit_log_cannot_be_forged_through_the_chain_trigger(live, rw_engine):
    create(live, "a")
    with rw_engine.begin() as c:
        c.execute(sa.text("SELECT set_config('trading.system', 'on', true)"))
        n = c.execute(sa.text(
            "INSERT INTO order_events (chain_no, at, actor_kind, kind, prev_hash, hash) "
            "VALUES (1, '2001-01-01', 'system', 'RECOVERY', 'f', 'f') RETURNING chain_no")).scalar()
    assert n > 1                                                # supplied numbers are overwritten
    assert live.verify_chain().ok


# =============================================================== database guards (owner too)

@pytest.mark.parametrize("sql,msg", [
    ("UPDATE trading_private.orders SET state = 'finalized', version = version + 1", "invalid order transition"),
    ("UPDATE trading_private.orders SET state = 'quote_review'", "version must increase"),
    ("UPDATE trading_private.orders SET state = 'quote_review', version = version + 2", "version must increase"),
    ("UPDATE trading_private.orders SET amount_in_wei = 1, version = version + 1", "immutable"),
    ("UPDATE trading_private.orders SET token = '0x' || repeat('9', 40), version = version + 1", "immutable"),
    ("UPDATE trading_private.orders SET plan_digest = repeat('0', 64), version = version + 1", "immutable"),
    ("DELETE FROM trading_private.orders", "never deleted"),
    ("TRUNCATE trading_private.orders CASCADE", "append-only"),
    ("UPDATE trading_private.order_events SET detail = '{}'::jsonb", "append-only"),
    ("DELETE FROM trading_private.order_events", "append-only"),
    ("TRUNCATE trading_private.order_events", "append-only"),
    ("UPDATE trading_private.controls SET paused = true", "exactly one version"),
    ("DELETE FROM trading_private.controls", "never deleted"),
])
def test_database_refuses_corruption_even_from_the_owner(live, owner_engine, sql, msg):
    create(live, "a")
    with pytest.raises(sa.exc.DBAPIError, match=msg):
        raw(owner_engine, sql)
    assert n_orders(owner_engine) == 1


def test_signature_status_cannot_be_unset(live, owner_engine):
    oid = create(live)[0].order.id
    to_submitted(live, oid)
    with pytest.raises(sa.exc.DBAPIError, match="signature status cannot be reverted"):
        raw(owner_engine, "UPDATE trading_private.orders SET signature_status='unsigned', "
            "version = version + 1")


def test_unique_constraints_backstop_idempotency(live, owner_engine):
    create(live, "a")
    with pytest.raises(sa.exc.IntegrityError):
        raw(owner_engine, """INSERT INTO trading_private.orders SELECT gen_random_uuid(), user_id,
            wallet, chain_id, idempotency_key, request_digest, intent_id, plan_id,
            repeat('9', 64), route_hash, token, side, amount_in_wei, min_out_wei, notional_wei,
            gas_reserve_wei, slippage_bps, price_impact_bps, deadline, signature_status, state,
            version, nextval('trading_private.order_update_seq'), policy_version, created_at,
            updated_at, expires_at, terminal_at, last_reason FROM trading_private.orders""")


# =============================================================== audit chain

def test_audit_chain_detects_tampering(live, owner_engine):
    oid = create(live)[0].order.id
    step(live, oid, S.QUOTE_REVIEW, "t1")
    step(live, oid, S.REQUIRES_APPROVAL, "t2")
    assert live.verify_chain().ok and live.verify_chain().checked >= 4
    try:
        with owner_engine.begin() as c:                          # an attacker with owner rights
            c.execute(sa.text("ALTER TABLE trading_private.order_events DISABLE TRIGGER USER"))
            c.execute(sa.text("UPDATE trading_private.order_events SET detail = "
                              "'{\"edited\": true}'::jsonb WHERE chain_no = 2"))
            c.execute(sa.text("ALTER TABLE trading_private.order_events ENABLE TRIGGER USER"))
        bad = live.verify_chain()
        assert (bad.ok, bad.first_bad, bad.reason) == (False, 2, "hash mismatch")
    finally:
        reset_ledger(owner_engine)


def test_audit_chain_detects_deleted_events(live, owner_engine):
    oid = create(live)[0].order.id
    step(live, oid, S.QUOTE_REVIEW, "d1")
    step(live, oid, S.REQUIRES_APPROVAL, "d2")
    try:
        with owner_engine.begin() as c:
            c.execute(sa.text("ALTER TABLE trading_private.order_events DISABLE TRIGGER USER"))
            c.execute(sa.text("DELETE FROM trading_private.order_events WHERE chain_no = 3"))
            c.execute(sa.text("ALTER TABLE trading_private.order_events ENABLE TRIGGER USER"))
        bad = live.verify_chain()
        assert not bad.ok and bad.reason == "gap in chain numbers"
    finally:
        reset_ledger(owner_engine)


def test_concurrent_writers_keep_one_linear_chain(live):
    def go(i):
        oid = create(live, f"w{i}", user=2000 + i, wallet="0x" + f"{0xd0 + i:02x}" * 20)[0].order.id
        step(live, oid, S.QUOTE_REVIEW, f"wq{i}", actor=Actor.user(2000 + i))
        return oid
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(go, range(6)))
    chk = live.verify_chain()
    assert chk.ok and chk.checked >= 12


# =============================================================== migration / backup

def test_migration_is_additive_and_round_trips_without_touching_existing_data(pg_engine):
    reset_ledger(pg_engine)
    raw(pg_engine, "INSERT INTO service_state (key, value) VALUES ('ledger-test', '{\"k\": 1}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING")
    before = raw(pg_engine, "SELECT key, value FROM service_state WHERE key='ledger-test'")
    downgrade(pg_engine, "0010")                                # roll back ONLY the ledger
    gone = raw(pg_engine, "SELECT count(*) FROM information_schema.schemata "
               "WHERE schema_name = 'trading_private'")[0][0]
    role = raw(pg_engine, "SELECT count(*) FROM pg_roles WHERE rolname='trading_rw'")[0][0]
    assert (gone, role) == (0, 0)
    assert raw(pg_engine, "SELECT key, value FROM service_state WHERE key='ledger-test'") == before
    assert raw(pg_engine, "SELECT count(*) FROM information_schema.tables "
               "WHERE table_schema='public_api'")[0][0] > 0     # public views untouched
    upgrade(pg_engine)
    assert raw(pg_engine, "SELECT count(*) FROM trading_private.order_state_edges")[0][0] == len(EDGES)
    assert raw(pg_engine, "SELECT key, value FROM service_state WHERE key='ledger-test'") == before
    raw(pg_engine, "DELETE FROM service_state WHERE key='ledger-test'")


def test_ledger_survives_dump_and_restore_with_its_chain_intact(live, owner_engine, tmp_path):
    import os
    import shutil
    import subprocess
    if not (shutil.which("pg_dump") and shutil.which("pg_restore")):
        pytest.skip("pg_dump/pg_restore not installed")
    out = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True).stdout
    server = raw(owner_engine, "SHOW server_version_num")[0][0]
    if int(out.split()[-1].split(".")[0]) < int(server) // 10000:
        pytest.skip("pg_dump older than the server")
    oid = create(live)[0].order.id
    to_submitted(live, oid)
    live.pause(Actor.owner(OWNER), "before backup")
    libpq = sa.engine.make_url(TEST_URL).set(drivername="postgresql").render_as_string(
        hide_password=False)
    dump = tmp_path / "ledger.dump"
    subprocess.run(["pg_dump", "-Fc", "-n", "trading_private", "-f", str(dump), libpq],
                   check=True, env={**os.environ})
    reset_ledger(owner_engine)                                  # simulate losing the ledger
    assert n_orders(owner_engine) == 0
    with owner_engine.begin() as c:                             # restore needs an empty schema
        c.execute(sa.text("DROP SCHEMA trading_private CASCADE"))
    subprocess.run(["pg_restore", "--no-owner", "--role=" + sa.engine.make_url(TEST_URL).username,
                    "-d", libpq, str(dump)], check=True, env={**os.environ})
    set_trading_password(owner_engine, TRADING_PASSWORD)
    eng = make_ledger_engine(trading_url())
    try:
        restored = OrderLedger(eng, owner_ids={OWNER}, clock=live.clock)
        assert restored.is_paused() is True
        assert restored.get_order(U1, oid).state == "submitted"
        assert restored.verify_chain().ok
        assert restored.get_order(U1, oid).attempts[0]["nonce"] == 7
        # privileges and guards came back with the schema
        with pytest.raises(sa.exc.DBAPIError), eng.begin() as c:
            c.execute(sa.text("DELETE FROM orders"))
    finally:
        eng.dispose()


def test_tracker_outbox_and_scanner_state_are_independent_of_the_ledger(live, pg_engine):
    """Pausing, recovery and heavy ledger traffic never write to the original tracker tables."""
    tables = ["notification_outbox", "indexer_checkpoints", "trades", "wallet_positions", "signals"]
    snap = lambda: [raw(pg_engine, f"SELECT count(*) FROM public.{t}")[0][0] for t in tables]  # noqa: E731
    before = snap()
    create(live, "a")
    live.pause(Actor.owner(OWNER), "x")
    live.recover()
    assert snap() == before
