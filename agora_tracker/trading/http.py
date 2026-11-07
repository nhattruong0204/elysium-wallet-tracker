"""Bounded JSON downloads for private PAPER transports; errors never include payloads."""

from __future__ import annotations

import json
import time

from .paper import TradeRefused

MAX_JSON_BYTES = 1_048_576


def bounded_json(response, *, deadline: float):
    try:
        length = response.headers.get("Content-Length")
        if isinstance(length, str) and (not length.isdigit() or int(length) > MAX_JSON_BYTES):
            raise TradeRefused("Paper response exceeds the JSON size limit")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=4096):
            if time.monotonic() > deadline:
                raise TradeRefused("Paper response download budget exhausted")
            if len(body) + len(chunk) > MAX_JSON_BYTES:
                raise TradeRefused("Paper response exceeds the JSON size limit")
            body.extend(chunk)
        if time.monotonic() > deadline:
            raise TradeRefused("Paper response download budget exhausted")
        try:
            return json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise TradeRefused("Invalid paper JSON response") from None
    finally:
        response.close()
