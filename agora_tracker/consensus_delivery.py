"""Independent Consensus alerts: shared saved web settings, read-only analytics, group bot.

No getUpdates, transaction execution, wallet credentials, or modifications to the existing
Telegram notification_outbox. Delivery is at-least-once; retries may duplicate after a
Telegram success followed by a lost response. Disabled by default.
"""
from __future__ import annotations

import dataclasses
import html
import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .config import Config
from .web.consensus import (
    NET_CHOICES,
    RANGE_CHOICES,
    TRADER_CHOICES,
    WINDOW_CHOICES,
    ConsensusParams,
)

log = logging.getLogger(__name__)
UTC = timezone.utc
SCHEMA = "consensus_alerts"
PATTERN = re.compile(r"0x[0-9a-f]{40}\Z")
LOCK_ID = 0x434F4E53   # 'CONS', separate from the tracker and Stage C locks
POLL_SECONDS = 45
MAX_EVENTS = 10
MAX_SENDS = 5


def validate_settings(raw: Any) -> dict[str, Any]:
    """Strictly validate an owner's full replacement. Never silently default invalid input."""
    if not isinstance(raw, dict) or set(raw) != {
        "version", "enabled", "traders", "window", "min_net", "range",
        "launchpad", "independent",
    }:
        raise ValueError("Invalid or incomplete Consensus settings")
    if type(raw["version"]) is not int or not 0 <= raw["version"] < 2**31:
        raise ValueError("Invalid settings version")
    if type(raw["enabled"]) is not bool or type(raw["independent"]) is not bool:
        raise ValueError("Enabled and independent must be booleans")
    if type(raw["traders"]) is not int or raw["traders"] not in TRADER_CHOICES:
        raise ValueError("Invalid trader threshold")
    if type(raw["window"]) is not int or raw["window"] not in WINDOW_CHOICES:
        raise ValueError("Invalid time window")
    if type(raw["min_net"]) not in (float, int) or raw["min_net"] not in NET_CHOICES:
        raise ValueError("Invalid minimum HYPE amount")
    if raw["range"] not in RANGE_CHOICES:
        raise ValueError("Invalid time range")
    lp = raw["launchpad"]
    if not isinstance(lp, str) or len(lp) > 24 or (lp and not lp.isalnum()):
        raise ValueError("Invalid launchpad")
    if lp != lp.lower():
        raise ValueError("Launchpad must be lowercase")
    return dict(raw)


def make_settings_engine(url: str) -> Engine:
    if not url or "://" not in url:
        raise ValueError("CONSENSUS_DATABASE_URL must point to the restricted role")
    engine = sa.create_engine(url, pool_size=2, max_overflow=0, pool_timeout=4,
                              pool_pre_ping=True, pool_recycle=600,
                              connect_args={"connect_timeout": 5,
                                            "application_name": "consensus-alert-settings"})
    try:
        with engine.connect() as c:
            row = c.execute(sa.text(
                "SELECT current_user, r.rolsuper, r.rolcreatedb, r.rolcreaterole "
                "FROM pg_roles r WHERE r.rolname=current_user")).one()
            if row[0] != "consensus_rw" or any(row[1:]):
                raise ValueError("Consensus settings DB must use restricted consensus_rw")
            if c.execute(sa.text(
                "SELECT has_table_privilege(current_user, "
                "'public.notification_outbox', 'SELECT')")).scalar():
                raise ValueError("Consensus settings role can access private tracker tables")
            c.execute(sa.text(f"SELECT 1 FROM {SCHEMA}.settings LIMIT 1"))
    except Exception:
        engine.dispose()
        raise
    return engine


class ConsensusSettingsStore:
    """Writable only as the isolated consensus_rw DB role; no tracker tables accessible."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def read(self) -> dict[str, Any]:
        with self.engine.connect() as c:
            r = c.execute(sa.text(f"SELECT * FROM {SCHEMA}.settings WHERE id=1")).mappings().one()
        return self._as_dict(r)

    @staticmethod
    def _as_dict(r: Any) -> dict[str, Any]:
        return {"version": int(r["version"]), "enabled": bool(r["enabled"]),
                "updated_at": r["updated_at"].astimezone(UTC).isoformat(),
                "traders": int(r["min_traders"]), "window": int(r["window_min"]),
                "min_net": float(r["min_net_hype"]), "range": r["period"],
                "launchpad": r["launchpad"], "independent": bool(r["independent"])}

    def save(self, raw: Any) -> dict[str, Any]:
        d = validate_settings(raw)
        with self.engine.begin() as c:
            row = c.execute(sa.text(
                f"SELECT version FROM {SCHEMA}.settings WHERE id=1 FOR UPDATE")).one()
            if int(row[0]) != d["version"]:
                raise RuntimeError("Settings changed since page loaded; refresh before saving")
            r = c.execute(sa.text(f"""
                UPDATE {SCHEMA}.settings
                SET enabled=:enabled, min_traders=:traders, window_min=:window,
                    min_net_hype=:min_net, period=:range, launchpad=:launchpad,
                    independent=:independent, version=version+1, updated_at=now()
                WHERE id=1 RETURNING *"""), d).mappings().one()
            new = self._as_dict(r)
            # Existing unsent events belong to the previous policy. Never leak them after
            # a settings change or re-enabling the bot.
            c.execute(sa.text(f"""
                UPDATE {SCHEMA}.deliveries SET status='cancelled'
                WHERE status='pending'"""))
            c.execute(sa.text(f"""
                INSERT INTO {SCHEMA}.settings_audit (version, config)
                VALUES (:version, CAST(:cfg AS json))"""),
                {"version": new["version"], "cfg": json.dumps(new, sort_keys=True)})
        return new

    def queue(self, settings: dict[str, Any], cards: list[dict[str, Any]],
              now: datetime, links: Any) -> int:
        cutoff = max(datetime.fromisoformat(settings["updated_at"]), now - timedelta(minutes=10))
        inserted = 0
        with self.engine.begin() as c:
            # Guard against an admin policy update while card generation was in flight.
            current = c.execute(sa.text(
                f"SELECT version, enabled FROM {SCHEMA}.settings WHERE id=1 FOR SHARE")).one()
            if current[0] != settings["version"] or not current[1]:
                return 0
            for card in cards[:MAX_EVENTS]:
                at = card["trigger_at"]
                token = str(card["token"]).lower()
                tx = str(card["trigger_tx"]).lower()
                if at < cutoff or at > now + timedelta(seconds=30):
                    continue
                if not PATTERN.fullmatch(token) or not tx.startswith("0x"):
                    continue
                body, buttons = render_card(card, links)
                key = f"{token}:{at.isoformat()}:{tx}"
                result = c.execute(sa.text(f"""
                    INSERT INTO {SCHEMA}.deliveries
                        (version, event_key, token, trigger_at, body, buttons)
                    VALUES (:v, :k, :token, :at, :body, CAST(:buttons AS json))
                    ON CONFLICT (version, event_key) DO NOTHING
                    RETURNING id"""), {"v": settings["version"], "k": key, "token": token,
                                       "at": at, "body": body, "buttons": json.dumps(buttons)})
                inserted += int(result.scalar() is not None)
        return inserted

    def send_due(self, settings: dict[str, Any], bot: ConsensusSender) -> int:
        if not settings["enabled"]:
            return 0
        sent = 0
        for _ in range(MAX_SENDS):
            with self.engine.begin() as c:
                # Re-check settings under lock so turning alerts OFF stops new sends.
                current = c.execute(sa.text(
                    f"SELECT version, enabled FROM {SCHEMA}.settings WHERE id=1 FOR SHARE")).one()
                if not current[1] or current[0] != settings["version"]:
                    return sent
                row = c.execute(sa.text(f"""
                    SELECT * FROM {SCHEMA}.deliveries
                    WHERE status='pending' AND version=:v AND next_retry_at <= now()
                    ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"""),
                    {"v": settings["version"]}).mappings().first()
                if row is None:
                    return sent
                ok, permanent, error, retry = bot.send(row["body"], row["buttons"])
                if ok:
                    c.execute(sa.text(f"""
                        UPDATE {SCHEMA}.deliveries
                        SET status='sent', attempts=attempts+1, sent_at=now(), last_error=NULL
                        WHERE id=:id"""), {"id": row["id"]})
                    sent += 1
                else:
                    attempts = int(row["attempts"]) + 1
                    status = "dead" if permanent or attempts >= 12 else "pending"
                    delay = min(1800, max(retry, 5 * 2 ** min(attempts, 8)))
                    c.execute(sa.text(f"""
                        UPDATE {SCHEMA}.deliveries SET status=:status, attempts=:attempts,
                            last_error=:error, next_retry_at=now()+make_interval(secs=>:delay)
                        WHERE id=:id"""),
                        {"status": status, "attempts": attempts, "error": error[:120],
                         "delay": delay, "id": row["id"]})
            if not ok:
                log.warning("Consensus Telegram delivery failed (%s); retry deferred", error)
                break
        return sent


def _money(value: Any) -> str:
    if value is None:
        return "unavailable"
    x = float(value)
    if abs(x) >= 1_000_000:
        return f"${x / 1_000_000:,.2f}M"
    if abs(x) >= 1_000:
        return f"${x / 1_000:,.2f}K"
    return f"${x:,.2f}"


def render_card(c: dict[str, Any], links: Any) -> tuple[str, dict[str, Any]]:
    """Safe HTML rendering based on the same card data displayed by Consensus on the web."""
    token = str(c["token"]).lower()
    if not PATTERN.fullmatch(token):
        raise ValueError("Bad Consensus contract")
    symbol = html.escape(str(c.get("symbol") or "Token")[:45])
    net = float(c.get("net_hype") or 0)
    usd = c.get("net_usd")
    lines = [
        f"🟢 <b>CONSENSUS BUY · ${symbol}</b>",
        f"👥 <b>{int(c['traders'])} traders</b> · "
        f"{int(c['independent'])} independent"
        + (" · bundle wallet" if c.get("bundled") else ""),
        f"⏱ {int(c['first_n'])} traders within the threshold window",
        f"💰 Net <b>{net:,.2f} HYPE</b>"
        + (f" ≈ {_money(usd)}" if usd is not None else ""),
        f"📈 MC at consensus: <b>{_money(c.get('mcap_at_usd'))}</b>"
        f" · Now: <b>{_money((c.get('mcap_now') or {}).get('market_cap_usd'))}</b>",
    ]
    for m in c.get("members", [])[:6]:
        address = str(m["wallet"]).lower()
        if not PATTERN.fullmatch(address):
            continue
        rank = f" #{int(m['rank'])}" if m.get("rank") is not None else ""
        lines.append(f"🔹 <code>{address[:8]}…{address[-4:]}</code>{rank}"
                     f" · {float(m['net_hype']):.2f} HYPE")
    lines.append(f"📋 <code>{token}</code>")
    address = links.token(token)
    buttons = [[{"text": "📊 Consensus on web", "url": "https://onchainflow.app/consensus"},
                {"text": "🔎 Elyscan", "url": address["Elyscan"]}],
               [{"text": "⚡ Maestro", "url": f"https://t.me/Maestro?start={token}"}]]
    fomo = links.fomo_token(token)
    if fomo:
        buttons[1].append({"text": "📱 Fomo", "url": fomo})
    tx = str(c.get("trigger_tx") or "")
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", tx):
        buttons.append([{"text": "Trigger tx", "url": links.tx(tx)}])
    return "\n".join(lines)[:3900], {"inline_keyboard": buttons}


class ConsensusSender:
    """A SEND-ONLY separate Telegram bot. Never calls getUpdates."""

    def __init__(self, token: str, group_id: str):
        if not token or not re.fullmatch(r"-100[0-9]{5,20}", group_id):
            raise ValueError("A separate bot token and a Telegram supergroup ID are required")
        if token == os.getenv("TG_BOT_TOKEN") or group_id == os.getenv("TG_CHAT_ID"):
            raise ValueError("Consensus bot/group must differ from the existing alert bot/chat")
        self.token, self.group = token, group_id
        self.session = requests.Session()
        self.last_sent = 0.0

    def send(self, body: str, buttons: dict[str, Any]) -> tuple[bool, bool, str, int]:
        wait = 1.2 - (time.monotonic() - self.last_sent)
        if wait > 0:
            time.sleep(wait)
        try:
            r = self.session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.group, "text": body, "parse_mode": "HTML",
                      "disable_web_page_preview": True, "reply_markup": buttons},
                timeout=12)
            self.last_sent = time.monotonic()
            if r.status_code == 429:
                try:
                    retry = int(r.json().get("parameters", {}).get("retry_after", 15))
                except (ValueError, TypeError):
                    retry = 15
                return False, False, "rate_limited", min(1800, retry)
            if r.status_code in (400, 401, 403):
                return False, True, f"HTTP_{r.status_code}", 0
            if r.status_code >= 400:
                return False, False, f"HTTP_{r.status_code}", 15
            if not r.json().get("ok"):
                return False, False, "telegram_not_ok", 15
            return True, False, "", 0
        except requests.RequestException as exc:
            # Never print exception URL: it contains the Telegram bot token.
            return False, False, type(exc).__name__, 15


def run_consensus_alerts(cfg: Config) -> None:
    """Dedicated low-rate, send-only service. Two independent least-privilege DB connections."""
    from .web.format import Links
    from .web.reports import PublicReports, StoredMarket
    from .web.run import assert_least_privilege, make_web_engine
    from .web.service import Dashboard
    from .web.usd import FxPolicy

    if cfg.chain.chain_id != 99901:
        raise SystemExit("Consensus alerts require Elysium chain 99901")
    public_url = os.getenv("CONSENSUS_PUBLIC_DATABASE_URL", "")
    writer_pw = os.getenv("CONSENSUS_DB_PASSWORD", "")
    settings_url = (str(sa.engine.make_url(public_url).set(
        username="consensus_rw", password=writer_pw)) if public_url and writer_pw else "")
    bot = ConsensusSender(os.getenv("CONSENSUS_TG_BOT_TOKEN", ""),
                          os.getenv("CONSENSUS_TG_CHAT_ID", ""))
    if not public_url or not settings_url:
        raise SystemExit("Dedicated settings and public read-only database URLs are required")
    settings_engine = make_settings_engine(settings_url)
    public_engine = make_web_engine(public_url, cfg)
    assert_least_privilege(public_engine)
    store = ConsensusSettingsStore(settings_engine)
    w = dataclasses.replace(cfg.ui, query_timeout_ms=cfg.web.query_timeout_ms)
    market = StoredMarket(public_engine, cfg.chain.chain_id, cfg.web.market_max_age_s)
    rep = PublicReports(public_engine, cfg.chain.chain_id, w, value_unit="HYPE",
                        market=market, collector_interval_s=cfg.collector.interval_s)
    rep.fx_policy = FxPolicy.from_web(cfg.web)
    rep.hidden_tokens = tuple(dict.fromkeys(
        list(cfg.alerts.mute_tokens)
        + [v.lower() for v in os.getenv("WEB_ACTIVITY_HIDDEN_TOKENS", "").split(",")
           if PATTERN.fullmatch(v.lower())]))
    dash = Dashboard(rep, cfg)
    links = Links(cfg.links)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    conn = settings_engine.connect()
    try:
        locked = conn.execute(sa.text("SELECT pg_try_advisory_lock(:k)"),
                              {"k": LOCK_ID}).scalar()
        conn.commit()
        if not locked:
            raise SystemExit("Another consensus sender holds the dedicated session lock")
        log.info("Consensus sender started (dedicated group; no Telegram update consumer)")
        while not stop.is_set():
            try:
                saved = store.read()
                if saved["enabled"]:
                    p = ConsensusParams(
                        min_traders=saved["traders"], window_min=saved["window"],
                        min_net_hype=saved["min_net"], range=saved["range"],
                        launchpad=saved["launchpad"], independent=saved["independent"])
                    now = datetime.now(UTC)
                    cards = dash.consensus(p, now)
                    if not cards["meta"]["stale"]:
                        queued = store.queue(saved, cards["cards"], now, links)
                        sent = store.send_due(saved, bot)
                        if queued or sent:
                            log.info("Consensus: queued=%d sent=%d", queued, sent)
                    else:
                        log.warning("Consensus input stale: deferring sends")
            except Exception:
                log.exception("Consensus cycle failed closed")
            stop.wait(POLL_SECONDS)
    finally:
        conn.close()
        public_engine.dispose()
        settings_engine.dispose()
