"""Telegram Intelligence Center views: report dicts -> (HTML text, inline keyboard).

Pure functions (no I/O) so every screen is unit-testable. Callback data is compact and
always <= 64 bytes (Telegram's limit); see tgui.CALLBACKS for the grammar.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .cards import Links, short
from .timerange import PRESETS, TimeRange, tz_label

UTC = timezone.utc
MAX_TEXT = 4000                       # Telegram's limit is 4096; keep margin for entities
Kb = dict[str, list[list[dict[str, str]]]]

MENU = [  # (label, callback) in the required 5x2 layout
    [("📊 Market Overview", "mo"), ("🧠 Smart Money", "sm|net|0")],
    [("🚀 Trending Projects", "tr|score|0"), ("🐳 Top Traders", "tt|agora|0")],
    [("📈 Top Gainers", "gl|up|price|0"), ("📉 Top Losers", "gl|down|price|0")],
    [("🔍 Search Wallet/Token", "s"), ("📤 Export Report", "x")],
    [("⚙️ System Health", "hl"), ("🕒 Time Range", "rg")],
]
MENU_TEXT = {label: cb for row in MENU for label, cb in row}

SM_TITLES = {"net": "Net verified buying", "buyers": "Most qualified buyers",
             "new": "Bought by newly ranked traders", "repeat": "Repeated accumulation",
             "exits": "Profitable wallets exiting", "clusters": "Coordinated buys (signals)"}
SM_TABS = [("Net", "net"), ("Buyers", "buyers"), ("New", "new"), ("Repeat", "repeat"),
           ("Exits", "exits"), ("Clusters", "clusters")]
TT_TITLES = {"agora": "Agora-reported PnL", "realized": "Measured realized PnL",
             "active": "Most active", "consistent": "Most consistent",
             "buyers": "Most active buyers", "movers": "Biggest rank gains"}
TT_TABS = [("Agora PnL", "agora"), ("Realized", "realized"), ("Active", "active"),
           ("Consistent", "consistent"), ("Buyers", "buyers"), ("Movers", "movers")]
TR_TABS = [("Score", "score"), ("Smart $", "smart"), ("Buying", "buying"),
           ("Newest", "new"), ("Growth", "growth")]
GL_TABS = [("Price", "price"), ("Volume", "volume"), ("Smart flow", "flow")]
EXPORT_KINDS = [("Market Overview", "market"), ("Smart Money", "smart"),
                ("Trending Projects", "trending"), ("Gainers / Losers", "perf"),
                ("Top Traders", "traders"), ("Wallet Transactions", "wallet"),
                ("Token Activity", "token"), ("Complete Intelligence Report", "full")]
EXPORT_NAMES = dict((k, n) for n, k in EXPORT_KINDS)
POPULATION = ("Tracked wallets = Agora top-100 PnL leaderboard + your watchlist, "
              "not chain-wide.")


# --- formatting ----------------------------------------------------------------------------
def e(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=False)


def a(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>'


def num(x: Any, signed: bool = False) -> str:
    if x is None:
        return "n/a"
    v = float(x)
    sign = "+" if signed and v > 0 else ""
    av = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if av >= div:
            return f"{sign}{v / div:.2f}{suf}"
    if av >= 1 or av == 0:
        return f"{sign}{v:,.2f}"
    if av >= 0.01:
        return f"{sign}{v:.3f}"
    return f"{sign}{v:.2g}"


def price(x: Any) -> str:
    return "n/a" if x is None else f"{float(x):.3g}"


def pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.1f}%" if abs(x) < 10 else f"{x * 100:+,.0f}%"


def delta(cur: Any, prev: Any) -> str:
    if not prev:
        return "new" if cur else "–"
    ch = (float(cur) - float(prev)) / float(prev)
    return ("▲" if ch >= 0 else "▼") + f"{abs(ch):.0%}"


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "never"
    s = int(((now or datetime.now(UTC)) - dt).total_seconds())
    if s < 0:
        return "just now"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return f"{s}s ago"


def local(dt: datetime | None, tz: ZoneInfo, f: str = "%d %b %H:%M") -> str:
    return "n/a" if dt is None else dt.astimezone(tz).strftime(f)


def sym(r: dict) -> str:
    return e(r.get("symbol") or (r.get("token") or "")[:8])


def btn(text: str, data: str) -> dict[str, str]:
    assert len(data.encode()) <= 64, data
    return {"text": text, "callback_data": data}


def url_btn(text: str, url: str) -> dict[str, str]:
    return {"text": text, "url": url}


def kb(*rows: list[dict[str, str]]) -> Kb:
    return {"inline_keyboard": [r for r in rows if r]}


def clip(lines: list[str]) -> str:
    """Join lines, dropping trailing ones that would exceed Telegram's length limit."""
    out, n = [], 0
    for ln in lines:
        if n + len(ln) + 1 > MAX_TEXT - 40:
            out.append("<i>… truncated; export for the full list</i>")
            break
        out.append(ln)
        n += len(ln) + 1
    return "\n".join(out)


def controls(refresh: str, export: str | None = "x", back: str | None = "h") -> list[dict]:
    row = [btn("🔄 Refresh", refresh), btn("🕒 Period", "rg")]
    if export:
        row.append(btn("📤 Export", export))
    return row


def home_row(back: str | None = None) -> list[dict]:
    row = []
    if back:
        row.append(btn("⬅️ Back", back))
    row.append(btn("🏠 Home", "h"))
    return row


def pager(prefix: str, page: int, total: int, size: int) -> list[dict]:
    pages = max(1, -(-total // size))
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}|{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", "n"))
    if page < pages - 1:
        row.append(btn("▶️", f"{prefix}|{page + 1}"))
    return row


def tabs(items: list[tuple[str, str]], current: str, prefix: str, suffix: str = "|0",
         per_row: int = 3) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for i in range(0, len(items), per_row):
        rows.append([btn(("✓ " if key == current else "") + label, f"{prefix}|{key}{suffix}")
                     for label, key in items[i:i + per_row]])
    return rows


def header(title: str, tr: TimeRange, cov: dict | None, tz: ZoneInfo,
           now: datetime | None = None) -> list[str]:
    lines = [f"<b>{title}</b>",
             f"🕒 {e(tr.label())} · {e(tr.fmt(tz))} <i>({e(tz_label(tz, tr.end))})</i>"]
    if cov is not None:
        icon = {"full": "✅ full", "partial": "⚠️ partial", "none": "⛔ no data"}[cov["status"]]
        bits = [f"📦 block {cov['cursor'] if cov.get('cursor') is not None else 'n/a'}"]
        if cov.get("lag") is not None:
            bits.append(f"lag {cov['lag']}")
        if cov.get("indexed_through") is not None:
            bits.append(f"indexed {ago(cov['indexed_through'], now)}")
        bits.append(f"coverage {icon}")
        lines.append(" · ".join(bits))
        for w in cov.get("warnings", [])[:2]:
            lines.append(f"<i>⚠️ {e(w)}</i>")
    return lines


def universe_line(uni: Any, tz: ZoneInfo) -> str:
    if uni.basis == "leaderboard":
        s = (f"👥 qualified = Agora PnL &gt; 0 in the snapshot of "
             f"{local(uni.captured_at, tz)} ({len(uni.qualified)} wallets)")
    else:
        s = "👥 <b>unscored</b>: no leaderboard snapshot, every tracked wallet counts"
    if uni.note:
        s += f"\n<i>{e(uni.note)}</i>"
    return s


# --- screens -----------------------------------------------------------------------------
def home(tr: TimeRange, cov: dict | None, tz: ZoneInfo, wallets: int | None) -> tuple[str, Kb]:
    lines = ["<b>🛰 ELYSIUM INTELLIGENCE CENTER</b>",
             "Welcome to your Elysium blockchain intelligence platform.",
             "Monitor smart money, discover trending projects, inspect wallets and export "
             "historical reports.", "",
             f"🕒 Period: <b>{e(tr.label())}</b> · {e(tr.fmt(tz))}"]
    if cov is not None:
        lines.append(f"📦 Indexed to block {cov.get('cursor') or 'n/a'}"
                     + (f" · {wallets} wallets tracked" if wallets is not None else ""))
    rows = [[btn(label, cb) for label, cb in row] for row in MENU]
    return "\n".join(lines), {"inline_keyboard": rows}


def reply_keyboard() -> dict:
    return {"keyboard": [[{"text": label} for label, _ in row] for row in MENU],
            "is_persistent": True, "resize_keyboard": True,
            "input_field_placeholder": "Address, symbol or /menu"}


REMOVE_KEYBOARD = {"remove_keyboard": True}


def market_overview(o: dict, cov: dict, tz: ZoneInfo, L: Links) -> tuple[str, Kb]:
    t, p, tr = o["totals"], o["prev"], o["range"]
    lines = header("📊 MARKET OVERVIEW · tracked wallets", tr, cov, tz)
    lines += ["", f"<b>Activity</b> <i>(vs previous {_dur(tr)})</i>",
              f"• Verified volume: <b>{num(t['volume'])} HYPE</b> "
              f"(buy {num(t['buy'])} / sell {num(t['sell'])}) {delta(t['volume'], p['volume'])}",
              f"• Trades: {t['legs']} legs · {t['buys']} buys · {t['sells']} sells verified · "
              f"{t['est_legs']} estimated · {t['transfers'] + t['other_legs']} transfers/other",
              f"• Active wallets: {t['wallets']} ({delta(t['wallets'], p['wallets'])}) · "
              f"tokens: {t['tokens']} · first seen: {t['new_tokens']}",
              f"• Smart-money signals: {t['signals']}"]
    if t["buy_est"] or t["sell_est"]:
        lines.append(f"• Estimated (not in totals): buy {num(t['buy_est'])} / "
                     f"sell {num(t['sell_est'])} HYPE")
    lines += ["", "<b>Direction</b>",
              f"• Net: <b>{num(t['net'], True)} HYPE</b> · qualified net "
              f"{num(t['q_buy'] - t['q_sell'], True)} HYPE"]
    if o.get("concentration_top3") is not None:
        lines.append(f"• Top 3 tokens = {o['concentration_top3']:.0%} of verified volume")
    if o["rising"]:
        lines.append("• Rising buyers: " + ", ".join(
            f"{a(L.agora_token(r['token']), sym(r))} ({r['prev_buyers']}→{r['buyers']})"
            for r in o["rising"][:4]))
    if o["selling"]:
        lines.append("• Heavy selling: " + ", ".join(
            f"{a(L.agora_token(r['token']), sym(r))} {num(r['net'], True)}"
            for r in o["selling"][:4]))
    if o["top_tokens"]:
        lines.append("• Most traded: " + ", ".join(
            f"{a(L.agora_token(r['token']), sym(r))} {num(r['buy'] + r['sell'])}"
            for r in o["top_tokens"][:4]))
    if o["launchpads"]:
        lines += ["", "<b>Launchpads</b> <i>(verified HYPE)</i>",
                  " · ".join(f"{e(x['launchpad'])} {num(x['volume'])} ({x['tokens']} tok)"
                             for x in o["launchpads"][:5])]
    lines += ["", "<b>Summary</b>"] + [f"{e(s)}" for s in o["summary"]]
    lines += ["", f"<i>{POPULATION} Verified = HYPE leg measured on-chain. USD is not shown: "
                  "there is no time-aligned HYPE/USD history.</i>"]
    return clip(lines), kb([btn("📊 Compare Periods", "mo|cmp"), btn("🧠 Smart Money",
                                                                    "sm|net|0")],
                          controls("mo", "x|t|market"), home_row())


def compare(o: dict, cov: dict, tz: ZoneInfo) -> tuple[str, Kb]:
    t, p, tr = o["totals"], o["prev"], o["range"]
    rows = [("Verified volume", t["volume"], p["volume"]), ("Verified buys", t["buy"], p["buy"]),
            ("Verified sells", t["sell"], p["sell"]), ("Net", t["net"], p["net"]),
            ("Trade legs", t["legs"], p["legs"]), ("Buys (count)", t["buys"], p["buys"]),
            ("Sells (count)", t["sells"], p["sells"]), ("Wallets", t["wallets"], p["wallets"]),
            ("Tokens", t["tokens"], p["tokens"])]
    w = max(len(r[0]) for r in rows)
    body = [f"{'':{w}}  {'now':>9} {'before':>9}  Δ"]
    for name, cur, prev in rows:
        body.append(f"{name:{w}}  {num(cur):>9} {num(prev):>9}  {delta(cur, prev)}")
    lines = header("📊 COMPARE PERIODS · tracked wallets", tr, cov, tz)
    lines += [f"Previous: {e(o['previous'].fmt(tz))}", "", "<pre>" + e("\n".join(body)) + "</pre>",
              f"<i>{POPULATION} HYPE amounts are verified only.</i>"]
    return clip(lines), kb([btn("⬅️ Overview", "mo")], controls("mo|cmp", "x|t|market"),
                          home_row())


def smart_money(d: dict, cov: dict, tz: ZoneInfo, L: Links, page: int, size: int,
                market: dict[str, Any]) -> tuple[str, Kb]:
    mode, tr, rows = d["mode"], d["range"], d["rows"]
    lines = header(f"🧠 SMART MONEY · {SM_TITLES[mode]}", tr, cov, tz)
    lines.append(universe_line(d["universe"], tz))
    if d.get("note"):
        lines.append(f"<i>{e(d['note'])}</i>")
    lines.append("")
    chunk = rows[page * size:(page + 1) * size]
    buttons: list[list[dict]] = []
    if not rows:
        lines.append("No qualifying activity in this window.")
    for i, r in enumerate(chunk, start=page * size + 1):
        if mode == "clusters":
            lines.append(f"{i}. {a(L.agora_token(r['token']), '<b>' + sym(r) + '</b>')} · "
                         f"{r['n_wallets']} wallets · {local(r['created_at'], tz)}")
            lines.append(f"   <code>{e(r['token'])}</code>")
            buttons.append([btn(f"🔍 {r.get('symbol') or r['token'][:8]}"[:30], f"tk|{r['token']}"),
                            btn("🐳 Buyers", f"tb|{r['token']}|0")])
            continue
        info = market.get(r["token"])
        lp = f" · {e(r.get('launchpad'))}" if r.get("launchpad") else ""
        name = f" · {e(r['name'])}" if r.get("name") and r.get("name") != r.get("symbol") else ""
        lines.append(f"{i}. {a(L.agora_token(r['token']), '<b>' + sym(r) + '</b>')}{name}{lp}")
        lines.append(f"   👥 {r['q_buyers']} qualified buyers / {r['q_sellers']} sellers"
                     f" (all tracked: {r['buyers']}/{r['sellers']})")
        lines.append(f"   ✓ buy {num(r['q_buy'])} · sell {num(r['q_sell'])} · "
                     f"<b>net {num(r['q_net'], True)} HYPE</b>"
                     + (f" · ≈est {num(r['buy_est'] + r['sell_est'])}"
                        if r["buy_est"] or r["sell_est"] else ""))
        extra = []
        if info is not None and info.mcap_hype is not None:
            extra.append(f"MC {num(info.mcap_hype)} HYPE (now)")
        if mode == "repeat":
            extra.append(f"bought in {r['q_buy_intervals']}/4 sub-intervals")
        if r["transfers"]:
            extra.append(f"{r['transfers']} transfers (not counted)")
        if extra:
            lines.append("   " + " · ".join(extra))
        lines.append(f"   <code>{e(r['token'])}</code>")
        buttons.append([btn(f"🔍 {r.get('symbol') or r['token'][:8]}"[:30], f"tk|{r['token']}"),
                        btn("🐳 Buyers", f"tb|{r['token']}|0")])
    lines += ["", f"<i>{POPULATION} ✓ = verified HYPE; estimates shown apart. Distinct "
                  "addresses may share an owner.</i>"]
    k = kb(*tabs(SM_TABS, mode, "sm"), *buttons, pager(f"sm|{mode}", page, len(rows), size),
           controls(f"sm|{mode}|{page}", "x|t|smart"), home_row())
    return clip(lines), k


def token_detail(d: dict, cov: dict, tz: ZoneInfo, L: Links, hype_usd: float | None,
                 now: datetime | None = None) -> tuple[str, Kb]:
    tok, m, info, f = d["token"], d["meta"], d["info"], d["flow"]
    symbol = (info.symbol if info else None) or m.get("symbol") or tok[:8]
    name = (info.name if info else None) or m.get("name")
    pad = (info.launchpad if info else None) or m.get("launchpad")
    title = f"🪙 {e(symbol)}" + (f" · {e(name)}" if name and name != symbol else "")
    lines = [f"<b>{title}</b>" + (f" · {e(pad)}" if pad else ""), f"<code>{e(tok)}</code>"]
    lines += header("", d["range"], cov, tz, now)[1:]
    lines += ["", "<b>Market</b> <i>(Agora, live now)</i>"]
    if info is None:
        lines.append("Market data unavailable (Agora did not return this token).")
    else:
        usd = f" (${num(info.mcap_hype * hype_usd)})" if hype_usd and info.mcap_hype else ""
        bits = [f"price {price(info.price_hype)} HYPE", f"MC {num(info.mcap_hype)} HYPE{usd}",
                f"24h vol {num(info.vol_24h_hype)} HYPE", f"24h {pct(info.change_24h)}"]
        if info.holders is not None:
            bits.append(f"👥 {info.holders:,} holders")
        if info.created_at:
            bits.append(f"age {ago(datetime.fromtimestamp(info.created_at, UTC), now)[:-4]}")
        lines.append(" · ".join(bits))
        lines.append("<i>liquidity: not published by Agora</i>")
    perf = d.get("perf")
    lines.append("<b>Period price change</b>: " + (
        f"{pct(perf['ret'])} ({price(perf['p0'])} → {price(perf['p1'])} HYPE, "
        f"{e(perf['source'])})" if perf else "n/a (no snapshots or candles cover this window)"))
    lines += ["", "<b>Tracked flow in period</b>"]
    if not f:
        lines.append("No tracked-wallet trades in this window.")
    else:
        lines.append(f"👥 {f['buyers']} buyers ({f['q_buyers']} qualified) · {f['sellers']} sellers")
        lines.append(f"✓ buy {num(f['buy'])} · sell {num(f['sell'])} · "
                     f"<b>net {num(f['net'], True)} HYPE</b>")
        if f["buy_est"] or f["sell_est"]:
            lines.append(f"≈ estimated: buy {num(f['buy_est'])} / sell {num(f['sell_est'])} HYPE")
        if f["transfers"] or f["other_legs"]:
            lines.append(f"{f['transfers']} transfers, {f['other_legs']} other legs (not counted)")
    if d["recent"]:
        lines += ["", "<b>Recent tracked trades</b>"]
        for r in d["recent"][:6]:
            lines.append(trade_line(r, tz, L, show_token=False))
    seen = d.get("seen") or {}
    if seen.get("first_at"):
        lines.append(f"<i>first tracked trade: {local(seen['first_at'], tz, '%Y-%m-%d %H:%M')}"
                     f" · {seen.get('n', 0)} legs total</i>")
    lines.append("🔗 " + " · ".join([a(L.agora_token(tok), "Agora"), a(L.kleos_token(tok), "Kleos"),
                                     a(L.boxa_token(tok), "BOXA"),
                                     a(L.explorer_token(tok), "Elyscan")]))
    k = kb(L.wallet_buttons(tok, (symbol or tok[:6])[:12]),
           [btn("🐳 View Buyers", f"tb|{tok}|0"), btn("🧾 Trades", f"tx|{tok}|0")],
           [url_btn("🛒 Agora", L.agora_token(tok)), url_btn("Kleos", L.kleos_token(tok)),
            url_btn("BOXA", L.boxa_token(tok))],
           controls(f"tk|{tok}", f"xs|t|{tok}"), home_row("sm|net|0"))
    return clip(lines), k


def trade_line(r: dict, tz: ZoneInfo, L: Links, show_token: bool = True,
               show_wallet: bool = True) -> str:
    icon = {"BUY": "🟢", "SELL": "🔴", "TRANSFER_IN": "📥", "TRANSFER_OUT": "📤",
            "SWAP": "🔁", "MULTI": "🧩"}.get(r["kind"], "•")
    conf = "" if r["confidence"] == "high" else f" <i>({e(r['confidence'])})</i>"
    amt = f"{num(r['hype'])} HYPE" if r.get("hype") is not None else "HYPE n/a"
    parts = [icon, local(r["block_time"], tz, "%d %b %H:%M")]
    if show_wallet:
        parts.append(a(L.address(r["wallet"]), f"<code>{short(r['wallet'])}</code>"))
    if show_token:
        parts.append(a(L.agora_token(r["token"]), sym(r)))
    parts.append(f"{num(r['qty_units'])} · {amt}{conf}")
    parts.append(a(L.tx(r["tx_hash"]), "tx"))
    return " ".join(parts)


def token_buyers(d: dict, cov: dict, tz: ZoneInfo, L: Links, page: int, size: int
                 ) -> tuple[str, Kb]:
    tok, rows = d["token"], d["rows"]
    s = e(d["meta"].get("symbol") or tok[:8])
    lines = header(f"🐳 {s} · tracked buyers &amp; sellers", d["range"], cov, tz)
    lines += [f"<code>{e(tok)}</code>", universe_line(d["universe"], tz), ""]
    if not rows:
        lines.append("No tracked wallet traded this token in this window.")
    buttons = []
    for i, r in enumerate(rows[page * size:(page + 1) * size], start=page * size + 1):
        rank = f"#{r['rank']}" if r["rank"] else "unranked"
        q = " ⭐" if r["qualified"] else ""
        pnl = f" · Agora PnL {num(r['agora_pnl'])}" if r["agora_pnl"] is not None else ""
        lines.append(f"{i}. {rank}{q} {a(L.agora_trader(r['wallet']), '<code>' + short(r['wallet']) + '</code>')}{pnl}")
        lines.append(f"   ✓ {r['buys']} buys {num(r['buy'])} HYPE · {r['sells']} sells "
                     f"{num(r['sell'])} HYPE" + (f" · ≈{num(r['est'])} est" if r["est"] else "")
                     + (f" · {r['transfers']} transfers" if r["transfers"] else ""))
        lines.append(f"   first buy {local(r['first_buy'], tz)} · last {local(r['last_at'], tz)}")
        buttons.append([btn(f"👤 {rank} {short(r['wallet'])}", f"w|{r['wallet']}")])
    lines += ["", "<i>⭐ = qualified (Agora PnL &gt; 0). Agora PnL is their all-time figure; "
                  "flows are verified HYPE in this window.</i>"]
    return clip(lines), kb(*buttons, pager(f"tb|{tok}", page, len(rows), size),
                          controls(f"tb|{tok}|{page}", f"xs|t|{tok}"), home_row(f"tk|{tok}"))


def trades_list(title: str, rows: list[dict], tr: TimeRange, cov: dict, tz: ZoneInfo,
                L: Links, prefix: str, page: int, size: int, has_more: bool, back: str,
                show_token: bool, show_wallet: bool) -> tuple[str, Kb]:
    lines = header(title, tr, cov, tz) + [""]
    if not rows:
        lines.append("No trades in this window.")
    for r in rows:
        lines.append(trade_line(r, tz, L, show_token, show_wallet))
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"{prefix}|{page - 1}"))
    if has_more:
        nav.append(btn("▶️", f"{prefix}|{page + 1}"))
    return clip(lines), kb(nav, home_row(back))


def top_traders(d: dict, cov: dict, tz: ZoneInfo, L: Links, page: int, size: int
                ) -> tuple[str, Kb]:
    sort, rows = d["sort"], d["rows"]
    lines = header(f"🐳 TOP TRADERS · {TT_TITLES[sort]}", d["range"], cov, tz)
    lines.append(universe_line(d["universe"], tz))
    if d.get("note"):
        lines.append(f"<i>{e(d['note'])}</i>")
    lines.append("")
    if not rows:
        lines.append("Not enough evidence in this window for this ranking.")
    buttons = []
    for i, r in enumerate(rows[page * size:(page + 1) * size], start=page * size + 1):
        rank = f"#{r['rank']}" if r["rank"] else "unranked"
        head = f"{i}. {rank} {a(L.agora_trader(r['wallet']), '<code>' + short(r['wallet']) + '</code>')}"
        if r["agora_pnl"] is not None:
            head += f" · Agora PnL {num(r['agora_pnl'])} HYPE"
        if sort == "movers" and r.get("delta") is not None:
            head += f" · {r['old_rank']}→{r['new_rank']} ({r['delta']:+d})"
        lines.append(head)
        bits = [f"{r['legs']} legs ({r['buys']}B/{r['sells']}S)"]
        if r["tokens_bought"]:
            bits.append(f"bought {r['tokens_bought']} tokens · {num(r['buy'])} HYPE")
        if r["n_measured"]:
            bits.append(f"realized {num(r['realized'], True)} HYPE measured "
                        f"({r['wins']}/{r['n_measured']} wins)")
        if r["realized_est"]:
            bits.append(f"≈{num(r['realized_est'], True)} est.")
        lines.append("   " + " · ".join(bits))
        buttons.append([btn(f"👤 {rank} {short(r['wallet'])}", f"w|{r['wallet']}")])
    lines += ["", "<i>Agora PnL = Agora's all-time accounting. Realized = closing trades we "
                  "measured in this window. They are never mixed.</i>"]
    return clip(lines), kb(*tabs(TT_TABS, sort, "tt"), *buttons,
                          pager(f"tt|{sort}", page, len(rows), size),
                          controls(f"tt|{sort}|{page}", "x|t|traders"), home_row())


def wallet(d: dict, cov: dict, tz: ZoneInfo, L: Links) -> tuple[str, Kb]:
    w, act = d["wallet"], d["activity"]
    lines = [f"<b>👤 WALLET</b> <code>{e(w)}</code>"]
    srcs = [x["source"] for x in d["watch"]]
    label = next((x["label"] for x in d["watch"] if x.get("label")), None)
    status = ("⭐ watched (" + ", ".join(srcs) + ")") if srcs else "not watched"
    lines.append(f"{status}" + (f" · {e(label)}" if label else ""))
    if d["rank"] is not None:
        lines.append(f"🏆 Agora rank #{d['rank']} · PnL {num(d['agora_pnl'])} HYPE "
                     "<i>(Agora-reported, all-time)</i>")
    else:
        lines.append("🏆 not in the latest Agora leaderboard snapshot")
    lines += header("", d["range"], cov, tz)[1:]
    lines += ["", "<b>Period activity</b>",
              f"{act.get('legs', 0)} legs · {act.get('buys', 0)} buys · {act.get('sells', 0)} "
              f"sells verified · {act.get('est_legs', 0)} estimated · "
              f"{act.get('transfers', 0)} transfers · {act.get('tokens', 0)} tokens",
              f"✓ bought {num(d['buy'])} · sold {num(d['sell'])} HYPE"]
    rz = d["realized"]
    lines += ["", "<b>Realized PnL</b> <i>(measured by this bot, this period)</i>"]
    if not rz:
        lines.append("no closing trades in this window")
    else:
        m = rz.get("measured")
        lines.append(f"✓ {num(m['pnl'], True)} HYPE over {m['n']} sells" if m
                     else "✓ none measured")
        for k in ("estimated", "unknown"):
            if k in rz:
                lines.append(f"≈ {k}: {num(rz[k]['pnl'], True)} HYPE over {rz[k]['n']} sells "
                             "(kept separate)" if k == "estimated"
                             else f"? {rz[k]['n']} sells with unknown cost basis")
    if d["tokens"]:
        lines += ["", "<b>Tokens traded</b>"]
        for r in d["tokens"][:5]:
            lines.append(f"• {a(L.agora_token(r['token']), sym(r))} buy {num(r['buy'])} / "
                         f"sell {num(r['sell'])} HYPE · {r['legs']} legs")
    if d["positions"]:
        lines += ["", "<b>Open positions</b> <i>(current, not period-specific)</i>"]
        for r in d["positions"][:5]:
            lines.append(f"• {a(L.agora_token(r['token']), sym(r))} {num(r['qty_units'])} · "
                         f"cost {num(r['cost'])} HYPE ({e(r['basis'])})")
    if d["recent"]:
        lines += ["", "<b>Recent</b>"]
        for r in d["recent"][:5]:
            lines.append(trade_line(r, tz, L, show_wallet=False))
    last = d["last"]
    lines.append(f"\nLast active: {local(last.get('last_at'), tz, '%Y-%m-%d %H:%M')} · "
                 f"tracked since {local(last.get('first_at'), tz, '%Y-%m-%d')}")
    lines.append("<i>All-time returns are not inferred: tracking covers only the period "
                 "since this bot started following the wallet.</i>")
    watch_btn = (btn("✖ Unwatch", f"ww|{w}|0") if "telegram" in srcs
                 else btn("⭐ Watch", f"ww|{w}|1"))
    return clip(lines), kb([watch_btn, btn("🧾 Trades", f"wt|{w}|0")],
                          [url_btn("Agora", L.agora_trader(w)), url_btn("Elyscan", L.address(w))],
                          controls(f"w|{w}", f"xs|w|{w}"), home_row("tt|agora|0"))


def trending(d: dict, cov: dict, tz: ZoneInfo, L: Links, page: int, size: int,
             now: datetime | None = None) -> tuple[str, Kb]:
    rows, sort = d["rows"], d["sort"]
    lines = header("🚀 TRENDING PROJECTS", d["range"], cov, tz, now)
    lines.append(f"<i>{e(d['note'])}</i>")
    if d.get("live") and d.get("meta", {}).get("totals"):
        tot = d["meta"]["totals"]
        lines.append(f"<i>Market population: top 200 by 24h volume of {tot.get('tokens')} "
                     "Elysium tokens on Agora (chain-wide)</i>")
    lines.append("")
    if not rows:
        lines.append("Nothing scored in this window.")
    buttons = []
    nowts = (now or datetime.now(UTC)).timestamp()
    for i, r in enumerate(rows[page * size:(page + 1) * size], start=page * size + 1):
        age = ""
        if r.get("created_at"):
            hours = (nowts - r["created_at"]) / 3600
            age = f" · {hours:.0f}h" if hours < 72 else f" · {hours / 24:.0f}d"
        lp = f" · {e(r['launchpad'])}" if r.get("launchpad") else ""
        lines.append(f"{i}. {a(L.agora_token(r['token']), '<b>' + sym(r) + '</b>')} — "
                     f"<b>{r['score']}/100</b>{lp}{age}")
        for w in r["why"][:4]:
            lines.append(f"   • {e(w)}")
        for fl in r["flags"][:2]:
            lines.append(f"   ⚠️ {e(fl)}")
        if r.get("mcap") is not None:
            lines.append(f"   MC {num(r['mcap'])} HYPE")
        lines.append(f"   <code>{e(r['token'])}</code>")
        buttons.append([btn(f"🔍 {r.get('symbol') or r['token'][:8]}"[:30], f"tk|{r['token']}"),
                        url_btn("Agora", L.agora_token(r["token"])),
                        url_btn("BOXA", L.boxa_token(r["token"]))])
    lines += ["", "<i>Score = smart buyers 30 + smart net 15 + volume acceleration 15 + "
                  "buy pressure 10 + trade frequency 10 + recency 10 + momentum 10, "
                  "−10 wash risk. Holder growth: not available (no holder history).</i>"]
    return clip(lines), kb(*tabs(TR_TABS, sort, "tr"), *buttons,
                          pager(f"tr|{sort}", page, len(rows), size),
                          controls(f"tr|{sort}|{page}", "x|t|trending"), home_row())


def performance(d: dict, cov: dict, tz: ZoneInfo, L: Links, page: int, size: int
                ) -> tuple[str, Kb]:
    up = d["direction"] == "up"
    metric, rows = d["metric"], d["rows"]
    title = ("📈 TOP GAINERS" if up else "📉 TOP LOSERS") + " · " + {
        "price": "price", "volume": "volume " + ("growth" if up else "decline"),
        "flow": "smart-money " + ("inflow" if up else "net selling")}[metric]
    lines = header(title, d["range"], cov, tz)
    src = {"agora_live": "Agora live", "snapshots": "stored snapshots",
           "agora_candles": "Agora candles", "tracker": "tracked wallets"}.get(d.get("source") or "",
                                                                             "none")
    lines.append(f"<i>Source: {e(src)} — {e(d.get('note', ''))}</i>")
    if d.get("population"):
        lines.append(f"<i>Population: {e(d['population'])}</i>")
    if d.get("excluded"):
        lines.append("<i>Excluded: " + ", ".join(f"{v} {e(k)}" for k, v in d["excluded"].items())
                     + f" (min vol {d.get('min_volume', '?')} HYPE, MC, age filters)</i>")
    lines.append("")
    if not rows:
        lines.append("No tokens qualify for this window. " + (
            "Price history builds up as snapshots are collected." if d.get("source") in
            (None, "snapshots") else ""))
    buttons = []
    for i, r in enumerate(rows[page * size:(page + 1) * size], start=page * size + 1):
        s = a(L.agora_token(r["token"]), "<b>" + sym(r) + "</b>")
        if metric == "flow":
            lines.append(f"{i}. {s} · net {num(r['q_net'], True)} HYPE verified · "
                         f"{r['q_buyers']} buyers / {r['q_sellers']} sellers (qualified)")
        else:
            main = (f"{pct(r['ret'])}" if metric == "price"
                    else f"{r['accel']:.2f}x avg rate")
            lines.append(f"{i}. {s} <b>{main}</b>" + (f" · {e(r['launchpad'])}"
                                                      if r.get("launchpad") else ""))
            bits = []
            if r.get("price_start") is not None and metric == "price":
                bits.append(f"{price(r['price_start'])} → {price(r['price_end'])} HYPE")
            if r.get("volume") is not None:
                bits.append(f"vol {num(r['volume'])} HYPE")
            if r.get("mcap") is not None:
                bits.append(f"MC {num(r['mcap'])} HYPE")
            if r.get("q_buyers") is not None:
                bits.append(f"qualified {r['q_buyers']}B/{r['q_sellers']}S")
            lines.append("   " + " · ".join(bits))
        lines.append(f"   <code>{e(r['token'])}</code>")
        buttons.append([btn(f"🔍 {r.get('symbol') or r['token'][:8]}"[:30], f"tk|{r['token']}"),
                        url_btn("Agora", L.agora_token(r["token"]))])
    lines += ["", "<i>Token price performance (market-wide) is separate from wallets' trading "
                  "PnL. Liquidity is not published by Agora; MC and volume filters guard "
                  "against tiny illiquid moves.</i>"]
    dkey = "up" if up else "down"
    return clip(lines), kb(*tabs(GL_TABS, metric, f"gl|{dkey}"),
                          [btn("📉 Losers" if up else "📈 Gainers",
                               f"gl|{'down' if up else 'up'}|{metric}|0")], *buttons,
                          pager(f"gl|{dkey}|{metric}", page, len(rows), size),
                          controls(f"gl|{dkey}|{metric}|{page}", "x|t|perf"), home_row())


def health(h: dict, tz: ZoneInfo, now: datetime | None = None) -> tuple[str, Kb]:
    now = now or datetime.now(UTC)
    if not h.get("db_ok"):
        return (f"<b>⚙️ SYSTEM HEALTH</b>\n🗄 PostgreSQL: ❌ {e(h.get('db_error'))}",
                kb([btn("🔄 Refresh", "hl")], home_row()))
    cov = h["coverage"]

    def ok(cond: bool) -> str:
        return "✅" if cond else "⚠️"
    hb = cov.get("heartbeat_age_s")
    ui = h.get("ui_heartbeat_age_s")
    lines = ["<b>⚙️ SYSTEM HEALTH</b>",
             f"🤖 Telegram UI: {ok(ui is not None and ui < 120)} heartbeat "
             f"{_secs(ui)}",
             f"⛓ Indexer: {ok(hb is not None and hb < 180)} heartbeat {_secs(hb)} · block "
             f"{_na(cov.get('cursor'))} / head {_na(cov.get('head'))} "
             f"(lag {_na(cov.get('lag'))})",
             f"🗄 PostgreSQL: ✅ {h['db_ms']:.0f} ms",
             f"🏆 Leaderboard: last ok {ago(h.get('lb_ok'), now)}"
             + (f" · last attempt {ago(h.get('lb_any'), now)}" if h.get("lb_any") else ""),
             f"👛 Monitored wallets: {h.get('wallets')} · trades stored {h.get('trades'):,}",
             f"🧾 Last tracked trade: {ago(h.get('last_trade'), now)}",
             _disk_line(h),
             "📨 Alerts: " + (" · ".join(f"{k} {v}" for k, v in sorted(h["outbox"].items()))
                              or "none yet")]
    heads = h.get("heads") or {}
    eps = heads.get("endpoints") or []
    if eps:
        lines.append("🌐 RPC (checked " + ago(_ts(heads.get("at")), now) + "):")
        for ep in eps:
            mark = "⚠️ skipped" if ep.get("lagging") else "✅"
            lines.append(f"   {mark} {e(ep.get('host'))} {ep.get('head') or 'down'}")
    coll = h.get("collector") or {}
    lines.append(f"📈 Market snapshots: last {ago(h.get('market_at'), now)} · "
                 f"{h.get('market_tokens') or 0} tokens/24h"
                 + (f" · since {local(h.get('market_since'), tz, '%d %b %H:%M')}"
                    if h.get("market_since") else "")
                 + (f" · last error: {e(coll.get('error'))}" if coll.get("error") else ""))
    if h["warnings"]:
        lines += ["", "<b>Coverage warnings</b>"] + [f"⚠️ {e(w)}" for w in h["warnings"]]
    else:
        lines += ["", "✅ No coverage warnings"]
    return clip(lines), kb([btn("🔄 Refresh", "hl")], home_row())


def _disk_line(h: dict) -> str:
    d = h.get("disk") or {}
    db = f" · tracker DB {h['db_bytes'] / 1e6:.0f} MB" if h.get("db_bytes") else ""
    if not d.get("total"):
        return "💾 Disk: not measured yet" + db
    pct = d["free"] / d["total"]
    return (f"💾 Disk: {'✅' if pct >= 0.15 else '⚠️'} {d['free'] / 1e9:.1f} GB free of "
            f"{d['total'] / 1e9:.0f} GB ({pct:.0%}){db}")


def _na(v: Any) -> str:
    return "n/a" if v is None else str(v)


def _secs(v: Any) -> str:
    return "n/a" if v is None else f"{v}s"


def _ts(v: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(v), UTC)
    except (TypeError, ValueError):
        return None


def time_range(tr: TimeRange, tz: ZoneInfo, back: str | None) -> tuple[str, Kb]:
    lines = ["<b>🕒 TIME RANGE</b>",
             f"Current: <b>{e(tr.label())}</b> · {e(tr.fmt(tz))} <i>({e(tz_label(tz))})</i>",
             "", "Presets roll forward (always end now). A custom range is fixed and is "
             "never moved to the present.", "Reports cover [start, end): start included, "
             "end excluded."]
    cur = tr.preset or "c"
    presets = list(PRESETS)
    rows = [[btn(("✓ " if p == cur else "") + p, f"rg|{p}") for p in presets[i:i + 3]]
            for i in range(0, len(presets), 3)]
    rows.append([btn(("✓ " if cur == "c" else "") + "✏️ Custom", "rg|c")])
    rows.append(home_row(back))
    return "\n".join(lines), {"inline_keyboard": rows}


def search_prompt() -> tuple[str, Kb]:
    return ("<b>🔍 SEARCH</b>\nSend a wallet address, a token contract address, or a token "
            "symbol (e.g. <code>BLOB</code>).\n/cancel to stop.", kb(home_row()))


def search_choices(res: dict, tz: ZoneInfo) -> tuple[str, Kb]:
    lines = [f"<b>🔍 {len(res['rows'])} tokens use the symbol {e(res['symbol'])}</b>",
             "Several contracts share this symbol; pick by address:", ""]
    buttons = []
    for r in res["rows"]:
        lines.append(f"• <code>{e(r['address'])}</code> · {e(r.get('launchpad') or 'unknown')}"
                     f" · {r['tracked_legs']} tracked legs"
                     + (f" · launched {local(r['launched_at'], tz, '%Y-%m-%d')}"
                        if r.get("launched_at") else ""))
        buttons.append([btn(f"{r['address'][:10]}…{r['address'][-4:]}", f"tk|{r['address']}")])
    return "\n".join(lines), kb(*buttons, home_row("s"))


def export_kinds() -> tuple[str, Kb]:
    rows = [[btn(n, f"x|t|{k}") for n, k in EXPORT_KINDS[i:i + 2]]
            for i in range(0, len(EXPORT_KINDS), 2)]
    return ("<b>📤 EXPORT REPORT</b> · step 1/3\nSelect the report type.",
            {"inline_keyboard": rows + [home_row()]})


def export_range(kind: str, tr: TimeRange, tz: ZoneInfo, subject: str | None) -> tuple[str, Kb]:
    lines = [f"<b>📤 {e(EXPORT_NAMES[kind])}</b> · step 2/3",
             f"Select the period. Current selection: <b>{e(tr.label())}</b> · {e(tr.fmt(tz))}"]
    if subject:
        lines.append(f"Subject: <code>{e(subject)}</code>")
    presets = list(PRESETS)
    rows = [[btn(p, f"x|r|{p}") for p in presets[i:i + 3]] for i in range(0, len(presets), 3)]
    rows.append([btn("✓ Use current selection", "x|r|cur"), btn("✏️ Custom", "x|r|c")])
    rows.append(home_row("x"))
    return "\n".join(lines), {"inline_keyboard": rows}


def export_format(kind: str, tr: TimeRange, tz: ZoneInfo) -> tuple[str, Kb]:
    return (f"<b>📤 {e(EXPORT_NAMES[kind])}</b> · step 3/3\nPeriod: {e(tr.label())} · "
            f"{e(tr.fmt(tz))}\nSelect the format. Excel has one sheet per section; CSV is a "
            "single table (a .zip of tables for the complete report).",
            kb([btn("📄 CSV", "x|f|csv"), btn("📊 Excel XLSX", "x|f|xlsx")], home_row("x")))


def _dur(tr: TimeRange) -> str:
    s = tr.seconds
    if s % 86400 == 0:
        return f"{s // 86400}d"
    if s % 3600 == 0:
        return f"{s // 3600}h"
    return f"{s // 60}m"


def hype_dec(v: Decimal | None) -> str:
    return num(v)
