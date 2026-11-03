"""trace_token.py: keccak/selector correctness and a full run against a fake JSON-RPC node."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "trace_token.py"
spec = importlib.util.spec_from_file_location("trace_token", SCRIPT)
tt = importlib.util.module_from_spec(spec)
sys.modules["trace_token"] = tt
spec.loader.exec_module(tt)

TOKEN = "0x" + "11" * 20
CREATOR = "0x" + "aa" * 20
FACTORY = "0x" + "fa" * 20
CURVE = "0x" + "cc" * 20
BUYER = "0x" + "bb" * 20
TX0, TX1 = "0x" + "01" * 32, "0x" + "02" * 32


def pad(a: str) -> str:
    return "0x" + "0" * 24 + a[2:]


def word(n: int) -> str:
    return "0x" + f"{n:064x}"


def log(frm: str, to: str, v: int, blk: int, tx: str, idx: int) -> dict:
    return {"address": TOKEN, "topics": [tt.TRANSFER, pad(frm), pad(to)], "data": word(v),
            "blockNumber": hex(blk), "transactionHash": tx, "logIndex": hex(idx)}


E18 = 10**18
LOGS = [log(tt.ZERO, CURVE, 1000 * E18, 100, TX0, 0), log(CURVE, BUYER, 5 * E18, 105, TX1, 0)]
# dispatcher: PUSH4 mint(address,uint256), PUSH4 setSellFee(uint256), PUSH4 owner()
CODE = "0x" + "63" + tt.selector("mint(address,uint256)")[2:] + "63" + tt.selector("setSellFee(uint256)")[2:] \
    + "63" + tt.selector("owner()")[2:]


def handle(method: str, params: list):
    if method == "eth_chainId":
        return hex(99901)
    if method == "eth_blockNumber":
        return hex(200)
    if method == "eth_getCode":
        return CODE if params[0] == TOKEN else "0x6000" if params[0] in (FACTORY, CURVE) else "0x"
    if method == "eth_call":
        d = params[0]["data"]
        if d == "0x95d89b41":
            return "0x" + "20".rjust(64, "0") + "3".rjust(64, "0") + b"TST".hex().ljust(64, "0")
        if d == "0x06fdde03":
            return "0x" + "20".rjust(64, "0") + "4".rjust(64, "0") + b"Test".hex().ljust(64, "0")
        if d == "0x313ce567":
            return word(18)
        if d == "0x18160ddd":
            return word(1000 * E18)
        if d == "0x8da5cb5b":
            return pad(CREATOR)
        return "0x"
    if method == "eth_getStorageAt":
        return word(0)
    if method == "eth_getLogs":
        f = params[0]
        return [x for x in LOGS if int(f["fromBlock"], 16) <= int(x["blockNumber"], 16) <= int(f["toBlock"], 16)]
    if method == "eth_getBlockByNumber":
        return {"timestamp": hex(1_800_000_000 + int(params[0], 16))}
    if method == "eth_getTransactionByHash":
        if params[0] == TX0:
            return {"from": CREATOR, "to": FACTORY, "value": "0x0"}
        return {"from": BUYER, "to": CURVE, "value": hex(6 * E18)}
    if method == "eth_getTransactionReceipt":
        return {"logs": [{"address": TOKEN}, {"address": FACTORY}], "contractAddress": None}
    if method == "eth_getBalance":
        return hex(40 * E18) if params[0] == CURVE else hex(2 * E18)
    raise AssertionError(method)


class H(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        req = json.loads(self.rfile.read(int(self.headers["content-length"])))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": handle(req["method"], req["params"])}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_keccak_vectors():
    assert tt.keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert tt.selector("transfer(address,uint256)") == "0xa9059cbb"
    assert tt.selector("balanceOf(address)") == "0x70a08231"
    assert tt.selector("renounceOwnership()") == "0x715018a6"


def test_push4_ignores_push_data():
    code = "0x63a9059cbb7f" + "63" * 32 + "6370a08231"
    assert tt.push4s(code) == {"0xa9059cbb", "0x70a08231"}


def test_full_trace_against_fake_node(capsys, tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    known = tmp_path / "known.tsv"
    known.write_text(f"somepad {TX1}\n")
    try:
        assert tt.main([TOKEN, "--rpc", f"http://127.0.0.1:{srv.server_port}", "--tx", TX1,
                        "--known", str(known), "--hype-usd", "85"]) == 0
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert "symbol='TST'" in out and "totalSupply=1,000.0000" in out
    assert "RISK  mint(address,uint256)" in out and "RISK  setSellFee(uint256)" in out
    assert f"tx.from (creator / caller): {CREATOR}" in out
    assert f"tx.to (factory / launchpad contract): {FACTORY}" in out
    assert f"{CURVE}  " in out and "KNOWN LAUNCHPAD: somepad" in out          # buy tx.to == curve
    assert "holds 40.0000 HYPE" in out and "(~$3,400)" in out                  # liquidity ceiling
    assert "99.5%" in out or "99.5 %" in out or "995.00" in out                # curve still holds supply
    assert "owner(): " + CREATOR in out
