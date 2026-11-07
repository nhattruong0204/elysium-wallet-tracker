"""Private, expiring PAPER intents. No keys, RPC writes, or live execution switch.

Production has no verified quote adapter yet, so review fails closed. Tests inject a
fixture quote provider and allowlist; fixture results must never be shown as live quotes.
Sessions/orders are bounded, in memory, and invalidated by restart; not a live order store.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

CHAIN = 99901
ADDR = re.compile(r'^0x[0-9a-fA-F]{40}$')


class TradeRefused(ValueError):
    pass


class RouteUnavailable(TradeRefused):
    pass


def hype_wei(text: str) -> int:
    if not re.fullmatch(r'[0-9]{1,12}(?:\.[0-9]{1,18})?', text):
        raise TradeRefused('Use a positive plain HYPE amount, at most 18 decimals')
    whole, _, fraction = text.partition('.')
    value = int(whole) * 10**18 + int(fraction.ljust(18, '0') or '0')
    if value <= 0:
        raise TradeRefused('Amount must be positive')
    return value


@dataclass(frozen=True)
class ExecutionRoute:
    chain_id: int
    target: str
    code_hash: str
    kind: str


@dataclass(frozen=True)
class TradeIntent:
    id: str
    user_id: int
    chain_id: int
    token: str
    side: str
    source_chat: int
    source_message: int
    expires_at: float
    amount_wei: int | None = None
    sell_bps: int | None = None
    state: str = 'select'


@dataclass(frozen=True)
class Quote:
    intent_id: str
    chain_id: int
    token: str
    side: str
    amount_in_wei: int
    amount_out_wei: int
    min_out_wei: int
    balance_wei: int
    gas_wei: int
    price_impact_bps: int
    created_at: float
    expires_at: float
    route: ExecutionRoute
    simulated: bool
    native_balance_wei: int = 0
    fee_wei: int = 0


@dataclass(frozen=True)
class SignedTransaction:
    """Future interface only; PAPER code never creates or accepts signed bytes."""
    chain_id: int
    tx_hash: str


@dataclass(frozen=True)
class Order:
    intent_id: str
    user_id: int
    mode: str = 'PAPER'
    status: str = 'hypothetical_only'


@dataclass(frozen=True)
class ExecutionAttempt:
    order_id: str
    status: str


@dataclass(frozen=True)
class Receipt:
    tx_hash: str
    status: str


class QuoteProvider(Protocol):
    def quote(self, intent: TradeIntent, slippage_bps: int) -> Quote: ...


class PaperFlow:
    def __init__(self, allowed: set[int], *, provider: QuoteProvider | None = None,
                 routes: frozenset[ExecutionRoute] = frozenset(),
                 now: Callable[[], float] = time.time, ttl: int = 180,
                 max_buy_wei: int = 10**18):
        if type(ttl) is not int or type(max_buy_wei) is not int or not 0 < ttl <= 180 or not 0 < max_buy_wei <= 10**18:
            raise TradeRefused('Invalid paper limits')
        self.allowed = frozenset(allowed)
        self.provider, self.routes, self.now = provider, routes, now
        self.ttl, self.max_buy_wei = ttl, max_buy_wei
        self._intents: dict[str, TradeIntent] = {}
        self._quotes: dict[str, tuple[Quote, int]] = {}
        self._lock = threading.RLock()
        self.paused = False

    def _auth(self, uid: int, chat: int) -> None:
        if self.paused:
            raise TradeRefused("Paper trading paused")
        if type(uid) is not int or type(chat) is not int or uid <= 0 or uid not in self.allowed or uid != chat:
            raise TradeRefused('Authorized private chat required')

    def create(self, uid: int, chat: int, token: str, side: str,
               source_chat: int, source_message: int, chain_id: int = CHAIN) -> TradeIntent:
        self._auth(uid, chat)
        if chain_id != CHAIN or not ADDR.fullmatch(token) or side not in ('buy', 'sell'):
            raise TradeRefused('Invalid chain, token or side')
        if type(source_chat) is not int or type(source_message) is not int or source_message <= 0:
            raise TradeRefused('Alert context required')
        with self._lock:
            self._intents = {k: v for k, v in self._intents.items() if v.expires_at > self.now()}
            self._quotes = {k: v for k, v in self._quotes.items() if k in self._intents}
            for i in self._intents.values():
                if (i.user_id, i.token, i.side, i.source_chat, i.source_message) == (
                        uid, token.lower(), side, source_chat, source_message):
                    return i
            if len(self._intents) >= 256:
                raise TradeRefused('Paper session capacity reached; try later')
            i = TradeIntent(secrets.token_urlsafe(12), uid, CHAIN, token.lower(), side,
                            source_chat, source_message, self.now() + self.ttl)
            self._intents[i.id] = i
            return i

    def get(self, uid: int, chat: int, sid: str) -> TradeIntent:
        self._auth(uid, chat)
        with self._lock:
            i = self._intents.get(sid)
            if not i or i.user_id != uid or i.expires_at <= self.now() or i.state in ('cancelled', 'done'):
                raise TradeRefused('Expired, completed or invalid paper session')
            return i

    def select(self, uid: int, chat: int, sid: str, value: str) -> TradeIntent:
        with self._lock:
            i = self.get(uid, chat, sid)
            if i.side == 'buy':
                amount = hype_wei(value)
                if amount > self.max_buy_wei:
                    raise TradeRefused('Paper amount exceeds the per-trade cap')
                i = replace(i, amount_wei=amount, state='select')
            else:
                if value not in ('25', '50', '100'):
                    raise TradeRefused('Sell percent must be 25, 50 or 100 of verified wallet balance')
                i = replace(i, sell_bps=int(value) * 100, state='select')
            self._quotes.pop(sid, None)
            self._intents[sid] = i
            return i

    def _validate(self, i: TradeIntent, q: Quote, slippage: int) -> None:
        now = self.now()
        amounts = (q.amount_in_wei, q.amount_out_wei, q.min_out_wei, q.balance_wei,
                   q.gas_wei, q.price_impact_bps, q.native_balance_wei, q.fee_wei)
        if any(type(v) is not int or v < 0 for v in amounts):
            raise TradeRefused('Invalid integer amount')
        if (q.intent_id, q.chain_id, q.token.lower(), q.side) != (i.id, CHAIN, i.token, i.side):
            raise TradeRefused('Quote binding mismatch')
        if not 0 <= now - q.created_at <= 30 or not now < q.expires_at <= q.created_at + 30:
            raise TradeRefused('Stale or future quote')
        if q.route not in self.routes or q.route.chain_id != CHAIN or q.simulated is not True:
            raise TradeRefused('Unverified route or failed simulation')
        if not 0 <= q.price_impact_bps <= 100 or q.gas_wei > 10**16 or q.amount_out_wei <= 0:
            raise TradeRefused('Unsafe price impact, gas or output')
        expected = i.amount_wei if i.side == 'buy' else q.balance_wei * (i.sell_bps or 0) // 10000
        if not expected or expected != q.amount_in_wei or q.balance_wei < expected:
            raise TradeRefused('Amount or available balance mismatch')
        if i.side == 'sell' and q.native_balance_wei < q.gas_wei:
            raise TradeRefused('Insufficient HYPE for sell gas')
        if i.side == 'buy' and q.balance_wei < expected + q.gas_wei + q.fee_wei:
            raise TradeRefused('Insufficient HYPE including gas')
        if q.min_out_wei != q.amount_out_wei * (10000 - slippage) // 10000 or q.min_out_wei <= 0:
            raise TradeRefused('Invalid minimum output')

    def review(self, uid: int, chat: int, sid: str, slippage: int = 50) -> Quote:
        with self._lock:
            i = self.get(uid, chat, sid)
            # A failed refresh must never leave an older quote confirmable.
            self._quotes.pop(sid, None)
            self._intents[sid] = replace(i, state='select')
            if type(slippage) is not int or not 0 <= slippage <= 100:
                raise TradeRefused('Slippage exceeds paper limit')
            if not self.provider:
                raise RouteUnavailable('No verified quote adapter; no order was quoted, signed or submitted')
            q = self.provider.quote(i, slippage)
            self._validate(i, q, slippage)
            self._quotes[sid] = (q, slippage)
            self._intents[sid] = replace(i, state='review')
            return q

    def confirm(self, uid: int, chat: int, sid: str) -> Order:
        with self._lock:
            i = self.get(uid, chat, sid)
            if i.state != 'review' or sid not in self._quotes:
                raise TradeRefused('Fresh review required')
            q, slippage = self._quotes[sid]
            self._validate(i, q, slippage)
            self._intents[sid] = replace(i, state='done')
            self._quotes.pop(sid, None)
            return Order(sid, uid)

    def cancel(self, uid: int, chat: int, sid: str) -> None:
        with self._lock:
            i = self.get(uid, chat, sid)
            self._intents[sid] = replace(i, state='cancelled')
            self._quotes.pop(sid, None)

    def broadcast(self, *args: object, **kwargs: object) -> None:
        raise TradeRefused('Live execution is not implemented; security review and explicit approval required')
