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
            f"Session expires in {max(0, int(i.expires_at - flow.now()))} seconds.\n"
            "Slippage limit: 0.50%; max price impact: 1%\n")
    q = flow.reviewed(uid, chat, sid)
    if q:
        text += (f"<b>Hypothetical review — no actual fill</b>\n"
                 f"Route: {html.escape(q.route.kind)} / <code>{html.escape(q.route.target)}</code>\n"
                 f"Input: {q.amount_in_wei} raw units\nExpected output: {q.amount_out_wei} raw units\n"
                 f"Minimum output: {q.min_out_wei} raw units\n"
                 f"Available input balance: {q.balance_wei} raw units\n"
                 f"Native HYPE balance: {q.native_balance_wei} wei\n"
                 f"Fee reserve: {q.fee_wei} wei HYPE; gas reserve: {q.gas_wei} wei HYPE\n"
                 f"Price impact: {q.price_impact_bps} bps\n"
                 f"Quote age: {int(flow.now() - q.created_at)} seconds\n"
                 "Confirm records a PAPER outcome only. Nothing is signed or submitted.")
    else:
        text += ("Wallet/balance: not connected, unknown\nVerified route/fees/gas: unavailable\n"
                 "This token is not tradeable through the bot. Indicative prices cannot be confirmed.\n"
                 "No order is signed or submitted.")
    if i.amount_wei:
        text += f"\nRequested input: {i.amount_wei} wei (HYPE)"
    if i.sell_bps:
        text += f"\nRequested sell: {i.sell_bps / 100:g}% of verified wallet token balance"
    values = ("0.1", "0.5", "1") if i.side == "buy" else ("25", "50", "100")
    presets = [{"text": v + (" HYPE" if i.side == "buy" else "%"),
                "callback_data": f"ps|{sid}|{v}"} for v in values]
    rows = [presets, [{"text": "Custom amount", "callback_data": f"pa|{sid}"}],
        [{"text": "Review / refresh verified quote", "callback_data": f"pr|{sid}"}]]
    if i.side == 'buy':
        rows.append([{"text": "Indicative Agora price (read-only)", "callback_data": f"pq|{sid}"}])
    if q:
        rows.append([{"text": "Confirm PAPER record", "callback_data":
                      f"pf|{sid}|{flow.confirmation_token(uid, chat, sid)}"}])
    rows += [[{"text": "Open Agora (token page)", "url": links.agora_token(i.token)},
              {"text": "Explorer", "url": links.explorer_token(i.token)}],
             [{"text": "Cancel", "callback_data": f"pc|{sid}"}]]
    if str(i.source_chat).startswith('-100'):
        rows.insert(-1, [{"text": "Original alert", "url":
                          f"https://t.me/c/{str(i.source_chat)[4:]}/{i.source_message}"}])
    return text, {"inline_keyboard": rows}


def custom_amount(bot, msg: dict, arg: str) -> None:
    uid = msg.get('from', {}).get('id')
    chat = msg.get('chat', {}).get('id')
    try:
        if not bot.cfg.trading.paper_enabled or msg.get('chat', {}).get('type') != 'private':
            raise TradeRefused('Custom PAPER input requires your private chat with trading enabled')
        parts = arg.split()
        if len(parts) != 2:
            raise TradeRefused('Use /paper_amount SESSION AMOUNT (HYPE for buys, percent for sells)')
        bot.paper_trades.select(uid, chat, parts[0], parts[1])
        bot._send(chat, *panel(bot.paper_trades, uid, chat, parts[0], bot.L))
    except TradeRefused as e:
        bot._send(chat, str(e), None)


def callback(bot, cq: dict) -> None:
    """Called after IntelBot allowlist check. Only alert selection can come from groups."""
    uid = cq['from']['id']
    msg = cq.get('message') or {}
    chat = msg.get('chat', {}).get('id')
    kind = msg.get('chat', {}).get('type')
    data = (cq.get('data') or '').split('|')

    def answer(text: str) -> None:
        if cq.get('id'):
            bot._answer(cq['id'], text)

    try:
        if not bot.cfg.trading.paper_enabled or bot.cfg.chain.chain_id != 99901:
            raise TradeRefused('Trading is disabled')
        if kind == 'private' and chat != uid:
            raise TradeRefused('Private chat identity mismatch')
        if data[0] == 'tp' and len(data) == 3:
            side = {'b': 'buy', 's': 'sell'}.get(data[1], '')
            i = bot.paper_trades.create(uid, uid, data[2], side, chat, msg.get('message_id'),
                                        bot.cfg.chain.chain_id)
            text, kb = panel(bot.paper_trades, uid, uid, i.id, bot.L)
            bot._send(uid, text, kb)
            answer('Open your private chat; send /start if needed (PAPER only)')
            return
        if chat != uid or kind != 'private' or len(data) not in (2, 3):
            raise TradeRefused('Paper order actions require your private chat')
        sid = data[1]
        if data[0] == 'ps' and len(data) == 3:
            bot.paper_trades.select(uid, chat, sid, data[2])
        elif data[0] == 'pr' and len(data) == 2:
            bot.paper_trades.review(uid, chat, sid)
        elif data[0] == 'pa' and len(data) == 2:
            bot.paper_trades.get(uid, chat, sid)
            text, kb = panel(bot.paper_trades, uid, chat, sid, bot.L)
            text += f'\nSend <code>/paper_amount {sid} AMOUNT</code> (HYPE for buys, percent for sells).'
            bot._show(chat, msg.get('message_id'), text, kb)
            answer('PAPER custom amount')
            return
        elif data[0] == 'pq' and len(data) == 2:
            i = bot.paper_trades.get(uid, chat, sid)
            if not getattr(bot, 'paper_market', None):
                raise TradeRefused('Indicative quote provider unavailable')
            q = bot.paper_market.quote(i)
            # Recheck after network I/O so expiry/pause/selection changes cannot show stale data.
            if bot.paper_trades.get(uid, chat, sid) != i:
                raise TradeRefused('Selection changed; request a new indicative quote')
            text, kb = panel(bot.paper_trades, uid, chat, sid, bot.L)
            text += (f'\n\n<b>INDICATIVE — not reviewed, simulated or executable</b>\n'
                     f'Agora-reported venue: {html.escape(q.venue)}\n'
                     f'Output: {q.amount_out_raw} raw token units (decimals unverified)\n'
                     f'Minimum: {q.min_out_raw} raw units\n'
                     f'Reported fee: {q.fee_bps} bps (wallet applicability unverified)\n'
                     f'Reported impact: {q.impact_bps} bps\n'
                     'Wallet balance, gas and route verification unavailable. No confirmation enabled.')
            bot._show(chat, msg.get('message_id'), text, kb)
            answer('Read-only indicative quote')
            return
        elif data[0] == 'pf' and len(data) == 3:
            order = bot.paper_trades.confirm(uid, chat, sid, data[2])
            text = f'PAPER {order.status}. No actual fill, signing or submission.\nRecord: {order.intent_id}'
            if not bot.paper_trades.journal:
                text += '\nIn-memory only; no durable journal configured.'
            bot._show(chat, msg.get('message_id'), text, None)
            answer('Hypothetical PAPER record only')
            return
        elif data[0] == 'pc' and len(data) == 2:
            bot.paper_trades.cancel(uid, chat, sid)
            answer('Paper selection cancelled')
            bot._show(chat, msg.get('message_id'), 'PAPER selection cancelled. Nothing was submitted.', None)
            return
        else:
            raise TradeRefused('Invalid paper action')
        bot._show(chat, msg.get('message_id'), *panel(bot.paper_trades, uid, chat, sid, bot.L))
        answer('PAPER only')
    except TradeRefused as e:
        answer(str(e))
