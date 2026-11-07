"""Documented public Agora v1 builder observations; not an aggregator ABI adapter."""

from __future__ import annotations

import re
import threading
import time
from decimal import Decimal, InvalidOperation

import requests

from .chain import address, units
from .http import bounded_json
from .paper import CHAIN, TradeIntent, TradeRefused

AGORA_ROUTER = "0xaf8dec958df2bd664aa661716a8de5fe0417a295"


class AgoraV1:
    def __init__(self, *, monotonic=time.monotonic):
        self.session = requests.Session()
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._next = 0.0
        self.last_latency_ms: float | None = None

    def observation(self, i: TradeIntent, amount: int, decimals: int, slippage: int) -> dict:
        if i.chain_id != CHAIN or not i.wallet or i.side not in ("buy", "sell"):
            raise TradeRefused("Bound watch-only wallet required")
        self.last_latency_ms = None
        if not self._lock.acquire(blocking=False):
            raise TradeRefused("Agora quote busy")
        try:
            if self.monotonic() < self._next:
                raise TradeRefused("Agora quote rate limited")
            self._next = self.monotonic() + 2.1
            started = self.monotonic()
            try:
                r = self.session.get(
                    "https://api.agoradex.app/api/v1/quote",
                    params={
                        "token": address(i.token),
                        "recipient": address(i.wallet),
                        "side": i.side,
                        "amount": units(amount, 18 if i.side == "buy" else decimals),
                        "slippageBps": str(slippage),
                    },
                    timeout=5,
                    allow_redirects=False,
                    stream=True,
                )
                if r.status_code == 429:
                    self._next = self.monotonic() + 60
                if r.status_code != 200:
                    r.close()
                    raise TradeRefused("Agora quote unavailable or rate limited")
                q = bounded_json(r, deadline=time.monotonic()+5)
            except (requests.RequestException, ValueError) as e:
                if isinstance(e, TradeRefused):
                    raise
                raise TradeRefused("Agora quote unavailable") from None
            finally:
                self.last_latency_ms = (self.monotonic() - started) * 1000
        finally:
            self._lock.release()
        try:
            if (
                not isinstance(q, dict)
                or q["token"].lower() != i.token
                or q["side"] != i.side
                or q.get("payToken")
            ):
                raise ValueError
            for key in ["amountIn", "amountOut", "minAmountOut"]:
                if not isinstance(q[key], str) or not re.fullmatch(r"[0-9]{1,78}", q[key]):
                    raise ValueError
                if not 0 < int(q[key]) < 2**256:
                    raise ValueError
            if (
                int(q["amountIn"]) != amount
                or int(q["minAmountOut"]) != int(q["amountOut"]) * (10000 - slippage) // 10000
            ):
                raise ValueError
            impact = Decimal(str(q["priceImpact"]))
            if not impact.is_finite() or not -1 <= impact <= 1:
                raise ValueError
            if type(q["feeBps"]) is not int or not 0 <= q["feeBps"] <= 100:
                raise ValueError
            if not isinstance(q["venue"], str) or not 0 < len(q["venue"]) <= 120:
                raise ValueError
            tx = q["transaction"]
            if (
                not isinstance(tx, dict)
                or type(tx.get("chainId")) is not int
                or tx["chainId"] != CHAIN
                or address(tx["to"]) != AGORA_ROUTER
                or not isinstance(tx["value"], str)
                or not re.fullmatch(r"[0-9]{1,78}", tx["value"])
                or int(tx["value"]) != (amount if i.side == "buy" else 0)
                or not isinstance(tx["data"], str)
                or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){4,16384}", tx["data"])
            ):
                raise ValueError
            approvals = q["approvals"]
            if (
                not isinstance(approvals, list)
                or len(approvals) > 1
                or (i.side == "buy" and approvals)
            ):
                raise ValueError
            for a in approvals:
                if (
                    a.get("kind") != "erc20"
                    or address(a["token"]) != i.token
                    or address(a["spender"]) != AGORA_ROUTER
                    or a["amount"] != str(amount)
                ):
                    raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation):
            raise TradeRefused("Invalid Agora public builder response") from None
        # The opaque aggregator calldata is intentionally never routed to signing/execution.
        return q

    def compare(self, i: TradeIntent, amount: int, decimals: int, slippage: int) -> dict:
        q = self.observation(i, amount, decimals, slippage)
        return {
            "agora_amount_out_wei": int(q["amountOut"]),
            "agora_venue": q["venue"],
            "agora_fee_bps": q["feeBps"],
            "agora_status": "reported_builder_unverified",
        }
