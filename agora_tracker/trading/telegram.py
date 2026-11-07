"""Opt-in PAPER controls; never sends a signing request or transaction."""
from __future__ import annotations

import html
import re

from ..cards import Links
from .paper import PaperFlow, TradeRefused


def alert_controls(markup: dict | None, token: str, chain_id: int) -> dict | None:
    if chain_id != 99901 or not re.fullmatch(r"0x[0-9a-fA-F]{40}", token):
        return markup
    rows = list((markup or {}).get("inline_keyboard", []))
    rows.append([{"text": "⚡ Buy (PAPER)", "callback_data": "tp|b|" + token.lower()},
                 {"text": "💸 Sell (PAPER)", "callback_data": "tp|s|" + token.lower()}])
    return {"inline_keyboard": rows}


def panel(flow: PaperFlow, uid: int, chat: int, sid: str, links: Links) -> tuple[str, dict]:
    i = flow.get(uid, chat, sid)
    text = (f"<b>PAPER {i.side.upper()} — no live trading</b>\nChain: {i.chain_id}\n"
            f"Token: <code>{html.escape(i.token)}</code>\n"
            f"Original alert: message {i.source_message}\n"
            "Wallet/balance: not connected, unknown\nRoute/quote/fees/gas: unavailable\n"
            "Slippage limit: 0.50%; max price impact: 1%\n"
            "No verified adapter is configured. This token is not tradeable through the bot.\n"
            "Selection expires in 3 minutes. No order is signed or submitted.")
    if i.amount_wei:
        text += f"\nRequested input: {i.amount_wei} wei (HYPE)"
    if i.sell_bps:
        text += f"\nRequested sell: {i.sell_bps // 100}% of verified wallet token balance (unknown)"
    values = ("0.1", "0.5", "1") if i.side == "buy" else ("25", "50", "100")
    presets = [{"text": v + (" HYPE" if i.side == "buy" else "%"),
                "callback_data": f"ps|{sid}|{v}"} for v in values]
    return text, {"inline_keyboard": [presets,
        [{"text": "Review / refresh quote", "callback_data": f"pr|{sid}"}],
        [{"text": "Open Agora (token page)", "url": links.agora_token(i.token)},
         {"text": "Explorer", "url": links.explorer_token(i.token)}],
        [{"text": "Cancel", "callback_data": f"pc|{sid}"}]]}


def callback(bot, cq: dict) -> None:
    """Called only after the existing IntelBot allowlist check. Group actions go to DM."""
    uid = cq["from"]["id"]
    msg = cq.get("message") or {}
    chat = msg.get("chat", {}).get("id")
    data = (cq.get("data") or "").split("|")
    def answer(text: str) -> None:
        if cq.get("id"):
            bot._answer(cq["id"], text)

    try:
        if not bot.cfg.trading.paper_enabled or bot.cfg.chain.chain_id != 99901:
            raise TradeRefused("Trading is disabled")
        if msg.get("chat", {}).get("type") == "private" and chat != uid:
            raise TradeRefused("Private chat identity mismatch")
        if data[0] == "tp" and len(data) == 3:
            side = {"b": "buy", "s": "sell"}.get(data[1], "")
            i = bot.paper_trades.create(uid, uid, data[2], side, chat, msg.get("message_id"),
                                        bot.cfg.chain.chain_id)
            text, kb = panel(bot.paper_trades, uid, uid, i.id, bot.L)
            # Never edit the group alert or disclose any user's state there.
            bot._send(uid, text, kb)
            answer("Open your private chat; send /start if needed (PAPER only)")
            return
        if chat != uid or len(data) not in (2, 3):
            raise TradeRefused("Paper order actions require your private chat")
        if data[0] == "ps" and len(data) == 3:
            bot.paper_trades.select(uid, chat, data[1], data[2])
        elif data[0] == "pr" and len(data) == 2:
            bot.paper_trades.review(uid, chat, data[1])
        elif data[0] == "pc" and len(data) == 2:
            bot.paper_trades.cancel(uid, chat, data[1])
            answer("Paper selection cancelled")
            bot._show(chat, msg.get("message_id"), "PAPER selection cancelled. Nothing was submitted.", None)
            return
        else:
            raise TradeRefused("Invalid paper action")
        bot._show(chat, msg.get("message_id"), *panel(bot.paper_trades, uid, chat, data[1], bot.L))
        answer("PAPER only")
    except TradeRefused as e:
        answer(str(e))
