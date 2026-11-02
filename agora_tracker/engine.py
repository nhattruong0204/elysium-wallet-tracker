"""Applies trade events to state (positions, cluster detection) and turns them into alerts.

Everything for one block range is written in a single DB transaction together with the
cursor, so a crash mid-range replays cleanly and never double-alerts.
"""

from __future__ import annotations

import html
import logging
import time

from .chain import TokenCache
from .classify import TradeEvent
from .config import AlertConfig, ChainConfig
from .db import Position, Store
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
    @staticmethod
    def _apply(p: Position, ev: TradeEvent, ts: int | None) -> list[str]:
        notes: list[str] = []
        if ev.kind == "BUY" and ev.hype_wei is not None:
            if p.qty == 0:
                notes.append("🆕 new position")
                p.opened_at = ts
            else:
                notes.append("➕ adding")
            p.qty += ev.qty
            p.cost_wei += ev.hype_wei
        elif ev.kind == "SELL" and ev.hype_wei is not None:
            if p.qty <= 0:
                notes.append("position predates tracking, PnL unknown")
            else:
                sold = min(ev.qty, p.qty)
                basis = p.cost_wei * sold // p.qty
                pnl = ev.hype_wei * sold // ev.qty - basis
                p.realized_wei += pnl
                p.qty -= sold
                p.cost_wei -= basis
                pct = (pnl / basis * 100) if basis else 0.0
                notes.append(f"realized {pnl / WEI:+.3f} HYPE ({pct:+.0f}%)")
                notes.append("🚪 full exit" if p.qty == 0 else
                             f"sold {sold / (sold + p.qty) * 100:.0f}% of bag")
        return notes

    # --- main entry ---------------------------------------------------------------
    def apply_range(self, events: list[TradeEvent], ts: dict[int, int], cursor: int) -> int:
        """Persist events + alerts + cursor atomically. Returns number of new events."""
        min_wei = int(self.cfg.min_trade_hype * WEI)
        new = 0
        with self.store.tx() as c:
            for ev in events:
                bts = ts.get(ev.block)
                if not Store.insert_event(c, ev, bts):
                    continue  # already processed (replay)
                new += 1
                notes: list[str] = []
                if ev.kind in ("BUY", "SELL"):
                    p = Store.get_position(c, ev.wallet, ev.token)
                    notes = self._apply(p, ev, bts)
                    Store.put_position(c, ev.wallet, ev.token, p)
                if not self._should_alert(ev, min_wei):
                    continue
                Store.enqueue(c, f"ev:{ev.key}", self._fmt_event(ev, notes))
                if ev.kind == "BUY":
                    self._maybe_cluster(c, ev, bts)
            self.store.set_kv("cursor", str(cursor))
        return new

    def _should_alert(self, ev: TradeEvent, min_wei: int) -> bool:
        if ev.kind in ("BUY", "SELL"):
            return ev.hype_wei is not None and ev.hype_wei >= min_wei
        if ev.kind in ("TRANSFER_IN", "TRANSFER_OUT"):
            return self.cfg.alert_transfers
        return True  # SWAP / MULTI are rare and usually interesting

    def _fmt_event(self, ev: TradeEvent, notes: list[str]) -> str:
        head = f"{ICON.get(ev.kind, '•')} <b>{ev.kind}</b> {self._who(ev.wallet)}"
        if ev.kind in ("BUY", "SELL") and ev.hype_wei:
            _, dec = self.tokens.get(ev.token)
            hype = ev.hype_wei / WEI
            px = hype / (ev.qty / 10 ** dec) if ev.qty else 0
            verb = "for" if ev.kind == "BUY" else "→"
            body = f"{self._amt(ev.token, ev.qty)} {verb} <b>{hype:.3f} HYPE</b> (px {px:.3g})"
        elif ev.kind == "SWAP":
            body = f"{self._amt(ev.counter_token, ev.counter_qty)} → {self._amt(ev.token, ev.qty)}"
        else:
            body = self._amt(ev.token, ev.qty)
        lines = [head, body]
        if notes:
            lines.append(" · ".join(notes))
        if ev.approx and ev.kind in ("BUY", "SELL"):
            lines.append("<i>HYPE leg approximate</i>")
        lines.append(f"<code>{ev.token}</code> · {self._link_tx(ev.tx)} · block {ev.block}")
        return "\n".join(lines)

    def _maybe_cluster(self, c, ev: TradeEvent, bts: int | None) -> None:
        now = bts or int(time.time())
        window = self.cfg.cluster_window_s
        buyers = Store.recent_buyers(c, ev.token, now - window)
        if len(buyers) < self.cfg.cluster_min_wallets:
            return
        if not Store.cluster_due(c, ev.token, now, window):
            return
        sym, _ = self.tokens.get(ev.token)
        who = "\n".join(f"  • {self._who(w)}" for w in buyers[:10])
        text = (f"🚨 <b>CLUSTER BUY</b> ${html.escape(sym)}: {len(buyers)} tracked wallets "
                f"bought within {window // 60} min\n{who}\n<code>{ev.token}</code>")
        Store.enqueue(c, f"cluster:{ev.token}:{now // window}", text)

    def leaderboard_alerts(self, changes: list[tuple[str, str, Trader]]) -> None:
        bucket = int(time.time()) // 60
        for kind, detail, t in changes:
            icon = {"ENTERED": "⭐", "EXITED": "⬇️", "RANK": "📊", "PNL": "💰"}[kind]
            text = f"{icon} <b>LEADERBOARD</b> <code>{short(t.address)}</code> {detail}"
            if t.pnl is not None and kind != "PNL":
                text += f" · PnL ${human(t.pnl)}"
            self.store.enqueue_now(f"lb:{kind}:{t.address}:{bucket}", text)
