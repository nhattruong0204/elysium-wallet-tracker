"""Bot-secret HMAC validation for private inline-button Mini App launches only."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl


class AuthRefused(Exception):
    """Messages are intentionally static; never include credentials or PII."""


class RateLimited(AuthRefused):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuthRefused("Authentication refused")
        result[key] = value
    return result


@dataclass(frozen=True)
class Launch:
    user_id: int
    fingerprint: str
    expires: int


class TelegramVerifier:
    MAX_AGE = 120

    def __init__(self, bot_token: str, bot_id: int):
        if (
            type(bot_id) is not int
            or bot_id <= 0
            or not isinstance(bot_token, str)
            or not re.fullmatch(str(bot_id) + r":[A-Za-z0-9_-]{20,128}", bot_token)
        ):
            raise ValueError("Invalid authentication bot configuration")
        self.bot_id = bot_id
        self._key = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")

    def rate_key(self, value: str) -> str:
        return hmac.digest(self._key, value.encode(), "sha256").hex()

    def verify(self, raw: str, intent: str, now: int) -> Launch:
        try:
            if (
                not isinstance(raw, str)
                or not raw.isascii()
                or len(raw) > 8192
                or re.search(r"%(?![0-9a-fA-F]{2})", raw)
            ):
                raise AuthRefused("Authentication refused")
            fields = unique_object(
                parse_qsl(
                    raw,
                    keep_blank_values=True,
                    strict_parsing=True,
                    encoding="utf-8",
                    errors="strict",
                    max_num_fields=16,
                )
            )
            if any(
                not re.fullmatch(r"[a-z_]{1,40}", k) or "\n" in v or "\r" in v
                for k, v in fields.items()
            ):
                raise AuthRefused("Authentication refused")
            digest = fields.pop("hash", "")
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise AuthRefused("Authentication refused")
            # Bot-secret validation excludes hash only. The optional Telegram signature
            # remains in this HMAC string; the third-party Ed25519 algorithm is different.
            check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
            expected = hmac.digest(self._key, check.encode(), "sha256").hex()
            if not hmac.compare_digest(expected, digest):
                raise AuthRefused("Authentication refused")
            date = fields.get("auth_date", "")
            if (
                not re.fullmatch(r"[1-9][0-9]{0,11}", date)
                or not 0 <= now - int(date) < self.MAX_AGE
            ):
                raise AuthRefused("Authentication refused")
            user = json.loads(fields.get("user", ""), object_pairs_hook=unique_object)
            uid = user.get("id") if isinstance(user, dict) else None
            if (
                type(uid) is not int
                or not 0 < uid < 2**52
                or user.get("is_bot", False) is not False
            ):
                raise AuthRefused("Authentication refused")
            # #100 uses a web_app button in the caller's private bot chat, not an
            # attachment/group/direct-link/shared launch. Never infer a chat from user input.
            if (
                not re.fullmatch(r"[A-Za-z0-9_+=/-]{1,256}", fields.get("query_id", ""))
                or any(
                    k in fields
                    for k in ("chat", "receiver", "chat_instance", "chat_join_request_query_id")
                )
                or fields.get("chat_type", "sender") != "sender"
                or fields.get("start_param", intent) != intent
                or fields.get("bot_id", str(self.bot_id)) != str(self.bot_id)
            ):
                raise AuthRefused("Authentication refused")
            return Launch(
                uid,
                hashlib.sha256(f"{self.bot_id}:{digest}".encode()).hexdigest(),
                int(date) + self.MAX_AGE,
            )
        except (ValueError, TypeError, UnicodeError):
            raise AuthRefused("Authentication refused") from None
