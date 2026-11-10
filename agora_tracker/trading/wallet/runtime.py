"""Startup wiring for the execution RPC (issue #93). Nothing calls this by default.

Fails closed: with wallet execution enabled and no usable execution RPC there is no way to
reconcile what a wallet did, so the flag must not be honoured."""

from __future__ import annotations

import os
from collections.abc import Mapping

from ...config import Config
from .rpc import ExecutionRpc


class ExecutionRpcConfigError(RuntimeError):
    pass


def build_execution_rpc(cfg: Config, environ: Mapping[str, str] = os.environ) -> ExecutionRpc | None:
    """None when wallet execution is off. Otherwise an isolated ExecutionRpc, or a fatal error.
    The URL is a secret: it comes only from the environment and is never echoed."""
    if not cfg.trading.wallet_execution_enabled:
        return None
    name = cfg.trading.execution_rpc_url_env
    url = environ.get(name, "")
    if not url:
        raise ExecutionRpcConfigError(f"{name} is not set; wallet execution needs its own RPC")
    shared = {cfg.trading.paper_rpc_url, *getattr(getattr(cfg, "chain", None), "rpc_urls", [])}
    if url.strip() in {str(u).strip() for u in shared}:
        raise ExecutionRpcConfigError("the execution RPC must not be the scanner/paper RPC")
    try:
        return ExecutionRpc(url)
    except ValueError:
        raise ExecutionRpcConfigError(f"{name} is not a valid http(s) URL") from None
