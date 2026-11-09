"""Operational liveness and incident alerts for the private trading services (#95 finding F-18).

Before this, health was pull-only (`/wallet_health`): a dead ingress listener, a stalled or
crash-looping reconciliation worker, a persistent RPC outage or a growing pile of ambiguous legs
went unnoticed until an owner happened to ask. `LivenessMonitor` is one low-priority daemon thread
in the `--ui` process that:

* evaluates a fixed set of conditions every `interval_s` from the existing in-memory snapshots
  (no extra database or RPC load: the worker already refreshes its queue counts each cycle);
* sends a PRIVATE Telegram message to each trading owner (positive user ids only, i.e. private
  chats) when a condition becomes active, reminds at most once per `cooldown_s` while it stays
  active, and says when it clears; a global hourly cap stops alert storms;
* restarts a dead ingress listener at most `ingress_max_restarts` times per window, then leaves it
  down (DEGRADED: no Mini App, no wallet prompts) and says so. The worker has its own crash-loop
  breaker and is never restarted here.

What it never does: poll Telegram (`getUpdates` stays with the one bot consumer), touch the
ledger, clear or set a pause, enable a flag, create a wallet request, or put a wallet address,
order id, user id, URL or secret in a message (only counts, states and error kinds).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_SENSITIVE = re.compile(r"0x[0-9a-fA-F]{6,}|https?://\S+|\b\d{6,}\b")


def scrub(text: Any, limit: int = 120) -> str:
    """Anything that could be an address, hash, URL or id is removed from operator text."""
    return _SENSITIVE.sub("[redacted]", str(text))[:limit]


@dataclass(frozen=True)
class Thresholds:
    interval_s: float = 30.0
    confirm_ticks: int = 2                 # consecutive bad ticks before alerting (no flapping)
    cooldown_s: float = 3600.0             # reminder period while a condition stays active
    max_messages_per_hour: int = 12
    worker_stale_s: float = 300.0          # no successful reconciliation cycle for this long
    worker_failures: int = 5               # consecutive failed cycles
    rpc_open_ticks: int = 4                # breaker open for this many consecutive ticks
    ambiguous_legs: int = 1                # any ambiguous leg needs an owner
    orphaned_legs: int = 1
    receipt_lag_s: float = 900.0           # oldest leg waiting for evidence
    queue_depth: int = 20                  # evidence + receipt queue
    ingress_max_restarts: int = 3
    ingress_restart_window_s: float = 3600.0
    startup_grace_s: float = 120.0


@dataclass(frozen=True)
class Condition:
    key: str
    severity: str                          # CRITICAL | WARNING
    text: str


class LivenessMonitor:
    def __init__(self, *, send: Callable[[int, str], None], owners: Iterable[int],
                 ingress: Any = None, worker: Any = None, thresholds: Thresholds | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 stop: threading.Event | None = None) -> None:
        self.owners = sorted({int(o) for o in owners if type(o) is int and o > 0})
        self.send, self.ingress, self.worker = send, ingress, worker
        self.t = thresholds or Thresholds()
        self.clock = clock
        self._stop = stop or threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at = clock()
        self._bad_ticks: dict[str, int] = {}
        self._active: dict[str, Condition] = {}
        self._last_sent: dict[str, float] = {}
        self._sent_times: list[float] = []
        self._restarts: list[float] = []
        self.degraded = False
        self.stats = {"ticks": 0, "sent": 0, "suppressed": 0, "delivery_errors": 0,
                      "ingress_restarts": 0, "errors": 0}

    # ------------------------------------------------------------------ evaluation
    def conditions(self) -> list[Condition]:
        out: list[Condition] = []
        t = self.t
        ing = self.ingress
        if ing is not None and not ing.healthy():
            snap = ing.snapshot()
            out.append(Condition("ingress_down", "CRITICAL",
                                 f"Mini App ingress listener is {snap.get('state')} "
                                 f"(error: {scrub(snap.get('error'))}); wallet prompts unavailable"))
        w = self.worker
        if w is not None:
            s = w.snapshot()
            grace = self.clock() - self.started_at < t.startup_grace_s
            if s["state"] == "failed" or (not s["thread_alive"] and s["state"] != "stopped"):
                out.append(Condition("worker_down", "CRITICAL",
                                     f"wallet reconciliation worker is {s['state']} "
                                     f"(crashes {s['crashes']}); receipts and ambiguous legs are "
                                     "NOT being reconciled"))
            elif s["state"] == "running" and not grace:
                since = s.get("seconds_since_success")
                if since is None or since > t.worker_stale_s:
                    out.append(Condition("worker_stale", "CRITICAL",
                                         "no successful reconciliation cycle for "
                                         f"{'ever' if since is None else f'{since:.0f}s'}"))
            if s.get("consecutive_failures", 0) >= t.worker_failures:
                out.append(Condition("worker_failing", "WARNING",
                                     f"{s['consecutive_failures']} consecutive failed cycles; last "
                                     f"error: {scrub(s.get('last_error'))}"))
            rpc = s.get("rpc") or {}
            if str(rpc.get("breaker", "")) == "open":
                out.append(Condition("rpc_outage", "CRITICAL",
                                     "execution RPC circuit breaker is OPEN (persistent RPC failure); "
                                     "run rpcdiag and check the provider"))
            q = s.get("queues") or {}
            if q:
                if q.get("ambiguous_legs", 0) >= t.ambiguous_legs:
                    out.append(Condition("ambiguous_legs", "WARNING",
                                         f"{q['ambiguous_legs']} wallet leg(s) ambiguous: their "
                                         "wallets stay blocked; see /wallet_recover"))
                if q.get("orphaned_legs", 0) >= t.orphaned_legs:
                    out.append(Condition("orphaned_legs", "WARNING",
                                         f"{q['orphaned_legs']} leg(s) orphaned by a reorg"))
                oldest = q.get("oldest_wait_s")
                if oldest is not None and oldest > t.receipt_lag_s:
                    out.append(Condition("receipt_lag", "WARNING",
                                         f"oldest leg has waited {oldest / 60:.0f} min for chain "
                                         "evidence"))
                depth = int(q.get("evidence_queue", 0)) + int(q.get("receipt_queue", 0))
                if depth > t.queue_depth:
                    out.append(Condition("queue_growth", "WARNING",
                                         f"{depth} legs waiting for evidence/receipts"))
        return out

    # ------------------------------------------------------------------ one tick
    def tick(self) -> None:
        self.stats["ticks"] += 1
        now = self.clock()
        try:
            found = {c.key: c for c in self.conditions()}
        except Exception as exc:                                     # noqa: BLE001
            self.stats["errors"] += 1
            log.warning("liveness evaluation failed: %s", type(exc).__name__)
            found = {"monitor_error": Condition("monitor_error", "WARNING",
                                                f"liveness evaluation failed ({type(exc).__name__})")}
        if "ingress_down" in found:
            self._maybe_restart_ingress(now)
            if self.ingress is not None and self.ingress.healthy():
                found.pop("ingress_down")
        for key in list(self._bad_ticks):
            if key not in found:
                del self._bad_ticks[key]
        for key, cond in found.items():
            self._bad_ticks[key] = self._bad_ticks.get(key, 0) + 1
            if self._bad_ticks[key] < self.t.confirm_ticks and key not in self._active:
                continue
            first = key not in self._active
            self._active[key] = cond
            if first or now - self._last_sent.get(key, -1e18) >= self.t.cooldown_s:
                if self._notify(f"{'🔴' if cond.severity == 'CRITICAL' else '🟠'} "
                                f"{cond.severity} [{key}] {cond.text}"
                                + ("" if first else " (still active)"), now):
                    self._last_sent[key] = now
        for key in [k for k in self._active if k not in found]:
            cond = self._active.pop(key)
            self._last_sent.pop(key, None)
            self._notify(f"🟢 RESOLVED [{key}] {cond.text.split(';')[0]}", now)

    def _maybe_restart_ingress(self, now: float) -> None:
        if self.ingress is None or self.degraded:
            return
        self._restarts = [r for r in self._restarts if now - r < self.t.ingress_restart_window_s]
        if len(self._restarts) >= self.t.ingress_max_restarts:
            self.degraded = True
            self._notify("🔴 CRITICAL [ingress_degraded] the ingress listener died "
                         f"{len(self._restarts)} times within "
                         f"{self.t.ingress_restart_window_s / 60:.0f} min; automatic restarts are "
                         "STOPPED. Mini App and wallet prompts stay offline until an operator "
                         "investigates and restarts the service. Trading state is unchanged.", now)
            return
        self._restarts.append(now)
        self.stats["ingress_restarts"] += 1
        try:
            self.ingress.restart()
        except Exception as exc:                                     # noqa: BLE001
            log.error("ingress restart failed: %s", type(exc).__name__)

    def _notify(self, text: str, now: float) -> bool:
        self._sent_times = [x for x in self._sent_times if now - x < 3600]
        if len(self._sent_times) >= self.t.max_messages_per_hour:
            self.stats["suppressed"] += 1
            log.warning("liveness alert suppressed by the hourly cap: %s", text[:80])
            return False
        self._sent_times.append(now)
        ok = False
        for owner in self.owners:
            try:
                self.send(owner, "Elysium trading health\n" + text)
                ok = True
            except Exception as exc:                                 # noqa: BLE001 - never raise
                self.stats["delivery_errors"] += 1
                log.warning("liveness alert delivery failed: %s", type(exc).__name__)
        if ok:
            self.stats["sent"] += 1
        log.warning("liveness: %s", text)
        return ok

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="liveness", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.t.interval_s):
            try:
                self.tick()
            except Exception:                                        # noqa: BLE001 - keep going
                self.stats["errors"] += 1
                log.exception("liveness tick failed")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def snapshot(self) -> dict[str, Any]:
        return {"active": sorted(self._active), "degraded": self.degraded,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "owners": len(self.owners), **self.stats}


def telegram_sender(api: Any) -> Callable[[int, str], None]:
    """sendMessage only (never getUpdates). Plain text: nothing in the alert is markup."""
    def send(chat_id: int, text: str) -> None:
        api.call("sendMessage", chat_id=chat_id, text=text[:3500], disable_notification=False,
                 link_preview_options={"is_disabled": True})
    return send
