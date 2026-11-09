#!/usr/bin/env python3
"""Bounded PAPER handler benchmark; uses real reads and a fake Telegram sender.

No Telegram API, signer, approval, submission, balances or wallet IDs in public output.
Three samples maximum per side. User dwell and Telegram network delivery are excluded.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agora_tracker.cards import Links  # noqa: E402
from agora_tracker.config import LinksConfig, TradingConfig  # noqa: E402
from agora_tracker.tgui import IntelBot  # noqa: E402
from agora_tracker.trading.agora import AgoraV1  # noqa: E402
from agora_tracker.trading.chain import ReadRpc  # noqa: E402
from agora_tracker.trading.journal import PaperJournal  # noqa: E402
from agora_tracker.trading.paper import PaperFlow, TradeRefused  # noqa: E402
from agora_tracker.trading.v2 import DIRECT_ROUTE, DirectV2Provider  # noqa: E402
from scripts.trade_rpc_bench import percentile  # noqa: E402


def summary(values):
    return {
        "samples": len(values),
        "p50_ms": percentile(values, 0.5),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
    }


def benchmark(
    wallet: str, token: str, samples: int, buy: str, sell: str, *, optimize=True, warmup=False
) -> dict:
    if not 1 <= samples <= 3:
        raise ValueError("Bounded to 1-3 samples per side")
    rpc, api = ReadRpc("https://rpc.agoradex.app"), AgoraV1()
    provider = DirectV2Provider(rpc, agora=api, optimize=optimize)
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        f = PaperFlow(
            {1},
            provider=provider,
            routes=frozenset({DIRECT_ROUTE}),
            journal=PaperJournal(str(Path(directory) / "paper.db")),
        )
        f.set_wallet(1, 1, wallet)
        bot = IntelBot.__new__(IntelBot)
        bot.cfg = SimpleNamespace(
            trading=TradingConfig(True), chain=SimpleNamespace(chain_id=99901)
        )
        bot.allowed, bot._refused, bot.paper_trades = {1}, {}, f
        bot.L, bot._show, bot._send, bot._answer = Links(LinksConfig()), Mock(), Mock(), Mock()
        cold_start = None
        if warmup:
            cold = f.create(1, 1, token, "buy", -100123, 900)
            f.select(1, 1, cold.id, buy)
            start = time.monotonic()
            try:
                cold_quote = f.review(1, 1, cold.id)
                cold_start = {
                    "ms": (time.monotonic() - start) * 1000,
                    "rpc_calls": len(rpc.timings),
                    "simulation_status": cold_quote.simulation_status,
                }
            except TradeRefused as e:
                cold_start = {
                    "ms": (time.monotonic() - start) * 1000,
                    "rpc_calls": len(rpc.timings),
                    "simulation_status": "refused",
                    "refusal_reason": str(e),
                }
            f.cancel(1, 1, cold.id)
            time.sleep(2.1)
        for side in ["buy", "sell"]:
            for n in range(samples):
                i = f.create(1, 1, token, side, -100123, 1 + n)
                f.select(1, 1, i.id, buy if side == "buy" else sell)
                rpc.timings.clear()
                rpc.trace.clear()
                render_times = []
                from agora_tracker.trading import telegram

                real_panel = telegram.panel

                def timed_panel(*args, _panel=real_panel, _times=render_times, **kwargs):
                    render_start = time.monotonic()
                    try:
                        return _panel(*args, **kwargs)
                    finally:
                        _times.append((time.monotonic() - render_start) * 1000)

                started = time.monotonic()
                with patch.object(telegram, "panel", timed_panel):
                    bot.on_callback(
                        {
                            "id": "local-fixture",
                            "from": {"id": 1},
                            "data": "pr|" + i.id,
                            "message": {"message_id": 55, "chat": {"id": 1, "type": "private"}},
                        }
                    )
                elapsed = (time.monotonic() - started) * 1000
                q = f.reviewed(1, 1, i.id)
                if q is None:
                    rows.append(
                        {
                            "side": side,
                            "simulation_status": "refused",
                            "refusal_reason": bot._answer.call_args.args[1],
                            "review_click_to_render_ms": round(elapsed, 2),
                            "quote_ready_ms": None,
                            "agora_api_ms": None,
                            "agora_status": "not_requested",
                            "direct_quote_eth_call_ms": provider.metrics.get("getAmountsOut"),
                            "rpc_calls": len(rpc.timings),
                            "eth_call_ms": [
                                round(ms, 2) for m, ms in rpc.timings if m == "eth_call"
                            ],
                            "estimate_gas_ms": [],
                            "paper_record_status": "no_record",
                            "agora_vs_direct_output_delta_bps": None,
                        }
                    )
                    time.sleep(2.1)
                    continue
                confirmation = f.confirmation_token(1, 1, i.id)
                bot.on_callback(
                    {
                        "id": "local-fixture",
                        "from": {"id": 1},
                        "data": f"pf|{i.id}|{confirmation}",
                        "message": {"message_id": 55, "chat": {"id": 1, "type": "private"}},
                    }
                )
                rows.append(
                    {
                        "side": side,
                        "simulation_status": q.simulation_status,
                        "review_click_to_render_ms": round(elapsed, 2),
                        "quote_ready_ms": q.quote_latency_ms,
                        "agora_api_ms": round(api.last_latency_ms or 0, 2),
                        "agora_status": q.agora_status,
                        "direct_quote_eth_call_ms": (
                            round(provider.metrics["getAmountsOut"], 2)
                            if "getAmountsOut" in provider.metrics
                            else None
                        ),
                        "calculation_ms": provider.metrics["calculation_ms"],
                        "render_ms": sum(render_times),
                        "metadata_cache_hit": bool(provider.metrics["metadata_cache_hit"]),
                        "rpc_operations": [{**op, "ms": round(op["ms"], 2)} for op in rpc.trace],
                        "rpc_calls": len(rpc.timings),
                        "eth_call_ms": [round(ms, 2) for m, ms in rpc.timings if m == "eth_call"],
                        "estimate_gas_ms": [
                            round(ms, 2) for m, ms in rpc.timings if m == "eth_estimateGas"
                        ],
                        "agora_vs_direct_output_delta_bps": (
                            (q.agora_amount_out_wei - q.amount_out_wei) * 10000 // q.amount_out_wei
                            if q.agora_amount_out_wei is not None
                            else None
                        ),
                        "paper_record_status": f.orders(1, 1)[0]["status"],
                    }
                )
                time.sleep(2.1)
    return {
        "location": "WSL2 local network; egress region unverified",
        "token": token,
        "samples_per_side": samples,
        "optimization": optimize,
        "cold_start": cold_start,
        "buy_hype": buy,
        "sell_wallet_percent": sell,
        "observations": rows,
        "refusals": sum(r["simulation_status"] == "refused" for r in rows),
        "review_click_to_render": summary(
            [r["review_click_to_render_ms"] for r in rows if r["simulation_status"] != "refused"]
        ),
        "agora_quote": summary(
            [r["agora_api_ms"] for r in rows if r["agora_status"] == "reported_builder_unverified"]
        ),
        "direct_quote": summary(
            [
                r["direct_quote_eth_call_ms"]
                for r in rows
                if r["direct_quote_eth_call_ms"] is not None
            ]
        ),
        "eth_call": summary([ms for r in rows for ms in r["eth_call_ms"]]),
        "estimate_gas": summary([ms for r in rows for ms in r["estimate_gas_ms"]]),
        "limitations": [
            "Three samples per side are descriptive; no reliable tail estimates.",
            "Click timing covers the actual local Telegram handler, not Telegram delivery or user dwell.",
            "Direct pool expected output assumes normal token transfers; no actual fill or balance mutation.",
            "Agora comparison is API-reported, may use a different block and has unverified outer ABI.",
            "Missing SELL allowance is a conditional PAPER scenario with no successful simulation or gas estimate.",
            "No signer, approvals, transaction submissions, production bot messages or production DB writes.",
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--wallet", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--samples", type=int, default=3)
    p.add_argument("--buy", default="0.1")
    p.add_argument(
        "--compare",
        action="store_true",
        help="Compare unoptimized and cached steady-state workloads, with explicit cold starts",
    )
    p.add_argument("--sell-percent", default="1")
    a = p.parse_args()
    if a.compare:
        results = {
            label: benchmark(
                a.wallet, a.token, a.samples, a.buy, a.sell_percent, optimize=optimize, warmup=True
            )
            for label, optimize in [("before", False), ("after", True)]
        }
        pairs = [
            (a, b)
            for a, b in zip(
                results["before"]["observations"], results["after"]["observations"], strict=True
            )
            if a["simulation_status"] != "refused" and b["simulation_status"] != "refused"
        ]
        results["paired_successful"] = {
            "before": summary([a["review_click_to_render_ms"] for a, b in pairs]),
            "after": summary([b["review_click_to_render_ms"] for a, b in pairs]),
            "note": "Paired by side/index/size only when both succeeded; all refusals remain in raw observations.",
        }
        results["method"] = (
            "Same handler, RPC, token, wallet and sizes; both phases include the same security checks. "
            "Separate cold starts excluded from steady-state quantiles. Sequential chain state can differ."
        )
        print(json.dumps(results, indent=2))
    else:
        print(json.dumps(benchmark(a.wallet, a.token, a.samples, a.buy, a.sell_percent), indent=2))


if __name__ == "__main__":
    main()
