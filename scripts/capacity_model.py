#!/usr/bin/env python3
"""RPC budget projection for the tracked-wallet universe (a MODEL, not a measurement).

Inputs are the values measured by scripts/scan_bench.py on 2026-11-06 (override on the command
line). Nothing here talks to the network. It answers: at N wallets and a given poll interval,
how many eth_getLogs calls per minute does each scan strategy need, how big is a response, and
does that fit a node's rate limit?

  capacity_model.py [--poll 5] [--chunk 50] [--density 0.35] [--limit-per-min 15]
"""
from __future__ import annotations

import argparse
import math

KIB_PER_LOG = 0.62        # 459 KiB / 744 logs measured on 2,000 blocks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=float, default=5.0, help="poll interval, seconds")
    ap.add_argument("--chunk", type=int, default=50, help="wallets per OR-list (filtered)")
    ap.add_argument("--density", type=float, default=0.35, help="chain-wide Transfer logs per block")
    ap.add_argument("--blocks-per-s", type=float, default=1.0)
    ap.add_argument("--limit-per-min", type=float, default=15.0,
                    help="eth_getLogs calls/min the strictest node tolerates (Kleos: ~5-15 observed)")
    ap.add_argument("--max-span", type=int, default=1000, help="unfiltered span cap (Kleos)")
    a = ap.parse_args()
    polls_per_min = 60 / a.poll
    blocks_per_poll = a.poll * a.blocks_per_s
    print(f"poll every {a.poll:g}s -> {polls_per_min:g} polls/min, ~{blocks_per_poll:g} blocks/poll; "
          f"chain {a.density} logs/block = {a.density * blocks_per_poll:.1f} logs/poll, "
          f"{a.density * a.max_span * KIB_PER_LOG:.0f} KiB per {a.max_span}-block catch-up call")
    print(f"strictest node: {a.limit_per_min:g} getLogs/min\n")
    hdr = ("wallets", "filtered calls/poll", "filtered calls/min", "fits limit", "full calls/min",
           "fits limit")
    print("{:>8} {:>20} {:>19} {:>11} {:>15} {:>11}".format(*hdr))
    for n in (100, 331, 500, 1000, 5000):
        fc = 2 * math.ceil(n / a.chunk)
        per_min, full_min = fc * polls_per_min, 1 * polls_per_min
        print(f"{n:>8} {fc:>20} {per_min:>19.0f} {'yes' if per_min <= a.limit_per_min else 'NO':>11} "
              f"{full_min:>15.0f} {'yes' if full_min <= a.limit_per_min else 'NO':>11}")
    print("\nfull mode cost is independent of the wallet count; what grows is the chain's own "
          "Transfer volume (see full_scan_max_logs_per_block).\nLocal filtering cost: one set "
          "lookup per log, so 5,000 wallets x 100k logs/range is milliseconds (tests/"
          "test_scan_strategy.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
