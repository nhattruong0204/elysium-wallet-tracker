"""Applies trade events to state (positions, cluster detection) and turns them into alerts.

Everything for one block range is written in a single DB transaction together with the
cursor, so a crash mid-range replays cleanly and never double-alerts.
"""

from __future__ import annotations

import html
import logging
import time
from decimal import Decimal

from .chain import TokenCache
from .classify import TradeEvent
from .config import AlertConfig, ChainConfig
from .db import BASIS_ESTIMATED, BASIS_KNOWN, BASIS_PARTIAL, Position, Store
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


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


class Engine:
    def __init__(self, store: Store, tokens: TokenCache, chain_cfg: ChainConfig,
                 alert_cfg: AlertConfig, labels: dict[str, str]):
        self.store, self.tokens = store, tokens
        self.chain_cfg, self.cfg = chain_cfg, alert_cfg
        self.labels = labels

    # --- formatting -------------------------------------------------------------
    def _link_tx(self, tx: str) -> str:
        t = self.chain_cfg.explorer_tx_url
        return f'<a href="{html.escape(t.format(tx=tx))}">tx</a>' if t else f"<code>{tx[:10]}…</code>"

    def _who(self, wallet: str) -> str:
        row = self.store.trader(wallet)
        bits = []
        if row is not None and row["in_top"]:
            bits.append(f"#{row['rank']}")
        label = self.labels.get(wallet)
        if label:
            bits.append(html.escape(label))
        a = self.chain_cfg.explorer_address_url
        addr = (f'<a href="{html.escape(a.format(address=wallet))}">{short(wallet)}</a>'
                if a else f"<code>{short(wallet)}</code>")
        bits.append(addr)
        if row is not None and row["pnl"] is not None:
            bits.append(f"PnL ${human(row['pnl'])}")
        return " · ".join(bits)

    def _amt(self, token: str, raw: int) -> str:
        sym, dec = self.tokens.get(token)
        return f"{human(raw / 10 ** dec)} ${html.escape(sym)}"

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
    def _apply(p: Position, ev: TradeEvent, ts: int | None) -> list[str]:
        notes: list[str] = []
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
                return notes
            sold = min(ev.qty, p.qty)
            basis_cost = p.cost_wei * sold // p.qty
            proceeds = ev.hype_wei * sold // ev.qty
            if p.basis == BASIS_PARTIAL:
                notes.append("PnL unknown (part of the bag has unknown cost)")
            else:
                pnl = proceeds - basis_cost
                exact = p.basis == BASIS_KNOWN and ev.confidence == "high"
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
                return notes
            moved = min(ev.qty, p.qty)
            p.cost_wei -= p.cost_wei * moved // p.qty
            p.qty -= moved
            pct = moved / (moved + p.qty) * 100
            what = ("sold or moved (HYPE leg unknown)" if ev.confidence == "unknown"
                    else "moved to another address")
            notes.append(f"{pct:.0f}% of bag {what}")
            Engine._reset_if_closed(p)
        return notes

    # --- main entry ---------------------------------------------------------------
    def apply_range(self, events: list[TradeEvent], ts: dict[int, int], cursor: int) -> int:
        """Persist events + alerts + cursor atomically. Returns number of new events."""
        min_wei = int(Decimal(str(self.cfg.min_trade_hype)) * WEI)
        new = 0
        with self.store.tx() as c:
            for ev in events:
                bts = ts.get(ev.block)
                if not Store.insert_event(c, ev, bts):
                    continue  # this (tx, wallet, token) leg was already processed (replay)
                new += 1
                notes: list[str] = []
                if ev.kind in ("BUY", "SELL", "TRANSFER_IN", "TRANSFER_OUT", "MULTI"):
                    p = Store.get_position(c, ev.wallet, ev.token)
                    notes = self._apply(p, ev, bts)
                    Store.put_position(c, ev.wallet, ev.token, p)
                if not self._should_alert(ev, min_wei):
                    continue
                Store.enqueue(c, f"ev:{ev.key}", self._fmt_event(ev, notes))
                if ev.kind == "BUY" and ev.confidence == "high" and bts is not None:
                    self._maybe_cluster(c, ev, bts, min_wei)
            self.store.set_kv("cursor", str(cursor))
        return new

    def _should_alert(self, ev: TradeEvent, min_wei: int) -> bool:
        if ev.kind in ("BUY", "SELL"):
            return ev.hype_wei is not None and ev.hype_wei >= min_wei
        if ev.kind == "TRANSFER_OUT" and ev.confidence == "unknown":
            return True  # may be a sell we could not measure
        if ev.kind in ("TRANSFER_IN", "TRANSFER_OUT"):
            return self.cfg.alert_transfers
        return True  # SWAP / MULTI are rare and usually interesting

    def _fmt_event(self, ev: TradeEvent, notes: list[str]) -> str:
        kind = ev.kind
        if kind == "TRANSFER_OUT" and ev.confidence == "unknown":
            kind = "OUT (sell?)"
        head = f"{ICON.get(ev.kind, '•')} <b>{kind}</b> {self._who(ev.wallet)}"
        if ev.kind in ("BUY", "SELL") and ev.hype_wei:
            _, dec = self.tokens.get(ev.token)
            hype = ev.hype_wei / WEI
            px = hype / (ev.qty / 10 ** dec) if ev.qty else 0
            verb = "for" if ev.kind == "BUY" else "→"
            a = "" if ev.confidence == "high" else "≈"
            body = (f"{self._amt(ev.token, ev.qty)} {verb} <b>{a}{hype:.3f} HYPE</b> "
                    f"(px {a}{px:.3g})")
        elif ev.kind == "SWAP" and ev.counter_token and ev.counter_qty is not None:
            body = f"{self._amt(ev.counter_token, ev.counter_qty)} → {self._amt(ev.token, ev.qty)}"
        else:
            body = self._amt(ev.token, ev.qty)
        lines = [head, body]
        if notes:
            lines.append(" · ".join(notes))
        if ev.confidence == "estimated":
            lines.append(f"<i>HYPE amount estimated ({ev.hype_method})</i>")
        elif ev.confidence == "unknown" and ev.kind != "TRANSFER_OUT":
            lines.append("<i>HYPE leg unknown</i>")
        lines.append(f"<code>{ev.token}</code> · {self._link_tx(ev.tx)} · block {ev.block}")
        return "\n".join(lines)

    def _maybe_cluster(self, c, ev: TradeEvent, bts: int, min_wei: int) -> None:
        window = self.cfg.cluster_window_s
        buyers = Store.recent_buyers(c, ev.token, bts - window, min_wei)
        if len(buyers) < self.cfg.cluster_min_wallets:
            return
        if not Store.cluster_due(c, ev.token, bts, window):
            return
        sym, _ = self.tokens.get(ev.token)
        who = "\n".join(f"  • {self._who(w)}" for w in buyers[:10])
        text = (f"🚨 <b>CLUSTER BUY</b> ${html.escape(sym)}: {len(buyers)} tracked wallets "
                f"bought ≥{self.cfg.min_trade_hype:g} HYPE within {window // 60} min "
                f"(measured buys only; wallets may share an owner)\n{who}\n"
                f"<code>{ev.token}</code>")
        Store.enqueue(c, f"cluster:{ev.token}:{bts // window}", text)

    def leaderboard_alerts(self, changes: list[tuple[str, str, Trader]]) -> None:
        bucket = int(time.time()) // 60
        for kind, detail, t in changes:
            icon = {"ENTERED": "⭐", "EXITED": "⬇️", "RANK": "📊", "PNL": "💰"}[kind]
            text = f"{icon} <b>LEADERBOARD</b> <code>{short(t.address)}</code> {detail}"
            if t.pnl is not None and kind != "PNL":
                text += f" · PnL ${human(t.pnl)}"
            self.store.enqueue_now(f"lb:{kind}:{t.address}:{bucket}", text)
