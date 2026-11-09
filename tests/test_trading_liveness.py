"""Liveness push alerts and bounded ingress recovery (#95 F-18). No network: a fake Telegram API
records what would be sent; the ingress restart test uses the REAL loopback listener."""

from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace

import pytest
import requests
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from agora_tracker.config import TradingConfig
from agora_tracker.trading import ingress as ing
from agora_tracker.trading.ingress import IngressServer, IngressStats
from agora_tracker.trading.liveness import LivenessMonitor, Thresholds, scrub, telegram_sender

FAST = Thresholds(interval_s=0.05, confirm_ticks=2, cooldown_s=100.0, max_messages_per_hour=50,
                  startup_grace_s=0.0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Outbox:
    def __init__(self, fail=False):
        self.msgs: list[tuple[int, str]] = []
        self.fail = fail

    def __call__(self, chat, text):
        if self.fail:
            raise RuntimeError("telegram down")
        self.msgs.append((chat, text))

    def texts(self):
        return [t for _, t in self.msgs]


class FakeIngress:
    def __init__(self, ok=True, restart_ok=True):
        self.ok, self.restart_ok, self.restarts = ok, restart_ok, 0

    def healthy(self):
        return self.ok

    def snapshot(self):
        return {"state": "running" if self.ok else "failed", "error": "OSError 0xdeadbeefcafe"}

    def restart(self):
        self.restarts += 1
        if not self.restart_ok:
            raise ing.IngressSetupError("cannot bind")
        self.ok = True


def wsnap(**over):
    s = {"state": "running", "thread_alive": True, "crashes": 0, "seconds_since_success": 5.0,
         "consecutive_failures": 0, "last_error": None, "rpc": {"breaker": "closed"},
         "queues": {"evidence_queue": 0, "receipt_queue": 0, "ambiguous_legs": 0,
                    "orphaned_legs": 0, "oldest_wait_s": None}}
    s.update(over)
    return s


class FakeWorker:
    def __init__(self, **over):
        self.s = wsnap(**over)

    def snapshot(self):
        return self.s


def mon(out, clock, **kw):
    return LivenessMonitor(send=out, owners=[7, 8], clock=clock, thresholds=FAST, **kw)


def test_nothing_is_sent_while_healthy():
    out, clk = Outbox(), Clock()
    m = mon(out, clk, ingress=FakeIngress(), worker=FakeWorker())
    for _ in range(5):
        m.tick()
    assert out.msgs == [] and m.snapshot()["active"] == []


def test_a_condition_alerts_once_after_confirmation_reminds_after_cooldown_and_resolves():
    out, clk = Outbox(), Clock()
    w = FakeWorker(queues={"evidence_queue": 0, "receipt_queue": 0, "ambiguous_legs": 2,
                           "orphaned_legs": 0, "oldest_wait_s": None})
    m = mon(out, clk, worker=w)
    m.tick()
    assert out.msgs == []                                   # one bad tick is not an incident
    m.tick()
    assert [c for c, _ in out.msgs] == [7, 8] and "[ambiguous_legs]" in out.msgs[0][1]
    for _ in range(5):
        clk.t += 10
        m.tick()
    assert len(out.msgs) == 2                               # deduplicated inside the cooldown
    clk.t += 100
    m.tick()
    assert len(out.msgs) == 4 and "still active" in out.msgs[-1][1]
    w.s["queues"]["ambiguous_legs"] = 0
    m.tick()
    assert "RESOLVED [ambiguous_legs]" in out.msgs[-1][1] and m.snapshot()["active"] == []


@pytest.mark.parametrize("over,key", [
    ({"state": "failed", "thread_alive": False, "crashes": 5}, "worker_down"),
    ({"seconds_since_success": 9999.0}, "worker_stale"),
    ({"seconds_since_success": None}, "worker_stale"),
    ({"consecutive_failures": 7, "last_error": "RpcUnavailable at https://rpc.x/KEY"}, "worker_failing"),
    ({"rpc": {"breaker": "open"}}, "rpc_outage"),
    ({"queues": {"evidence_queue": 0, "receipt_queue": 0, "ambiguous_legs": 0, "orphaned_legs": 1,
                 "oldest_wait_s": None}}, "orphaned_legs"),
    ({"queues": {"evidence_queue": 0, "receipt_queue": 3, "ambiguous_legs": 0, "orphaned_legs": 0,
                 "oldest_wait_s": 5000.0}}, "receipt_lag"),
    ({"queues": {"evidence_queue": 15, "receipt_queue": 10, "ambiguous_legs": 0, "orphaned_legs": 0,
                 "oldest_wait_s": 10.0}}, "queue_growth"),
])
def test_each_operational_condition_is_detected(over, key):
    out, clk = Outbox(), Clock()
    m = mon(out, clk, worker=FakeWorker(**over))
    m.tick()
    m.tick()
    assert key in m.snapshot()["active"]
    joined = " ".join(out.texts())
    assert f"[{key}]" in joined and "https://" not in joined and "KEY" not in joined


def test_a_stopped_worker_and_the_startup_grace_do_not_alert():
    out, clk = Outbox(), Clock()
    m = LivenessMonitor(send=out, owners=[7], clock=clk,
                        thresholds=Thresholds(confirm_ticks=1, startup_grace_s=300),
                        worker=FakeWorker(seconds_since_success=None))
    m.tick()
    assert out.msgs == []                                   # inside the grace period
    m2 = mon(out, clk, worker=FakeWorker(state="stopped", thread_alive=False))
    m2.tick()
    m2.tick()
    assert out.msgs == []


def test_messages_never_carry_addresses_hashes_urls_or_ids():
    assert scrub("leg 0x" + "ab" * 20 + " at https://rpc.example/KEY user 123456789") == \
        "leg [redacted] at [redacted] user [redacted]"


def test_the_hourly_cap_stops_an_alert_storm():
    out, clk = Outbox(), Clock()
    t = Thresholds(confirm_ticks=1, cooldown_s=60, max_messages_per_hour=3, startup_grace_s=0)
    w = FakeWorker(rpc={"breaker": "open"})
    m = LivenessMonitor(send=out, owners=[7], clock=clk, thresholds=t, worker=w)
    for _ in range(20):
        clk.t += 61
        m.tick()
    assert len(out.msgs) == 3 and m.snapshot()["suppressed"] > 0


def test_delivery_failures_are_counted_and_never_break_the_monitor():
    out, clk = Outbox(fail=True), Clock()
    m = mon(out, clk, worker=FakeWorker(rpc={"breaker": "open"}))
    m.tick()
    m.tick()
    assert m.snapshot()["delivery_errors"] == 2 and m.snapshot()["sent"] == 0


def test_only_private_owner_ids_are_alerted():
    m = LivenessMonitor(send=Outbox(), owners=[7, -100123, 0, True, 7], worker=None)
    assert m.owners == [7]


def test_a_dead_listener_is_restarted_a_bounded_number_of_times_then_degraded():
    out, clk = Outbox(), Clock()
    ingr = FakeIngress(ok=False, restart_ok=False)
    t = Thresholds(confirm_ticks=1, ingress_max_restarts=2, ingress_restart_window_s=3600,
                   startup_grace_s=0, max_messages_per_hour=50)
    m = LivenessMonitor(send=out, owners=[7], clock=clk, thresholds=t, ingress=ingr)
    for _ in range(6):
        clk.t += 1
        m.tick()
    assert ingr.restarts == 2 and m.snapshot()["degraded"] is True
    assert any("[ingress_degraded]" in x for x in out.texts())
    assert any("[ingress_down]" in x for x in out.texts())
    assert "deadbeef" not in " ".join(out.texts())


def test_a_successful_restart_is_silent():
    out, clk = Outbox(), Clock()
    ingr = FakeIngress(ok=False, restart_ok=True)
    m = mon(out, clk, ingress=ingr)
    m.tick()
    m.tick()
    assert ingr.restarts == 1 and ingr.ok and out.msgs == []


def test_an_evaluation_error_is_itself_an_alert_and_the_monitor_survives():
    class Broken:
        def snapshot(self):
            raise RuntimeError("boom")
    out, clk = Outbox(), Clock()
    m = mon(out, clk, worker=Broken())
    m.tick()
    m.tick()
    assert "[monitor_error]" in out.texts()[0] and m.snapshot()["errors"] == 2


def test_the_telegram_sender_only_ever_calls_sendMessage():
    calls = []

    class Api:
        def call(self, method, **params):
            calls.append((method, params))
    telegram_sender(Api())(7, "x" * 5000)
    assert [c[0] for c in calls] == ["sendMessage"] and len(calls[0][1]["text"]) == 3500
    assert "parse_mode" not in calls[0][1]


def test_the_thread_runs_and_stops():
    out = Outbox()
    m = LivenessMonitor(send=out, owners=[7], thresholds=FAST, worker=FakeWorker(rpc={"breaker": "open"}))
    m.start()
    deadline = time.time() + 5
    while not out.msgs and time.time() < deadline:
        time.sleep(0.02)
    m.stop()
    assert out.msgs and not m.snapshot()["thread_alive"]


# ------------------------------------------------------------------ the real listener
def test_the_real_ingress_listener_is_rebound_on_the_same_port_after_dying():
    async def index(request):
        return PlainTextResponse("ok")
    app = Starlette(routes=[Route("/", index)])
    sock = ing._bind_loopback("127.0.0.1", 0)
    srv = IngressServer(app, sock, origin="https://app.example.test",
                        cfg=SimpleNamespace(trading=TradingConfig()), stats=IngressStats(),
                        wallet_routes=False)
    srv.start()
    port = srv.address[1]
    try:
        assert requests.get(f"http://127.0.0.1:{port}/", timeout=5).text == "ok"
        srv._server.should_exit = True                      # the listener thread dies
        srv._thread.join(5)
        assert not srv.healthy()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        out = Outbox()
        m = LivenessMonitor(send=out, owners=[7], thresholds=FAST, ingress=srv)
        m.tick()
        assert srv.healthy() and srv.restarts == 1 and srv.address[1] == port
        assert requests.get(f"http://127.0.0.1:{port}/", timeout=5).text == "ok"
        assert [t.name for t in threading.enumerate()].count("ingress") == 1
        assert out.msgs == []
    finally:
        srv.stop(3)


def test_liveness_config_is_validated():
    assert TradingConfig().liveness_alerts_enabled is True
    for k, bad in (("liveness_alerts_enabled", "yes"), ("liveness_interval_s", 1),
                   ("liveness_cooldown_s", 10), ("liveness_ingress_max_restarts", 11)):
        with pytest.raises(ValueError):
            TradingConfig(**{k: bad})
