"""Drains the alert outbox to Telegram (or stdout when no bot is configured)."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import requests

from .config import TelegramConfig
from .db import Store

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, store: Store, cfg: TelegramConfig, dry_run: bool = False,
                 renderer: Callable[[dict], tuple] | None = None):
        self.store, self.cfg = store, cfg
        self.renderer = renderer
        self.enabled = bool(cfg.bot_token and cfg.chat_id) and not dry_run
        self.session = requests.Session()
        self._last = 0.0
        self._blocked_until = 0.0
        if not self.enabled:
            log.info("Telegram not configured (or --dry-run): alerts print to stdout")

    def _render(self, row: dict) -> tuple[str, dict | None, str | None]:
        """Final text, inline keyboard and optional image URL shown above the card. A
        rendering failure falls back to the stored text: an alert is never held back
        because market data or rendering broke."""
        if self.renderer is None or not row.get("payload"):
            return row["text"], None, None
        try:
            out = self.renderer(row)
        except Exception:  # noqa: BLE001 - delivery must not depend on enrichment
            log.exception("rendering alert %s failed; sending stored text", row.get("id"))
            return row["text"], None, None
        return (out[0], out[1], out[2] if len(out) > 2 else None)

    def _send(self, text: str, markup: dict | None = None,
              preview: str | None = None) -> tuple[bool, str, bool]:
        """Returns (ok, error, permanent)."""
        if not self.enabled:
            buttons = ""
            if markup:
                buttons = "\n" + "  ".join(f"[{b['text']}]" for row in markup["inline_keyboard"]
                                            for b in row)
            print("-" * 60 + "\n" + re.sub(r"<[^>]+>", "", text) + buttons, flush=True)
            return True, "", False
        wait = self.cfg.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage"
        try:
            # No link previews, except the token picture when the card provides one.
            lp = ({"url": preview, "prefer_large_media": True, "show_above_text": True}
                  if preview else {"is_disabled": True})
            body: dict = {"chat_id": self.cfg.chat_id, "text": text[:4000], "parse_mode": "HTML",
                          "link_preview_options": lp}
            if markup:
                body["reply_markup"] = markup
            r = self.session.post(url, json=body, timeout=self.cfg.timeout_s)
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
        """Send due alerts oldest first. Each alert is claimed with FOR UPDATE SKIP LOCKED
        inside its own transaction, so concurrent notifiers never send the same row.

        Delivery is at-least-once: Telegram has no transaction shared with PostgreSQL. If
        Telegram accepts a message but the response is lost (timeout) or the process dies
        before COMMIT, the alert is sent again later. Duplicates are therefore possible
        but rare; losses are not. Every attempt is recorded in notification_deliveries.
        On a transient failure the alert backs off and the drain stops (Telegram is
        probably down or limiting); a permanent failure dead-letters only that alert."""
        if time.monotonic() < self._blocked_until:
            return 0
        sent = 0
        for _ in range(max_items):
            with self.store.tx() as c:
                row = self.store.claim_due_alert(c)
                if row is None:
                    break
                text, markup, preview = self._render(row)
                ok, err, permanent = self._send(text, markup, preview)
                if ok:
                    self.store.mark_sent(c, row["id"])
                    sent += 1
                    continue
                status = self.store.mark_failed(c, row["id"], row["attempts"], err,
                                                self.cfg.max_attempts, permanent)
            log.warning("alert %s not delivered (%s) -> %s", row["id"], err, status)
            if not permanent:
                break
        return sent
