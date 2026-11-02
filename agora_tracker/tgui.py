"""Telegram Intelligence Center: the interactive bot (the `ui` service).

Design (see docs/TELEGRAM_UI.md):
  * One update consumer: long-polling getUpdates in this process only. The tracker never
    reads updates; it only sends alerts (sendMessage), so both can share the bot token.
    The update offset is persisted (service_state 'tg_offset'), so a restart neither
    replays old commands nor loses new ones.
  * Bounded concurrency: interactive handlers run on `ui.workers` threads, serialized per
    user; exports run on `ui.report_workers` threads with a small queue. Reports use
    read-only transactions with a statement timeout and never touch the indexer's lock.
  * Authorization: only user ids in ui.admin_ids (TG_CHAT_ID + TG_ADMIN_IDS). Everyone else
    gets one short refusal per hour and no data.
  * Navigation edits the message in place (editMessageText); every callback query is
    answered. Callback data is validated server side; anything malformed or stale shows a
    friendly "expired" notice and the home screen.

Callback grammar (all <= 64 bytes):
  h | mo[|cmp] | sm|<mode>|<page> | tk|<token> | tb|<token>|<page> | tx|<token>|<page>
  tt|<sort>|<page> | w|<wallet> | wt|<wallet>|<page> | ww|<wallet>|<0/1>
  tr|<sort>|<page> | gl|<up/down>|<metric>|<page> | hl | s | rg[|<preset>|c] | n
  x | x|t|<kind> | x|r|<preset/cur/c> | x|f|<csv/xlsx> | xs|<t/w>|<address>
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import schema as t
from . import tgviews as V
from .cards import Links
from .config import Config
from .db import Store
from .export import EXPORT_ERRORS, Exporter
from .market import AgoraMarket
from .reports import Reports
from .timerange import PRESETS, RangeError, TimeRange, custom_range, from_pref, parse_local

UTC = timezone.utc
log = logging.getLogger(__name__)
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
COMMANDS = [("start", "Open the Intelligence Center"), ("menu", "Main menu"),
            ("market", "Market overview"), ("smart", "Smart money"),
            ("trending", "Trending projects"), ("traders", "Top traders"),
            ("gainers", "Top gainers"), ("losers", "Top losers"),
            ("search", "Search wallet / token"), ("export", "Export a report"),
            ("range", "Change the time range"), ("health", "System health"),
            ("status", "System status"), ("help", "How to use the bot"),
            ("cancel", "Cancel the current input")]
CMD_ROUTE = {"/menu": "h", "/market": "mo", "/smart": "sm|net|0", "/trending": "tr|score|0",
             "/traders": "tt|agora|0", "/gainers": "gl|up|price|0",
             "/losers": "gl|down|price|0", "/export": "x", "/range": "rg", "/health": "hl",
             "/status": "hl", "/search": "s"}
HELP = ("<b>How to use</b>\n• Tap a menu button or send a command (/menu, /smart, …).\n"
        "• 🕒 Time Range applies to every view and is remembered.\n"
        "• Send any wallet / token address or a token symbol to search.\n"
        "• 📤 Export Report sends a CSV or Excel file.\n"
        "Numbers marked ✓ are HYPE amounts measured on-chain; estimates are always shown "
        "separately. Wallet metrics cover the wallets this bot tracks, not the whole chain.")


class TgError(Exception):
    def __init__(self, code: int | None, description: str, retry_after: int | None = None):
        super().__init__(f"{code}: {description}")
        self.code, self.description, self.retry_after = code, description, retry_after


class Expired(Exception):
    """A callback that no longer matches anything valid."""


class TelegramApi:
    """Minimal Bot API client. Never logs the token (it is part of every URL)."""

    def __init__(self, token: str, timeout: float = 15.0,
                 session: requests.Session | None = None):
        self._base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout
        self.session = session or requests.Session()

    def call(self, method: str, timeout: float | None = None, **params: Any) -> Any:
        try:
            r = self.session.post(f"{self._base}/{method}", json=params,
                                  timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise TgError(None, type(e).__name__) from None
        return self._result(r)

    def send_document(self, chat_id: int, path: str, filename: str, caption: str,
                      reply_markup: dict | None = None) -> Any:
        data: dict[str, Any] = {"chat_id": str(chat_id), "caption": caption[:1000],
                                "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            with open(path, "rb") as f:
                r = self.session.post(f"{self._base}/sendDocument", data=data,
                                      files={"document": (filename, f)}, timeout=120)
        except requests.RequestException as e:
            raise TgError(None, type(e).__name__) from None
        return self._result(r)

    @staticmethod
    def _result(r: requests.Response) -> Any:
        try:
            j = r.json()
        except ValueError:
            raise TgError(r.status_code, "non-JSON response") from None
        if not j.get("ok"):
            raise TgError(j.get("error_code"), str(j.get("description", "")),
                          (j.get("parameters") or {}).get("retry_after"))
        return j.get("result")


class Sessions:
    """Per-user UI state in ui_sessions (survives restarts)."""

    def __init__(self, store: Store):
        self.store = store

    def get(self, user_id: int) -> dict[str, Any]:
        s = t.ui_sessions
        with self.store.engine.connect() as c:
            r = c.execute(sa.select(s).where(s.c.user_id == user_id)).mappings().first()
        if r is None:
            return {"user_id": user_id, "range_preset": None, "range_start": None,
                    "range_end": None, "state": None, "state_data": {}}
        return dict(r)

    def save(self, user_id: int, chat_id: int, **fields: Any) -> None:
        s = t.ui_sessions
        vals = {"user_id": user_id, "chat_id": chat_id, **fields}
        stmt = pg_insert(s).values(**vals)
        upd: dict[str, Any] = {k: stmt.excluded[k] for k in vals if k != "user_id"}
        upd["updated_at"] = sa.func.now()
        with self.store.engine.begin() as c:
            c.execute(stmt.on_conflict_do_update(index_elements=["user_id"], set_=upd))


class IntelBot:
    def __init__(self, cfg: Config, api: TelegramApi, store: Store, reports: Reports,
                 market: AgoraMarket | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.cfg, self.api, self.store, self.reports = cfg, api, store, reports
        self.market = market
        self.now = now
        self.tz = ZoneInfo(cfg.ui.timezone)
        self.L = Links(cfg.links)
        self.allowed = set(cfg.ui.admin_ids)
        self.sessions = Sessions(store)
        self.exporter = Exporter(reports, self.tz, max_rows=cfg.ui.report_max_rows,
                                 max_bytes=cfg.ui.report_max_bytes)
        self.size = max(1, cfg.ui.page_size)
        self.pool = ThreadPoolExecutor(max(1, cfg.ui.workers), thread_name_prefix="ui")
        self.report_pool = ThreadPoolExecutor(max(1, cfg.ui.report_workers),
                                              thread_name_prefix="report")
        self._slots = threading.BoundedSemaphore(max(1, cfg.ui.report_workers)
                                                 + max(0, cfg.ui.report_queue))
        self._exporting: set[int] = set()
        self._exp_lock = threading.Lock()
        self._user_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)
        self._refused: dict[int, float] = {}

    # --- lifecycle -------------------------------------------------------------------------
    def setup(self) -> None:
        try:
            info = self.api.call("getWebhookInfo")
            if isinstance(info, dict) and info.get("url"):
                log.warning("a webhook is set; removing it so getUpdates polling can work")
                self.api.call("deleteWebhook", drop_pending_updates=False)
        except TgError as e:
            log.warning("getWebhookInfo failed: %s", e.description)
        try:
            self.api.call("setMyCommands", commands=[{"command": c, "description": d}
                                                     for c, d in COMMANDS])
        except TgError as e:
            log.warning("setMyCommands failed: %s", e.description)
        self._recover_jobs()
        if not self.allowed:
            log.error("no authorized Telegram users (set TG_CHAT_ID / TG_ADMIN_IDS): every "
                      "request will be refused")

    def _recover_jobs(self) -> None:
        with self.store.engine.begin() as c:
            rows = c.execute(sa.text("""
                UPDATE report_jobs SET status = 'failed', error = 'interrupted by a restart',
                       finished_at = now()
                WHERE status = 'running' RETURNING user_id, kind""")).all()
        for uid, kind in rows:
            self._send(uid, f"⚠️ Your {V.EXPORT_NAMES.get(kind, kind)} export was interrupted by a "
                       "restart. Please run it again from 📤 Export Report.", V.kb(V.home_row()))

    def run(self, stop: threading.Event) -> None:
        self.setup()
        offset = int(self.store.get_kv("tg_offset") or 0) or None
        backoff = 1.0
        log.info("Telegram UI polling (authorized users: %d)", len(self.allowed))
        while not stop.is_set():
            self.store.set_kv("ui_heartbeat", str(int(time.time())))
            try:
                params: dict[str, Any] = {"timeout": self.cfg.ui.poll_timeout_s,
                                          "allowed_updates": ["message", "callback_query"]}
                if offset:
                    params["offset"] = offset
                updates = self.api.call("getUpdates", timeout=self.cfg.ui.poll_timeout_s + 10,
                                        **params) or []
                backoff = 1.0
            except TgError as e:
                if e.code == 409:
                    log.error("another getUpdates consumer or webhook is active for this bot "
                              "(409); retrying in 30s")
                    stop.wait(30)
                elif e.retry_after:
                    stop.wait(e.retry_after)
                else:
                    log.warning("getUpdates failed: %s (retry in %.0fs)", e.description, backoff)
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
                continue
            for upd in updates:
                offset = int(upd["update_id"]) + 1
                self.dispatch(upd)
            if updates:
                self.store.set_kv("tg_offset", str(offset))

    def dispatch(self, upd: dict) -> None:
        uid = _user_id(upd)
        if uid is None:
            return

        def job() -> None:
            with self._user_locks[uid]:
                self.handle(upd)
        self.pool.submit(job)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.report_pool.shutdown(wait=True)

    # --- update handling ---------------------------------------------------------------------
    def handle(self, upd: dict) -> None:
        try:
            if "callback_query" in upd:
                self.on_callback(upd["callback_query"])
            elif "message" in upd:
                self.on_message(upd["message"])
        except Exception:  # noqa: BLE001 - one bad update must not kill the poller
            log.exception("handling update %s failed", upd.get("update_id"))

    def authorized(self, uid: int, chat_id: int, cq_id: str | None = None) -> bool:
        if uid in self.allowed:
            return True
        log.warning("unauthorized Telegram user %s", uid)
        if cq_id:
            self._answer(cq_id, "Not authorized")
        if time.monotonic() - self._refused.get(uid, -1e9) > 3600:
            self._refused[uid] = time.monotonic()
            self._send(chat_id, f"⛔ This bot is private. Your user id is <code>{uid}</code>; "
                       "the owner can add it to TG_ADMIN_IDS.", None)
        return False

    def on_message(self, msg: dict) -> None:
        uid, chat = msg.get("from", {}).get("id"), msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if uid is None or chat is None or not text:
            return
        if not self.authorized(uid, chat):
            return
        sess = self.sessions.get(uid)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        if cmd == "/start":
            self.sessions.save(uid, chat, state=None, state_data={})
            self._send(chat, "⌨️ Menu keyboard enabled. Tap a button or send /menu.",
                       V.reply_keyboard())
            self._show(chat, None, *self._view(uid, chat, "h"))
            return
        if cmd == "/cancel":
            self.sessions.save(uid, chat, state=None, state_data=_keep(sess))
            self._show(chat, None, *self._view(uid, chat, "h"))
            return
        if cmd == "/help":
            self._send(chat, HELP, V.kb(V.home_row()))
            return
        if cmd in ("/token", "/wallet", "/search") and arg.strip():
            self._search(uid, chat, arg.strip())
            return
        if cmd in CMD_ROUTE:
            self.sessions.save(uid, chat, state=None, state_data=_keep(sess))
            self._show(chat, None, *self._view(uid, chat, CMD_ROUTE[cmd]))
            return
        if text in V.MENU_TEXT:
            self.sessions.save(uid, chat, state=None, state_data=_keep(sess))
            self._show(chat, None, *self._view(uid, chat, V.MENU_TEXT[text]))
            return
        state = sess.get("state")
        if state in ("range_start", "range_end"):
            self._range_input(uid, chat, sess, text)
            return
        if state == "export_subject":
            self._export_subject(uid, chat, sess, text)
            return
        if state == "search" or ADDR.match(text) or re.fullmatch(r"\$?[A-Za-z0-9._-]{1,20}", text):
            self._search(uid, chat, text)
            return
        self._send(chat, "I didn't understand that. " + HELP, V.kb(V.home_row()))

    def on_callback(self, cq: dict) -> None:
        uid = cq.get("from", {}).get("id")
        msg = cq.get("message") or {}
        chat, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
        data = cq.get("data") or ""
        if uid is None or chat is None:
            self._answer(cq["id"], None)
            return
        if not self.authorized(uid, chat, cq["id"]):
            return
        if data == "n":
            self._answer(cq["id"], None)
            return
        try:
            out = self.render(uid, chat, data, message_id=mid)
        except Expired:
            self._answer(cq["id"], "This button has expired — showing the menu.")
            self._show(chat, mid, *self._view(uid, chat, "h"))
            return
        except Exception:  # noqa: BLE001
            log.exception("view %r failed", data)
            self._answer(cq["id"], "Something went wrong; try again.")
            return
        self._answer(cq["id"], None)
        if out is not None:
            self._show(chat, mid, *out)

    # --- routing -------------------------------------------------------------------------------
    def range_for(self, sess: dict) -> TimeRange:
        return from_pref(sess.get("range_preset"), sess.get("range_start"), sess.get("range_end"),
                         self.now())

    def render(self, uid: int, chat: int, data: str, message_id: int | None = None
               ) -> tuple[str, V.Kb] | None:
        p = data.split("|")
        v = p[0]
        sess = self.sessions.get(uid)
        tr = self.range_for(sess)
        if v in ("mo", "sm", "tk", "tb", "tx", "tt", "w", "wt", "tr", "gl", "hl", "h"):
            sd = _keep(sess)
            sd["last"] = data
            self.sessions.save(uid, chat, state=None, state_data=sd)
        if v == "h":
            cov = self.reports.coverage(tr, self.now())
            wallets = (self.reports.health(self.now()) or {}).get("wallets")
            return V.home(tr, cov, self.tz, wallets)
        if v == "x" or v == "xs":
            return self._export_step(uid, chat, sess, p, message_id)
        if v == "rg":
            return self._range_step(uid, chat, sess, p)
        if v == "s":
            self.sessions.save(uid, chat, state="search", state_data=_keep(sess))
            return V.search_prompt()
        if v == "hl":
            return V.health(self.reports.health(self.now()), self.tz, self.now())
        cov = self.reports.coverage(tr, self.now())
        if v == "mo":
            o = self.reports.market_overview(tr)
            return V.compare(o, cov, self.tz) if p[1:2] == ["cmp"] else \
                V.market_overview(o, cov, self.tz, self.L)
        if v == "sm":
            mode, page = _choice(p, 1, self.reports.SM_MODES), _page(p, 2)
            d = self.reports.smart_money(tr, mode)
            chunk = d["rows"][page * self.size:(page + 1) * self.size]
            mk = {} if mode == "clusters" or self.market is None else {
                r["token"]: self.market.token(r["token"]) for r in chunk}
            return V.smart_money(d, cov, self.tz, self.L, page, self.size, mk)
        if v == "tk":
            d = self.reports.token_detail(_addr(p, 1), tr)
            return V.token_detail(d, cov, self.tz, self.L,
                                  self.market.hype_usd() if self.market else None, self.now())
        if v == "tb":
            return V.token_buyers(self.reports.token_buyers(_addr(p, 1), tr), cov, self.tz, self.L,
                                  _page(p, 2), self.size)
        if v in ("tx", "wt"):
            a, page = _addr(p, 1), _page(p, 2)
            rows = self.reports.trades(tr, token=a if v == "tx" else None,
                                       wallet=a if v == "wt" else None,
                                       limit=self.size * 2 + 1, offset=page * self.size * 2)
            more = len(rows) > self.size * 2
            title = ("🧾 TOKEN TRADES" if v == "tx" else "🧾 WALLET TRADES") + \
                f" · <code>{V.short(a)}</code>"
            return V.trades_list(title, rows[:self.size * 2], tr, cov, self.tz, self.L, f"{v}|{a}",
                                 page, self.size, more, f"tk|{a}" if v == "tx" else f"w|{a}",
                                 show_token=(v == "wt"), show_wallet=(v == "tx"))
        if v == "tt":
            sort, page = _choice(p, 1, self.reports.TT_SORTS), _page(p, 2)
            return V.top_traders(self.reports.top_traders(tr, sort), cov, self.tz, self.L, page,
                                 self.size)
        if v == "w":
            return V.wallet(self.reports.wallet_detail(_addr(p, 1), tr), cov, self.tz, self.L)
        if v == "ww":
            a = _addr(p, 1)
            on = _choice(p, 2, ("0", "1")) == "1"
            if on:
                self.store.watch_add(a, source="telegram", reason=f"added via Telegram by {uid}")
            else:
                self.store.watch_remove(a, source="telegram")
            text, k = V.wallet(self.reports.wallet_detail(a, tr), cov, self.tz, self.L)
            note = ("\n\n✅ Added to the watchlist; the tracker picks it up within one "
                    "leaderboard refresh (~5 min)." if on else "\n\n✅ Removed from the watchlist.")
            return text + note, k
        if v == "tr":
            sort, page = _choice(p, 1, self.reports.TR_SORTS), _page(p, 2)
            return V.trending(self.reports.trending(tr, sort, self.now()), cov, self.tz, self.L,
                              page, self.size, self.now())
        if v == "gl":
            direction = _choice(p, 1, ("up", "down"))
            metric = _choice(p, 2, ("price", "volume", "flow"))
            d = self.reports.performance(tr, direction, metric, self.now())
            return V.performance(d, cov, self.tz, self.L, _page(p, 3), self.size)
        raise Expired(data)

    def _view(self, uid: int, chat: int, data: str) -> tuple[str, V.Kb]:
        out = self.render(uid, chat, data)
        return out if out is not None else ("✅", V.kb(V.home_row()))

    # --- time range -------------------------------------------------------------------------------
    def _range_step(self, uid: int, chat: int, sess: dict, p: list[str]) -> tuple[str, V.Kb]:
        back = (sess.get("state_data") or {}).get("last")
        if len(p) == 1:
            return V.time_range(self.range_for(sess), self.tz, back)
        choice = p[1]
        if choice in PRESETS:
            self.sessions.save(uid, chat, range_preset=choice, range_start=None, range_end=None,
                               state=None, state_data=_keep(sess))
            target = back or "h"
            out = self.render(uid, chat, target)
            assert out is not None
            return out[0] + f"\n\n✅ Period set: {V.e(self.range_for({'range_preset': choice}).label())}", out[1]
        if choice == "c":
            sd = _keep(sess)
            sd["purpose"] = "view"
            self.sessions.save(uid, chat, state="range_start", state_data=sd)
            return self._custom_prompt("start")
        raise Expired("|".join(p))

    def _custom_prompt(self, which: str, err: str | None = None) -> tuple[str, V.Kb]:
        ex = "2026-11-01 08:00" if which == "start" else "2026-11-02 18:00"
        lines = [f"<b>✏️ CUSTOM RANGE</b> · {which.upper()}",
                 f"Send the {which} time in {self.tz.key} "
                 f"({V.tz_label(self.tz)}), e.g. <code>{ex}</code>."]
        if which == "end":
            lines.append("The end is exclusive.")
        if err:
            lines.insert(1, f"⚠️ {V.e(err)}")
        lines.append("/cancel to stop.")
        return "\n".join(lines), V.kb(V.home_row("rg"))

    def _range_input(self, uid: int, chat: int, sess: dict, text: str) -> None:
        sd = dict(sess.get("state_data") or {})
        try:
            dt = parse_local(text, self.tz)
            if sess["state"] == "range_start":
                if dt >= self.now():
                    raise RangeError("the start is in the future")
                sd["custom_start"] = dt.isoformat()
                self.sessions.save(uid, chat, state="range_end", state_data=sd)
                self._send(chat, *self._custom_prompt("end"))
                return
            start = datetime.fromisoformat(sd["custom_start"])
            tr = custom_range(start, dt, self.now())
        except (RangeError, KeyError) as e:
            which = "start" if sess["state"] == "range_start" else "end"
            self._send(chat, *self._custom_prompt(which, str(e) if isinstance(e, RangeError)
                                                  else "start missing; begin again"))
            return
        sd.pop("custom_start", None)
        purpose = sd.pop("purpose", "view")
        if purpose == "export":
            exp = dict(sd.get("export") or {})
            exp["start"], exp["end"], exp["preset"] = tr.start.isoformat(), tr.end.isoformat(), None
            sd["export"] = exp
            self.sessions.save(uid, chat, state=None, state_data=sd)
            self._send(chat, *V.export_format(exp["kind"], tr, self.tz))
            return
        self.sessions.save(uid, chat, range_preset=None, range_start=tr.start, range_end=tr.end,
                           state=None, state_data=sd)
        out = self.render(uid, chat, sd.get("last") or "h")
        assert out is not None
        self._send(chat, out[0] + f"\n\n✅ Custom period set: {V.e(tr.fmt(self.tz))}", out[1])

    # --- search -------------------------------------------------------------------------------------
    def _search(self, uid: int, chat: int, q: str) -> None:
        sess = self.sessions.get(uid)
        self.sessions.save(uid, chat, state=None, state_data=_keep(sess))
        res = self.reports.search(q)
        if res["kind"] == "token":
            out = self.render(uid, chat, f"tk|{res['address']}")
        elif res["kind"] == "wallet":
            out = self.render(uid, chat, f"w|{res['address']}")
            if out and res.get("unknown"):
                out = ("<i>No tracked activity for this address yet.</i>\n" + out[0], out[1])
        elif res["kind"] == "tokens":
            out = V.search_choices(res, self.tz)
        else:
            out = (f"🔍 Nothing found for <code>{V.e(q[:40])}</code>. Send a full 0x address or "
                   "an exact token symbol.", V.kb([V.btn("🔍 Search again", "s")], V.home_row()))
        if out:
            self._show(chat, None, *out)

    # --- export ---------------------------------------------------------------------------------------
    def _export_step(self, uid: int, chat: int, sess: dict, p: list[str],
                     message_id: int | None) -> tuple[str, V.Kb] | None:
        sd = _keep(sess)
        exp = dict(sd.get("export") or {})
        if p[0] == "xs":
            kind = {"t": "token", "w": "wallet"}.get(p[1] if len(p) > 1 else "")
            if kind is None:
                raise Expired("|".join(p))
            exp = {"kind": kind, "subject": _addr(p, 2)}
            sd["export"] = exp
            self.sessions.save(uid, chat, state=None, state_data=sd)
            return V.export_range(kind, self.range_for(sess), self.tz, exp["subject"])
        if len(p) == 1:
            sd["export"] = {}
            self.sessions.save(uid, chat, state=None, state_data=sd)
            return V.export_kinds()
        step = p[1]
        if step == "t":
            kind = _choice(p, 2, tuple(V.EXPORT_NAMES))
            exp = {"kind": kind}
            sd["export"] = exp
            if kind in ("wallet", "token"):
                self.sessions.save(uid, chat, state="export_subject", state_data=sd)
                return (f"<b>📤 {V.EXPORT_NAMES[kind]}</b>\nSend the {kind} address "
                        "(0x…). /cancel to stop.", V.kb(V.home_row("x")))
            self.sessions.save(uid, chat, state=None, state_data=sd)
            return V.export_range(kind, self.range_for(sess), self.tz, None)
        if not exp.get("kind"):
            raise Expired("|".join(p))
        if step == "r":
            choice = p[2] if len(p) > 2 else ""
            if choice == "c":
                sd["purpose"] = "export"
                self.sessions.save(uid, chat, state="range_start", state_data=sd)
                return self._custom_prompt("start")
            if choice == "cur":
                cur = self.range_for(sess)
                exp.update(preset=cur.preset, start=cur.start.isoformat(), end=cur.end.isoformat())
            elif choice in PRESETS:
                exp.update(preset=choice, start=None, end=None)
            else:
                raise Expired("|".join(p))
            sd["export"] = exp
            self.sessions.save(uid, chat, state=None, state_data=sd)
            return V.export_format(exp["kind"], self._export_range(exp), self.tz)
        if step == "f":
            fmt = _choice(p, 2, ("csv", "xlsx"))
            if "preset" not in exp:
                raise Expired("|".join(p))
            return self.start_export(uid, chat, message_id, exp["kind"], exp.get("subject"),
                                     self._export_range(exp), fmt)
        raise Expired("|".join(p))

    def _export_range(self, exp: dict) -> TimeRange:
        if exp.get("preset") in PRESETS:
            return from_pref(exp["preset"], None, None, self.now())
        return from_pref(None, datetime.fromisoformat(exp["start"]),
                         datetime.fromisoformat(exp["end"]), self.now())

    def _export_subject(self, uid: int, chat: int, sess: dict, text: str) -> None:
        sd = _keep(sess)
        exp = dict(sd.get("export") or {})
        if not ADDR.match(text) or not exp.get("kind"):
            self._send(chat, "⚠️ Send a full 0x… address (42 characters), or /cancel.",
                       V.kb(V.home_row("x")))
            return
        exp["subject"] = text.lower()
        sd["export"] = exp
        self.sessions.save(uid, chat, state=None, state_data=sd)
        self._send(chat, *V.export_range(exp["kind"], self.range_for(sess), self.tz,
                                         exp["subject"]))

    def start_export(self, uid: int, chat: int, message_id: int | None, kind: str,
                     subject: str | None, tr: TimeRange, fmt: str) -> tuple[str, V.Kb] | None:
        with self._exp_lock:
            if uid in self._exporting:
                return ("⏳ You already have an export running; it will arrive shortly.",
                        V.kb(V.home_row()))
            if not self._slots.acquire(blocking=False):
                return ("⏳ The report queue is full. Try again in a minute.", V.kb(V.home_row()))
            self._exporting.add(uid)
        with self.store.engine.begin() as c:
            job = c.execute(sa.insert(t.report_jobs).values(
                user_id=uid, kind=kind, fmt=fmt, range_start=tr.start, range_end=tr.end,
                status="running").returning(t.report_jobs.c.id)).scalar_one()
        text = (f"⏳ Generating <b>{V.EXPORT_NAMES[kind]}</b> ({fmt.upper()}) for "
                f"{V.e(tr.fmt(self.tz))}…")
        self._show(chat, message_id, text, V.kb(V.home_row()))
        self.report_pool.submit(self._run_export, job, uid, chat, message_id, kind, subject, tr,
                                fmt)
        return None

    def _run_export(self, job: int, uid: int, chat: int, message_id: int | None, kind: str,
                    subject: str | None, tr: TimeRange, fmt: str) -> None:
        status, err, rows, size, name = "failed", None, None, None, None
        try:
            with tempfile.TemporaryDirectory(prefix="elysium-report-") as d:
                path, name, rows = self.exporter.write(kind, tr, fmt, d, subject)
                size = os.path.getsize(path)
                cov = self.reports.coverage(tr, self.now())
                cap = (f"📤 <b>{V.EXPORT_NAMES[kind]}</b>\n{V.e(tr.label())} · "
                       f"{V.e(tr.fmt(self.tz))} ({V.e(V.tz_label(self.tz, tr.end))})\n"
                       f"{rows} data rows · coverage {cov['status']}"
                       + ("" if rows else "\n<i>No records in this window; the file contains "
                                          "headers and metadata only.</i>")
                       + "\nTimes in the file are UTC.")
                self.api.send_document(chat, path, name, cap)
            status = "done"
            self._show(chat, message_id, f"✅ Sent <code>{V.e(name)}</code> ({rows} rows, "
                       f"{size / 1024:.0f} KB).", V.kb([V.btn("📤 Another export", "x")],
                                                     V.home_row()))
        except EXPORT_ERRORS as e:
            err = str(e)
            self._show(chat, message_id, f"⚠️ Export failed: {V.e(err)}",
                       V.kb([V.btn("📤 Try again", "x")], V.home_row()))
        except TgError as e:
            err = f"telegram: {e.description}"
            self._show(chat, message_id, f"⚠️ Telegram rejected the file: {V.e(e.description)}",
                       V.kb([V.btn("📤 Try again", "x")], V.home_row()))
        except Exception as e:  # noqa: BLE001
            log.exception("export job %s failed", job)
            err = type(e).__name__
            self._show(chat, message_id, f"⚠️ Export failed with an internal error (job {job}). "
                       "Try a shorter period; details are in the service log.",
                       V.kb([V.btn("📤 Try again", "x")], V.home_row()))
        finally:
            with self.store.engine.begin() as c:
                c.execute(sa.update(t.report_jobs).where(t.report_jobs.c.id == job).values(
                    status=status, error=err, rows=rows, bytes=size, file_name=name,
                    finished_at=sa.func.now()))
            with self._exp_lock:
                self._exporting.discard(uid)
            self._slots.release()

    # --- Telegram I/O ------------------------------------------------------------------------
    def _send(self, chat: int, text: str, markup: dict | None) -> None:
        params: dict[str, Any] = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                  "link_preview_options": {"is_disabled": True}}
        if markup:
            params["reply_markup"] = markup
        try:
            self.api.call("sendMessage", **params)
        except TgError as e:
            if e.code == 400 and "parse" in e.description.lower():
                params.pop("parse_mode")
                params["text"] = re.sub(r"<[^>]+>", "", text)
                try:
                    self.api.call("sendMessage", **params)
                except TgError as e2:
                    log.warning("sendMessage failed: %s", e2.description)
            else:
                log.warning("sendMessage failed: %s", e.description)

    def _show(self, chat: int, message_id: int | None, text: str, markup: dict | None) -> None:
        if message_id is not None:
            try:
                self.api.call("editMessageText", chat_id=chat, message_id=message_id, text=text,
                              parse_mode="HTML", reply_markup=markup or {"inline_keyboard": []},
                              link_preview_options={"is_disabled": True})
                return
            except TgError as e:
                if "not modified" in e.description:
                    return
                log.info("edit failed (%s); sending a new message", e.description)
        self._send(chat, text, markup)

    def _answer(self, cq_id: str, text: str | None) -> None:
        try:
            params: dict[str, Any] = {"callback_query_id": cq_id}
            if text:
                params["text"] = text
            self.api.call("answerCallbackQuery", **params)
        except TgError as e:  # "query is too old" etc.: nothing to do
            log.debug("answerCallbackQuery: %s", e.description)


def _user_id(upd: dict) -> int | None:
    for k in ("callback_query", "message"):
        if k in upd:
            return (upd[k].get("from") or {}).get("id")
    return None


def _keep(sess: dict) -> dict:
    """state_data minus transient input fields."""
    sd = dict(sess.get("state_data") or {})
    sd.pop("custom_start", None)
    return sd


def _page(p: list[str], i: int) -> int:
    try:
        v = int(p[i]) if len(p) > i else 0
    except ValueError:
        raise Expired("bad page") from None
    if not 0 <= v <= 1000:
        raise Expired("bad page")
    return v


def _choice(p: list[str], i: int, allowed: tuple[str, ...]) -> str:
    v = p[i] if len(p) > i else allowed[0]
    if v not in allowed:
        raise Expired(f"bad choice {v!r}")
    return v


def _addr(p: list[str], i: int) -> str:
    v = p[i] if len(p) > i else ""
    if not ADDR.match(v):
        raise Expired("bad address")
    return v.lower()
