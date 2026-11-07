"""Private, expiring PAPER intents. No keys, RPC writes, or live execution switch.

Explicit PAPER configuration can use runtime-verified direct V2 reads and simulations.
Test fixture providers remain separate; neither quotes nor confirmations authorize execution.
Sessions are bounded, in memory and invalidated by restart. Optional private journals
persist hypothetical records only; they are not live order stores.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
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
    wallet: str | None = None


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
    gas_wei: int | None
    price_impact_bps: int
    created_at: float
    expires_at: float
    route: ExecutionRoute
    simulated: bool
    native_balance_wei: int = 0
    fee_wei: int = 0
    wallet: str | None = None
    token_decimals: int | None = None
    simulation_status: str = 'success'
    approval_required_wei: int = 0
    approval_spender: str | None = None
    block_number: int | None = None
    block_hash: str | None = None
    deadline: int | None = None
    transaction: dict | None = None
    gas_estimate: int | None = None
    gas_price_wei: int | None = None
    pool: str | None = None
    pool_fee_bps: int | None = None
    quote_latency_ms: float | None = None
    agora_amount_out_wei: int | None = None
    agora_venue: str | None = None
    agora_fee_bps: int | None = None
    agora_status: str = 'not_requested'


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


class OrderJournal(Protocol):
    def save(self, order: Order, intent: TradeIntent, quote: Quote) -> None: ...
    def recent(self, uid: int) -> list[dict]: ...


class PaperFlow:
    def __init__(self, allowed: set[int], *, provider: QuoteProvider | None = None,
                 routes: frozenset[ExecutionRoute] = frozenset(),
                 now: Callable[[], float] = time.time, ttl: int = 180,
                 max_buy_wei: int = 10**18, journal: OrderJournal | None = None):
        if type(ttl) is not int or type(max_buy_wei) is not int or not 0 < ttl <= 180 or not 0 < max_buy_wei <= 10**18:
            raise TradeRefused('Invalid paper limits')
        self.allowed = frozenset(allowed)
        self.provider, self.routes, self.now = provider, routes, now
        self.ttl, self.max_buy_wei = ttl, max_buy_wei
        self._intents: dict[str, TradeIntent] = {}
        self._quotes: dict[str, tuple[Quote, int]] = {}
        self._review_tokens: dict[str, str] = {}
        self._quote_digests: dict[str, str] = {}
        self._pending_reviews: dict[str, str] = {}
        self._lock = threading.RLock()
        self.paused = False
        self.journal = journal
        self._wallets: dict[int, str] = {}

    def _auth(self, uid: int, chat: int, *, allow_paused: bool = False) -> None:
        if self.paused and not allow_paused:
            raise TradeRefused("Paper trading paused")
        if type(uid) is not int or type(chat) is not int or uid <= 0 or uid not in self.allowed or uid != chat:
            raise TradeRefused('Authorized private chat required')

    def set_wallet(self, uid: int, chat: int, wallet: str) -> None:
        from .chain import address
        self._auth(uid, chat)
        wallet = address(wallet)
        with self._lock:
            if self.journal and hasattr(self.journal, 'set_wallet'):
                self.journal.set_wallet(uid, wallet)
            self._wallets[uid] = wallet
            # A wallet change revokes every prior review and intent for this caller.
            for sid, i in list(self._intents.items()):
                if i.user_id == uid:
                    self._intents[sid] = replace(i, state='cancelled')
                    self._quotes.pop(sid, None)
                    self._review_tokens.pop(sid, None)
                    self._quote_digests.pop(sid, None)
                    self._pending_reviews.pop(sid, None)

    def wallet(self, uid: int, chat: int) -> str | None:
        self._auth(uid, chat, allow_paused=True)
        if self.journal and hasattr(self.journal, 'wallet'):
            return self.journal.wallet(uid)
        return self._wallets.get(uid)

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
            self._review_tokens = {k: v for k, v in self._review_tokens.items() if k in self._quotes}
            self._quote_digests = {k: v for k, v in self._quote_digests.items() if k in self._quotes}
            self._pending_reviews = {k: v for k, v in self._pending_reviews.items() if k in self._intents}
            for i in self._intents.values():
                if (i.user_id, i.token, i.side, i.source_chat, i.source_message) == (
                        uid, token.lower(), side, source_chat, source_message):
                    return i
            if len(self._intents) >= 256:
                raise TradeRefused('Paper session capacity reached; try later')
            i = TradeIntent(secrets.token_urlsafe(12), uid, CHAIN, token.lower(), side,
                            source_chat, source_message, self.now() + self.ttl, wallet=self.wallet(uid, chat))
            self._intents[i.id] = i
            return i

    def get(self, uid: int, chat: int, sid: str, *, allow_paused: bool = False) -> TradeIntent:
        self._auth(uid, chat, allow_paused=allow_paused)
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
                if not re.fullmatch(r'[0-9]{1,3}(?:\.[0-9]{1,2})?', value):
                    raise TradeRefused('Sell percent must be positive, at most 100 with two decimals')
                whole, _, fraction = value.partition('.')
                bps = int(whole) * 100 + int(fraction.ljust(2, '0') or '0')
                if not 0 < bps <= 10000:
                    raise TradeRefused('Sell percent must be positive and at most 100')
                i = replace(i, sell_bps=bps, state='select')
            self._quotes.pop(sid, None)
            self._review_tokens.pop(sid, None)
            self._quote_digests.pop(sid, None)
            self._pending_reviews.pop(sid, None)
            self._intents[sid] = i
            return i

    def _validate(self, i: TradeIntent, q: Quote, slippage: int) -> None:
        now = self.now()
        amounts = (q.amount_in_wei, q.amount_out_wei, q.min_out_wei, q.balance_wei,
                   q.price_impact_bps, q.native_balance_wei, q.fee_wei, q.approval_required_wei)
        if any(type(v) is not int or v < 0 for v in amounts):
            raise TradeRefused('Invalid integer amount')
        if (q.intent_id, q.chain_id, q.token.lower(), q.side) != (i.id, CHAIN, i.token, i.side):
            raise TradeRefused('Quote binding mismatch')
        if not 0 <= now - q.created_at <= 30 or not now < q.expires_at <= q.created_at + 30:
            raise TradeRefused('Stale or future quote')
        conditional = (i.side == 'sell' and q.simulation_status == 'approval_required'
            and q.simulated is False and q.wallet == i.wallet and q.wallet is not None
            and q.approval_required_wei == q.amount_in_wei
            and q.approval_spender == q.route.target and q.transaction is not None
            and q.gas_wei is None and q.gas_estimate is None and q.gas_price_wei is None)
        if q.route not in self.routes or q.route.chain_id != CHAIN or (q.simulated is not True and not conditional):
            raise TradeRefused('Unverified route or failed simulation')
        if q.route.kind == 'canonical-v2':
            from .v2 import validate_plan
            validate_plan(i, q, now)
        if i.wallet != q.wallet:
            raise TradeRefused('Quote wallet binding mismatch')
        if q.gas_wei is None and not conditional:
            raise TradeRefused('Missing verified gas estimate')
        if q.gas_wei is not None and (type(q.gas_wei) is not int or q.gas_wei < 0):
            raise TradeRefused('Invalid gas amount')
        if q.wallet and (q.token_decimals is None or q.transaction is None or q.block_number is None):
            raise TradeRefused('Incomplete verified quote')
        if not 0 <= q.price_impact_bps <= 100 or (q.gas_wei or 0) > 10**16 or q.amount_out_wei <= 0:
            raise TradeRefused('Unsafe price impact, gas or output')
        expected = i.amount_wei if i.side == 'buy' else q.balance_wei * (i.sell_bps or 0) // 10000
        if not expected or expected != q.amount_in_wei or q.balance_wei < expected:
            raise TradeRefused('Amount or available balance mismatch')
        if i.side == 'sell' and q.native_balance_wei < (q.gas_wei or 0):
            raise TradeRefused('Insufficient HYPE for sell gas')
        if i.side == 'buy' and q.balance_wei < expected + (q.gas_wei or 0) + q.fee_wei:
            raise TradeRefused('Insufficient HYPE including gas')
        if q.min_out_wei != q.amount_out_wei * (10000 - slippage) // 10000 or q.min_out_wei <= 0:
            raise TradeRefused('Invalid minimum output')

    @staticmethod
    def _digest(q: Quote) -> str:
        return hashlib.sha256(json.dumps(asdict(q), sort_keys=True, allow_nan=False).encode()).hexdigest()

    def review(self, uid: int, chat: int, sid: str, slippage: int = 50) -> Quote:
        with self._lock:
            i = self.get(uid, chat, sid)
            self._quotes.pop(sid, None)
            self._review_tokens.pop(sid, None)
            self._quote_digests.pop(sid, None)
            generation = secrets.token_urlsafe(8)
            self._pending_reviews[sid] = generation
            i = replace(i, state='select')
            self._intents[sid] = i
            if type(slippage) is not int or not 0 <= slippage <= 100:
                raise TradeRefused('Slippage exceeds paper limit')
            provider = self.provider
            if not provider:
                raise RouteUnavailable('No verified quote adapter; no order was quoted, signed or submitted')
        # No scanner transaction or global session lock is held during network I/O.
        q = provider.quote(i, slippage)
        with self._lock:
            current = self.get(uid, chat, sid)
            if current != i or self._pending_reviews.get(sid) != generation:
                raise TradeRefused('Selection changed while quoting; refresh')
            self._validate(i, q, slippage)
            self._quotes[sid] = (q, slippage)
            self._quote_digests[sid] = self._digest(q)
            self._review_tokens[sid] = secrets.token_urlsafe(8)
            self._pending_reviews.pop(sid, None)
            self._intents[sid] = replace(i, state='review')
            return q

    def reviewed(self, uid: int, chat: int, sid: str) -> Quote | None:
        with self._lock:
            i = self.get(uid, chat, sid)
            if i.state != 'review' or sid not in self._quotes:
                return None
            q, slippage = self._quotes[sid]
            self._validate(i, q, slippage)
            return q

    def orders(self, uid: int, chat: int) -> list[dict]:
        self._auth(uid, chat, allow_paused=True)
        return self.journal.recent(uid) if self.journal else []

    def confirmation_token(self, uid: int, chat: int, sid: str) -> str | None:
        with self._lock:
            self.get(uid, chat, sid)
            return self._review_tokens.get(sid)

    def confirm(self, uid: int, chat: int, sid: str, review_token: str | None = None) -> Order:
        with self._lock:
            i = self.get(uid, chat, sid)
            if i.state != 'review' or sid not in self._quotes:
                raise TradeRefused('Fresh review required')
            if not review_token or not secrets.compare_digest(review_token, self._review_tokens.get(sid, '')):
                raise TradeRefused('Review changed; open the latest review before confirming')
            q, slippage = self._quotes[sid]
            if self._digest(q) != self._quote_digests.get(sid):
                raise TradeRefused('Reviewed quote changed; refresh required')
            self._validate(i, q, slippage)
            order = Order(sid, uid, status=('hypothetical_requires_approval'
                if q.simulation_status == 'approval_required' else 'hypothetical_only'))
            if self.journal:
                self.journal.save(order, i, q)
            self._intents[sid] = replace(i, state='done')
            self._quotes.pop(sid, None)
            self._review_tokens.pop(sid, None)
            self._quote_digests.pop(sid, None)
            self._pending_reviews.pop(sid, None)
            return order

    def cancel(self, uid: int, chat: int, sid: str) -> None:
        with self._lock:
            i = self.get(uid, chat, sid, allow_paused=True)
            self._intents[sid] = replace(i, state='cancelled')
            self._quotes.pop(sid, None)
            self._review_tokens.pop(sid, None)
            self._quote_digests.pop(sid, None)
            self._pending_reviews.pop(sid, None)

    def broadcast(self, *args: object, **kwargs: object) -> None:
        raise TradeRefused('Live execution is not implemented; security review and explicit approval required')
