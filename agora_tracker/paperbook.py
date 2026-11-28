"""PAPER book: copies buys of top-ranked wallets on Montra tokens with a fixed $100 bankroll.

Owner request 2026-11-28: run $100 as if it were the last $100, through the bot, the database
and what we learned. Nothing here can trade: no keys, no signing, no RPC, no router calldata,
no import of the `trading` package. Fills are simulated from Montra's public pool price
(api.montramarkets.xyz, the same client as market.py) with the pool's own fee, a slippage
allowance and gas. The book is one JSON document in service_state; every entry, exit, daily
summary and the death notice is a Telegram card with the token's Fomo button.

Rules, and the evidence behind each (docs/wiki/How-tracked-wallets-made-money-2026-11-28.md):

* Copy only wallets ranked <= `max_rank` on performance.py's board (30-day measured PnL
  without launch farms and launch snipes). Entries by everyone else after the first minute
  were net negative over the whole history; launches and snipes cannot be copied.
* Montra tokens only: the one launchpad where non-launch trading was net positive, and the
  only one whose pool price, fee tier and anti-snipe window can be read exactly.
* Market cap >= $10k at entry. Below $5k, 84% of Consensus cards were trash; $5-10k, 67%.
* Token at least 3 minutes old, and never while its anti-snipe fee is active (a buy can pay
  up to 99% in a pool's first seconds, falling to the pool fee within 30 minutes).
* Pool fee <= 2% per swap. Montra tiers are 1/2/3/5/10% on buys AND sells, so a 5% pool
  costs ~10% round trip: more than the median copy gain we measured (+7 to +10%).
* Do not chase: skip when the pool price is more than 15% above the leader's own fill, and
  ignore signals older than 2 minutes (the copy edge fell from +9.5% to +0.1% median between
  a 15 s and a 60 s delay).
* Exit when the leader sells (the tested rule; also caught from the leader's position when
  the sell itself was too small to alert), stop at -35%, sell half at +100% and trail the
  rest 30% from its peak, give up after 72 h.
* Size 10% of equity (at least $5), at most 5 positions, one per token, at most 2 copied
  from the same wallet (on 11-28 one wallet made most of the qualifying buys), 24 h
  cooldown after an exit. Equity down 20% in a UTC day halts new entries until the next day. Equity at or
  below $50 is death: everything is closed at the last price and the book stops for good
  (a new book needs a new `book` name).

Cash is held in dollars between trades (as if in a stablecoin), so HYPE's own moves do not
count. Values are indicative: the last pool price, not an executable quote.
"""

from __future__ import annotations

import dataclasses
import html
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import MarketConfig, PaperBookConfig
from .market import UA, MontraMarket
from .performance import tier_of

log = logging.getLogger(__name__)
WEI = 10**18
PIPS = 1_000_000            # Montra fees in pips: 1,000,000 = 100%
VERSION = 1


def _f(v: Any) -> float | None:
    try:
        x = None if v is None else float(v)
    except (TypeError, ValueError):
        return None
    return x if x is not None and x == x else None


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _ms_to_s(v: Any) -> float | None:
    x = _f(v)
    if not x:
        return None
    return x / 1000 if x > 10**11 else x


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def money(v: float, signed: bool = False) -> str:
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ("-" if v < 0 else "")
    return f"{sign}${abs(v):,.2f}"


def compact(v: float | None) -> str:
    if v is None:
        return "n/a"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.2f}{suf}"
    return f"${v:,.0f}"


def reason_code(why: str) -> str:
    """Stable counter key for a skip reason: its words without the numbers in it."""
    words = [w for w in why.split()[:5] if not any(ch.isdigit() for ch in w) and "$" not in w]
    return " ".join(words)[:40] or "other"


def pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def px_str(v: float | None) -> str:
    if not v or v <= 0:
        return "n/a"
    if v >= 1:
        return f"${v:,.4f}"
    s = f"{v:.12f}".rstrip("0")
    head, _, tail = s.partition(".")
    zeros = len(tail) - len(tail.lstrip("0"))
    return f"${head}.{tail[:zeros + 4]}"


def short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else str(a)


# --- market input ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Quote:
    """What a buy and a sell of this token cost right now, from Montra's token detail."""
    price_usd: float              # pool price after the last trade, in dollars per token
    pool_fee: float               # fraction every sell pays (the pool's total fee)
    buy_fee: float                # fraction a buy pays now (> pool_fee inside an anti-snipe window)
    mcap_usd: float | None
    launched_at: float | None     # unix seconds
    symbol: str | None
    quote: str | None


def quote_from(detail: dict | None, now: float) -> Quote | None:
    """Quote from Montra's /api/tokens/<token> (card + pool + live). None without a price."""
    if not isinstance(detail, dict):
        return None
    price = _f(detail.get("priceUsd"))
    if not price or price <= 0:
        return None
    pool, live = _dict(detail.get("pool")), _dict(detail.get("live"))
    fee_pips = _f(detail.get("totalFeePips")) or _f(pool.get("totalFeePips")) or _f(
        live.get("totalFee"))
    if fee_pips is None or fee_pips < 0 or fee_pips > PIPS:
        return None
    pool_fee = fee_pips / PIPS
    buy_fee = pool_fee
    live_buy = _f(live.get("buyEffectiveTotalFee")) or _f(live.get("buyFee"))
    if live_buy is not None and 0 <= live_buy <= PIPS:
        buy_fee = max(buy_fee, live_buy / PIPS)
    # the window's own terms as well (the live read can be missing or a few seconds old):
    # the rate falls in a straight line from the start rate to the pool fee at `until`
    until = _ms_to_s(pool.get("earlyBuyFeeUntil"))
    window = _f(pool.get("earlyBuyFeeWindow")) or 0.0
    start = (_f(pool.get("earlyBuyFeeStartPips")) or 0.0) / PIPS
    if until and now < until and start > pool_fee:
        left = (until - now) / window if window > 0 else 1.0
        buy_fee = max(buy_fee, pool_fee + (start - pool_fee) * min(1.0, max(0.0, left)))
    q = _dict(detail.get("quote"))
    return Quote(price_usd=price, pool_fee=pool_fee, buy_fee=min(buy_fee, 0.99),
                 mcap_usd=_f(detail.get("marketCapUsd")),
                 launched_at=_ms_to_s(detail.get("launchedAt")),
                 symbol=detail.get("symbol"), quote=q.get("symbol"))


@dataclass(frozen=True)
class Signal:
    """One BUY or SELL alert of a tracked wallet (the engine's trade payload)."""
    key: str
    kind: str
    token: str
    wallet: str
    ts: float | None
    hype: float | None
    tokens: float | None
    rank: int | None
    tier: str | None
    label: str | None
    symbol: str | None
    confidence: str
    launcher_token: bool
    cohort_wallets: int
    pos_qty_raw: int | None

    @classmethod
    def from_payload(cls, p: dict) -> Signal | None:
        if not isinstance(p, dict) or p.get("type") != "trade":
            return None
        token, wallet = str(p.get("token") or "").lower(), str(p.get("wallet") or "").lower()
        if not (token.startswith("0x") and len(token) == 42 and wallet.startswith("0x")):
            return None
        dec = int(p.get("decimals") or 18)
        try:
            qty = int(p["qty"]) / 10 ** dec if p.get("qty") is not None else None
            hype = int(p["hype_wei"]) / WEI if p.get("hype_wei") is not None else None
            pos_raw = int(p["pos_qty"]) if p.get("pos_qty") is not None else None
        except (TypeError, ValueError):
            return None
        perf = _dict(p.get("perf"))
        rank = perf.get("rank")
        return cls(key=str(p.get("tx") or "") + ":" + wallet + ":" + token,
                   kind=str(p.get("kind") or ""), token=token, wallet=wallet,
                   ts=_f(p.get("ts")), hype=hype, tokens=qty,
                   rank=int(rank) if isinstance(rank, (int, float)) else None,
                   tier=perf.get("tier"), label=p.get("label"), symbol=p.get("symbol"),
                   confidence=str(p.get("confidence") or ""),
                   launcher_token=bool(p.get("launcher_token")),
                   cohort_wallets=int(p.get("cohort_wallets") or 0), pos_qty_raw=pos_raw)

    def leader_px_usd(self, hype_usd: float | None) -> float | None:
        if not (self.hype and self.tokens and hype_usd):
            return None
        return self.hype * hype_usd / self.tokens


# --- pure rules -----------------------------------------------------------------------------
def new_book(cfg: PaperBookConfig, now: float) -> dict:
    return {"v": VERSION, "book": cfg.book, "status": "alive", "created_at": now,
            "renew_at": now + cfg.life_days * 86400, "bankroll": cfg.bankroll_usd,
            "cash": cfg.bankroll_usd, "open": {}, "closed": [], "cooldown": {}, "seen": [],
            "day": None, "seq": 0, "last_report": None,
            "stats": {"trades": 0, "wins": 0, "losses": 0, "realized": 0.0, "fees": 0.0,
                      "skipped": {}}}


def liquidation(tokens: float, price: float, fee: float, cfg: PaperBookConfig) -> float:
    """Dollars a sell of `tokens` at `price` would bring after slippage, the pool fee and gas."""
    gross = tokens * price * (1 - cfg.slippage)
    return max(0.0, gross * (1 - fee) - cfg.gas_usd)


def equity(book: dict, cfg: PaperBookConfig) -> float:
    return book["cash"] + sum(liquidation(p["tokens"], p["last_px"], p["pool_fee"], cfg)
                              for p in book["open"].values())


def pre_filter(sig: Signal, book: dict, cfg: PaperBookConfig, now: float) -> str | None:
    """Reason to ignore a BUY before asking Montra anything, or None."""
    if book["status"] != "alive":
        return "book is dead"
    if sig.kind != "BUY":
        return "not a buy"
    if sig.confidence != "high":
        return "buy amount not measured"
    if sig.rank is None or sig.rank > cfg.max_rank:
        return f"leader not ranked top {cfg.max_rank}"
    if sig.launcher_token:
        return "token made by a serial launcher"
    if sig.cohort_wallets:
        return "launch-cohort wallets on the token"
    if sig.hype is None or sig.hype < cfg.min_leader_hype:
        return f"leader's buy under {cfg.min_leader_hype:g} HYPE"
    if sig.ts is not None and now - sig.ts > cfg.max_signal_age_s:
        return f"signal older than {cfg.max_signal_age_s:.0f} s"
    if sig.token in book["open"]:
        return "already holding the token"
    if (book["cooldown"].get(sig.token) or 0) > now:
        return "token in cooldown after an exit"
    if sig.key in book["seen"]:
        return "signal already handled"
    if (book.get("day") or {}).get("halted"):
        return "daily loss limit hit"
    if len(book["open"]) >= cfg.max_open:
        return f"{cfg.max_open} positions open"
    if sum(1 for p in book["open"].values() if p.get("leader") == sig.wallet) >= cfg.max_per_leader:
        return f"{cfg.max_per_leader} positions already copied from this wallet"
    if book["cash"] < cfg.min_position_usd:
        return "not enough cash"
    return None


def entry_check(sig: Signal, q: Quote | None, book: dict, cfg: PaperBookConfig, now: float,
                hype_usd: float | None) -> tuple[float | None, str]:
    """(position size in dollars, why) or (None, why not). Run after pre_filter."""
    if q is None:
        return None, "not a Montra token (or no dollar price)"
    if q.mcap_usd is None or q.mcap_usd < cfg.min_mcap_usd:
        return None, f"market cap {compact(q.mcap_usd)} under {compact(cfg.min_mcap_usd)}"
    if q.launched_at is not None and now - q.launched_at < cfg.min_token_age_s:
        return None, f"token younger than {cfg.min_token_age_s / 60:g} min"
    if q.pool_fee * PIPS > cfg.max_fee_pips + 1e-6:
        return None, f"pool fee {q.pool_fee:.0%} above {cfg.max_fee_pips / PIPS:.0%}"
    if q.buy_fee > q.pool_fee + 1e-9:
        return None, f"anti-snipe fee active ({q.buy_fee:.1%} on a buy)"
    lp = sig.leader_px_usd(hype_usd)
    if lp and q.price_usd > lp * (1 + cfg.max_chase):
        return None, f"price {pct(q.price_usd / lp - 1)} above the leader's fill"
    size = max(cfg.min_position_usd, cfg.position_pct * equity(book, cfg))
    size = min(size, book["cash"])
    if size < cfg.min_position_usd:
        return None, "not enough cash"
    return round(size, 2), "ok"


def fill_buy(size_usd: float, q: Quote, cfg: PaperBookConfig) -> tuple[float, float]:
    """(tokens received, fees paid in dollars) for spending `size_usd` including gas."""
    after_gas = max(0.0, size_usd - cfg.gas_usd)
    fee = after_gas * q.buy_fee
    tokens = (after_gas - fee) / (q.price_usd * (1 + cfg.slippage))
    return tokens, fee + cfg.gas_usd


def fill_sell(tokens: float, q_price: float, fee: float, cfg: PaperBookConfig
              ) -> tuple[float, float]:
    """(dollars received, fees paid in dollars) for selling `tokens`."""
    gross = tokens * q_price * (1 - cfg.slippage)
    out = max(0.0, gross * (1 - fee) - cfg.gas_usd)
    return out, gross * fee + cfg.gas_usd


def exit_rule(pos: dict, price: float, cfg: PaperBookConfig, now: float
              ) -> tuple[float, str] | None:
    """(fraction of the open position to sell, reason) when a price rule fires, else None."""
    entry = pos["entry_px"]
    if price <= entry * (1 - cfg.stop_loss):
        return 1.0, f"stop loss ({pct(price / entry - 1)})"
    if not pos["tp_done"] and price >= entry * (1 + cfg.take_profit):
        return cfg.take_profit_fraction, f"take profit ({pct(price / entry - 1)}), half off"
    if pos["tp_done"] and price <= pos["peak_px"] * (1 - cfg.trail):
        return 1.0, f"trailing stop ({pct(price / pos['peak_px'] - 1)} from the peak)"
    if now - pos["opened_at"] >= cfg.time_stop_h * 3600:
        return 1.0, f"time stop ({cfg.time_stop_h:g} h)"
    return None


# --- the book --------------------------------------------------------------------------------
class PaperBook:
    """Runs in the tracker: `on_trade` is called by the engine after each committed alert; the
    work happens on the book's own thread so a slow Montra answer never delays indexing."""

    def __init__(self, store: Any, cfg: PaperBookConfig, market_cfg: MarketConfig,
                 hype_usd: Callable[[], float | None], tz: str = "UTC",
                 now: Callable[[], float] = time.time,
                 montra: MontraMarket | None = None):
        self.store, self.cfg, self.hype_usd, self.now = store, cfg, hype_usd, now
        self.tz = ZoneInfo(tz)
        if montra is None:
            session = requests.Session()
            session.headers.update({"user-agent": UA, "accept": "application/json"})
            montra = MontraMarket(dataclasses.replace(market_cfg, cache_s=cfg.quote_cache_s),
                                  session)
        self.montra = montra
        self.key = f"paper:{cfg.book}"
        self.q: queue.Queue[dict] = queue.Queue(maxsize=1000)
        self._next_poll = 0.0
        self.book = self._load()

    # persistence ---------------------------------------------------------------------------
    def _load(self) -> dict:
        raw = self.store.get_kv(self.key)
        if raw:
            try:
                book = json.loads(raw)
                if isinstance(book, dict) and book.get("v") == VERSION:
                    log.info("paper book %s: %s, cash %s, %d open", self.cfg.book,
                             book["status"], money(book["cash"]), len(book["open"]))
                    return book
            except (ValueError, KeyError, TypeError):
                log.exception("paper book %s unreadable; starting a new one", self.cfg.book)
        book = new_book(self.cfg, self.now())
        self._card("born", "🧪 <b>PAPER BOOK OPENED</b> · " + esc(self.cfg.book) + "\n"
                   f"💵 Bankroll {money(self.cfg.bankroll_usd)} · {self._rules_line()}\n"
                   f"☠️ Dies at {money(self.cfg.death_equity_usd)} · renewal check "
                   f"{self._local(book['renew_at']):%d %b}", book=book)
        self.store.set_kv(self.key, json.dumps(book))
        return book

    def _save(self) -> None:
        self.store.set_kv(self.key, json.dumps(self.book))

    def _rules_line(self) -> str:
        c = self.cfg
        return (f"copies top-{c.max_rank} wallets on Montra · MC ≥ {compact(c.min_mcap_usd)} · "
                f"fee ≤ {c.max_fee_pips / PIPS:.0%} · {c.position_pct:.0%} per trade · "
                f"stop -{c.stop_loss:.0%}")

    def _local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, timezone.utc).astimezone(self.tz)

    def _card(self, kind: str, text: str, token: str | None = None,
              book: dict | None = None) -> None:
        b = book or self.book
        b["seq"] = int(b.get("seq") or 0) + 1
        payload = {"type": "paper", "kind": kind, "text": text, "book": self.cfg.book}
        if token:
            payload["token"] = token
        try:
            self.store.enqueue_now(f"paper:{self.cfg.book}:{b['seq']}:{kind}", text,
                                   severity="info", payload=payload)
        except Exception:  # noqa: BLE001 - a card must never break the book
            log.exception("paper card %s not queued", kind)

    # engine hook ---------------------------------------------------------------------------
    def on_trade(self, payload: dict) -> None:
        """Called on the engine's thread after the alert's transaction committed."""
        if not isinstance(payload, dict) or payload.get("kind") not in ("BUY", "SELL"):
            return
        try:
            self.q.put_nowait(payload)
        except queue.Full:
            log.warning("paper book queue full: signal dropped")

    def run(self, stop: threading.Event) -> None:
        log.info("paper book %s running (%s)", self.cfg.book, self._rules_line())
        while not stop.is_set():
            try:
                payload = self.q.get(timeout=1.0)
            except queue.Empty:
                payload = None
            try:
                if payload is not None:
                    self.handle(payload)
                if time.monotonic() >= self._next_poll:
                    self._next_poll = time.monotonic() + self.cfg.poll_s
                    self.poll()
            except Exception:  # noqa: BLE001 - keep the book alive; the next step retries
                log.exception("paper book step failed")

    # signals -------------------------------------------------------------------------------
    def handle(self, payload: dict) -> None:
        sig = Signal.from_payload(payload)
        if sig is None or self.book["status"] != "alive":
            return
        now = self.now()
        self._roll_day(now)
        if sig.kind == "SELL":
            pos = self.book["open"].get(sig.token)
            if pos and pos["leader"] == sig.wallet:
                q = quote_from(self.montra.card(sig.token), now)    # the price after their sell
                if q is not None:
                    pos["last_px"], pos["last_px_at"] = q.price_usd, now
                self._exit(pos, 1.0, "leader sold", now, q.price_usd if q else None)
                self._after_change(now)
            return
        why = pre_filter(sig, self.book, self.cfg, now)
        if why is not None:
            if sig.rank is not None and sig.rank <= self.cfg.max_rank:
                self._skip(sig, why)
            return
        q = quote_from(self.montra.card(sig.token), now)
        size, why = entry_check(sig, q, self.book, self.cfg, now, self.hype_usd())
        self._remember(sig.key)
        if size is None or q is None:
            self._skip(sig, why)
            self._save()
            return
        self._enter(sig, q, size, now)
        self._after_change(now)

    def _remember(self, key: str) -> None:
        seen = self.book["seen"]
        seen.append(key)
        del seen[:-300]

    def _skip(self, sig: Signal, why: str) -> None:
        sk = self.book["stats"].setdefault("skipped", {})
        reason = reason_code(why)
        sk[reason] = int(sk.get(reason, 0)) + 1
        log.info("paper: skip %s by %s (rank %s): %s", sig.symbol or sig.token[:10],
                 short(sig.wallet), sig.rank, why)

    def _enter(self, sig: Signal, q: Quote, size: float, now: float) -> None:
        tokens, fees = fill_buy(size, q, self.cfg)
        b = self.book
        b["cash"] = round(b["cash"] - size, 6)
        pid = int(b["stats"]["trades"]) + len(b["open"]) + 1
        lp = sig.leader_px_usd(self.hype_usd())
        pos = {"id": pid, "token": sig.token, "symbol": q.symbol or sig.symbol or sig.token[:8],
               "leader": sig.wallet, "label": sig.label, "rank": sig.rank, "tier": sig.tier,
               "leader_px": lp, "leader_hype": sig.hype, "leader_qty_raw": sig.pos_qty_raw,
               "opened_at": now, "entry_px": q.price_usd, "cost": size, "cost_open": size,
               "tokens": tokens, "fees": fees, "pool_fee": q.pool_fee, "quote": q.quote,
               "mcap_usd": q.mcap_usd, "peak_px": q.price_usd, "last_px": q.price_usd,
               "last_px_at": now, "tp_done": False, "realized": 0.0}
        b["open"][sig.token] = pos
        b["stats"]["fees"] = round(b["stats"]["fees"] + fees, 6)
        t = tier_of(sig.rank)
        badge = f"{t[1]} {t[2]} #{sig.rank}" if t else f"#{sig.rank}"
        who = esc(sig.label) if sig.label else f"<code>{short(sig.wallet)}</code>"
        lead = (f" · their fill {px_str(lp)} ({pct(q.price_usd / lp - 1)} since)" if lp else "")
        eq = equity(b, self.cfg)
        self._card("open", "\n".join([
            f"🧪 <b>PAPER BUY · ${esc(pos['symbol'])}</b> · trade #{pid}",
            f"{badge} {who} bought {sig.hype:,.1f} HYPE{lead}",
            f"💵 {money(size)} → {tokens:,.0f} @ {px_str(q.price_usd)} "
            f"(fee {q.buy_fee:.1%}, slippage {self.cfg.slippage:.1%})",
            f"📊 MC {compact(q.mcap_usd)} · pool fee {q.pool_fee:.0%}"
            + (f" · quote {esc(q.quote)}" if q.quote else ""),
            f"🛡 Stop {px_str(q.price_usd * (1 - self.cfg.stop_loss))} "
            f"(-{self.cfg.stop_loss:.0%}) · half off at +{self.cfg.take_profit:.0%} · "
            f"out when they sell · max {self.cfg.time_stop_h:g} h",
            f"💼 Cash {money(b['cash'])} · equity {money(eq)} · open {len(b['open'])}/"
            f"{self.cfg.max_open}"]), token=sig.token)
        log.info("paper: BUY %s %s for %s at %s (leader %s rank %s)", pos["symbol"],
                 sig.token, money(size), px_str(q.price_usd), short(sig.wallet), sig.rank)

    def _exit(self, pos: dict, fraction: float, reason: str, now: float,
              price: float | None = None) -> None:
        b, cfg = self.book, self.cfg
        price = price if price is not None else pos["last_px"]
        fraction = min(1.0, max(0.0, fraction))
        sell = pos["tokens"] * fraction
        out, fees = fill_sell(sell, price, pos["pool_fee"], cfg)
        basis = pos["cost_open"] * fraction
        pnl = out - basis
        b["cash"] = round(b["cash"] + out, 6)
        b["stats"]["fees"] = round(b["stats"]["fees"] + fees, 6)
        pos["tokens"] -= sell
        pos["cost_open"] -= basis
        pos["realized"] += pnl
        pos["fees"] += fees
        full = fraction >= 1.0 or pos["tokens"] * price < cfg.min_position_usd / 10
        if not full:
            pos["tp_done"] = True
            self._card("partial", "\n".join([
                f"🧪 <b>PAPER SELL {fraction:.0%} · ${esc(pos['symbol'])}</b> · trade "
                f"#{pos['id']}", f"✂️ {esc(reason)} at {px_str(price)}",
                f"💵 {money(out)} back · {money(pnl, True)} on this part · rest trails "
                f"{cfg.trail:.0%} from the peak",
                f"💼 Cash {money(b['cash'])} · equity {money(equity(b, cfg))}"]),
                token=pos["token"])
            return
        if pos["tokens"] > 0:              # dust left by a partial: close it with the rest
            dust, dfees = fill_sell(pos["tokens"], price, pos["pool_fee"], cfg)
            b["cash"] = round(b["cash"] + dust, 6)
            pos["realized"] += dust - pos["cost_open"]
            pos["fees"] += dfees
            pos["tokens"], pos["cost_open"] = 0.0, 0.0
        total = pos["realized"]
        ret = total / pos["cost"] if pos["cost"] else 0.0
        st = b["stats"]
        st["trades"] += 1
        st["wins" if total > 0 else "losses"] += 1
        st["realized"] = round(st["realized"] + total, 6)
        b["open"].pop(pos["token"], None)
        b["cooldown"][pos["token"]] = now + cfg.cooldown_h * 3600
        held = now - pos["opened_at"]
        closed = {k: pos[k] for k in ("id", "token", "symbol", "leader", "rank", "opened_at",
                                      "entry_px", "cost", "fees")}
        closed.update(closed_at=now, exit_px=price, pnl=round(total, 6), ret=round(ret, 6),
                      reason=reason)
        b["closed"].append(closed)
        del b["closed"][:-100]
        eq = equity(b, cfg)
        icon = "✅" if total > 0 else "🔻"
        self._card("close", "\n".join([
            f"🧪 <b>PAPER SELL · ${esc(pos['symbol'])}</b> · trade #{pos['id']} closed",
            f"🚪 {esc(reason)} at {px_str(price)} (entry {px_str(pos['entry_px'])})",
            f"{icon} {money(total, True)} ({pct(ret)}) after {money(pos['fees'])} fees · held "
            f"{_dur(held)}",
            f"💼 Equity {money(eq)} ({pct(eq / b['bankroll'] - 1)} on "
            f"{money(b['bankroll'])}) · {st['wins']}W/{st['losses']}L"]), token=pos["token"])
        log.info("paper: SELL %s %s: %s, %s", pos["symbol"], reason, money(total, True),
                 pct(ret))

    # periodic ------------------------------------------------------------------------------
    def poll(self) -> None:
        if self.book["status"] != "alive":
            return
        now = self.now()
        self._roll_day(now)
        changed = False
        for token, pos in list(self.book["open"].items()):
            q = quote_from(self.montra.card(token), now)
            if q is not None:
                pos["last_px"], pos["last_px_at"] = q.price_usd, now
                pos["peak_px"] = max(pos["peak_px"], q.price_usd)
                pos["pool_fee"] = q.pool_fee
                changed = True
                rule = exit_rule(pos, q.price_usd, self.cfg, now)
                if rule:
                    self._exit(pos, rule[0], rule[1], now, q.price_usd)
                    continue
            elif now - pos["opened_at"] >= self.cfg.time_stop_h * 3600:
                self._exit(pos, 1.0, f"time stop ({self.cfg.time_stop_h:g} h, last price)", now)
                changed = True
                continue
            if self._leader_left(pos):
                self._exit(pos, 1.0, "leader sold (position check)", now)
                changed = True
        self._after_change(now, report=True, changed=changed)

    def _leader_left(self, pos: dict) -> bool:
        """The leader's own position dropped by half or more since our entry (a sell below
        the alert minimum, a transfer out, or one we missed while the tracker was down)."""
        start = pos.get("leader_qty_raw")
        if not start:
            return False
        try:
            now_qty = self.store.position_qty(pos["leader"], pos["token"])
        except Exception:  # noqa: BLE001 - a failed read is not a sell
            log.warning("paper: leader position read failed", exc_info=True)
            return False
        return now_qty is not None and now_qty <= int(start) // 2

    def _roll_day(self, now: float) -> None:
        today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if (self.book.get("day") or {}).get("date") != today:
            self.book["day"] = {"date": today, "start_equity": round(equity(self.book, self.cfg),
                                                                     6), "halted": False}

    def _after_change(self, now: float, report: bool = False, changed: bool = True) -> None:
        b, cfg = self.book, self.cfg
        eq = equity(b, cfg)
        day = b.get("day") or {}
        if b["status"] == "alive" and eq <= cfg.death_equity_usd:
            for pos in list(b["open"].values()):
                self._exit(pos, 1.0, "death: equity at the floor", now)
            b["status"] = "dead"
            st = b["stats"]
            self._card("dead", "\n".join([
                f"☠️ <b>PAPER BOOK DEAD</b> · {esc(cfg.book)}",
                f"Equity {money(equity(b, cfg))} hit the {money(cfg.death_equity_usd)} floor "
                f"after {st['trades']} trades ({st['wins']}W/{st['losses']}L, "
                f"{money(st['fees'])} fees).", "No more trades. A new book needs a new name."]))
        elif day and not day.get("halted") and eq < day["start_equity"] * (1 - cfg.daily_loss_limit):
            day["halted"] = True
            self._card("halt", f"⏸ <b>PAPER: daily loss limit</b> · equity {money(eq)} is "
                       f"{pct(eq / day['start_equity'] - 1)} today. No new entries until "
                       "00:00 UTC; exits keep running.")
        if report:
            self._maybe_report(now)
        if changed or report:
            self._save()

    def _maybe_report(self, now: float) -> None:
        b, cfg = self.book, self.cfg
        local = self._local(now)
        today = local.strftime("%Y-%m-%d")
        if b.get("last_report") is None:
            b["last_report"] = today
            return
        if b["last_report"] == today:
            return
        b["last_report"] = today
        eq = equity(b, cfg)
        st = b["stats"]
        left = max(0.0, (b["renew_at"] - now) / 86400)
        lines = [f"🧪 <b>PAPER BOOK · {local:%d %b}</b> · {esc(cfg.book)} · "
                 + ("🫀 alive" if b["status"] == "alive" else "☠️ dead"),
                 f"💼 Equity {money(eq)} ({pct(eq / b['bankroll'] - 1)}) · cash {money(b['cash'])}",
                 f"📒 {st['trades']} closed · {st['wins']}W/{st['losses']}L · realized "
                 f"{money(st['realized'], True)} · fees {money(st['fees'])}",
                 f"⏳ Renewal check in {left:.0f} days: needs ≥ {money(b['bankroll'])}"]
        for pos in b["open"].values():
            v = liquidation(pos["tokens"], pos["last_px"], pos["pool_fee"], cfg)
            lines.append(f"• ${esc(pos['symbol'])}: {money(v)} "
                         f"({pct((v + pos['realized']) / pos['cost'] - 1)}), held "
                         f"{_dur(now - pos['opened_at'])}")
        sk = st.get("skipped") or {}
        if sk:
            top = sorted(sk.items(), key=lambda kv: -kv[1])[:3]
            lines.append("🚫 Skipped: " + ", ".join(f"{esc(k)} ×{v}" for k, v in top))
        self._card("daily", "\n".join(lines))


def _dur(s: float) -> str:
    s = max(0, int(s))
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600}h"
