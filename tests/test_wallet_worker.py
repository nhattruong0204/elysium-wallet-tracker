# ruff: noqa: F811
"""Supervised lifecycle of the reconciliation workers (#95 finding F-01).

Unit tests use fakes (no database); integration tests use the real PostgreSQL ledger, the real
LegReconciler/ReceiptReconciler and a scripted chain. Nothing here signs, sends or funds anything."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import TEST_URL  # noqa: F401
from test_trading_ledger import (  # noqa: F401
    OWNER,
    U1,
    Actor,
    clock,
    ledger,
    live,
    owner_engine,
    raw,
    rw_engine,
)
from test_wallet_receipts import mined_buy, rr  # noqa: F401
from test_wallet_reconcile import FakeNode, errored, node  # noqa: F401
from walletkit import ReceiptChain, providers

import agora_tracker
from agora_tracker.config import TradingConfig
from agora_tracker.trading.paper import TradeRefused
from agora_tracker.trading.wallet import supervisor as sv
from agora_tracker.trading.wallet.rpc import RpcBusy, RpcUnavailable
from agora_tracker.trading.wallet.supervisor import (
    BudgetedChain,
    ChainGroup,
    PgAdvisoryLease,
    WorkerSetupError,
    WorkerSupervisor,
    build_worker,
    render_text,
)

needs_db = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")


def wait_for(cond, timeout=8.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


# ------------------------------------------------------------------------------- fakes

class FakeLease:
    def __init__(self, free=True):
        self.free, self.have, self.released, self.acquires, self.boom = free, False, 0, 0, None

    def acquire(self):
        self.acquires += 1
        self.have = self.free
        return self.have

    def held(self):
        if self.boom:
            raise self.boom
        return self.have

    def release(self):
        self.have = False
        self.released += 1


class FakeInner:
    """Read-only chain double with a switchable outage."""

    def __init__(self):
        self.down = False
        self.n = 0

    def _go(self):
        self.n += 1
        if self.down:
            raise RpcUnavailable("down")
        return None

    def head(self): return self._go()
    def transaction(self, h): return self._go()
    def receipt(self, h): return self._go()
    def tx_count(self, a, t): return self._go()
    def block_transactions(self, n): return self._go()
    def block_header(self, n): return self._go()
    def native_balance(self, a, b): return self._go()
    def token_balance(self, t, a, b): return self._go()


class FakeLedger:
    def __init__(self):
        self.stats = {"evidence_queue": 0, "receipt_queue": 0, "ambiguous_legs": 0,
                      "orphaned_legs": 0, "oldest_wait_s": None}
        self.boom = None

    def worker_stats(self):
        if self.boom:
            raise self.boom
        return dict(self.stats)


class FakeRecon:
    """Stands in for LegReconciler / ReceiptReconciler."""

    def __init__(self, chain=None, calls=0, boom=None):
        self.chain, self.calls, self.boom, self.runs, self.counts = chain, calls, boom, 0, {"x": 1}
        self.saw_continue = []

    def run_once(self, limit=100, should_continue=None):
        self.runs += 1
        self.saw_continue.append(should_continue() if should_continue else None)
        if self.boom:
            raise self.boom
        for _ in range(self.calls):
            try:
                self.chain.head()
            except TradeRefused:
                pass
        return SimpleNamespace(counts=dict(self.counts))


def mk(*, lease=None, ledger=None, inner=None, legs=None, rcpts=None, calls_per_min=1000, **kw):
    inner = inner or FakeInner()
    chain = BudgetedChain(inner, calls_per_min=calls_per_min)
    args = dict(interval_s=0.01, idle_interval_s=0.02, standby_retry_s=0.02, pass_budget_s=5.0,
                max_backoff_s=0.05, lower_priority=False, finality_confirmations=12)
    args.update(kw)
    return WorkerSupervisor(
        ledger or FakeLedger(), chain, lease=lease or FakeLease(),
        leg_reconciler=legs or FakeRecon(chain), receipt_reconciler=rcpts or FakeRecon(chain),
        **args), chain, inner


# ------------------------------------------------------------------ fail-closed startup

ENV = {"TRADING_EXEC_RPC_URL": "https://exec.example/KEY"}


def cfg(**kw):
    return SimpleNamespace(trading=TradingConfig(**kw), chain=SimpleNamespace(rpc_urls=["https://scanner.example"]))


def test_disabled_by_default_builds_nothing_even_with_no_configuration_at_all():
    assert build_worker(cfg(), None, environ={}) is None
    assert build_worker(cfg(ledger_enabled=True), object(), environ={}) is None
    assert TradingConfig().wallet_execution_enabled is False


def test_enabled_without_a_ledger_is_fatal():
    with pytest.raises(WorkerSetupError, match="ledger"):
        build_worker(cfg(ledger_enabled=True, wallet_execution_enabled=True), None, environ=ENV)


def test_enabled_without_an_execution_rpc_is_fatal_and_leaks_nothing():
    with pytest.raises(WorkerSetupError, match="TRADING_EXEC_RPC_URL"):
        build_worker(cfg(ledger_enabled=True, wallet_execution_enabled=True), object(), environ={})
    with pytest.raises(WorkerSetupError) as e:
        build_worker(cfg(ledger_enabled=True, wallet_execution_enabled=True), object(),
                     environ={"TRADING_EXEC_RPC_URL": "https://scanner.example"})
    assert "scanner.example" not in str(e.value) or "must not" in str(e.value)
    with pytest.raises(WorkerSetupError) as e:
        build_worker(cfg(ledger_enabled=True, wallet_execution_enabled=True), object(),
                     environ={"TRADING_EXEC_RPC_URL": "ftp://h/SECRETKEY"})
    assert "SECRETKEY" not in str(e.value)


def test_enabled_and_complete_builds_a_supervisor_but_does_not_start_it():
    engine = SimpleNamespace(url="postgresql+psycopg://trading_rw:pw@127.0.0.1/db")
    w = build_worker(cfg(ledger_enabled=True, wallet_execution_enabled=True), SimpleNamespace(engine=engine),
                     environ=ENV)
    assert isinstance(w, WorkerSupervisor) and w.snapshot()["state"] == "stopped"
    assert w.snapshot()["thread_alive"] is False


@pytest.mark.parametrize("field,bad", [
    ("worker_interval_s", 0), ("worker_idle_interval_s", 99999), ("worker_batch", 0),
    ("worker_batch", True), ("worker_rpc_calls_per_min", 5), ("worker_pass_budget_s", "x"),
    ("worker_max_backoff_s", None), ("worker_standby_retry_s", -1)])
def test_worker_bounds_are_validated(field, bad):
    with pytest.raises(ValueError):
        TradingConfig(**{field: bad})


# ------------------------------------------------------------------ the read-only chain handle

def test_the_workers_chain_handle_cannot_send_sign_or_call_arbitrary_methods():
    chain = BudgetedChain(FakeInner(), calls_per_min=10)
    methods = {n for n in dir(chain) if not n.startswith("_") and callable(getattr(chain, n))}
    reads = {"head", "transaction", "receipt", "tx_count", "block_transactions", "block_header",
             "native_balance", "token_balance", "finalized_head"}
    assert methods == reads | {"take_cycle", "in_window"}      # exact allowlist: nothing else
    assert not hasattr(chain, "rpc") and not hasattr(chain, "call")


def test_rpc_budget_is_per_minute_shared_and_slides():
    t = [0.0]
    chain = BudgetedChain(FakeInner(), calls_per_min=3, clock=lambda: t[0])
    for _ in range(3):
        chain.head()
    with pytest.raises(RpcBusy):
        chain.receipt("0x" + "11" * 32)
    assert chain.refused == 1 and chain.in_window() == 3
    t[0] = 61.0
    chain.head()
    assert chain.in_window() == 1 and chain.calls == 4


def test_rpc_failures_are_counted_and_reraised_for_the_reconcilers_to_fail_closed():
    inner = FakeInner()
    chain = BudgetedChain(inner, calls_per_min=100)
    chain.head()
    inner.down = True
    with pytest.raises(RpcUnavailable):
        chain.head()
    assert (chain.calls, chain.failures) == (2, 1) and chain.take_cycle() == (1, 1)
    assert chain.take_cycle() == (0, 0)


# ------------------------------------------------------------------ lifecycle (fakes)

def test_start_run_and_graceful_stop_release_the_lease():
    lease = FakeLease()
    w, _, _ = mk(lease=lease)
    assert w.snapshot()["state"] == "stopped"
    w.start()
    assert wait_for(lambda: w.snapshot()["cycles"] >= 2 and w.snapshot()["running"])
    s = w.snapshot()
    assert s["state"] == "running" and s["thread_alive"] and s["last_success_at"] is not None
    assert w.healthy()
    assert w.stop() is True
    assert not w.snapshot()["thread_alive"] and w.snapshot()["state"] == "stopped"
    assert lease.released >= 1 and not lease.have
    assert w.stop() is True                              # idempotent


def test_shutdown_is_prompt_even_during_a_long_idle_sleep():
    w, _, _ = mk(interval_s=300, idle_interval_s=300, standby_retry_s=300)
    w.start()
    assert wait_for(lambda: w.snapshot()["cycles"] >= 1)
    t0 = time.monotonic()
    assert w.stop(timeout=5)
    assert time.monotonic() - t0 < 3


def test_process_wide_stop_event_also_stops_it():
    ext = threading.Event()
    w, _, _ = mk(external_stop=ext)
    w.start()
    assert wait_for(lambda: w.snapshot()["running"])
    ext.set()
    assert wait_for(lambda: not w.snapshot()["thread_alive"])
    assert w.snapshot()["state"] == "stopped"


def test_a_second_start_of_the_same_supervisor_is_refused():
    w, _, _ = mk()
    w.start()
    try:
        with pytest.raises(WorkerSetupError, match="already running"):
            w.start()
    finally:
        w.stop()


def test_restart_after_stop_resumes():
    w, _, _ = mk()
    w.start()
    wait_for(lambda: w.snapshot()["cycles"] >= 1)
    w.stop()
    n = w.snapshot()["cycles"]
    w.start()
    assert wait_for(lambda: w.snapshot()["cycles"] > n)
    w.stop()


def test_standby_does_not_work_until_it_gets_the_lease():
    lease = FakeLease(free=False)
    legs, rcpts = FakeRecon(), FakeRecon()
    w, _, _ = mk(lease=lease, legs=legs, rcpts=rcpts)
    w.start()
    try:
        assert wait_for(lambda: lease.acquires >= 3)
        assert w.snapshot()["state"] == "standby" and legs.runs == 0 and rcpts.runs == 0
        lease.free = True
        assert wait_for(lambda: legs.runs >= 1 and w.snapshot()["state"] == "running")
    finally:
        w.stop()


def test_losing_the_lease_stops_work_immediately_and_is_counted():
    lease = FakeLease()
    legs = FakeRecon()
    w, _, _ = mk(lease=lease, legs=legs)
    w.start()
    try:
        assert wait_for(lambda: legs.runs >= 2)
        lease.free, lease.have = False, False            # session died; someone else may hold it
        assert wait_for(lambda: w.snapshot()["state"] == "standby")
        runs = legs.runs
        time.sleep(0.15)
        assert legs.runs == runs and w.snapshot()["lease_losses"] == 1
    finally:
        w.stop()


# ------------------------------------------------------------------ crash and failure handling

def test_a_failing_reconciler_does_not_stop_the_other_and_backs_off():
    legs, rcpts = FakeRecon(boom=RuntimeError("secret detail https://x/KEY")), FakeRecon()
    w, _, _ = mk(legs=legs, rcpts=rcpts)
    assert w.run_cycle() is False
    assert rcpts.runs == 1                                # the receipt pass still ran
    s = w.snapshot()
    assert s["consecutive_failures"] == 1 and s["errors_total"] == 1
    assert s["last_error"] == "RuntimeError"              # class name only, never the message
    assert "KEY" not in str(s)
    for _ in range(4):
        w.run_cycle()
    d = [w._next_delay(False) for _ in range(50)]
    assert all(0.8 * w.max_backoff_s <= x <= 1.2 * w.max_backoff_s for x in d)   # capped
    legs.boom = None
    assert w.run_cycle() is True and w.snapshot()["consecutive_failures"] == 0


def test_backoff_grows_then_is_capped_and_idle_uses_the_idle_interval():
    w, _, _ = mk(interval_s=1.0, idle_interval_s=7.0, max_backoff_s=10.0)
    w._h["consecutive_failures"] = 1
    assert 1.6 <= w._next_delay(False) <= 2.4
    w._h["consecutive_failures"] = 3
    assert 6.4 <= w._next_delay(False) <= 9.6
    w._h["consecutive_failures"] = 30
    assert 8.0 <= w._next_delay(False) <= 12.0
    w._h["queues"] = {"evidence_queue": 0, "receipt_queue": 0}
    assert w._next_delay(True) == 7.0
    w._h["queues"] = {"evidence_queue": 2, "receipt_queue": 0}
    assert w._next_delay(True) == 1.0


def test_rpc_outage_is_a_failed_cycle_with_no_state_change_and_recovers():
    inner = FakeInner()
    chain_holder = {}
    w, chain, inner = mk(inner=inner)
    w.legs = FakeRecon(chain, calls=3)
    w.receipts = FakeRecon(chain, calls=2)
    chain_holder["c"] = chain
    assert w.run_cycle() is True
    inner.down = True
    assert w.run_cycle() is False
    s = w.snapshot()
    assert s["last_error"] == "RpcUnavailable" and s["rpc"]["failures"] == 5
    inner.down = False
    assert w.run_cycle() is True and w.snapshot()["consecutive_failures"] == 0


def test_database_trouble_in_the_stats_query_is_a_failed_cycle_not_a_crash():
    led = FakeLedger()
    led.boom = OSError("db down")
    w, _, _ = mk(ledger=led)
    assert w.run_cycle() is False and w.snapshot()["last_error"] == "OSError"
    led.boom = None
    assert w.run_cycle() is True


def test_budget_exhaustion_is_reported_and_fails_the_cycle_only_if_nothing_succeeded():
    w, chain, _ = mk(calls_per_min=2)
    w.legs, w.receipts = FakeRecon(chain, calls=5), FakeRecon(chain, calls=0)
    assert w.run_cycle() is True                          # two calls succeeded, the rest refused
    assert chain.refused == 3 and w.snapshot()["rpc"]["budget_refusals"] == 3


def test_a_cycle_is_bounded_by_wall_time_and_by_shutdown():
    t = [0.0]
    w, _, _ = mk(pass_budget_s=5.0, mono=lambda: t[0])
    t[0] = 100.0                                          # budget already spent when polled
    w.legs.saw_continue.clear()
    w.run_cycle()
    # deadline = t0 + 5 where t0 is read at cycle start (100): continue() true at first poll...
    assert w.legs.saw_continue[0] is True
    w2, _, _ = mk(pass_budget_s=5.0)
    w2._own.set()
    w2.run_cycle()
    assert w2.legs.saw_continue == [False]


def test_overrun_is_counted():
    t = [0.0]

    class Slow(FakeRecon):
        def run_once(self, limit=100, should_continue=None):
            t[0] += 50.0
            assert should_continue() is False
            return SimpleNamespace(counts={})
    w, _, _ = mk(pass_budget_s=5.0, mono=lambda: t[0], legs=Slow())
    w.run_cycle()
    assert w.snapshot()["pass_overruns"] == 1


def test_a_bug_in_the_loop_is_retried_then_a_crash_loop_stops_visibly(monkeypatch):
    monkeypatch.setattr(sv, "CRASH_LIMIT", 3)
    lease = FakeLease()
    lease.boom = RuntimeError("bug")
    w, _, _ = mk(lease=lease)
    monkeypatch.setattr(w, "_sleep", lambda s: None)
    w.start()
    assert wait_for(lambda: w.snapshot()["state"] == "failed")
    assert wait_for(lambda: not w.snapshot()["thread_alive"])
    s = w.snapshot()
    assert s["crashes"] == 3 and s["last_error"] == "RuntimeError" and not w.healthy()
    assert lease.released >= 1


def test_one_crash_is_survived():
    lease = FakeLease()
    lease.boom = RuntimeError("once")
    w, _, _ = mk(lease=lease)
    w.start()
    try:
        assert wait_for(lambda: w.snapshot()["crashes"] >= 1)
        lease.boom = None
        assert wait_for(lambda: w.snapshot()["running"] and w.snapshot()["cycles"] >= 1)
    finally:
        w.stop()


# ------------------------------------------------------------------ health view

def test_health_snapshot_contains_every_required_metric_and_renders_escaped():
    led = FakeLedger()
    led.stats.update(evidence_queue=2, receipt_queue=3, ambiguous_legs=1, orphaned_legs=1,
                     oldest_wait_s=42.0)
    w, _, _ = mk(ledger=led)
    w.run_cycle()
    s = w.snapshot()
    assert {"state", "running", "last_success_at", "seconds_since_success", "errors_total",
            "consecutive_failures", "crashes", "queues", "rpc", "lease_losses"} <= set(s)
    assert s["queues"]["evidence_queue"] == 2 and s["queues"]["receipt_queue"] == 3
    assert s["queues"]["ambiguous_legs"] == 1 and s["queues"]["oldest_wait_s"] == 42.0   # lag
    assert {"calls", "failures", "budget_refusals", "calls_last_minute"} <= set(s["rpc"])
    s["last_error"] = "<b>x</b>"
    text = render_text(s, __import__("html").escape)
    assert "<b>x</b>" not in text and "ambiguous 1" in text and "queue: evidence 2" in text


def test_the_worker_module_never_touches_telegram():
    src = Path(sv.__file__).read_text()
    for banned in ("tgui", "TelegramApi", "getUpdates", "sendMessage", "api.telegram"):
        assert banned not in src.replace("`getUpdates`", "")


def test_only_tgui_consumes_get_updates():
    root = Path(agora_tracker.__file__).parent
    users = {p.name for p in root.rglob("*.py") if 'call("getUpdates"' in p.read_text()}
    assert users == {"tgui.py"}


# ------------------------------------------------------------------ owner-only Telegram view

def bot_with(worker, owners=(7,)):
    from agora_tracker.tgui import IntelBot
    sent = []
    bot = SimpleNamespace(ledger=SimpleNamespace(owner_ids=set(owners)), worker=worker,
                          _send=lambda chat, text, markup: sent.append(text))
    return bot, sent, IntelBot._worker_health


def test_health_command_is_owner_private_only():
    w, _, _ = mk()
    w.run_cycle()
    bot, sent, cmd = bot_with(w)
    priv = {"chat": {"type": "private"}}
    cmd(bot, 7, 7, priv)
    assert "Wallet reconciliation worker" in sent[-1]
    cmd(bot, 8, 8, priv)                                   # not an owner
    cmd(bot, 7, -100, {"chat": {"type": "supergroup"}})     # not private
    bot.worker = None
    cmd(bot, 7, 7, priv)                                   # worker off
    assert sent[1:] == ["Not available."] * 3


# ------------------------------------------------------------------ PostgreSQL integration

@needs_db
def test_pg_lease_is_exclusive_releases_and_dies_with_its_session(live, owner_engine):
    url = live.engine.url
    a, b = PgAdvisoryLease(url), PgAdvisoryLease(url)
    try:
        assert a.acquire() and a.held() and not b.acquire() and not b.held()
        a.release()
        assert not a.held() and b.acquire()
        raw(owner_engine, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE "
                          "application_name = 'elysium-trading-worker-lease'")
        assert wait_for(lambda: not b.held())              # lock died with the session
        assert a.acquire()                                 # ...and is available again
    finally:
        a.release()
        b.release()


def real_worker(live, ch, lease=None, *, calls_per_min=100_000, sources=2, **kw):
    """The real supervisor over `sources` independent providers of the scripted chain `ch`, each
    behind its own per-minute budget (as `build_worker` does)."""
    chain = ChainGroup([BudgetedChain(p, calls_per_min=calls_per_min)
                        for p in providers(ch, live.clock, sources)])
    args = dict(interval_s=0.01, idle_interval_s=0.02, standby_retry_s=0.02, pass_budget_s=10.0,
                lower_priority=False, finality_confirmations=12, clock=live.clock)
    args.update(kw)
    return WorkerSupervisor(live, chain, lease=lease or FakeLease(), **args)


@needs_db
def test_real_reconcilers_run_provisional_then_final_through_the_supervisor(live, node):
    oid, leg, ch = mined_buy(live, node)
    w = real_worker(live, ch)
    assert w.run_cycle() is True
    assert live.get_order(U1, oid).state == "included"
    q = w.snapshot()["queues"]
    assert q["receipt_queue"] == 1 and q["ambiguous_legs"] == 0
    ch.head_n = 1011
    assert w.run_cycle() is True
    assert live.get_order(U1, oid).state == "finalized"
    assert w.snapshot()["queues"]["receipt_queue"] == 0 and w.snapshot()["queues"]["oldest_wait_s"] is None


@needs_db
def test_receipt_monitoring_continues_while_paused_and_with_the_execution_flag_off(live, node):
    oid, leg, ch = mined_buy(live, node)
    live.pause(Actor.owner(OWNER), "emergency")
    live.set_execution_enabled(Actor.owner(OWNER), False, "off")
    w = real_worker(live, ch)
    assert w.run_cycle() is True
    assert live.get_order(U1, oid).state == "included" and live.is_paused()
    ch.head_n = 1011
    w.run_cycle()
    assert live.get_order(U1, oid).state == "finalized"
    assert live.is_paused()                                # the worker never resumes anything


@needs_db
def test_restart_recovery_resumes_from_the_database_without_duplicates(live, node, owner_engine):
    oid, leg, ch = mined_buy(live, node)
    w1 = real_worker(live, ch)
    w1.run_cycle()
    w1.stop()
    w2 = real_worker(live, ch)                             # "process restarted": all memory gone
    ch.head_n = 1011
    assert w2.run_cycle() is True
    assert live.get_order(U1, oid).state == "finalized"
    assert len(live.get_fills(U1, oid, include_rolled_back=True)) == 1
    n = raw(owner_engine, "SELECT count(*) FROM trading_private.order_events WHERE order_id = :o "
                          "AND kind = 'WALLET_RESULT'", o=oid)[0][0]
    w2.run_cycle()
    assert raw(owner_engine, "SELECT count(*) FROM trading_private.order_events WHERE order_id = :o "
                             "AND kind = 'WALLET_RESULT'", o=oid)[0][0] == n


@needs_db
def test_two_workers_without_any_lease_still_reconcile_exactly_once(live, node):
    """Correctness must not depend on the advisory lock: the ledger is the arbiter."""
    oid, leg, ch = mined_buy(live, node)
    ws = [real_worker(live, ch) for _ in range(3)]
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(lambda w: w.run_cycle(), ws))
    assert live.get_order(U1, oid).state == "included"
    assert len(live.get_fills(U1, oid, include_rolled_back=True)) == 1
    assert live.get_legs(U1, oid)[0]["state"] == "included"


@needs_db
def test_duplicate_processes_one_leads_one_stands_by_and_takes_over(live, node):
    oid, leg, ch = mined_buy(live, node)
    url = live.engine.url
    a = real_worker(live, ch, lease=PgAdvisoryLease(url))
    b = real_worker(live, ch, lease=PgAdvisoryLease(url))
    a.start()
    assert wait_for(lambda: a.snapshot()["running"])
    b.start()
    try:
        assert wait_for(lambda: b.snapshot()["state"] == "standby")
        assert wait_for(lambda: live.get_order(U1, oid).state == "included")
        assert b.snapshot()["cycles"] == 0                  # the standby did no work at all
        assert a.stop()
        assert wait_for(lambda: b.snapshot()["running"])    # takeover after the leader left
        ch.head_n = 1011
        assert wait_for(lambda: live.get_order(U1, oid).state == "finalized")
        assert len(live.get_fills(U1, oid, include_rolled_back=True)) == 1
    finally:
        a.stop()
        b.stop()


@needs_db
def test_rpc_outage_changes_no_ledger_state_and_recovery_completes_the_work(live, node):
    oid, leg, ch = mined_buy(live, node)
    w = real_worker(live, ch)
    ch.down = True
    assert w.run_cycle() is False
    assert live.get_order(U1, oid).state == "submitted" and live.get_fills(U1, oid) == []
    assert w.snapshot()["rpc"]["failures"] >= 1 and w.snapshot()["last_error"] == "RpcUnavailable"
    ch.down = False
    assert w.run_cycle() is True
    assert live.get_order(U1, oid).state == "included"


@needs_db
def test_rpc_budget_exhaustion_is_fail_closed_for_real_reconcilers(live, node):
    oid, leg, ch = mined_buy(live, node)
    w = real_worker(live, ch)
    for m in w.chain.sources:
        m._limit = 2                                       # each provider's own per-minute budget
    w.run_cycle()
    assert live.get_order(U1, oid).state == "submitted"    # not enough evidence: nothing recorded
    assert w.snapshot()["rpc"]["budget_refusals"] >= 1
    for m in w.chain.sources:
        m._limit = 10_000
        m._stamps.clear()
    w.run_cycle()
    assert live.get_order(U1, oid).state == "included"


@needs_db
def test_stats_report_ambiguous_legs_and_lag(live, node, clock):
    from test_wallet_reconcile import errored
    oid, plan, leg, flow = errored(live, node)
    clock.t += 120
    s = live.worker_stats()
    assert s["ambiguous_legs"] == 1 and s["evidence_queue"] == 1 and s["receipt_queue"] == 0
    assert 119 <= s["oldest_wait_s"] <= 125
    w = real_worker(live, ReceiptChain())
    assert w.run_cycle() in (True, False)
    assert w.snapshot()["queues"]["ambiguous_legs"] == 1


# ------------------------------------------------------------------ process wiring (run_ui)

def _run_ui_harness(monkeypatch, *, trading, env=None):
    """Drive the real `run_ui` with every external piece stubbed; returns what happened."""
    import dataclasses

    import agora_tracker.__main__ as main
    import agora_tracker.db as dbmod
    import agora_tracker.migrate as mig
    import agora_tracker.reports as reports
    import agora_tracker.tgui as tgui
    import agora_tracker.trading.ledger.runtime as lrt
    seen = {"runs": 0, "workers_during_run": [], "bot": None}

    class StubLedger:
        engine = SimpleNamespace(url="postgresql+psycopg://trading_rw:x@127.0.0.1:1/none")
        owner_ids = frozenset({1})

        def worker_stats(self):
            return {"evidence_queue": 0, "receipt_queue": 0, "ambiguous_legs": 0,
                    "orphaned_legs": 0, "oldest_wait_s": None}

        def active_legs(self, limit=0):
            return []

        def receipt_candidates(self, limit=0):
            return []

    class StubBot:
        worker = None

        def __init__(self, *a, **k):
            seen["bot"] = self

        def run(self, stop):
            seen["runs"] += 1
            time.sleep(0.2)
            seen["workers_during_run"] = [t.name for t in threading.enumerate()
                                          if t.name == "wallet-reconciler"]

        def shutdown(self):
            pass

    from agora_tracker.config import load_config
    base = load_config(Path(agora_tracker.__file__).parent.parent / "config.example.yaml")
    c = dataclasses.replace(base, telegram=dataclasses.replace(base.telegram, bot_token="t"),
                            trading=TradingConfig(**trading),
                            collector=dataclasses.replace(base.collector, enabled=False),
                            scoring=dataclasses.replace(base.scoring, enabled=False))
    monkeypatch.setattr(dbmod, "make_engine", lambda pool_size=1: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(mig, "current_revision", lambda e: "x")
    monkeypatch.setattr(mig, "head_revision", lambda: "x")
    monkeypatch.setattr(main, "Store", lambda *a, **k: object())
    monkeypatch.setattr(main, "AgoraMarket", lambda *a, **k: object())
    monkeypatch.setattr(reports, "Reports", lambda *a, **k: object())
    monkeypatch.setattr(tgui, "IntelBot", StubBot)
    monkeypatch.setattr(tgui, "TelegramApi", lambda *a, **k: object())
    monkeypatch.setattr(lrt, "open_ledger", lambda cfg: StubLedger())
    monkeypatch.setattr(main.signal, "signal", lambda *a, **k: None)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TRADING_EXEC_RPC_URL", raising=False) if not env else None
    return main, c, seen


def test_run_ui_starts_no_worker_by_default(monkeypatch):
    main, c, seen = _run_ui_harness(monkeypatch, trading={"ledger_enabled": True})
    main.run_ui(c)
    assert seen["runs"] == 1 and seen["workers_during_run"] == [] and seen["bot"].worker is None


def test_run_ui_enabled_without_an_rpc_is_fatal_before_polling_starts(monkeypatch):
    main, c, seen = _run_ui_harness(
        monkeypatch, trading={"ledger_enabled": True, "wallet_execution_enabled": True})
    with pytest.raises(SystemExit, match="worker misconfigured"):
        main.run_ui(c)
    assert seen["runs"] == 0                               # the update consumer never started


def test_run_ui_enabled_runs_one_worker_beside_the_single_update_consumer(monkeypatch):
    main, c, seen = _run_ui_harness(
        monkeypatch, trading={"ledger_enabled": True, "wallet_execution_enabled": True},
        env={"TRADING_EXEC_RPC_URL": "https://exec.example/KEY"})
    main.run_ui(c)
    assert seen["runs"] == 1                               # exactly one getUpdates consumer
    assert seen["workers_during_run"] == ["wallet-reconciler"]
    assert seen["bot"].worker is not None
    assert not [t for t in threading.enumerate() if t.name == "wallet-reconciler"]   # stopped


# ------------------------------------------------------------------ F-15: the worker's shared corroboration
def test_build_worker_wires_every_source_into_one_shared_corroborator_and_fails_closed_alone():
    engine = SimpleNamespace(url="postgresql+psycopg://trading_rw:pw@127.0.0.1/db")
    c = cfg(ledger_enabled=True, wallet_execution_enabled=True)
    w = build_worker(c, SimpleNamespace(engine=engine),
                     environ={**ENV, "TRADING_CORROBORATION_RPC_URLS": "https://rpc.other.example/K"})
    assert len(w.chain.sources) == 2 and w.snapshot()["rpc"]["sources"] == 2
    assert w.receipts.corro is w.legs.corro and len(w.receipts.corro.sources) == 2
    solo = build_worker(c, SimpleNamespace(engine=engine), environ=ENV)       # one source only
    assert len(solo.chain.sources) == 1
    with pytest.raises(WorkerSetupError, match="operator"):
        build_worker(c, SimpleNamespace(engine=engine), environ={
            **ENV, "TRADING_CORROBORATION_RPC_URLS": "https://other.example/K,https://rpc.other.example/K"})
