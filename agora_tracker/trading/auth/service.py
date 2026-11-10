"""Bounded HTTPS authentication adapter for #100's read-only Mini App interface."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..chain import ReadRpc, address
from ..miniapp.app import Principal
from ..paper import PaperFlow, TradeRefused
from .siwe import message, recover, require_eoa
from .store import AuthStore, Challenge, Session
from .telegram import AuthRefused, RateLimited, TelegramVerifier, unique_object

COOKIE = "__Host-elysium_auth"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
INTENT = re.compile(r"[A-Za-z0-9_-]{16}")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuthService:
    """Explicit dependency injection only. Construction does not deploy or start a bot."""

    ownership_proof = True

    def __init__(
        self,
        *,
        flow: PaperFlow,
        store: AuthStore,
        bot_token: str,
        bot_id: int,
        origin: str,
        rpc: ReadRpc,
        now=time.time,
    ):
        url = urlsplit(origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
            or not origin.isascii()
            or url.netloc != url.netloc.lower()
            or any(ord(c) < 33 for c in origin)
        ):
            raise ValueError("Authentication requires a canonical HTTPS origin without a path")
        self.flow, self.store, self.rpc, self.now = flow, store, rpc, now
        self.origin, self.domain = origin, url.netloc
        self.uri = origin + "/"
        self.telegram = TelegramVerifier(bot_token, bot_id)
        self.realm = self.telegram.rate_key("session-realm:" + self.origin)

    def _origin(self, request: Request):
        if (
            request.headers.getlist("origin") != [self.origin]
            or request.headers.getlist("host") != [self.domain]
            or request.url.scheme != "https"
            or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
        ):
            raise AuthRefused("Authentication origin refused")

    @staticmethod
    def _cookie(request: Request) -> str | None:
        cookies = ";".join(request.headers.getlist("cookie"))
        if len(cookies) > 4096:
            raise AuthRefused("Authentication refused")
        matches = [
            part.strip().partition("=")[2]
            for part in cookies.split(";")
            if part.strip().partition("=")[0] == COOKIE
        ]
        if len(matches) > 1 or (matches and not TOKEN.fullmatch(matches[0])):
            raise AuthRefused("Authentication refused")
        return digest(matches[0]) if matches else None

    def _session(self, request: Request) -> Session:
        self._origin(request)
        cookie = self._cookie(request)
        headers = request.headers.getlist("x-csrf-token")
        if cookie is None or len(headers) != 1 or not TOKEN.fullmatch(headers[0]):
            raise AuthRefused("Authentication required")
        s = self.store.get(cookie, int(self.now()))
        if (
            s.origin != self.origin
            or s.bot != self.telegram.bot_id
            or not hmac.compare_digest(s.realm, self.realm)
            or not hmac.compare_digest(s.csrf, digest(headers[0]))
        ):
            raise AuthRefused("Authentication required")
        return s

    def _intent(self, s: Session, sid: str | None, *, allow_paused=False):
        if sid != s.intent:
            raise AuthRefused("Authentication session mismatch")
        return self.flow.get(s.uid, s.uid, sid, allow_paused=allow_paused)

    def _rate(self, name: str, subject: str, maximum: int):
        self.store.limit(self.telegram.rate_key(name + ":" + subject), maximum, int(self.now()))

    def _bootstrap(self, request, data):
        self._origin(request)
        self._rate("bootstrap-ip", request.client.host if request.client else "unknown", 10)
        sid, raw = data["intent"], data["init_data"]
        launch = self.telegram.verify(raw, sid, int(self.now()))
        self._rate("bootstrap-user", str(launch.user_id), 6)
        i = self.flow.get(launch.user_id, launch.user_id, sid)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(self.now())
        expires = min(int(i.expires_at), launch.expires, now + 120)
        if expires <= now:
            raise AuthRefused("Authentication required")
        s = Session(
            digest(token),
            digest(csrf),
            launch.user_id,
            self.telegram.bot_id,
            sid,
            self.origin,
            now,
            expires,
            None,
            0,
            self.realm,
        )
        self.store.create(s, launch.fingerprint, launch.expires, self._cookie(request))
        return self._success(token, csrf, expires)

    def _challenge(self, request, data):
        s = self._session(request)
        i = self._intent(s, data["intent"])
        self._rate("challenge", str(s.uid) + ":" + s.intent, 6)
        # Revoke previous proof/challenges even when the new account/chain is refused.
        s = self.store.revoke(s.token, int(self.now()))
        wallet = address(data["wallet"])
        if data["chain_id"] != 99901 or type(data["chain_id"]) is not int or wallet != i.wallet:
            raise AuthRefused(
                "Use the alert's watch wallet on chain 99901; reopen after changing it"
            )
        require_eoa(self.rpc, wallet)
        now, nonce = int(self.now()), secrets.token_hex(32)
        expires = min(now + 60, s.expires)
        binding = digest(f"{s.token}:{s.uid}:{s.intent}:{self.origin}:{nonce}")
        text = message(self.domain, self.uri, wallet, nonce, now, expires, binding)
        q = Challenge(nonce, s.token, s.generation, wallet, now, expires, binding, text, 0)
        self.store.challenge(q, now)
        return JSONResponse(
            {
                "nonce": nonce,
                "message": text,
                "wallet": wallet,
                "chain_id": 99901,
                "issued_at": now,
                "request_id": binding,
                "expires_at": expires,
                "signing_enabled": False,
                "authentication_only": True,
            }
        )

    def _verify(self, request, data):
        s = self._session(request)
        i = self._intent(s, data["intent"])
        self._rate("verify", str(s.uid) + ":" + s.intent, 10)
        wallet = address(data["wallet"])
        if data["chain_id"] != 99901 or type(data["chain_id"]) is not int or wallet != i.wallet:
            self.store.revoke(s.token, int(self.now()))
            raise AuthRefused("Ownership proof refused")
        if not isinstance(data["nonce"], str) or not re.fullmatch(r"[a-f0-9]{64}", data["nonce"]):
            raise AuthRefused("Ownership proof refused")
        q = self.store.consume(s.token, data["nonce"], wallet, int(self.now()))
        expected = message(self.domain, self.uri, wallet, q.nonce, q.issued, q.expires, q.binding)
        if q.message != expected or recover(expected, data["signature"]) != wallet:
            raise AuthRefused("Ownership proof refused")
        require_eoa(self.rpc, wallet)
        self._intent(s, data["intent"])  # alert/wallet/expiry may change during RPC I/O
        if self.flow.wallet(s.uid, s.uid) != wallet:
            raise AuthRefused("Ownership proof refused")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.store.complete(s, q, digest(token), digest(csrf), int(self.now()))
        return self._success(token, csrf, s.expires)

    @staticmethod
    def _success(token: str, csrf: str, expires: int):
        response = JSONResponse(
            {
                "csrf": csrf,
                "expires_at": expires,
                "authentication_only": True,
                "signing_enabled": False,
            }
        )
        response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="strict", path="/")
        return response

    async def authenticate(self, request: Request, init_data: str, claimed_wallet: str | None):
        # The old interface is retained. InitData is accepted ONLY by bootstrap,
        # not repeatedly used as a bearer credential by preview endpoints.
        try:
            return await run_in_threadpool(self._authenticate, request, init_data, claimed_wallet)
        except (AuthRefused, TradeRefused, sqlite3.Error, OSError, ValueError, TypeError):
            return None

    def _authenticate(self, request, init_data, claimed_wallet):
        if init_data:
            raise AuthRefused("Use one-time Telegram authentication")
        s = self._session(request)
        self._intent(
            s,
            getattr(request.state, "miniapp_intent", None),
            allow_paused=request.url.path in ("/api/cancel", "/api/revoke-plan"),
        )
        self._rate("preview", str(s.uid) + ":" + s.intent, 60)
        try:
            wallet = address(claimed_wallet) if claimed_wallet is not None else None
        except TradeRefused:
            self.store.revoke(s.token, int(self.now()))
            raise AuthRefused("Ownership proof refused") from None
        if s.wallet and wallet != s.wallet:
            s = self.store.revoke(s.token, int(self.now()))
        return Principal(
            s.uid,
            s.wallet,
            intent_id=s.intent,
            session_binding=digest(f"{s.token}:{s.generation}:{s.realm}"),
            session_expires_at=s.expires,
        )

    def routes(self):
        async def endpoint(request):
            try:
                self._origin(request)
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    raise AuthRefused("Authentication refused")
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 12288:
                        raise AuthRefused("Authentication refused")
                    body.extend(chunk)
                data = json.loads(body, object_pairs_hook=unique_object)
                action = request.url.path.rsplit("/", 1)[-1]
                fields = {
                    "bootstrap": {"intent", "init_data"},
                    "challenge": {"intent", "wallet", "chain_id"},
                    "verify": {"intent", "wallet", "chain_id", "nonce", "signature"},
                    "revoke": {"intent"},
                    "logout": {"intent"},
                }[action]
                if (
                    not isinstance(data, dict)
                    or set(data) != fields
                    or not isinstance(data.get("intent"), str)
                    or not INTENT.fullmatch(data["intent"])
                ):
                    raise AuthRefused("Authentication refused")
                if action in ("bootstrap", "challenge", "verify"):
                    return await run_in_threadpool(getattr(self, "_" + action), request, data)
                s = self._session(request)
                if data["intent"] != s.intent:
                    raise AuthRefused("Authentication session mismatch")
                self.store.revoke(s.token, int(self.now()), logout=action == "logout")
                response = JSONResponse({"revoked": True, "signing_enabled": False})
                if action == "logout":
                    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
                return response
            except RateLimited:
                return JSONResponse(
                    {
                        "error": "rate_limited",
                        "message": "Too many authentication attempts",
                        "signing_enabled": False,
                    },
                    429,
                    headers={"Retry-After": "60"},
                )
            except (AuthRefused, TradeRefused, ValueError, TypeError, KeyError):
                return JSONResponse(
                    {
                        "error": "not_authenticated",
                        "message": "Authentication refused. Reopen a fresh private Telegram alert; contract wallets are unsupported.",
                        "signing_enabled": False,
                    },
                    401,
                )
            except (sqlite3.Error, OSError):
                return JSONResponse(
                    {
                        "error": "auth_unavailable",
                        "message": "Authentication unavailable",
                        "signing_enabled": False,
                    },
                    503,
                )

        return [
            Route("/api/auth/" + action, endpoint, methods=["POST"])
            for action in ("bootstrap", "challenge", "verify", "revoke", "logout")
        ]
