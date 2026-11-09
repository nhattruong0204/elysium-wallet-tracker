#!/usr/bin/env python3
"""Disposable upstream for rehearsing the nginx template (#95 F-17): the REAL `IngressGuard` and
`IngressServer` around a stand-in app with the same route shapes as the Mini App / wallet routes.
Used by CI and for staging rehearsals only; it never touches a ledger, wallet or RPC.

    TRADING_INGRESS_SECRET=... python scripts/ci_ingress_upstream.py --origin https://app.example.test
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run from a checkout

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from agora_tracker.config import TradingConfig
from agora_tracker.trading import ingress as ing

HEADERS = {"Content-Security-Policy": "default-src 'self'", "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "no-referrer"}


def app() -> Starlette:
    async def page(request):
        return PlainTextResponse("ok", headers=HEADERS)

    async def api(request):
        return JSONResponse({"ok": False, "error": "stand-in"}, status_code=400, headers=HEADERS)
    return Starlette(routes=[Route("/", page), Route("/wallet", page),
                             Route("/api/{name}", api, methods=["POST"]),
                             Route("/wallet/api/{name}", api, methods=["POST"])])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", required=True)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    secret = os.environ["TRADING_INGRESS_SECRET"]
    t = TradingConfig()
    stats = ing.IngressStats()
    guard = ing.IngressGuard(app(), host=a.origin.split("://", 1)[1], secret=secret,
                             max_body=t.ingress_max_body_bytes, timeout_s=t.ingress_request_timeout_s,
                             threads=t.ingress_threads, stats=stats)
    srv = ing.IngressServer(guard, ing._bind_loopback("127.0.0.1", a.port), origin=a.origin,
                            cfg=SimpleNamespace(trading=t), stats=stats, wallet_routes=False)
    srv.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(f"upstream on 127.0.0.1:{a.port} for {a.origin}", flush=True)
    stop.wait()
    srv.stop(3)
    print("rejected:", stats.snapshot().get("rejected"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
