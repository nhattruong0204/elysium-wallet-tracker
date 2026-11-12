"""Telegram alert cards, laid out like the trading-bot cards traders already read daily.

    🆕🟢 BUY BOZO (ElysPump)                    <- symbol -> Agora token page
    🔹 #42 · 0x93f5…4489 · PnL $15.7K            <- #rank -> Agora trader, address -> Elyscan

    🔹 #42 swapped 2.00 HYPE ($172.04) for 23.29M BOZO @$0.00000739
    ✊ Holds: 23.29M (2.33%)

    📊 24h +138% · 1h 0% · Vol $21.2K · 👥 18 · 🛒 84/143 buys
    🔗 #BOZO (𝕏) | MC: $6.96K | Seen: 3d: Agora | Kleos | BOXA | ElysPump | 🔎𝕏
    0xef052b8ebfcf6cd8c4f5ca499cccb9388b6b1421   (tap to copy)
    🧾 Tx · 7s ago

    [🛒 Agora: BOZO] [BOXA: BOZO]
    [Kleos: BOZO]    [🧾 Tx]

Money is shown in USD (Agora's HYPE/USD rate at send time) next to the HYPE amount that
was measured on-chain; estimated amounts carry "≈". The token's picture is shown above
the card as a link preview when Agora has one. Cards are rendered from the payload stored
in the outbox, again right before sending with fresh market data; rendering never fails
because market data is missing.
"""
from __future__ import annotations

import html
import math
import os
import re
import time
from typing import Any
from urllib.parse import quote

from .cohorts import warning_line
from .config import LinksConfig
from .market import TokenInfo

WEI = 10 ** 18
ICON = {"BUY": "🟢", "SELL": "🔴", "SWAP": "🔁", "TRANSFER_IN": "📥", "TRANSFER_OUT": "📤",
        "MULTI": "🧩"}
LAUNCHPAD_NAMES = {"kleos": "Kleos", "boxa": "BOXA", "elyspump": "ElysPump", "elyxa": "Elyxa",
                   "obol": "Obol", "petals": "Petals", "stockium": "Stockium", "eleos": "Eleos",
                   "volta": "Volta", "rons": "RONS", "ascend": "Ascend", "other": "other venue"}
# Launchpads whose token page (Kleos / BOXA / ElysPump) can really trade the token. For any
# other venue those pages are a dead end, so alerts point at the pool/router instead.
TRADE_PAGES = {"kleos", "boxa", "elyspump"}
ASCEND_APP = "https://app.ascendlaunch.xyz/"
SHARED = {"0xe2e105d3f7209a9da11f83ccf3e7398a753823f1"}   # Uniswap v4 PoolManager (all pools)


def unknown_venue(info: TokenInfo | None, dd: Any = None) -> bool:
    if getattr(dd, "venue", None) in ("ascend", "v4"):
        return True
    pad = info.launchpad if info else None
    return bool(pad) and pad not in TRADE_PAGES


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


# Maestro's official @Maestro bot supports Elysium; direct token deep-link behaviour on Elysium
# needs a real Telegram smoke test. The tracker passes only a public contract address: it never
# trades, signs, holds wallet keys or learns the user's actions. Maestro can auto-buy on pasted
# tokens depending on user settings; this link is NOT guaranteed to be view-only.
MAESTRO_TOKEN_URL = "https://t.me/Maestro?start="
MAESTRO_CHAIN_ID = 99901
MAESTRO_LABEL = "⚡ Quick Buy / Sell · Maestro"
# Fomo uses public /coin share links with address and chainId. The Elysium iOS handoff
# still requires user confirmation on a physical device; this is a link, not a trade.
FOMO_COIN_URL = "https://fomo.family/coin?address="
FOMO_LABEL = "📱 Open in Fomo"
_TOKEN_ADDRESS = re.compile(r"0x[0-9a-f]{40}")


class Links:
    def __init__(self, cfg: LinksConfig, chain_id: int = MAESTRO_CHAIN_ID):
        self.c = cfg
        self.chain_id = chain_id
        # Operator off-switch: MAESTRO_BUTTON=off; disable unverified deep links via
        # MAESTRO_DEEPLINK=off to open @Maestro without a token start payload.
        self.maestro_on = os.environ.get("MAESTRO_BUTTON", "on").strip().lower() not in (
            "off", "0", "false", "no")
        # Off = open @Maestro chat without a start payload; the user can paste the CA.
        self.maestro_deeplink = os.environ.get("MAESTRO_DEEPLINK", "on").strip().lower() not in (
            "off", "0", "false", "no")
        # Independent Fomo button off-switch, without changing Maestro or the scanner.
        self.fomo_on = os.environ.get("FOMO_BUTTON", "on").strip().lower() not in (
            "off", "0", "false", "no")

    def maestro_token(self, t: object) -> str | None:
        """Open official @Maestro with a token start payload (or generic chat fallback).
        Only a public 20-byte Elysium token contract may be passed, never a symbol."""
        if not self.maestro_on or self.chain_id != MAESTRO_CHAIN_ID or not isinstance(t, str):
            return None
        addr = t.strip().lower()
        if not _TOKEN_ADDRESS.fullmatch(addr) or int(addr, 16) == 0:
            return None
        return MAESTRO_TOKEN_URL + addr if self.maestro_deeplink else "https://t.me/Maestro"

    def maestro_row(self, t: object) -> list[dict]:
        url = self.maestro_token(t)
        return [{"text": MAESTRO_LABEL, "url": url}] if url else []

    def fomo_token(self, t: object) -> str | None:
        """Open a public Elysium contract share link in Fomo (browser or iOS app).

        No wallet ID, user data, referral codes, or auth credentials enter this URL.
        """
        if not self.fomo_on or self.chain_id != MAESTRO_CHAIN_ID or not isinstance(t, str):
            return None
        addr = t.strip().lower()
        if not _TOKEN_ADDRESS.fullmatch(addr) or int(addr, 16) == 0:
            return None
        return f"{FOMO_COIN_URL}{addr}&chainId={MAESTRO_CHAIN_ID}"

    def fomo_row(self, t: object) -> list[dict]:
        url = self.fomo_token(t)
        return [{"text": FOMO_LABEL, "url": url}] if url else []

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

    def trade_page(self, t: str) -> str:
        return {"boxa": self.boxa_token, "kleos": self.kleos_token}.get(
            self.c.trade_site, self.agora_token)(t)

    def wallet_buttons(self, t: str, symbol: str) -> list[dict]:
        """Buttons that open the token's trade page inside a wallet app (mobile).

        Telegram buttons only accept https/tg links, so these are the wallets' own https
        universal links; if Telegram opens one in its in-app browser instead, the wallet's
        landing page hands over to the app."""
        page = quote(self.trade_page(t), safe="")
        out = []
        for w in self.c.wallet_buttons:
            if w == "rabby":
                out.append({"text": f"⚡ Rabby: {symbol}",
                            "url": f"https://go.rabby.io/mobile/?_cmd=open-dapp&dapp={page}"})
            elif w == "okx":
                deep = f"okx://wallet/dapp/url?dappUrl={page}"
                out.append({"text": f"⚡ OKX: {symbol}",
                            "url": "https://web3.okx.com/download?deeplink="
                                   + quote(deep, safe="")})
        return out

    def native(self, launchpad: str | None, t: str) -> tuple[str, str] | None:
        """The token's own launchpad page, when it isn't already Kleos or BOXA."""
        if launchpad == "elyspump":
            return "ElysPump", f"{self.c.elyspump}/#/token/{t}"
        return None



# Token routes verified by existing card helpers or explicit owner-provided examples.
def launchpad_token_url(chain_id: int, launchpad: str | None, token: str,
                        links: Links | None = None) -> str | None:
    from urllib.parse import urlsplit

    if chain_id != 99901 or not re.fullmatch(r"0x[0-9a-fA-F]{40}", token):
        return None
    links = links or Links(LinksConfig())
    pad = (launchpad or "").lower()
    registry = {"montra": ("montramarkets.xyz", lambda t: f"https://montramarkets.xyz/token/{t}"),
                "obol": ("tryobol.world", lambda t: f"https://tryobol.world/coin/{t}"),
                "kleos": ("www.thekleos.pro", links.kleos_token),
                "boxa": ("boxa.club", links.boxa_token),
                "elyspump": ("elysp.com", lambda t: (links.native("elyspump", t) or ("", ""))[1])}
    if pad not in registry:
        return None
    host, build = registry[pad]
    url = build(token.lower())
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != host:
        return None
    return url


# Platform homepages supplied by the project owner; no token route is inferred.
LAUNCHPAD_PLATFORMS = {
    "ascend": ASCEND_APP,
    "volta": "https://www.volta.family/",
    "eleos": "https://eleos.fun/",
    "elyxa": "https://elyxa.trade/",
}


def activity_launchpad_link(chain_id: int, launchpad: str | None, token: str,
                            links: Links | None = None) -> tuple[str | None, str | None]:
    """Verified token route first; otherwise an explicitly registered platform homepage."""
    token_url = launchpad_token_url(chain_id, launchpad, token, links)
    if token_url:
        return token_url, "token"
    if chain_id != 99901 or not re.fullmatch(r"0x[0-9a-fA-F]{40}", token):
        return None, None
    homepage = LAUNCHPAD_PLATFORMS.get((launchpad or "").lower())
    return (homepage, "platform") if homepage else (None, None)


def usd(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ("-" if v < 0 else "")
    x = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= div:
            return f"{sign}${x / div:.2f}{suf}"
    return f"{sign}${x:,.2f}"


def price_str(v: float | None) -> str:
    """$0.00000739 (3 significant digits, never scientific notation)."""
    if v is None or v <= 0:
        return "n/a"
    if v >= 1:
        return f"${v:,.2f}"
    digits = min(18, -math.floor(math.log10(v)) + 2)
    return f"${v:.{digits}f}"


def qty_str(v: float) -> str:
    return human(v) if abs(v) >= 1000 else f"{v:,.2f}"


def hashtag(symbol: str) -> str:
    tag = re.sub(r"\W", "", symbol or "")
    return f"#{tag}" if tag and not tag.isdigit() else ""


def _pct(f: float | None, signed: bool = True) -> str | None:
    if f is None:
        return None
    v = f * 100
    if abs(v) >= 1000:
        return f"{v:+,.0f}%" if signed else f"{v:,.0f}%"
    return f"{v:+.0f}%" if signed else f"{v:.0f}%"


def _to_usd(hype: float | None, rate: float | None) -> float | None:
    return None if hype is None or not rate else hype * rate


def _money(hype: float, rate: float | None, approx: bool = False) -> str:
    """'2.00 HYPE ($172.04)' – HYPE is what was measured, USD for readability."""
    ap = "≈" if approx else ""
    s = f"{ap}{hype:,.2f} HYPE" if hype >= 0.01 else f"{ap}{hype:.4f} HYPE"
    if rate:
        s += f" ({ap}{usd(hype * rate)})"
    return s


def _pnl_usd(hype_v: float, rate: float | None, approx: bool = False) -> str:
    ap = "≈" if approx else ""
    if rate:
        return f"{ap}{usd(hype_v * rate, signed=True)}"
    return f"{ap}{hype_v:+,.3f} HYPE"


def supply(info: TokenInfo | None) -> float | None:
    """Token supply implied by Agora's market cap / price (both in HYPE)."""
    if info is None or not info.mcap_hype or not info.price_hype:
        return None
    return info.mcap_hype / info.price_hype


def market_line(info: TokenInfo | None, hype_usd: float | None) -> str | None:
    """📊 24h +138% · 1h 0% · Vol $21.2K · 👥 18 · 🛒 84/143 buys"""
    if info is None:
        return None
    parts = []
    if (p := _pct(info.change_24h)) is not None:
        parts.append(f"24h <b>{p}</b>")
    if (p := _pct(info.change_1h)) is not None:
        parts.append(f"1h {p}")
    if info.vol_24h_hype is not None:
        parts.append(f"Vol {usd(info.vol_24h_hype * hype_usd)}" if hype_usd
                     else f"Vol {human(info.vol_24h_hype)} HYPE")
    if info.holders is not None:
        parts.append(f"👥 {info.holders:,}")
    if info.buys_24h is not None and info.trades_24h:
        parts.append(f"🛒 {info.buys_24h}/{info.trades_24h} buys")
    return "📊 " + " · ".join(parts) if parts else None


def market_cap_label(info: TokenInfo | None, hype_usd: float | None) -> str | None:
    """The same market-cap value for the headline and legacy link bars."""
    if info is None or info.mcap_hype is None:
        return None
    return (f"<b>MC</b>: {usd(info.mcap_hype * hype_usd)}" if hype_usd
            else f"<b>MC</b>: {human(info.mcap_hype)} HYPE")


def link_bar(L: Links, token: str, symbol: str, info: TokenInfo | None,
             hype_usd: float | None, now: float | None = None, venue_pages: bool = True,
             show_market_cap: bool = True) -> str:
    """🔗 #BOZO (𝕏) | MC: $6.96K | Seen: 3d: Agora | Kleos | BOXA | ElysPump | 🔎𝕏"""
    tag = hashtag((info.symbol if info and info.symbol else None) or symbol)
    head = tag or esc(symbol)
    socials = []
    if info is not None and info.x_url:
        socials.append(a(info.x_url, "𝕏"))
    if info is not None and info.telegram_url:
        socials.append(a(info.telegram_url, "TG"))
    if info is not None and info.website_url:
        socials.append(a(info.website_url, "Web"))
    if socials:
        head += " ( " + " · ".join(socials) + " )"
    segs = [head]
    if show_market_cap and (mc := market_cap_label(info, hype_usd)) is not None:
        segs.append(mc)
    venues = [a(L.agora_token(token), "Agora")]
    if venue_pages:
        venues += [a(L.kleos_token(token), "Kleos"), a(L.boxa_token(token), "BOXA")]
    else:
        venues.append(a(L.explorer_token(token), "Elyscan"))
    nat = L.native(info.launchpad if info else None, token)
    if nat:
        venues.append(a(nat[1], nat[0]))
    age = ago(info.created_at, now) if info else None
    lead = f"<b>Seen</b>: {age}: " if age else ""
    segs.append(lead + " | ".join(venues))
    segs.append(a(f"https://x.com/search?q={token}&f=live", "🔎𝕏"))
    return "🔗 " + " | ".join(segs)


def keyboard(L: Links, token: str, symbol: str, info: TokenInfo | None,
             tx: str | None, wallet: str | None = None, dd: Any = None,
             force_unknown: bool = False) -> dict:
    s = (symbol or token[:6])[:12]
    quick = [r for r in (L.maestro_row(token), L.fomo_row(token)) if r]  # two direct apps
    if force_unknown or unknown_venue(info, dd):
        return {"inline_keyboard": quick + _venue_rows(L, token, s, tx, dd)}
    rows = quick + [r for r in (L.wallet_buttons(token, s),) if r]
    rows += [[{"text": f"🛒 Agora: {s}", "url": L.agora_token(token)},
             {"text": f"BOXA: {s}", "url": L.boxa_token(token)}]]
    row2 = [{"text": f"Kleos: {s}", "url": L.kleos_token(token)}]
    nat = L.native(info.launchpad if info else None, token)
    if nat:
        row2.append({"text": f"{nat[0]}: {s}", "url": nat[1]})
    elif tx:
        row2.append({"text": "🧾 Tx", "url": L.tx(tx)})
    rows.append(row2)
    return {"inline_keyboard": rows}


def _venue_rows(L: Links, token: str, s: str, tx: str | None, dd: Any) -> list[list[dict]]:
    """Buttons for a token that trades somewhere Kleos/BOXA/Rabby links cannot reach: the
    pool or curve and the router the buyers actually used, on the explorer."""
    rows = [[{"text": f"📊 Agora: {s}", "url": L.agora_token(token)},
             {"text": "🔍 Token", "url": L.explorer_token(token)}]]
    r2 = []
    if getattr(dd, "venue", None) == "ascend":
        r2.append({"text": "🚀 Ascend", "url": ASCEND_APP})
    elif dd is not None and dd.pool and dd.pool not in SHARED:
        r2.append({"text": "🏊 Curve" if dd.pool_kind == "curve" else "🏊 Pool",
                   "url": L.address(dd.pool)})
    if dd is not None and dd.router and getattr(dd, "venue", None) != "ascend":
        r2.append({"text": "🔀 Router", "url": L.address(dd.router)})
    if tx:
        r2.append({"text": "🧾 Tx", "url": L.tx(tx)})
    if r2:
        rows.append(r2)
    return rows


def _qty(v: float, sym: str | None, hype_usd: float | None) -> str:
    if sym == "HYPE":
        return _money(v, hype_usd)
    if sym == "USDC":
        return f"${v:,.2f}"
    num = f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.4g}"
    return f"{num} {esc(sym or '')}".strip()


def _ascend_lines(dd: Any, L: Links, hype_usd: float | None) -> list[str]:
    sym = dd.quote_symbol or "quote"
    head = f"🚀 <b>Ascend</b> · {esc(sym)} pair"
    if dd.fee_pct is not None:
        head += f" · fee <b>{dd.fee_pct:g}%</b>"
    if dd.dev_pct is not None:
        head += f" · dev bought {dd.dev_pct:.2f}%"
    when = ago(int(dd.launched_at.timestamp())) if dd.launched_at is not None else None
    if when:
        head += f" · launched {when} ago"
    out = [head]
    if dd.paused:
        out.append("⛔ <b>Ascend trading is PAUSED</b>: sells are blocked on every Ascend pool")
    if dd.fee_pct is not None and dd.fee_pct > 3:
        out.append(f"⚠️ High fee: ~{2 * dd.fee_pct:g}% to buy and sell back")
    if dd.liquidity_quote is not None:
        mc = f" · MC {_qty(dd.mcap_quote, sym, hype_usd)}" if dd.mcap_quote else ""
        out.append(f"💧 Pool holds <b>{_qty(dd.liquidity_quote, sym, hype_usd)}</b> "
                   f"(exit ceiling){mc}")
    else:
        out.append("💧 <i>Pool reserve could not be read</i>")
    if dd.bid_quote:
        out.append(f"🧱 Standing bid {_qty(dd.bid_quote, sym, hype_usd)} absorbs sells near the average price")
    if dd.escrow_balance is not None:
        cl = f", claimable now {_qty(dd.escrow_claimable, sym, hype_usd)}" \
            if dd.escrow_claimable is not None else ""
        un = f", {_qty(dd.unsettled_fees, sym, hype_usd)} not yet settled" if dd.unsettled_fees else ""
        out.append(f"🔒 Creator escrow {_qty(dd.escrow_balance, sym, hype_usd)}{cl}{un} "
                   "<i>(held fees unlock only if MC holds ≥$40K for 1 h within 14 days)</i>")
    return out


def dd_lines(dd: Any, L: Links, hype_usd: float | None) -> list[str]:
    """🛡 Due diligence block (read-only RPC facts; every line is optional)."""
    if dd is None:
        return []
    out = ["🛡 <b>Due diligence</b>"]
    if getattr(dd, "venue", None) == "ascend":
        out += _ascend_lines(dd, L, hype_usd)
    elif getattr(dd, "venue", None) == "v4":
        out.append("💧 <i>Uniswap v4 pool: the PoolManager holds every v4 pool, so this "
                   "token's own liquidity is unknown here</i>")
    elif dd.pool:
        kind = "Bonding curve" if dd.pool_kind == "curve" else "Pool"
        liq = ""
        if dd.liquidity_hype is not None:
            liq = f" holds <b>{_money(dd.liquidity_hype, hype_usd)}</b> (exit ceiling)"
        out.append(f"💧 {kind} {a(L.address(dd.pool), '<code>' + short(dd.pool) + '</code>')}{liq}")
    else:
        out.append("💧 <i>No pool or curve found for this token yet</i>")
    if dd.router:
        out.append(f"🔀 Router/venue {a(L.address(dd.router), '<code>' + short(dd.router) + '</code>')}")
    if dd.top_pct is not None and dd.top_holder:
        who_ = "owner" if dd.top_is_owner else "wallet"
        out.append(f"👑 Largest holder ({who_}) "
                   f"{a(L.address(dd.top_holder), '<code>' + short(dd.top_holder) + '</code>')} "
                   f"<b>{dd.top_pct:.1f}%</b> of supply")
    elif dd.owner_pct is not None and dd.owner:
        out.append(f"👑 Owner holds <b>{dd.owner_pct:.1f}%</b> of supply")
    if dd.sellers is not None:
        sold = f", {dd.sold_pct:.1f}% of supply" if dd.sold_pct is not None else ""
        w = getattr(dd, "flow_window_s", None)
        span = ("since launch" if getattr(dd, "launch_block", None) and w is not None and w < 3600
                else f"in the last {round(w / 60)} min" if w else "recently")
        out.append(f"🚪 {dd.sellers} wallet(s) sold into it {span}{sold}"
                   if dd.sellers else f"🚪 Nobody has sold into it {span} (a sell is untested)")
    if dd.risks:
        out.append("⚠️ " + esc("; ".join(dd.risks[:4])))
    elif dd.decimals is not None:
        out.append("✅ No mint/pause/blacklist functions seen in the code")
    if dd.partial:
        out.append("<i>Some checks failed or timed out; missing lines are unknown, not safe.</i>")
    return out


def name_of(p: dict) -> str:
    """Short handle for the trader: label, else #rank, else short address."""
    if p.get("label"):
        return esc(p["label"])
    if p.get("rank"):
        return f"#{p['rank']}"
    return short(p["wallet"])


def who(L: Links, p: dict, hype_usd: float | None = None) -> str:
    """🔹 #42 · 0x93f5…4489 · PnL $15.7K  (rank -> Agora trader page, address -> Elyscan)"""
    w = p["wallet"]
    bits = []
    if p.get("label"):
        bits.append(f"<b>{esc(p['label'])}</b>")
    if p.get("rank"):
        bits.append(a(L.agora_trader(w), f"<b>#{p['rank']}</b>"))
    bits.append(a(L.address(w), f"<code>{short(w)}</code>"))
    if p.get("pnl") is not None:
        unit = (p.get("pnl_unit") or "USD").upper()
        v = float(p["pnl"])
        if unit == "USD":
            bits.append(f"PnL {usd(v, signed=True)}")
        elif hype_usd:
            bits.append(f"PnL {usd(v * hype_usd, signed=True)}")
        else:
            bits.append(f"PnL {v:+,.2f} {esc(unit)}")
    return "🔹 " + " · ".join(bits)


def _header(p: dict, L: Links, info: TokenInfo | None, sym: str) -> str:
    kind, conf = p["kind"], p.get("confidence", "high")
    label = {"TRANSFER_IN": "RECEIVED", "TRANSFER_OUT": "SENT", "MULTI": "MULTI"}.get(kind, kind)
    icon = ICON.get(kind, "•")
    if kind == "TRANSFER_OUT" and conf == "unknown":
        label, icon = "OUT (sell?)", "🟠"
    new = "🆕" if p.get("is_new") or "🆕 new position" in (p.get("notes") or []) else ""
    pad = (info.launchpad if info else None) or p.get("launchpad")
    pad_s = f" ({esc(LAUNCHPAD_NAMES.get(pad, pad))})" if pad else ""
    title = a(L.agora_token(p["token"]), f"<b>{esc(sym)}</b>")
    return f"{new}{icon} <b>{esc(label)}</b> {title}{pad_s}"


def render_trade(p: dict, L: Links, info: TokenInfo | None = None,
                 hype_usd: float | None = None, now: float | None = None,
                 dd: Any = None) -> tuple[str, dict]:
    kind, token, conf = p["kind"], p["token"], p.get("confidence", "high")
    sym = (info.symbol if info and info.symbol else None) or p.get("symbol") or token[:8]
    dec = int(p.get("decimals") or 18)
    qty = int(p["qty"]) / 10 ** dec
    approx = conf != "high"
    me = name_of(p)
    lines = [_header(p, L, info, sym)]
    if p.get("cohort_wallets"):
        lines.append("<b>" + esc(warning_line(int(p["cohort_wallets"]))) + "</b>")
    if kind in ("BUY", "SELL"):
        lines.append(f"💰 {market_cap_label(info, hype_usd) or '<b>MC</b>: unavailable'}")
    lines += [who(L, p, hype_usd), ""]
    hype_wei = int(p["hype_wei"]) if p.get("hype_wei") else None
    tok = a(L.agora_token(token), esc(sym))
    mkt_px = info.price_hype if info else None
    if kind in ("BUY", "SELL") and hype_wei:
        h = hype_wei / WEI
        px = price_str(h / qty * hype_usd) if (qty and hype_usd) else None
        at = f" @{px}" if px else ""
        if kind == "BUY":
            lines.append(f"🔹 {me} swapped <b>{_money(h, hype_usd, approx)}</b> for "
                         f"<b>{qty_str(qty)}</b> {tok}{at}")
        else:
            lines.append(f"🔹 {me} swapped <b>{qty_str(qty)}</b> {tok} for "
                         f"<b>{_money(h, hype_usd, approx)}</b>{at}")
    elif kind == "SWAP" and p.get("counter_token"):
        cq = int(p["counter_qty"]) / 10 ** int(p.get("counter_decimals") or 18)
        csym = esc(p.get("counter_symbol") or p["counter_token"][:8])
        lines.append(f"🔹 {me} swapped <b>{qty_str(cq)}</b> "
                     f"{a(L.agora_token(p['counter_token']), csym)} for <b>{qty_str(qty)}</b> {tok}")
    else:
        val = _to_usd(qty * mkt_px if mkt_px else None, hype_usd)
        v = f" ({usd(val)})" if val else ""
        cp = p.get("counterparty")
        cps = f" {'to' if kind == 'TRANSFER_OUT' else 'from'} " \
              f"{a(L.address(cp), '<code>' + short(cp) + '</code>')}" if cp else ""
        verb = {"TRANSFER_IN": "received", "TRANSFER_OUT": "sent"}.get(kind, "moved")
        lines.append(f"🔹 {me} {verb} <b>{qty_str(qty)}</b> {tok}{v}{cps}")
        if kind == "TRANSFER_OUT" and conf == "unknown":
            lines.append("<i>No HYPE came back in this tx: possibly a sell via a router, "
                         "or a move to another wallet.</i>")
    # position outcome
    if p.get("sold_pct") is not None and kind in ("SELL", "TRANSFER_OUT", "MULTI"):
        sp = float(p["sold_pct"])
        verb = "Sold" if kind == "SELL" else "Moved"
        lines.append(f"🚪 {verb}: 100% (full exit)" if sp >= 99.95
                     else f"➖ {verb}: {sp:.1f}% of bag")
    if kind == "SELL":
        st = p.get("realized_status")
        if st in ("measured", "estimated") and p.get("realized_wei") is not None:
            pnl = int(p["realized_wei"]) / WEI
            pct = p.get("realized_pct")
            icon = "📈" if pnl >= 0 else "📉"
            pct_s = f" ({'≈' if st == 'estimated' else ''}{pct:+.1f}%)" if pct is not None else ""
            lines.append(f"{icon} PnL: <b>{_pnl_usd(pnl, hype_usd, st == 'estimated')}</b>{pct_s}")
        elif st == "unknown":
            why = ("part of the bag arrived by transfer, cost unknown"
                   if p.get("realized_reason") == "unknown_cost" else "bought before tracking")
            lines.append(f"📊 PnL: <i>unknown ({why})</i>")
    hold = _holds_line(p, info, hype_usd, dec)
    if hold:
        lines.append(hold)
    notes = p.get("notes") or []
    if p.get("v", 1) >= 2:   # structured fields above replace most notes
        legacy = [n for n in notes if "of this sale bought before tracking" in n]
    else:                    # payloads queued before this layout: keep their notes
        legacy = [n for n in notes if not n.startswith("🆕")]
    for n in legacy:
        lines.append(f"<i>{esc(n)}</i>")
    if conf == "estimated":
        lines.append(f"<i>≈ HYPE amount estimated ({esc(p.get('hype_method', ''))})</i>")
    lines.append("")
    if (m := market_line(info, hype_usd)) is not None:
        lines.append(m)
    lines.append(link_bar(L, token, sym, info, hype_usd, now,
                          venue_pages=not unknown_venue(info, dd),
                          show_market_cap=kind not in ("BUY", "SELL")))
    lines.append(f"<code>{esc(token)}</code>")
    when = ago(p.get("ts"), now)
    lines.append(f"🧾 {a(L.tx(p['tx']), 'Tx')}" + (f" · {when} ago" if when else ""))
    if dd is not None:
        lines += [""] + dd_lines(dd, L, hype_usd)
    return "\n".join(lines), keyboard(L, token, sym, info, p["tx"], p["wallet"], dd=dd)


def _holds_line(p: dict, info: TokenInfo | None, hype_usd: float | None, dec: int
                ) -> str | None:
    """✊ Holds: 23.29M (2.33%) 📈 uPnL: +$12.40  (only what the tracker has observed)."""
    if p.get("pos_qty") is None:
        return None
    q = int(p["pos_qty"]) / 10 ** dec
    if q <= 0:
        return None
    s = f"✊ Holds: <b>{qty_str(q)}</b>"
    sup = supply(info)
    if sup:
        s += f" ({q / sup * 100:.2f}%)"
    basis = p.get("pos_basis")
    if info and info.price_hype and basis in ("known", "estimated") and p.get("pos_cost_wei"):
        upnl = q * info.price_hype - int(p["pos_cost_wei"]) / WEI
        s += f" {'📈' if upnl >= 0 else '📉'} uPnL: {_pnl_usd(upnl, hype_usd, basis != 'known')}"
    return s


def render_cluster(p: dict, L: Links, info: TokenInfo | None = None,
                   hype_usd: float | None = None, now: float | None = None,
                   dd: Any = None) -> tuple[str, dict]:
    token = p["token"]
    sym = (info.symbol if info and info.symbol else None) or p.get("symbol") or token[:8]
    strong = bool(p.get("strong"))
    icon = "🚨🚨" if strong else "🚨"
    lines = [f"{icon} <b>CLUSTER BUY</b> {a(L.agora_token(token), '<b>' + esc(sym) + '</b>')}"
             + (" ✅ independent" if strong else "")]
    if p.get("tier"):
        n_raw = p.get("n_raw", len(p["buyers"]))
        lines.append(f"🎯 <b>{esc(p['tier'])}</b> · {n_raw} addresses")
    if p.get("cohort_wallets"):
        lines.append("<b>" + esc(warning_line(int(p["cohort_wallets"]))) + "</b>")
    lines += [f"<b>{len(p['buyers'])} tracked wallets</b> bought ≥{p['min_hype']:g} HYPE within "
              f"{p['window_min']} min", ""]
    for b in p["buyers"][:10]:
        lines.append(who(L, b, hype_usd))
    note = ("Same-block buyers and known bundle wallets count as one operator."
            if p.get("tier") else "Measured buys only; different addresses may share an owner.")
    lines += [f"<i>{note}</i>", ""]
    if (m := market_line(info, hype_usd)) is not None:
        lines.append(m)
    unk = unknown_venue(info, dd)
    lines.append(link_bar(L, token, sym, info, hype_usd, now, venue_pages=not unk))
    lines.append(f"<code>{esc(token)}</code>")
    if dd is not None:
        lines += [""] + dd_lines(dd, L, hype_usd)
    return "\n".join(lines), keyboard(L, token, sym, info, p.get("tx"), dd=dd)


def render_launch(p: dict, L: Links, info: TokenInfo | None = None,
                  hype_usd: float | None = None, now: float | None = None,
                  dd: Any = None) -> tuple[str, dict]:
    """New venue alerts: a launchpad Agora has not listed before, or a fresh DEX pool."""
    if p.get("kind") == "new_launchpad":
        name = p.get("launchpad") or "?"
        lines = [f"🆕🚀 <b>NEW LAUNCHPAD</b> <code>{esc(name)}</code>",
                 "Agora lists tokens from a launchpad this tracker has not seen before."]
        for tk in (p.get("tokens") or [])[:5]:
            sy = esc(tk.get("symbol") or tk["token"][:8])
            lines.append(f"• {a(L.agora_token(tk['token']), sy)} <code>{esc(tk['token'])}</code>")
        lines.append("<i>Its token pages and trade routes are unknown here: check the "
                     "venue before buying.</i>")
        rows = [[{"text": f"📊 Agora: {(tk.get('symbol') or tk['token'][:6])[:12]}",
                  "url": L.agora_token(tk["token"])}] for tk in (p.get("tokens") or [])[:3]]
        return "\n".join(lines), {"inline_keyboard": rows}
    token = p["token"]
    sym = (info.symbol if info and info.symbol else None) or p.get("symbol") or token[:8]
    lines = [f"🆕🏊 <b>NEW DEX POOL</b> {a(L.agora_token(token), '<b>' + esc(sym) + '</b>')}",
             f"Pool {a(L.address(p['pool']), '<code>' + short(p['pool']) + '</code>')} holds "
             f"<b>{_money(p['liquidity_hype'], hype_usd)}</b> {p.get('age_s', 0) // 60} min after "
             f"creation"]
    if p.get("factory"):
        lines.append(f"Factory {a(L.address(p['factory']), '<code>' + short(p['factory']) + '</code>')}"
                     + (f" · fee {int(p['fee']) / 10000:g}%" if p.get("fee") else ""))
    lines.append("<i>A venue Agora may not index: it is not a Kleos/BOXA/ElysPump token.</i>")
    if (m := market_line(info, hype_usd)) is not None:
        lines.append(m)
    lines.append(f"<code>{esc(token)}</code>")
    if dd is not None:
        lines += [""] + dd_lines(dd, L, hype_usd)
    class _P:  # the pool is known from the event even when the DD probe is unavailable
        pool = p["pool"]
        pool_kind = "pool"
        router = getattr(dd, "router", None)
    return "\n".join(lines), keyboard(L, token, sym, info, None, dd=dd if dd is not None
                                      and dd.pool else _P(), force_unknown=True)


def preview_url(L: Links, info: TokenInfo | None) -> str | None:
    """The token's picture (Agora image proxy), shown above the card as a link preview."""
    if info is None or not info.image:
        return None
    img = info.image
    if img.startswith("/"):
        img = L.c.agora.rstrip("/") + img
    return img if img.startswith("https://") else None


def wants_dd(payload: dict | None) -> bool:
    """Alerts that get the due-diligence block: clusters, launches and first buys."""
    if not payload:
        return False
    t_ = payload.get("type")
    return t_ in ("cluster", "launch") or (
        t_ == "trade" and payload.get("kind") == "BUY"
        and (payload.get("is_new") or "🆕 new position" in (payload.get("notes") or [])))


def render(payload: dict | None, L: Links, info: TokenInfo | None = None,
           hype_usd: float | None = None, dd: Any = None) -> tuple[str, dict] | None:
    if not payload:
        return None
    if payload.get("type") == "trade":
        return render_trade(payload, L, info, hype_usd, dd=dd)
    if payload.get("type") == "cluster":
        return render_cluster(payload, L, info, hype_usd, dd=dd)
    if payload.get("type") == "launch":
        return render_launch(payload, L, info, hype_usd, dd=dd)
    return None
