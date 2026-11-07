"""Issue #88 UI factory. Authentication and live execution are intentionally absent."""

from .app import Principal, create_app

__all__ = ["Principal", "create_app"]
