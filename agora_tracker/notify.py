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

    def _send(self, text: str) -> tuple[bool, str, bool]:
        """Returns (ok, error, permanent)."""
        if not self.enabled:
            print("-" * 60 + "\n" + re.sub(r"<[^>]+>", "", text), flush=True)
            return True, "", False
        wait = self.cfg.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage"
        try:
            r = self.session.post(url, json={"chat_id": self.cfg.chat_id, "text": text[:4000],
                                             "parse_mode": "HTML",
                                             "disable_web_page_preview": True},
                                  timeout=self.cfg.timeout_s)
            self._last = time.monotonic()
            if r.status_code == 429:
                try:
                    retry = int(r.json().get("parameters", {}).get("retry_after", 5))
                except ValueError:
                    retry = 5
                self._blocked_until = time.monotonic() + retry
                return False, f"429 retry_after={retry}", False
            if r.status_code == 400 and "parse" in r.text.lower():
                # Bad HTML: resend as plain text instead of retrying forever.
                r = self.session.post(url, json={"chat_id": self.cfg.chat_id,
                                                 "text": re.sub(r"<[^>]+>", "", text)[:4000]},
                                      timeout=self.cfg.timeout_s)
            if r.status_code == 400:
                return False, f"400 {r.text[:200]}", True   # message itself is invalid
            r.raise_for_status()
            return True, "", False
        except requests.RequestException as e:
            # Never log the exception text verbatim: it contains the URL with the bot token.
            err = type(e).__name__
            if isinstance(e, requests.HTTPError) and e.response is not None:
                err = f"HTTP {e.response.status_code}"
            return False, err, False

    def drain(self, max_items: int = 20) -> int:
        """Send due alerts in order. On a transient failure the alert is rescheduled with
        exponential backoff and the drain stops (Telegram is probably down or limiting).

        Delivery is at-least-once: if Telegram accepted a message but the response was
        lost (timeout), it will be sent again. Duplicates are therefore possible but
        rare; losses are not."""
        if time.monotonic() < self._blocked_until:
            return 0
        sent = 0
        for row in self.store.pending_alerts(max_items):
            ok, err, permanent = self._send(row["text"])
            if ok:
                self.store.mark_sent(row["id"])
                sent += 1
                continue
            status = self.store.mark_failed(row["id"], err, self.cfg.max_attempts, permanent)
            log.warning("alert %s not delivered (%s) -> %s", row["id"], err, status)
            if not permanent:
                break
        return sent
