"""Official public API market observations, deliberately not executable Quotes.

Source: https://docs.agoradex.app/developers and linked OpenAPI v1. No recipient,
calldata, key, approval, wallet balance, gas or simulation is requested or accepted.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests

from .paper import CHAIN, TradeIntent, TradeRefused


@dataclass(frozen=True)
class IndicativeQuote:
    token: str
    amount_in_wei: int
    amount_out_raw: int
    min_out_raw: int
    venue: str
    fee_bps: int
    impact_bps: Decimal
    observed_at: float
    chain_id: int = CHAIN


class AgoraIndicative:
    """One bounded GET, no retries; shared client budget below 30 requests/minute."""
    def __init__(self, *, now=time.time, monotonic=time.monotonic):
        self.now, self.monotonic = now, monotonic
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._next_request = 0.

    def quote(self, intent: TradeIntent) -> IndicativeQuote:
        if intent.chain_id != CHAIN or intent.side != 'buy' or not intent.amount_wei:
            raise TradeRefused('Indicative buys need a HYPE amount; sells require verified token decimals/balance')
        if not re.fullmatch(r'0x[0-9a-fA-F]{40}', intent.token) or not 0 < intent.amount_wei <= 10**18:
            raise TradeRefused('Invalid indicative token or amount')
        whole, fraction = divmod(intent.amount_wei, 10**18)
        amount = f'{whole}.{fraction:018d}'
        # This transport is independent of scanner RPCs and uses no private credentials.
        if not self._lock.acquire(blocking=False):
            raise TradeRefused('Indicative quote busy; try later')
        try:
            if self.monotonic() < self._next_request:
                raise TradeRefused('Indicative quote rate limit; try later')
            self._next_request = self.monotonic() + 2.1
            try:
                r = self.session.get('https://api.agoradex.app/api/v1/quote',
                    params={'token': intent.token, 'side': 'buy', 'amount': amount,
                            'slippageBps': '50'}, timeout=5, allow_redirects=False)
                if r.status_code == 429:
                    self._next_request = self.monotonic() + 60
                    raise TradeRefused('Quote provider rate limited; try in a minute')
                r.raise_for_status()
                if r.status_code != 200:
                    raise TradeRefused('Quote provider unavailable')
                q = r.json()
            except TradeRefused:
                raise
            except (requests.RequestException, ValueError):
                raise TradeRefused('Indicative quote unavailable') from None
        finally:
            self._lock.release()
        try:
            if not isinstance(q, dict) or q.get('token', '').lower() != intent.token or q.get('side') != 'buy':
                raise ValueError
            amounts = []
            for key in ('amountIn', 'amountOut', 'minAmountOut'):
                value = q[key]
                if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,78}', value):
                    raise ValueError
                amounts.append(int(value))
            n, out, minimum = amounts
            if n != intent.amount_wei or not 0 < minimum <= out or minimum != out * 9950 // 10000:
                raise ValueError
            fee, venue = q['feeBps'], q['venue']
            impact = Decimal(str(q['priceImpact'])) * 10000
            if (type(fee) is not int or not 0 <= fee <= 100 or not impact.is_finite()
                    or not -10000 <= impact <= 10000 or not isinstance(venue, str) or not 0 < len(venue) <= 120):
                raise ValueError
            if q.get('transaction') is not None or q.get('approvals'):
                raise ValueError  # indicative endpoint must not supply actionable payloads
        except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
            raise TradeRefused('Invalid indicative quote; no order reviewed') from None
        return IndicativeQuote(intent.token, n, out, minimum, venue, fee, impact, self.now())
