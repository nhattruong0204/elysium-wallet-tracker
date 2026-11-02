"""Telegram alert cards: every detail is a link, token market data, quick-action buttons.

Layout (Raybot/Maestro-style):

    🟢 BUY  BLOB · Blob                      <- token name -> Agora token page
    👤 #3 whale-1 · 0x7df2…9158              <- rank -> Agora trader, address -> Elyscan
    💰 3.200 HYPE ($274) → 12.40M BLOB
    🆕 new position
    📊 MC $857K · 24h Vol 2.27K HYPE · 24h +73% · 👥 697 · ⏱ 6d · ElysPump
    🔗 Agora · Kleos · BOXA · ElysPump · Elyscan
    0x9a98…f534 (full address, tap to copy)
    🧾 Tx · block 199694 · 2m ago

    [🛒 Agora] [Kleos] [BOXA]
    [🧾 Tx] [👤 Trader]

Cards are rendered from a JSON payload stored in the outbox, twice: once without market
data when the alert is created (fallback text), and again right before sending with fresh
market data. Rendering never fails because market data is missing.
"""

from __future__ import annotations

import html
import time
from typing import Any

from .config import LinksConfig
from .market import TokenInfo

WEI = 10 ** 18
ICON = {"BUY": "🟢", "SELL": "🔴", "SWAP": "🔁", "TRANSFER_IN": "📥", "TRANSFER_OUT": "📤",
        "MULTI": "🧩"}
LAUNCHPAD_NAMES = {"kleos": "Kleos", "boxa": "BOXA", "elyspump": "ElysPump", "elyxa": "Elyxa",
                   "obol": "Obol", "petals": "Petals", "stockium": "Stockium", "eleos": "Eleos"}


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


def esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def a(url: str, text: str) -> str:
    return f'<a href="{esc(url)}">{text}</a>'


def ago(ts: int | None, now: float | None = None) -> str | None:
    if not ts:
        return None
    d = int((now or time.time()) - ts)
    if d < 0:
        return None
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{d // n}{unit}"
    return f"{d}s"


class Links:
    def __init__(self, cfg: LinksConfig):
        self.c = cfg

    def tx(self, h: str) -> str:
        return f"{self.c.explorer}/tx/{h}"

    def address(self, x: str) -> str:
        return f"{self.c.explorer}/address/{x}"

    def explorer_token(self, t: str) -> str:
        return f"{self.c.explorer}/token/{t}"

    def agora_token(self, t: str) -> str:
        return f"{self.c.agora}/token/{t}"

    def agora_trader(self, w: str) -> str:
        return f"{self.c.agora}/trader/{w}"

    def kleos_token(self, t: str) -> str:
        return f"{self.c.kleos}/token/{t}"

    def boxa_token(self, t: str) -> str:
        return f"{self.c.boxa}/token?address={t}"

    def native(self, launchpad: str | None, t: str) -> tuple[str, str] | None:
        """The token's own launchpad page, when it isn't already Kleos or BOXA."""
        if launchpad == "elyspump":
            return "ElysPump", f"{self.c.elyspump}/#/token/{t}"
        return None


def _hype(wei: int, hype_usd: float | None, approx: bool) -> str:
    v = wei / WEI
    s = f"{'≈' if approx else ''}{v:.3f} HYPE"
    if hype_usd:
        s += f" (${human(v * hype_usd)})"
    return s


def _pct(f: float | None) -> str | None:
    return None if f is None else f"{f * 100:+.0f}%"


def market_line(info: TokenInfo | None, hype_usd: float | None) -> str | None:
    if info is None:
        return None
    parts = []
    if info.mcap_hype is not None:
        # USD only; HYPE is the fallback when Agora's HYPE/USD rate is unavailable
        parts.append(f"MC <b>${human(info.mcap_hype * hype_usd)}</b>" if hype_usd
                     else f"MC <b>{human(info.mcap_hype)} HYPE</b>")
    if info.vol_24h_hype is not None:
        parts.append(f"24h Vol {human(info.vol_24h_hype)} HYPE")
    if (p := _pct(info.change_24h)) is not None:
        parts.append(f"24h {p}")
    if (p := _pct(info.change_1h)) is not None:
        parts.append(f"1h {p}")
    if info.holders is not None:
        parts.append(f"👥 {info.holders:,}")
    if info.buys_24h is not None and info.trades_24h:
        parts.append(f"🛒 {info.buys_24h}/{info.trades_24h} buys 24h")
    if (age := ago(info.created_at)) is not None:
        parts.append(f"⏱ {age}")
    if info.launchpad:
        parts.append(esc(LAUNCHPAD_NAMES.get(info.launchpad, info.launchpad)))
    return "📊 " + " · ".join(parts) if parts else None


def token_links(L: Links, token: str, info: TokenInfo | None) -> str:
    items = [a(L.agora_token(token), "Agora"), a(L.kleos_token(token), "Kleos"),
             a(L.boxa_token(token), "BOXA")]
    nat = L.native(info.launchpad if info else None, token)
    if nat:
        items.append(a(nat[1], nat[0]))
    items.append(a(L.explorer_token(token), "Elyscan"))
    return "🔗 " + " · ".join(items)


def keyboard(L: Links, token: str, info: TokenInfo | None, tx: str | None,
             wallet: str | None) -> dict:
    row1 = [{"text": "🛒 Agora", "url": L.agora_token(token)},
            {"text": "Kleos", "url": L.kleos_token(token)},
            {"text": "BOXA", "url": L.boxa_token(token)}]
    nat = L.native(info.launchpad if info else None, token)
    if nat:
        row1.append({"text": nat[0], "url": nat[1]})
    row2 = []
    if tx:
        row2.append({"text": "🧾 Tx", "url": L.tx(tx)})
    if wallet:
        row2.append({"text": "👤 Trader", "url": L.agora_trader(wallet)})
    return {"inline_keyboard": [row1, row2] if row2 else [row1]}


def who(L: Links, p: dict) -> str:
    """#rank (-> Agora trader) · label · address (-> Elyscan) · PnL"""
    w = p["wallet"]
    bits = []
    if p.get("rank"):
        bits.append(a(L.agora_trader(w), f"#{p['rank']}"))
    if p.get("label"):
        bits.append(esc(p["label"]))
    bits.append(a(L.address(w), f"<code>{short(w)}</code>"))
    if p.get("pnl") is not None:
        unit = p.get("pnl_unit") or "USD"
        v = p["pnl"]
        bits.append(f"PnL ${human(v)}" if unit.upper() == "USD" else f"PnL {human(v)} {esc(unit)}")
    return "👤 " + " · ".join(bits)


def _token_title(L: Links, token: str, symbol: str, info: TokenInfo | None) -> str:
    sym = esc((info.symbol if info and info.symbol else None) or symbol)
    name = f" · {esc(info.name)}" if info and info.name and info.name != sym else ""
    return a(L.agora_token(token), f"<b>{sym}</b>") + name


def render_trade(p: dict, L: Links, info: TokenInfo | None = None,
                 hype_usd: float | None = None, now: float | None = None
                 ) -> tuple[str, dict]:
    kind, token, conf = p["kind"], p["token"], p.get("confidence", "high")
    label = "OUT (sell?)" if kind == "TRANSFER_OUT" and conf == "unknown" else kind
    sym, dec = p.get("symbol") or token[:8], int(p.get("decimals") or 18)
    qty = int(p["qty"]) / 10 ** dec
    lines = [f"{ICON.get(kind, '•')} <b>{esc(label)}</b>  {_token_title(L, token, sym, info)}",
             who(L, p)]
    hype_wei = int(p["hype_wei"]) if p.get("hype_wei") else None
    if kind in ("BUY", "SELL") and hype_wei:
        approx = conf != "high"
        h = _hype(hype_wei, hype_usd, approx)
        amt = f"{human(qty)} {esc(sym)}"
        lines.append(f"💰 {h} → {amt}" if kind == "BUY" else f"💰 {amt} → {h}")
    elif kind == "SWAP" and p.get("counter_token"):
        cq = int(p["counter_qty"]) / 10 ** int(p.get("counter_decimals") or 18)
        csym = esc(p.get("counter_symbol") or p["counter_token"][:8])
        lines.append(f"💰 {human(cq)} {a(L.agora_token(p['counter_token']), csym)} → "
                     f"{human(qty)} {esc(sym)}")
    else:
        lines.append(f"💰 {human(qty)} {esc(sym)}")
    if p.get("notes"):
        lines.append(" · ".join(esc(n) for n in p["notes"]))
    if conf == "estimated":
        lines.append(f"<i>HYPE amount estimated ({esc(p.get('hype_method', ''))})</i>")
    elif conf == "unknown" and kind != "TRANSFER_OUT":
        lines.append("<i>HYPE leg unknown</i>")
    if (m := market_line(info, hype_usd)) is not None:
        lines.append(m)
    lines.append(token_links(L, token, info))
    lines.append(f"<code>{esc(token)}</code>")
    when = ago(p.get("ts"), now)
    lines.append(f"🧾 {a(L.tx(p['tx']), 'Tx')} · block {p['block']}"
                 + (f" · {when} ago" if when else ""))
    return "\n".join(lines), keyboard(L, token, info, p["tx"], p["wallet"])


def render_cluster(p: dict, L: Links, info: TokenInfo | None = None,
                   hype_usd: float | None = None) -> tuple[str, dict]:
    token, sym = p["token"], p.get("symbol") or p["token"][:8]
    lines = [f"🚨 <b>CLUSTER BUY</b>  {_token_title(L, token, sym, info)}",
             f"{len(p['buyers'])} tracked wallets bought ≥{p['min_hype']:g} HYPE within "
             f"{p['window_min']} min <i>(measured buys only; wallets may share an owner)</i>"]
    for b in p["buyers"][:10]:
        lines.append("  " + who(L, b)[2:])
    if (m := market_line(info, hype_usd)) is not None:
        lines.append(m)
    lines.append(token_links(L, token, info))
    lines.append(f"<code>{esc(token)}</code>")
    return "\n".join(lines), keyboard(L, token, info, None, None)


def render(payload: dict | None, L: Links, info: TokenInfo | None = None,
           hype_usd: float | None = None) -> tuple[str, dict] | None:
    if not payload:
        return None
    if payload.get("type") == "trade":
        return render_trade(payload, L, info, hype_usd)
    if payload.get("type") == "cluster":
        return render_cluster(payload, L, info, hype_usd)
    return None
