"""DATABASE_URL handling."""

from __future__ import annotations

import os
import re


def resolve_database_url(explicit: str | None = None) -> str:
    """explicit argument > DATABASE_URL env. Normalises postgres:// and postgresql:// to the
    psycopg 3 driver."""
    url = explicit or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (e.g. "
                           "postgresql+psycopg://tracker:PASSWORD@postgres:5432/tracker)")
    return re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url)


def redact_db_url(url: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:***@", url)
