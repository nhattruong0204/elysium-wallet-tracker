"""Execution RPC: allowlist, chain/head checks, 429/backoff, breaker, redaction, metrics (#93).
A scripted in-memory transport stands in for the node: no network."""

from __future__ import annotations

import json
import logging
import threading

import pytest
from walletkit import NOW

from agora_tracker.trading.paper import TradeRefused
from agora_tracker.trading.wallet.approvals import ChainReverted
from agora_tracker.trading.wallet.rpc import (
    FORBIDDEN_PREFIXES,
    READ_METHODS,
    CircuitBreaker,
    ExecutionRpc,
    RpcBusy,
    RpcError,
    RpcUnavailable,
)

SECRET_URL = "https://rpc.example.test/v1/SUPERSECRETKEY"
H = "0x" + "ab" * 32


class Script:
    """Answers by method; `plan[method]` is a list of replies consumed in order (last repeats)."""

    def __init__(self, **plan):
        self.plan = {"eth_chainId": [(200, {}, "0x1863d")]} | plan
        self.sent: list[dict] = []
        self.lock = threading.Lock()
        self.gate: threading.Event | None = None

    def post(self, url, payload, timeout):
        assert url == SECRET_URL
        if self.gate is not None:
            self.gate.wait(2)
        with self.lock:
            self.sent.append(payload)
            replies = self.plan[payload["method"]]
            status, headers, result = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(result, Exception):
            raise result
        if status != 200:
            return status, headers, b"<html>nope</html>"
        if isinstance(result, bytes):
            return 200, headers, result
        body = {"jsonrpc": "2.0", "id": payload["id"]}
        body.update({"error": result["error"]} if isinstance(result, dict) and "error" in result
                    else {"result": result})
        return 200, headers, json.dumps(body).encode()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(script, **kw):
    clock = Clock()
    sleeps: list[float] = []
    rpc = ExecutionRpc(SECRET_URL, transport=script, clock=clock, wall=lambda: NOW,
                       sleep=sleeps.append, **kw)
    rpc.clock_, rpc.sleeps = clock, sleeps
    return rpc


def ok(v):
    return [(200, {}, v)]


# ------------------------------------------------------------------ allowlist: no send path

def test_only_reads_are_allowlisted_and_nothing_can_send_or_sign():
    assert not any(m.startswith(FORBIDDEN_PREFIXES) for m in READ_METHODS)
    s = Script()
    rpc = make(s)
    for m in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_sign",
              "eth_signTransaction", "wallet_addEthereumChain", "debug_traceTransaction",
              "eth_accounts", "eth_getLogs"):
        with pytest.raises(TradeRefused):
            rpc.rpc(m, [])
    assert s.sent == []                                  # refused BEFORE touching the network


# ------------------------------------------------------------------ chain / head checks

def test_wrong_chain_is_refused_and_opens_the_breaker():
    s = Script(eth_chainId=ok("0x1"), eth_gasPrice=ok("0x5"))
    rpc = make(s)
    with pytest.raises(RpcUnavailable, match="wrong chain"):
        rpc.gas_price()
    assert rpc.breaker.state == "open"
    with pytest.raises(RpcUnavailable, match="circuit"):
        rpc.gas_price()
    assert [p["method"] for p in s.sent] == ["eth_chainId"]


def test_chain_is_verified_once_then_periodically():
    s = Script(eth_gasPrice=ok("0x5"))
    rpc = make(s)
    rpc.gas_price()
    rpc.gas_price()
    assert [p["method"] for p in s.sent].count("eth_chainId") == 1
    rpc.clock_.t += 301
    rpc.gas_price()
    assert [p["method"] for p in s.sent].count("eth_chainId") == 2


def block(ts=int(NOW) - 3, n=7):
    return {"number": hex(n), "hash": H, "timestamp": hex(ts), "transactions": []}


def test_head_must_be_fresh_and_well_formed():
    assert make(Script(eth_getBlockByNumber=ok(block()))).head().number == 7
    for bad in (block(ts=int(NOW) - 600), block(ts=int(NOW) + 600),
                {**block(), "hash": "0x12"}, {**block(), "number": "7"}, None):
        with pytest.raises(RpcUnavailable):
            make(Script(eth_getBlockByNumber=ok(bad))).head()


# ------------------------------------------------------------------ 429 / backoff / breaker

def test_429_honours_retry_after_then_succeeds():
    s = Script(eth_gasPrice=[(429, {"retry-after": "3"}, None), (200, {}, "0x9")])
    rpc = make(s)
    assert rpc.gas_price() == 9
    assert rpc.sleeps and rpc.sleeps[0] >= 3 and rpc.metrics()["methods"]["eth_gasPrice"]["rate_limited"] == 1


def test_retry_after_is_capped():
    s = Script(eth_gasPrice=[(429, {"retry-after": "999"}, None), (200, {}, "0x9")])
    rpc = make(s)
    rpc.gas_price()
    assert max(rpc.sleeps) <= 5.0


def test_persistent_failures_open_the_breaker_then_half_open_probe_recovers():
    s = Script(eth_gasPrice=[(500, {}, None)])
    rpc = make(s, retries=0, breaker=CircuitBreaker(threshold=3, reset_after=30, clock=lambda: 0))
    clock = Clock()
    rpc.breaker = CircuitBreaker(threshold=3, reset_after=30, clock=clock)
    for _ in range(3):
        with pytest.raises(RpcUnavailable):
            rpc.gas_price()
    assert rpc.breaker.state == "open"
    sent = len(s.sent)
    with pytest.raises(RpcUnavailable, match="circuit"):
        rpc.gas_price()
    assert len(s.sent) == sent                           # failing fast: nothing was sent
    clock.t += 31
    assert rpc.breaker.state == "half_open"
    s.plan["eth_gasPrice"] = ok("0x1")
    assert rpc.gas_price() == 1 and rpc.breaker.state == "closed"


def test_half_open_allows_only_one_probe():
    b = CircuitBreaker(threshold=1, reset_after=10, clock=(c := Clock()))
    b.failure()
    c.t += 11
    assert b.allow() is True and b.allow() is False


def test_reverts_and_node_errors_do_not_count_as_endpoint_failure():
    s = Script(eth_call=ok({"error": {"code": 3, "message": "execution reverted: secret reason"}}),
               eth_estimateGas=ok({"error": {"code": -32000, "message": "internal https://x/?k=1"}}))
    rpc = make(s, retries=0, breaker=CircuitBreaker(threshold=1))
    with pytest.raises(ChainReverted) as e1:
        rpc.call("0x" + "11" * 20, "0x", "latest")
    with pytest.raises(RpcError) as e2:
        rpc.estimate_gas({}, "latest")
    assert "secret" not in str(e1.value) and "https" not in str(e2.value)
    assert rpc.breaker.state == "closed"


@pytest.mark.parametrize("reply", [b"not json", b'{"jsonrpc":"2.0","id":999,"result":"0x1"}',
                                   b'{"jsonrpc":"1.0","id":1,"result":"0x1"}', b'{"id":1}', b"[]"])
def test_malformed_replies_are_endpoint_failures(reply):
    rpc = make(Script(eth_gasPrice=ok(reply)), retries=0, breaker=CircuitBreaker(threshold=1))
    with pytest.raises(RpcUnavailable):
        rpc.gas_price()
    assert rpc.breaker.state == "open"


def test_malformed_quantities_are_refused():
    for bad in ("5", "0x", "0x05", 5, None, "0xZZ"):
        with pytest.raises(RpcUnavailable):
            make(Script(eth_gasPrice=ok(bad))).gas_price()


# ------------------------------------------------------------------ bounded concurrency

def test_concurrency_is_bounded_and_overload_fails_fast():
    s = Script(eth_gasPrice=ok("0x1"))
    rpc = make(s, max_concurrency=1, queue_wait=0.05)
    rpc.gas_price()                                       # warm the chain check
    s.gate = threading.Event()
    result: list = []
    t = threading.Thread(target=lambda: result.append(rpc.gas_price()))
    t.start()
    while not rpc._slots._value == 0:                     # wait until the slot is taken
        pass
    with pytest.raises(RpcBusy):
        rpc.gas_price()
    s.gate.set()
    t.join(3)
    assert result == [1]


# ------------------------------------------------------------------ redaction

def test_url_key_never_appears_in_errors_logs_or_metrics(caplog):
    caplog.set_level(logging.DEBUG)
    rpc = make(Script(eth_chainId=ok("0x1")), retries=0)
    with pytest.raises(RpcUnavailable) as e:
        rpc.gas_price()
    blob = str(e.value) + caplog.text + json.dumps(rpc.metrics())
    assert "SUPERSECRETKEY" not in blob and "/v1/" not in blob
    assert rpc.metrics()["host"] == "rpc.example.test"


def test_transport_exceptions_are_scrubbed():
    s = Script(eth_gasPrice=[(200, {}, RpcUnavailable("execution RPC transport error"))])
    with pytest.raises(RpcUnavailable) as e:
        make(s, retries=0).gas_price()
    assert "SUPERSECRETKEY" not in str(e.value)


def test_invalid_url_is_refused():
    for bad in ("ftp://x", "not a url", "https://", ""):
        with pytest.raises(ValueError):
            ExecutionRpc(bad)


# ------------------------------------------------------------------ typed reads / metrics

def test_typed_reads_validate_inputs_and_shapes():
    tx = {"hash": H, "from": "0x" + "a1" * 20}
    s = Script(eth_getTransactionByHash=[(200, {}, tx), (200, {}, None), (200, {}, "x")],
               eth_getTransactionCount=ok("0x7"),
               eth_getBlockByNumber=ok({"transactions": [tx, 5, tx]}))
    rpc = make(s)
    assert rpc.transaction(H) == tx and rpc.transaction(H) is None
    with pytest.raises(RpcUnavailable):
        rpc.transaction(H)
    assert rpc.tx_count("0x" + "a1" * 20, "pending") == 7 == rpc.pending_nonce("0x" + "a1" * 20)
    assert rpc.block_transactions(5) == [tx, tx]
    for bad in (("0x" + "A1" * 20, "pending"), ("0x" + "a1" * 20, "earliest"), ("0x12", "latest")):
        with pytest.raises(TradeRefused):
            rpc.tx_count(*bad)
    with pytest.raises(TradeRefused):
        rpc.transaction("0x12")


def test_metrics_count_outcomes_and_latency_buckets():
    rpc = make(Script(eth_gasPrice=ok("0x1")))
    for _ in range(3):
        rpc.gas_price()
    m = rpc.metrics()["methods"]["eth_gasPrice"]
    assert m["ok"] == 3 and sum(m["buckets"]) == 3
