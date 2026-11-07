#!/usr/bin/env python3
"""Verify pinned canonical V2 artifacts and on-chain runtimes using read requests only."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import requests
from eth_hash.auto import keccak

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agora_tracker.trading.chain import ReadRpc  # noqa: E402
from agora_tracker.trading.v2 import CONTRACTS, FACTORY, ROUTER, WRAPPED  # noqa: E402


def verify(rpc: ReadRpc, pair: str) -> dict:
    block, header = rpc.snapshot()
    rows = []
    with requests.Session() as session:
        for kind, target in [("router", ROUTER), ("factory", FACTORY), ("pair", pair)]:
            item = CONTRACTS[kind]
            r = session.get(item["artifact_url"], timeout=10)
            r.raise_for_status()
            if (
                len(r.content) > 2_000_000
                or hashlib.sha256(r.content).hexdigest() != item["artifact_sha256"]
            ):
                raise ValueError("Published artifact hash changed")
            artifact = r.json()
            runtime = bytearray.fromhex(artifact["evm"]["deployedBytecode"]["object"])
            if kind == "router":
                for key, value in [("2357", FACTORY), ("2360", WRAPPED)]:
                    for ref in artifact["evm"]["deployedBytecode"]["immutableReferences"][key]:
                        if ref["length"] != 32:
                            raise ValueError("Unexpected canonical immutable length")
                        runtime[ref["start"] : ref["start"] + 32] = bytes.fromhex(
                            value[2:].zfill(64)
                        )
            expected = "0x" + keccak(bytes(runtime)).hex()
            if expected != item["runtime_keccak256"] or rpc.code_hash(target, block) != expected:
                raise ValueError("Runtime identity verification failed")
            for function in item["abi"]:
                if function not in artifact["abi"]:
                    raise ValueError("Vendored interface differs from published ABI")
            rows.append(
                {
                    "kind": kind,
                    "address": target,
                    "runtime_keccak256": expected,
                    "artifact_sha256": item["artifact_sha256"],
                    "verified": True,
                }
            )
    return {
        "chain_id": 99901,
        "block": int(block, 16),
        "block_hash": header["hash"],
        "contracts": rows,
        "scope": "Canonical direct V2 only. No aggregator ABI verification or writes.",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rpc", default="https://rpc.agoradex.app")
    p.add_argument("--pair", required=True)
    a = p.parse_args()
    print(json.dumps(verify(ReadRpc(a.rpc), a.pair), indent=2))


if __name__ == "__main__":
    main()
