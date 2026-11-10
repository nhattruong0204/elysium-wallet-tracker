"""Applies trade events to state (positions, cluster detection) and turns them into alerts.

Everything for one block range is written in a single DB transaction together with the
cursor, so a crash mid-range replays cleanly and never double-alerts.
"""

from __future__ import annotations

import html
import logging
import time
from decimal import Decimal

from . import cards
from .cards import Links
from .chain import ScanResult, TokenCache
from .classify import TradeEvent
from .config import AlertConfig, ChainConfig, IntelConfig, LinksConfig
from .db import BASIS_ESTIMATED, BASIS_KNOWN, BASIS_PARTIAL, Position, Store
from .launchintel import count_independent, tier_label
from .leaderboard import Trader

log = logging.getLogger(__name__)
WEI = 10 ** 18
ICON = {"BUY": "🟢", "SELL": "🔴", "SWAP": "🔁", "TRANSFER_IN": "📥", "TRANSFER_OUT": "📤",
        "MULTI": "🧩"}


def human(x: float) -> str:
    a = abs(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{x / div:.2f}{suf}"
    if a >= 1:
        return f"{x:.2f}"
    return f"{x:.4g}"


def fmt_value(v: float, unit: str) -> str:
    """Leaderboard figures in the unit the source reports (Agora: HYPE)."""
    return f"${human(v)}" if unit.upper() == "USD" else f"{human(v)} {unit}"


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


class Engine:
    def __init__(self, store: Store, tokens: TokenCache, chain_cfg: ChainConfig,
                 alert_cfg: AlertConfig, labels: dict[str, str], value_unit: str = "USD",
                 links: Links | None = None, intel: IntelConfig | None = None):
        self.store, self.tokens = store, tokens
        self.intel = intel or IntelConfig()
        self._muted = set(alert_cfg.mute_tokens)
        self._tg_muted = set(alert_cfg.telegram_mute_tokens)   # Telegram-only (signals kept)
        self._bundles_at = float("-inf")
        self._bundles: set[str] = set()
        self._bots_at = float("-inf")
        self._bots: set[str] = set()
        self.chain_cfg, self.cfg = chain_cfg, alert_cfg
        self.labels = labels
        self.value_unit = value_unit
        self.links = links or Links(LinksConfig())

    # --- alert payloads -------------------------------------------------------------
    # Alerts are stored as structured payloads and rendered by cards.py: once here (fallback
    # text, no market data) and again by the notifier right before sending (fresh data).
    def _wallet_ctx(self, wallet: str) -> dict:
        row = self.store.trader(wallet)
        return {"wallet": wallet, "label": self.labels.get(wallet),
                "rank": row["rank"] if row is not None and row["in_top"] else None,
                "pnl": row["pnl"] if row is not None else None, "pnl_unit": self.value_unit}

    def _trade_payload(self, ev: TradeEvent, notes: list[str], ts: int | None,
                       extra: dict | None = None) -> dict:
        sym, dec = self.tokens.get(ev.token)
        p = {"type": "trade", "v": 2, **(extra or {}), "kind": ev.kind, "tx": ev.tx, "block": ev.block, "ts": ts,
             "token": ev.token, "symbol": sym, "decimals": dec, "qty": str(ev.qty),
             "hype_wei": None if ev.hype_wei is None else str(ev.hype_wei),
             "confidence": ev.confidence, "hype_method": ev.hype_method, "notes": notes,
             **self._wallet_ctx(ev.wallet)}
        if ev.counter_token:
            csym, cdec = self.tokens.get(ev.counter_token)
            p.update(counter_token=ev.counter_token, counter_qty=str(ev.counter_qty),
                     counter_symbol=csym, counter_decimals=cdec)
        return p

    # --- position accounting ------------------------------------------------------
    # Rules (no fabricated precision):
    #  * realized_wei only gets PnL whose proceeds were measured AND whose cost basis is
    #    fully known; anything relying on an estimate goes to realized_est_wei.
    #  * tokens received without a measured cost make the basis "partial_unknown"; sells
    #    from such a position book no PnL at all (shown as unknown), they only reduce it.
    #  * transfers out reduce quantity and cost proportionally; they are never PnL.
    @staticmethod
    def _reset_if_closed(p: Position) -> None:
        if p.qty <= 0:
            p.qty, p.cost_wei, p.basis = 0, 0, BASIS_KNOWN

    @staticmethod
    def _apply(p: Position, ev: TradeEvent, ts: int | None) -> tuple[list[str], dict | None]:
        """Mutates the position; returns alert notes and, for sells against an existing
        position, the realized-PnL audit record."""
        notes: list[str] = []
        realized: dict | None = None
        inbound = ev.kind in ("BUY", "TRANSFER_IN") or (ev.kind == "MULTI" and ev.inbound)
        if ev.kind == "BUY" and ev.hype_wei is not None:
            if p.qty == 0:
                notes.append("🆕 new position")
                p.opened_at = ts
                p.basis = BASIS_KNOWN if ev.confidence == "high" else BASIS_ESTIMATED
            else:
                notes.append("➕ adding")
                if ev.confidence != "high" and p.basis == BASIS_KNOWN:
                    p.basis = BASIS_ESTIMATED
            p.qty += ev.qty
            p.cost_wei += ev.hype_wei
        elif ev.kind == "SELL" and ev.hype_wei is not None:
            if p.qty <= 0:
                notes.append("bought before tracking: PnL unknown")
                return notes, {"qty_sold": ev.qty, "proceeds_wei": ev.hype_wei,
                               "cost_basis_wei": None, "pnl_wei": None, "status": "unknown"}
            sold = min(ev.qty, p.qty)
            basis_cost = p.cost_wei * sold // p.qty
            proceeds = ev.hype_wei * sold // ev.qty
            if p.basis == BASIS_PARTIAL:
                notes.append("PnL unknown (part of the bag has unknown cost)")
                realized = {"qty_sold": sold, "proceeds_wei": proceeds, "cost_basis_wei": None,
                            "pnl_wei": None, "status": "unknown"}
            else:
                pnl = proceeds - basis_cost
                exact = p.basis == BASIS_KNOWN and ev.confidence == "high"
                realized = {"qty_sold": sold, "proceeds_wei": proceeds,
                            "cost_basis_wei": basis_cost, "pnl_wei": pnl,
                            "status": "measured" if exact else "estimated"}
                if exact:
                    p.realized_wei += pnl
                else:
                    p.realized_est_wei += pnl
                pct = (pnl / basis_cost * 100) if basis_cost else 0.0
                approx = "" if exact else "≈"
                notes.append(f"realized {approx}{pnl / WEI:+.3f} HYPE ({approx}{pct:+.0f}%)")
            if ev.qty > sold:
                notes.append(f"{(ev.qty - sold) / ev.qty * 100:.0f}% of this sale bought "
                             "before tracking")
            p.qty -= sold
            p.cost_wei -= basis_cost
            notes.append("🚪 full exit" if p.qty == 0 else
                         f"sold {sold / (sold + p.qty) * 100:.0f}% of bag")
            Engine._reset_if_closed(p)
        elif ev.kind in ("TRANSFER_IN", "MULTI") and inbound:
            if p.qty == 0:
                p.opened_at = ts
            p.qty += ev.qty
            p.basis = BASIS_PARTIAL
            notes.append("received with unknown cost")
        elif ev.kind in ("TRANSFER_OUT", "MULTI") and not inbound:
            if p.qty <= 0:
                return notes, None
            moved = min(ev.qty, p.qty)
            p.cost_wei -= p.cost_wei * moved // p.qty
            p.qty -= moved
            pct = moved / (moved + p.qty) * 100
            what = ("sold or moved (HYPE leg unknown)" if ev.confidence == "unknown"
                    else "moved to another address")
            notes.append(f"{pct:.0f}% of bag {what}")
            Engine._reset_if_closed(p)
        return notes, realized

    # --- main entry ---------------------------------------------------------------
    def apply_range(self, result: ScanResult, cursor: int) -> int:
        """Persist evidence, trades, positions, PnL records, signals, alerts and the
        checkpoint in ONE transaction. Returns the number of new trades."""
        min_wei = int(Decimal(str(self.cfg.min_trade_hype)) * WEI)
        new = 0
        bots = self.automated_wallets()
        # counterparty of each (tx, token, wallet) transfer leg, for "sent to 0x…" lines
        counter: dict[tuple[str, str, str], tuple[int, str]] = {}
        for tr in result.transfers:
            amt = int(tr.get("amount") or 0)
            for me, other in ((tr["from_address"], tr["to_address"]),
                              (tr["to_address"], tr["from_address"])):
                key = (tr["tx_hash"], tr["token"], me)
                if amt >= counter.get(key, (-1, ""))[0]:
                    counter[key] = (amt, other)
        with self.store.tx() as c:
            self.store.insert_evidence(c, result.transactions, result.transfers)
            self.store.record_blocks(c, result.block_times, result.block_hashes)
            for ev in result.events:
                bts = result.block_times.get(ev.block)
                trade_id = self.store.insert_trade(c, ev, bts)
                if trade_id is None:
                    continue  # this (tx, wallet, token) leg was already processed (replay)
                new += 1
                notes: list[str] = []
                extra: dict = {}
                if ev.kind in ("BUY", "SELL", "TRANSFER_IN", "TRANSFER_OUT", "MULTI"):
                    p = self.store.get_position(c, ev.wallet, ev.token)
                    before = p.qty
                    notes, realized = self._apply(p, ev, bts)
                    self.store.put_position(c, ev.wallet, ev.token, p)
                    if realized:
                        self.store.record_realized(c, trade_id, ev.wallet, ev.token, **realized)
                    extra = self._position_fields(ev, before, p, realized)
                cp = counter.get((ev.tx, ev.token, ev.wallet))
                if cp and ev.kind in ("TRANSFER_IN", "TRANSFER_OUT"):
                    extra["counterparty"] = cp[1]
                if not self._should_alert(ev, min_wei):
                    continue
                if ev.token not in self._tg_muted and ev.wallet not in bots:
                    # (_tg_muted: the reported token, never the quote asset)
                    payload = self._trade_payload(ev, notes, bts, extra)
                    text, _ = cards.render_trade(payload, self.links)
                    self.store.enqueue(c, f"ev:{ev.key}", text, payload=payload)
                if ev.kind == "BUY" and ev.confidence == "high" and bts is not None:
                    self._maybe_cluster(c, ev, bts, min_wei)
            self.store.set_cursor(c, cursor)
        return new

    @staticmethod
    def _position_fields(ev: TradeEvent, before: int, p: Position, realized: dict | None
                         ) -> dict:
        """Structured position outcome for the alert card (holdings, % sold, PnL)."""
        out: dict = {"pos_qty": str(p.qty), "pos_cost_wei": str(p.cost_wei),
                     "pos_basis": p.basis, "is_new": ev.kind == "BUY" and before == 0}
        if before > 0 and p.qty < before:
            out["sold_pct"] = round((before - p.qty) / before * 100, 2)
        if realized is not None:
            out["realized_status"] = realized["status"]
            if realized["status"] == "unknown":
                out["realized_reason"] = "before_tracking" if before <= 0 else "unknown_cost"
            if realized.get("pnl_wei") is not None:
                out["realized_wei"] = str(realized["pnl_wei"])
                cb = realized.get("cost_basis_wei")
                out["realized_pct"] = round(realized["pnl_wei"] / cb * 100, 2) if cb else None
        return out

    def _should_alert(self, ev: TradeEvent, min_wei: int) -> bool:
        if ev.token in self._muted:
            return False
        if ev.kind in ("BUY", "SELL"):
            return ev.hype_wei is not None and ev.hype_wei >= min_wei
        if ev.kind == "TRANSFER_OUT" and ev.confidence == "unknown":
            return True  # may be a sell we could not measure
        if ev.kind in ("TRANSFER_IN", "TRANSFER_OUT"):
            return self.cfg.alert_transfers
        return True  # SWAP / MULTI are rare and usually interesting

    def bundle_wallets(self) -> set[str]:
        """Wallets that split buys across addresses; refreshed every bundle_refresh_s."""
        ic = self.intel
        if time.monotonic() - self._bundles_at >= ic.bundle_refresh_s:
            self._bundles_at = time.monotonic()      # also on failure: retry next interval
            try:
                self._bundles = self.store.bundle_wallets(
                    lookback_days=ic.bundle_lookback_days, min_wallets=ic.bundle_min_wallets,
                    min_hits=ic.bundle_min_hits)
            except Exception:  # noqa: BLE001 - scoring must never block indexing
                log.exception("bundle wallet refresh failed; keeping %d known", len(self._bundles))
        return self._bundles

    def automated_wallets(self) -> set[str]:
        """Wallets judged automated by their transaction timing (IntelConfig.bot_*); refreshed
        every bot_refresh_s. Their trades are indexed as usual; only alerts treat them apart."""
        ic = self.intel
        if not ic.bot_filter:
            return set()
        if time.monotonic() - self._bots_at >= ic.bot_refresh_s:
            self._bots_at = time.monotonic()         # also on failure: retry next interval
            try:
                found = self.store.automated_wallets(
                    lookback_days=ic.bot_lookback_days, min_txs=ic.bot_min_txs,
                    fast_gap_s=ic.bot_fast_gap_s, fast_share=ic.bot_fast_share,
                    max_trade_share=ic.bot_max_trade_share)
                if set(found) != self._bots:
                    log.info("automated-wallet filter: %d wallet(s) not alerted: %s", len(found),
                             ", ".join(f"{w} ({v['txs']} txs, {v['fast_share']:.0%} under "
                                       f"{ic.bot_fast_gap_s:g}s, {v['tokens']} tokens)"
                                       for w, v in sorted(found.items())) or "none")
                self._bots = set(found)
            except Exception:  # noqa: BLE001 - a filter must never block indexing
                log.exception("automated-wallet refresh failed; keeping %d known", len(self._bots))
        return self._bots

    def _maybe_cluster(self, c, ev: TradeEvent, bts: int, min_wei: int) -> None:
        window = self.cfg.cluster_window_s
        rows = self.store.recent_buyer_blocks(c, ev.token, bts - window, min_wei)
        ic = self.intel
        strong = False
        if ic.independent_min > 0:
            irows = rows if ic.independent_window_s == window else \
                self.store.recent_buyer_blocks(c, ev.token, bts - ic.independent_window_s, min_wei)
            if len(irows) >= ic.independent_min:
                strong = self._strong_signal(c, ev, bts, irows)
        if len(rows) >= self.cfg.cluster_min_wallets:
            # one alert per moment: the strong card already says everything the plain one would
            self._raw_cluster(c, ev, bts, min_wei, rows, alert=not strong)

    def _score(self, rows: list[tuple[str, int, int, int]], window_s: int) -> dict:
        n = count_independent([(w, blk) for w, _, blk, _ in rows],
                              self.bundle_wallets() | self.automated_wallets())
        return {"independent": n, "n_raw": len(rows),
                "tier": tier_label(n, len(rows), window_s, self.intel.independent_min)}

    def _raw_cluster(self, c, ev: TradeEvent, bts: int, min_wei: int, rows: list,
                     alert: bool = True) -> None:
        window = self.cfg.cluster_window_s
        sid = self.store.record_signal(
            c, kind="cluster_buy", token=ev.token, dedup_key=f"cluster:{ev.token}:{bts // window}",
            window_start=bts - window, window_end=bts,
            details={"wallets": [r[0] for r in rows], "min_hype_wei": str(min_wei),
                     "window_s": window},
            trade_ids=[r[1] for r in rows], cooldown_s=window)
        if sid is None or not alert or not self.cfg.cluster_alerts or ev.token in self._tg_muted:
            return  # the signal stays in the database (Smart Money > Clusters)
        sym, _ = self.tokens.get(ev.token)
        payload = {"type": "cluster", "token": ev.token, "symbol": sym,
                   "min_hype": self.cfg.min_trade_hype, "window_min": window // 60,
                   "buyers": [self._wallet_ctx(r[0]) for r in rows], "tx": ev.tx,
                   **self._score(rows, window)}
        text, _ = cards.render_cluster(payload, self.links)
        self.store.enqueue(c, f"signal:{sid}", text, severity="high", payload=payload)

    def _strong_signal(self, c, ev: TradeEvent, bts: int, rows: list) -> bool:
        """>= independent_min unrelated wallets: the signal worth acting on. Fires once per
        token per cooldown, also records the paper trade that measures it forward."""
        ic = self.intel
        score = self._score(rows, ic.independent_window_s)
        if score["independent"] < ic.independent_min:
            return False                # several addresses, but one operator
        sid = self.store.record_signal(
            c, kind="independent_buy", token=ev.token,
            dedup_key=f"indep:{ev.token}:{bts // ic.independent_cooldown_s}",
            window_start=bts - ic.independent_window_s, window_end=bts,
            details={"wallets": [r[0] for r in rows], "window_s": ic.independent_window_s,
                     "independent": score["independent"]},
            trade_ids=[r[1] for r in rows], cooldown_s=ic.independent_cooldown_s)
        if sid is None:
            return False
        sym, dec = self.tokens.get(ev.token)
        if ic.paper_enabled:
            entry = None
            if ev.hype_wei and ev.qty > 0:
                entry = (Decimal(ev.hype_wei) / Decimal(WEI)) / (Decimal(ev.qty) / Decimal(10) ** int(dec or 18))
            self.store.record_paper_trade(
                c, token=ev.token, signal_id=sid, opened_at=bts, block=ev.block,
                entry_price_hype=entry, n_independent=score["independent"], n_raw=len(rows),
                details={"tx": ev.tx, "symbol": sym, "wallets": [r[0] for r in rows]})
        if not self.cfg.cluster_alerts or ev.token in self._tg_muted:
            return True                  # signal (and paper trade) recorded, nothing sent
        payload = {"type": "cluster", "strong": True, "token": ev.token, "symbol": sym,
                   "min_hype": self.cfg.min_trade_hype, "window_min": ic.independent_window_s // 60,
                   "buyers": [self._wallet_ctx(r[0]) for r in rows], "tx": ev.tx, **score}
        text, _ = cards.render_cluster(payload, self.links)
        self.store.enqueue(c, f"signal:{sid}", text, severity="high", payload=payload)
        return True

    def leaderboard_alerts(self, changes: list[tuple[str, str, Trader]]) -> None:
        bucket = int(time.time()) // 60
        for kind, detail, t in changes:
            icon = {"ENTERED": "⭐", "EXITED": "⬇️", "RANK": "📊", "PNL": "💰"}[kind]
            ctx = {"wallet": t.address, "rank": t.rank if kind != "EXITED" else None,
                   "label": self.labels.get(t.address), "pnl_unit": self.value_unit,
                   "pnl": t.pnl if kind != "PNL" else None}
            text = (f"{icon} <b>LEADERBOARD</b> {html.escape(detail)}\n{cards.who(self.links, ctx)}"
                    f"\n🔗 {cards.a(self.links.agora_trader(t.address), 'Agora profile')} · "
                    f"{cards.a(self.links.address(t.address), 'Elyscan')}")
            self.store.enqueue_now(f"lb:{kind}:{t.address}:{bucket}", text)
