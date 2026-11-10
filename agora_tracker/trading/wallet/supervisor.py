"""Supervised lifecycle for the reconciliation workers (Stage C2, issue #95 finding F-01).

One low-priority thread runs, in order, per cycle:

    LegReconciler.run_once   (#93: what became of each wallet request, from chain evidence)
    ReceiptReconciler.run_once (#94: validated receipts, finality, reorgs, verified fills)

It reuses those classes unchanged; this module only adds the lifecycle around them.

Guarantees
  * OFF unless `trading.wallet_execution_enabled`; with it on, a missing ledger / execution RPC is
    fatal at startup (`WorkerSetupError`), never a silent no-op. Nothing here can sign, approve or
    broadcast: the RPC handed to the workers is a read-only, explicit-method wrapper.
  * Exactly one active worker per database: a PostgreSQL session advisory lock on a dedicated
    connection (a second process becomes a hot standby and retries). Correctness never depends on
    the lock - every ledger write is optimistic (`expected_version`) and DB-guarded - the lock only
    prevents wasted RPC budget and noisy contention.
  * Keeps running while trading is paused and while the durable execution flag is OFF: what already
    happened on chain must still be recorded. Pausing stops *new* prompts, not bookkeeping.
  * Bounded everything: legs per pass, wall time per cycle, execution-RPC calls per minute (shared by
    both reconcilers), exponential backoff with a ceiling, a crash-loop breaker, a lowered thread
    priority. It never touches Telegram (the single `getUpdates` consumer is untouched) or the
    scanner's RPC.
  * Graceful stop: the stop request is honoured between legs (each leg is its own atomic ledger
    transaction), the lease is released, and a restart simply resumes from the database.
  * Private health: `snapshot()` / `render_text()` - served only to ledger owners, never on a
    public route; only exception *class names* are kept, never messages, URLs or hashes of users."""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from ...config import Config
from ..paper import TradeRefused
from .receipts import ReceiptReconciler
from .reconcile import LegReconciler
from .rpc import ExecutionRpc, RpcBusy
from .runtime import ExecutionRpcConfigError, build_execution_rpc

log = logging.getLogger(__name__)

# Fixed advisory-lock key pair ("elys", "recn"): one reconciliation worker per database.
LEASE_KEY = (0x656C7973, 0x7265636E)
CRASH_WINDOW_S = 600.0
CRASH_LIMIT = 10
SUMMARY_EVERY_S = 300.0
STOP_POLL_S = 1.0


class WorkerSetupError(RuntimeError):
    """The worker is enabled but cannot be built safely. Fatal at startup."""


# ---------------------------------------------------------------------------------- lease

class Lease(Protocol):
    def acquire(self) -> bool: ...
    def held(self) -> bool: ...
    def release(self) -> None: ...


class PgAdvisoryLease:
    """Session-level advisory lock on its OWN single connection (a separate 1-connection engine, so
    the ledger pool the Telegram handlers use is not reduced). The lock lives exactly as long as
    that session: if the connection dies, Postgres releases it and `held()` turns false."""

    def __init__(self, url: sa.URL | str, *, key: tuple[int, int] = LEASE_KEY) -> None:
        self._url, self._key = url, key
        self._engine: Engine | None = None
        self._conn: Connection | None = None
        self._lock = threading.Lock()

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.invalidate()
                conn.close()
            except Exception:                              # noqa: BLE001 - best effort
                pass

    def acquire(self) -> bool:
        with self._lock:
            if self._conn is not None:
                return self._alive()
            try:
                if self._engine is None:
                    self._engine = sa.create_engine(
                        self._url, pool_size=1, max_overflow=0, pool_pre_ping=False,
                        connect_args={"application_name": "elysium-trading-worker-lease",
                                      "connect_timeout": 10})
                conn = self._engine.connect()
                got = conn.execute(sa.text("SELECT pg_try_advisory_lock(:a, :b)"),
                                   {"a": self._key[0], "b": self._key[1]}).scalar_one()
                conn.commit()
            except Exception:                              # noqa: BLE001
                log.warning("worker lease could not be acquired (database unavailable)")
                return False
            if got is not True:
                conn.close()
                return False
            self._conn = conn
            return True

    def _alive(self) -> bool:
        try:
            assert self._conn is not None
            self._conn.execute(sa.text("SELECT 1"))
            self._conn.commit()
            return True
        except Exception:                                  # noqa: BLE001 - session gone => lock gone
            log.warning("worker lease connection lost")
            self._drop()
            return False

    def held(self) -> bool:
        with self._lock:
            return self._conn is not None and self._alive()

    def release(self) -> None:
        with self._lock:
            conn = self._conn
            if conn is not None:
                try:
                    conn.execute(sa.text("SELECT pg_advisory_unlock(:a, :b)"),
                                 {"a": self._key[0], "b": self._key[1]})
                    conn.commit()
                except Exception:                          # noqa: BLE001 - closing releases it anyway
                    pass
            self._drop()
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None


# ------------------------------------------------------------------------- budgeted chain

class ChainSource(Protocol):
    def head(self) -> Any: ...
    def transaction(self, tx_hash: str) -> dict | None: ...
    def receipt(self, tx_hash: str) -> dict | None: ...
    def tx_count(self, address: str, tag: str) -> int: ...
    def block_transactions(self, number: int) -> list[dict]: ...
    def block_header(self, number: int) -> dict | None: ...
    def native_balance(self, address: str, block: int) -> int: ...
    def token_balance(self, token: str, address: str, block: int) -> int: ...


class BudgetedChain:
    """The ONLY chain handle the workers get: explicit read methods (no send/sign/call-anything),
    one shared per-minute call budget, and failure/success accounting for health and backoff.
    Over budget raises `RpcBusy` (a TradeRefused), which both reconcilers already treat as
    "evidence unavailable right now": they change nothing and retry on a later cycle."""

    def __init__(self, inner: ChainSource, *, calls_per_min: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._inner, self._limit, self._clock = inner, calls_per_min, clock
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()
        self.calls = self.failures = self.refused = 0
        self._cycle_ok = self._cycle_fail = 0

    def _admit(self) -> None:
        now = self._clock()
        with self._lock:
            while self._stamps and now - self._stamps[0] >= 60.0:
                self._stamps.popleft()
            if len(self._stamps) >= self._limit:
                self.refused += 1
                raise RpcBusy("worker RPC budget exhausted for this minute")
            self._stamps.append(now)

    def _do(self, name: str, *args: Any) -> Any:
        self._admit()
        try:
            out = getattr(self._inner, name)(*args)
        except TradeRefused:
            with self._lock:
                self.calls += 1
                self.failures += 1
                self._cycle_fail += 1
            raise
        with self._lock:
            self.calls += 1
            self._cycle_ok += 1
        return out

    def head(self):
        return self._do("head")

    def transaction(self, tx_hash):
        return self._do("transaction", tx_hash)

    def receipt(self, tx_hash):
        return self._do("receipt", tx_hash)

    def tx_count(self, address, tag):
        return self._do("tx_count", address, tag)

    def block_transactions(self, number):
        return self._do("block_transactions", number)

    def block_header(self, number):
        return self._do("block_header", number)

    def native_balance(self, address, block):
        return self._do("native_balance", address, block)

    def token_balance(self, token, address, block):
        return self._do("token_balance", token, address, block)

    def take_cycle(self) -> tuple[int, int]:
        """(ok, failed) RPC calls since the previous call; resets the window."""
        with self._lock:
            out, self._cycle_ok, self._cycle_fail = (self._cycle_ok, self._cycle_fail), 0, 0
        return out

    def in_window(self) -> int:
        now = self._clock()
        with self._lock:
            return sum(1 for t in self._stamps if now - t < 60.0)


# ------------------------------------------------------------------------------ supervisor

class WorkerSupervisor:
    def __init__(self, ledger: Any, chain: BudgetedChain, *, lease: Lease,
                 finality_confirmations: int, interval_s: float = 10.0, idle_interval_s: float = 30.0,
                 max_backoff_s: float = 300.0, batch: int = 50, pass_budget_s: float = 20.0,
                 standby_retry_s: float = 15.0, external_stop: threading.Event | None = None,
                 rpc_info: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic,
                 leg_reconciler: Any = None, receipt_reconciler: Any = None,
                 lower_priority: bool = True) -> None:
        self.ledger, self.chain, self.lease = ledger, chain, lease
        self.interval_s, self.idle_interval_s = interval_s, idle_interval_s
        self.max_backoff_s, self.batch = max_backoff_s, batch
        self.pass_budget_s, self.standby_retry_s = pass_budget_s, standby_retry_s
        self.clock, self.mono = clock, mono
        self._ext = external_stop
        self._rpc_info = rpc_info or (lambda: {})
        self._own = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._lower_priority = lower_priority
        self.legs = leg_reconciler or LegReconciler(ledger, [chain], clock=clock)
        self.receipts = receipt_reconciler or ReceiptReconciler(
            ledger, chain, finality_confirmations=finality_confirmations, clock=clock)
        self._h: dict[str, Any] = {
            "state": "stopped", "started_at": None, "cycles": 0, "last_cycle_at": None,
            "last_success_at": None, "last_duration_s": None, "consecutive_failures": 0,
            "errors_total": 0, "crashes": 0, "last_error": None, "pass_overruns": 0,
            "last_leg_counts": {}, "last_receipt_counts": {}, "queues": None,
            "lease_losses": 0, "standby_since": None}
        self._crash_times: list[float] = []
        self._last_summary = 0.0

    # ----------------------------------------------------------------------- control
    def start(self) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                raise WorkerSetupError("this supervisor is already running")
            self._own.clear()
            self._h.update(state="standby", started_at=self.clock())
            self._thread = threading.Thread(target=self._main, name="wallet-reconciler",
                                            daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 20.0) -> bool:
        """Request shutdown and wait for the thread; always releases the lease. Idempotent."""
        self._own.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        alive = t is not None and t.is_alive()
        if not alive:
            self.lease.release()
            self._h["state"] = "stopped"
        return not alive

    def _halted(self) -> bool:
        return self._own.is_set() or (self._ext is not None and self._ext.is_set())

    def _sleep(self, seconds: float) -> None:
        end = self.mono() + seconds
        while not self._halted():
            left = end - self.mono()
            if left <= 0:
                return
            self._own.wait(min(STOP_POLL_S, left))

    # ------------------------------------------------------------------------- loop
    def _main(self) -> None:
        if self._lower_priority:
            try:                                           # Linux: nice is per-thread
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            except (AttributeError, OSError):
                pass
        try:
            while not self._halted():
                try:
                    self._loop()
                    break
                except Exception as exc:                   # noqa: BLE001 - a bug must not end monitoring
                    now = self.mono()
                    self._crash_times = [t for t in self._crash_times if now - t < CRASH_WINDOW_S]
                    self._crash_times.append(now)
                    self._h["crashes"] += 1
                    self._h["last_error"] = type(exc).__name__
                    log.exception("wallet reconciler crashed (%d in window)", len(self._crash_times))
                    if len(self._crash_times) >= CRASH_LIMIT:
                        self._h["state"] = "failed"
                        log.critical("wallet reconciler is crash-looping; stopped. Legs are NOT "
                                     "being monitored until it is restarted.")
                        return
                    self._sleep(min(self.max_backoff_s, 2.0 ** len(self._crash_times)))
        finally:
            self.lease.release()
            if self._h["state"] != "failed":
                self._h["state"] = "stopped"

    def _loop(self) -> None:
        while not self._halted():
            if not self._has_lease():
                self._sleep(self.standby_retry_s)
                continue
            ok = self.run_cycle()
            if self._halted():
                return
            delay = self._next_delay(ok)
            self._sleep(delay)

    def _next_delay(self, ok: bool) -> float:
        """Seconds to wait after a cycle: the busy/idle interval, or capped exponential backoff
        (with jitter, so restarted workers do not synchronize) after consecutive failures."""
        if not ok:
            n = self._h["consecutive_failures"]
            return min(self.max_backoff_s, self.interval_s * (2 ** min(n, 10))) * random.uniform(0.8, 1.2)
        q = self._h["queues"] or {}
        busy = (q.get("evidence_queue", 0) + q.get("receipt_queue", 0)) > 0
        return self.interval_s if busy else self.idle_interval_s

    def _has_lease(self) -> bool:
        if self.lease.held():
            if self._h["state"] != "running":
                self._h.update(state="running", standby_since=None)
                log.info("wallet reconciler is the active worker")
            return True
        was = self._h["state"] == "running"
        if was:
            self._h["lease_losses"] += 1
            log.warning("wallet reconciler lost its lease; standing by")
        if self.lease.acquire():
            self._h.update(state="running", standby_since=None)
            log.info("wallet reconciler acquired the lease")
            return True
        if self._h["state"] != "standby":
            self._h["state"] = "standby"
        if self._h["standby_since"] is None:
            self._h["standby_since"] = self.clock()
        return False

    # ------------------------------------------------------------------------ cycle
    def run_cycle(self) -> bool:
        """One bounded cycle. Returns True on success. Public so tests and tools can drive it."""
        t0 = self.mono()
        deadline = t0 + self.pass_budget_s
        overran = False

        def keep_going() -> bool:
            nonlocal overran
            if self._halted():
                return False
            if self.mono() >= deadline:
                overran = True
                return False
            return True

        self.chain.take_cycle()
        failed: str | None = None
        leg_counts: dict[str, int] = {}
        rcpt_counts: dict[str, int] = {}
        try:
            leg_counts = dict(self.legs.run_once(limit=self.batch, should_continue=keep_going).counts)
        except Exception as exc:                           # noqa: BLE001 - one reconciler failing
            failed = type(exc).__name__                    # must not stop the other
            log.warning("leg reconciliation pass failed: %s", failed)
        try:
            rcpt_counts = dict(self.receipts.run_once(limit=self.batch,
                                                      should_continue=keep_going).counts)
        except Exception as exc:                           # noqa: BLE001
            failed = failed or type(exc).__name__
            log.warning("receipt pass failed: %s", failed)
        ok_calls, bad_calls = self.chain.take_cycle()
        if failed is None and bad_calls > 0 and ok_calls == 0:
            failed = "RpcUnavailable"                      # every RPC call failed: node is down
        stats = None
        try:
            stats = self.ledger.worker_stats()
        except Exception as exc:                           # noqa: BLE001
            failed = failed or type(exc).__name__
        h = self._h
        h["cycles"] += 1
        h["last_cycle_at"] = self.clock()
        h["last_duration_s"] = self.mono() - t0
        h["last_leg_counts"], h["last_receipt_counts"] = leg_counts, rcpt_counts
        if stats is not None:
            h["queues"] = stats
        if overran:
            h["pass_overruns"] += 1
        if failed is None:
            h["last_success_at"] = h["last_cycle_at"]
            h["consecutive_failures"] = 0
        else:
            h["consecutive_failures"] += 1
            h["errors_total"] += 1
            h["last_error"] = failed
        self._summarize()
        return failed is None

    def _summarize(self) -> None:
        now = self.mono()
        if now - self._last_summary < SUMMARY_EVERY_S:
            return
        self._last_summary = now
        s = self.snapshot()
        q = s["queues"] or {}
        log.info("wallet worker: state=%s cycles=%d failures=%d queue=%s/%s ambiguous=%s lag=%s "
                 "rpc_fail=%d", s["state"], s["cycles"], s["consecutive_failures"],
                 q.get("evidence_queue"), q.get("receipt_queue"), q.get("ambiguous_legs"),
                 q.get("oldest_wait_s"), s["rpc"]["failures"])

    # ----------------------------------------------------------------------- health
    def snapshot(self) -> dict[str, Any]:
        """Private health view (no DB access; refreshed once per cycle)."""
        h = dict(self._h)
        now = self.clock()
        alive = self._thread is not None and self._thread.is_alive()
        since = None if h["last_success_at"] is None else max(0.0, now - h["last_success_at"])
        return {
            "state": h["state"], "running": bool(alive and h["state"] == "running"),
            "thread_alive": alive, "started_at": h["started_at"], "cycles": h["cycles"],
            "last_cycle_at": h["last_cycle_at"], "last_success_at": h["last_success_at"],
            "seconds_since_success": since, "last_duration_s": h["last_duration_s"],
            "consecutive_failures": h["consecutive_failures"], "errors_total": h["errors_total"],
            "crashes": h["crashes"], "last_error": h["last_error"], "lease_losses": h["lease_losses"],
            "pass_overruns": h["pass_overruns"], "queues": h["queues"],
            "last_leg_counts": h["last_leg_counts"], "last_receipt_counts": h["last_receipt_counts"],
            "rpc": {"calls": self.chain.calls, "failures": self.chain.failures,
                    "budget_refusals": self.chain.refused, "calls_last_minute": self.chain.in_window(),
                    **self._rpc_info()},
        }

    def healthy(self, max_age_s: float | None = None) -> bool:
        s = self.snapshot()
        limit = max_age_s if max_age_s is not None else 3 * self.idle_interval_s + self.pass_budget_s
        return bool(s["running"] and s["seconds_since_success"] is not None
                    and s["seconds_since_success"] <= limit)


def render_text(s: dict[str, Any], esc: Callable[[str], str]) -> str:
    """Owner-only Telegram text (HTML-escaped)."""
    q = s["queues"] or {}

    def v(x: Any) -> str:
        return "n/a" if x is None else (f"{x:.0f}" if isinstance(x, float) else str(x))
    rpc = s["rpc"]
    lines = [
        "<b>Wallet reconciliation worker</b>",
        f"state: {esc(s['state'])} (running={s['running']}, thread={s['thread_alive']})",
        f"cycles: {s['cycles']}; last success {v(s['seconds_since_success'])}s ago; "
        f"last cycle {v(s['last_duration_s'])}s",
        f"errors: {s['errors_total']} (consecutive {s['consecutive_failures']}, crashes "
        f"{s['crashes']}, lease losses {s['lease_losses']}); last: {esc(str(s['last_error']))}",
        f"queue: evidence {v(q.get('evidence_queue'))}, receipts {v(q.get('receipt_queue'))}; "
        f"oldest waiting {v(q.get('oldest_wait_s'))}s",
        f"needs attention: ambiguous {v(q.get('ambiguous_legs'))}, orphaned {v(q.get('orphaned_legs'))}",
        f"rpc: {rpc['calls']} calls, {rpc['failures']} failed, {rpc['budget_refusals']} over budget, "
        f"{rpc['calls_last_minute']} in the last minute, breaker {esc(str(rpc.get('breaker', 'n/a')))}",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- wiring

def build_worker(cfg: Config, ledger: Any, *, stop: threading.Event | None = None,
                 environ: Mapping[str, str] = os.environ, rpc: ExecutionRpc | None = None,
                 lease: Lease | None = None) -> WorkerSupervisor | None:
    """None when `trading.wallet_execution_enabled` is false (the default): nothing is built.
    When it is true every missing requirement raises `WorkerSetupError` - a half-configured
    execution stack must stop the process, not degrade into "nothing watches the chain"."""
    t = cfg.trading
    if not t.wallet_execution_enabled:
        return None
    if ledger is None or not t.ledger_enabled:
        raise WorkerSetupError("wallet execution needs the order ledger")
    if rpc is None:
        try:
            rpc = build_execution_rpc(cfg, environ)
        except ExecutionRpcConfigError as exc:
            raise WorkerSetupError(str(exc)) from None
    if rpc is None:
        raise WorkerSetupError("no execution RPC")
    chain = BudgetedChain(rpc, calls_per_min=t.worker_rpc_calls_per_min)
    if lease is None:
        try:
            lease = PgAdvisoryLease(ledger.engine.url)
        except Exception:                                  # noqa: BLE001
            raise WorkerSetupError("cannot build the worker lease") from None
    rpc_ref = rpc
    return WorkerSupervisor(
        ledger, chain, lease=lease, finality_confirmations=t.finality_confirmations,
        interval_s=t.worker_interval_s, idle_interval_s=t.worker_idle_interval_s,
        max_backoff_s=t.worker_max_backoff_s, batch=t.worker_batch,
        pass_budget_s=t.worker_pass_budget_s, standby_retry_s=t.worker_standby_retry_s,
        external_stop=stop, rpc_info=lambda: {"breaker": rpc_ref.breaker.state})
