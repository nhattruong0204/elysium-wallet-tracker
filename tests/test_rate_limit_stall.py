# ruff: noqa: F811
"""2026-11-14 incident: one rate-limited receipt failed the whole block range, the range was
retried in full and never fitted the nodes' per-minute quota; the indexer stood still for three
days. Rate-limited items are now retried on their own, a failing range gets smaller, and the
catch-up afterwards does not send hours-old alerts."""

from __future__ import annotations

import time

import pytest
from test_airdrop_filter import leg, w
from test_launch_cohorts import _outbox, tok
from test_telegram_mute import eng  # noqa: F401
from test_tracker import E18

from agora_tracker import rpc as rpc_mod
from agora_tracker.chain import ChainFollower, ScanResult
from agora_tracker.config import load_config
from agora_tracker.rpc import RpcClient, RpcError

LIMIT = "Too many requests from this address this minute; try again shortly"


@pytest.fixture
def naps(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(rpc_mod.time, "sleep", lambda s: slept.append(s))
    return slept


def client(**kw):
    return RpcClient(["https://a.example/rpc"], expected_chain_id=None, **kw)


def test_only_the_rate_limited_items_are_asked_again(naps):
    c = client()
    seen: list[list[str]] = []
    quota = {"left": 3}

    def post(url, payload):
        items = payload if isinstance(payload, list) else [payload]
        seen.append([p["params"][0] for p in items])
        out = []
        for p in items:
            if quota["left"] > 0:
                quota["left"] -= 1
                out.append({"id": p["id"], "result": "r" + p["params"][0]})
            else:
                out.append({"id": p["id"], "error": {"code": -32005, "message": LIMIT}})
        if len(seen) == 1:
            quota["left"] = 10                       # the minute rolls over before the retry
        return out if isinstance(payload, list) else out[0]

    c._raw_post = post
    got = c.batch([("eth_getTransactionReceipt", [x]) for x in "abcde"])
    assert got == ["ra", "rb", "rc", "rd", "re"]
    assert seen == [list("abcde"), list("de")]       # the three good answers were kept
    assert naps == [3.0]


def test_a_node_that_never_recovers_still_returns_errors_not_a_hang(naps):
    c = client(item_retry_waits=(1.0, 2.0))
    c._raw_post = lambda url, payload: [
        {"id": p["id"], "error": {"code": 429, "message": LIMIT}} for p in payload]
    got = c.batch([("m", ["a"]), ("m", ["b"])])
    assert all(isinstance(r, RpcError) and r.retryable for r in got)
    assert naps == [1.0, 2.0]


def test_other_item_errors_are_not_retried(naps):
    c = client()
    calls = {"n": 0}

    def post(url, payload):
        calls["n"] += 1
        return [{"id": p["id"], "error": {"code": -32000, "message": "execution reverted"}}
                for p in payload]

    c._raw_post = post
    got = c.batch([("eth_call", ["a"]), ("eth_call", ["b"])])
    assert calls["n"] == 1 and naps == [] and all(isinstance(r, RpcError) for r in got)


def test_http_429_on_a_pinned_endpoint_waits_before_giving_up(naps):
    c = client(http_429_waits=(5.0, 20.0))
    answers = iter([RpcError("HTTP 429", 429), RpcError("HTTP 429", 429), {"id": 1, "result": "0x10"}])

    def post(url, payload):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return {**a, "id": payload["id"]}

    c._raw_post = post
    with c.pinned():
        assert c.block_number() == 16
    assert [s for s in naps if s >= 5] == [5.0, 20.0]

    c2 = client(http_429_waits=(5.0,))
    c2._raw_post = lambda url, payload: (_ for _ in ()).throw(RpcError("HTTP 429", 429))
    with pytest.raises(RpcError, match="pinned endpoint"), c2.pinned():
        c2._post({"id": 1, "method": "x", "params": []})


def test_a_failed_range_is_retried_smaller_and_grows_back():
    cfg = load_config("config.example.yaml").chain
    f = ChainFollower.__new__(ChainFollower)
    f.cfg, f.span_cap, f._ok_ranges = cfg, None, 0
    f.uses_full_scan = lambda n: False
    assert f.next_span() == cfg.max_block_range == 2000
    f.shrink_after_failure()
    assert f.next_span() == 1000
    for _ in range(6):
        f.next_span()
        f.shrink_after_failure()
    assert f.next_span() == cfg.min_block_range == 50          # never below the floor
    for _ in range(200):
        f.next_span()
    assert f.next_span() == 2000                               # back to full size by itself


def test_catch_up_indexes_old_trades_without_alerting_them(store, eng):
    eng.max_alert_age_s = 7200.0
    now = int(time.time())
    old, fresh = "0x" + "1" * 64, "0x" + "2" * 64
    eng.apply_range(ScanResult(events=[leg("BUY", tok(1), w(1), old, hype=5 * E18, block=10)],
                               block_times={10: now - 3 * 86400}), 10)
    eng.apply_range(ScanResult(events=[leg("BUY", tok(1), w(2), fresh, hype=5 * E18, block=11)],
                               block_times={11: now - 30}), 11)
    out = _outbox(store)
    trades = [r["payload"]["tx"] for r in out if (r["payload"] or {}).get("type") == "trade"]
    assert trades == [fresh]
    import sqlalchemy as sa
    with store.engine.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM trades")).scalar() == 2      # both indexed
        notes = c.execute(sa.text("SELECT count(*) FROM notification_outbox WHERE dedup_key "
                                  "LIKE 'catchup:%'")).scalar()
    assert notes == 1 and store.get_cursor() == 11


def test_defaults_and_env(monkeypatch):
    monkeypatch.delenv("ALERTS_MAX_AGE_S", raising=False)
    assert load_config("config.example.yaml").alerts.max_alert_age_s == 7200.0
    monkeypatch.setenv("ALERTS_MAX_AGE_S", "0")
    assert load_config("config.example.yaml").alerts.max_alert_age_s == 0.0
