"""Drains the alert outbox to Telegram (or stdout when no bot is configured)."""

from __future__ import annotations

import logging
import re
import time

import requests

from .config import TelegramConfig
from .db import Store

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, store: Store, cfg: TelegramConfig, dry_run: bool = False):
        self.store, self.cfg = store, cfg
        self.enabled = bool(cfg.bot_token and cfg.chat_id) and not dry_run
        self.session = requests.Session()
        self._last = 0.0
        self._blocked_until = 0.0
        if not self.enabled:
            log.info("Telegram not configured (or --dry-run): alerts print to stdout")

    def _send(self, text: str) -> bool:
        if not self.enabled:
            print("-" * 60 + "\n" + re.sub(r"<[^>]+>", "", text), flush=True)
            return True
        wait = self.cfg.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = self.session.post(
                f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage",
                json={"chat_id": self.cfg.chat_id, "text": text[:4000], "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=self.cfg.timeout_s)
            self._last = time.monotonic()
            if r.status_code == 429:
                retry = r.json().get("parameters", {}).get("retry_after", 5)
                self._blocked_until = time.monotonic() + retry
                log.warning("Telegram rate limited for %ss", retry)
                return False
            if r.status_code == 400 and "parse" in r.text.lower():
                # Bad HTML: resend as plain text instead of retrying forever.
                r = self.session.post(
                    f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage",
                    json={"chat_id": self.cfg.chat_id, "text": re.sub(r"<[^>]+>", "", text)[:4000]},
                    timeout=self.cfg.timeout_s)
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.warning("Telegram send failed: %s", e)
            return False

    def drain(self, max_items: int = 20) -> int:
        if time.monotonic() < self._blocked_until:
            return 0
        sent = 0
        for row in self.store.pending_alerts(max_items):
            if self._send(row["text"]):
                self.store.mark_sent(row["id"])
                sent += 1
            else:
                self.store.mark_failed(row["id"])
                break
        return sent
