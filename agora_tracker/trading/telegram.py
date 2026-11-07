"""Opt-in PAPER controls; never sends a signing request or transaction."""
from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ..cards import Links
from .chain import units
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
    if i.wallet:
        text += f"Watch-only wallet: <code>{html.escape(i.wallet)}</code> (ownership unverified)\n"
    else:
        text += "Set a public watch-only wallet with /paper_wallet ADDRESS.\n"
    q = flow.reviewed(uid, chat, sid)
    if q:
        observed = datetime.fromtimestamp(q.created_at, ZoneInfo('Asia/Ho_Chi_Minh')).isoformat(timespec='seconds')
        text += f'Quote observation: {observed}\n'
        idec = 18 if i.side == 'buy' else (q.token_decimals if q.token_decimals is not None else 0)
        odec = (q.token_decimals if q.token_decimals is not None else 0) if i.side == 'buy' else 18
        inu, outu = ('HYPE', 'tokens') if i.side == 'buy' else ('tokens', 'HYPE')
        text += (f"<b>Hypothetical review — no actual fill</b>\n"
                 f"Route: {html.escape(q.route.kind)} / <code>{html.escape(q.route.target)}</code>\n"
                 f"Input: {units(q.amount_in_wei, idec)} {inu}\n"
                 f"Expected output: {units(q.amount_out_wei, odec)} {outu}\n"
                 f"Minimum output: {q.min_out_wei} raw units ({units(q.min_out_wei, odec)} {outu})\n"
                 f"Available input balance: {units(q.balance_wei, idec)} {inu}\n"
                 f"Native HYPE balance: {units(q.native_balance_wei, 18)} HYPE\n"
                 f"Pool fee: {q.pool_fee_bps if q.pool_fee_bps is not None else 'fixture'} bps, included in output\n"
                 f"Price impact including pool fee: {q.price_impact_bps} bps\n"
                 f"Quote age: {int(flow.now() - q.created_at)} seconds; expires in {max(0, int(q.expires_at-flow.now()))} seconds\n"
                 f"Simulation: {html.escape(q.simulation_status)}\n")
        if q.gas_wei is not None:
            text += f"Gas reserve: {units(q.gas_wei, 18)} HYPE (estimate plus 20% gas-unit buffer)\n"
        else:
            text += "Gas estimate: unavailable until required approval exists.\n"
        if q.simulation_status == 'approval_required':
            text += (f"<b>CONDITIONAL PAPER scenario — not simulated</b>\n"
                     f"Required finite allowance: {units(q.approval_required_wei, idec)} tokens\n"
                     f"Spender: <code>{html.escape(q.approval_spender or '')}</code>\n"
                     "No approval is requested or sent. Output assumes normal token transfers; token taxes/sell restrictions remain uncertain.\n")
        else:
            text += "Read-only simulation enforced the minimum at the quoted block. Exact delivered output is unavailable; future execution is not guaranteed.\n"
        if q.agora_amount_out_wei is not None:
            text += (f"Agora API comparison: {units(q.agora_amount_out_wei, odec)} {outu} via {html.escape(q.agora_venue or '')}\n"
                     f"Agora-reported fee: {q.agora_fee_bps} bps; aggregator calldata ABI unverified.\n"
                     "Venue comparisons can observe different blocks; this is not guaranteed net output.\n")
        elif q.wallet:
            text += "Agora comparison: unavailable.\n"
        if q.wallet:
            text += "Expected pool output assumes normal transfers; token taxes remain unverified.\n"
        if q.transaction and q.deadline is not None:
            deadline = datetime.fromtimestamp(q.deadline, ZoneInfo('Asia/Ho_Chi_Minh')).isoformat(timespec='seconds')
            text += (f"Native transaction value: {units(int(q.transaction['value'], 16), 18)} HYPE\n"
                     f"Deadline: {deadline}\n"
                     f"Verified method selector: <code>{q.transaction['data'][:10]}</code>\n")
        if q.block_number is not None:
            text += f"Quote block: {q.block_number}\n"
        text += "Confirm records a PAPER scenario only. Nothing is signed or submitted."
    elif flow.provider:
        text += "Select an amount, then Review / refresh quote. Unsupported routes refuse.\n"
    else:
        text += ("Wallet/balance: not connected, unknown\nVerified route/fees/gas: unavailable\n"
                 "No verified quote adapter is configured. This token is not tradeable through the bot.\n"
                 "Indicative prices cannot be confirmed. No order is signed or submitted.")
    if i.amount_wei:
        text += f"\nRequested input: {i.amount_wei} wei (HYPE)"
    if i.sell_bps:
        text += f"\nRequested sell: {i.sell_bps / 100:g}% of verified wallet token balance"
    values = ("0.1", "0.5", "1") if i.side == "buy" else ("25", "50", "100")
    presets = [{"text": v + (" HYPE" if i.side == "buy" else "%"),
                "callback_data": f"ps|{sid}|{v}"} for v in values]
    rows = [presets, [{"text": "Custom amount", "callback_data": f"pa|{sid}"}],
        [{"text": "Review / refresh pool quote", "callback_data": f"pr|{sid}"}]]
    if i.side == 'buy':
        rows.append([{"text": "Indicative Agora price (read-only)", "callback_data": f"pq|{sid}"}])
    if q:
        rows.append([{"text": ("Record conditional PAPER scenario" if q.simulation_status == "approval_required" else "Confirm PAPER record"), "callback_data":
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
            if order.status == 'hypothetical_requires_approval':
                text += '\nCONDITIONAL: missing allowance; swap not simulated, gas unavailable. No approval sent.'
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


def wallet_command(bot, msg: dict, command: str, arg: str) -> None:
    uid, chat = msg.get('from', {}).get('id'), msg.get('chat', {}).get('id')
    try:
        if not bot.cfg.trading.paper_enabled or msg.get('chat', {}).get('type') != 'private':
            raise TradeRefused('Watch-only PAPER wallets require your private chat with PAPER enabled')
        if command == '/paper_wallet':
            bot.paper_trades.set_wallet(uid, chat, arg.strip())
            text = ('Watch-only public wallet saved. Ownership is unverified; PAPER only.\n'
                    'Previous selections were cancelled. Open Buy/Sell from an alert or use /trade TOKEN buy|sell.')
        else:
            wallet = bot.paper_trades.wallet(uid, chat)
            provider = bot.paper_trades.provider
            if not wallet or not provider or not hasattr(provider, 'balances'):
                raise TradeRefused('Configure a private PAPER journal and watch-only wallet first')
            token_balance, native, decimals, block = provider.balances(wallet, arg.strip())
            text = (f'On-chain snapshot at block {int(block, 16)} — not a PAPER position\n'
                    f'Watch-only wallet: <code>{html.escape(wallet)}</code>\n'
                    f'Token: <code>{html.escape(arg.strip())}</code>\n'
                    f'Token balance: {units(token_balance, decimals)} (decimals {decimals})\n'
                    f'HYPE: {units(native, 18)}\nNo wallet ownership or spending authority is established.')
        bot._send(chat, text, None)
    except TradeRefused as e:
        bot._send(chat, str(e), None)


def order_history(flow: PaperFlow, uid: int, chat: int) -> str:
    rows = flow.orders(uid, chat)
    if not rows:
        return 'No durable PAPER records. No live position or receipt is implied.'
    lines = ['<b>PAPER scenario history — no actual fills</b>']
    for row in rows:
        snapshot = row.get('snapshot') or {}
        i, q = snapshot.get('intent') or {}, snapshot.get('quote') or {}
        decimals = q.get('token_decimals')
        decimals = decimals if type(decimals) is int and 0 <= decimals <= 36 else 0
        side = i.get('side', 'unknown')
        idec, odec = (18, decimals) if side == 'buy' else (decimals, 18)
        try:
            timestamp = datetime.fromtimestamp(row['created_at'], ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%m-%d %H:%M:%S +07')
            details = (f"{html.escape(side.upper())} <code>{html.escape(i.get('token', 'unknown'))}</code>\n"
                f"Input {units(q['amount_in_wei'], idec)} → expected {units(q['amount_out_wei'], odec)} "
                f"({'tokens' if side == 'buy' else 'HYPE'})\n"
                f"{html.escape(row['status'])}; {html.escape(q.get('simulation_status', 'unknown'))}\n"
                f"{timestamp}; <code>{html.escape(row['intent_id'])}</code>")
        except (KeyError, ValueError, TypeError):
            details = 'Stored PAPER details unavailable'
        lines.append(details)
    return '\n\n'.join(lines)
