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


def build_recovery_sources(cfg: Config, exec_rpc: ExecutionRpc | None,
                           environ: Mapping[str, str] = os.environ) -> list[ExecutionRpc]:
    """Chain sources for owner leg recovery (F-02): the execution RPC plus the endpoints listed in
    `recovery_rpc_urls_env`. Every endpoint is read-only (ExecutionRpc allowlist). Endpoints that
    are the scanner/paper RPC, duplicate another endpoint or share a host are refused: two names
    for one provider are not two independent sources. Fewer than two sources is allowed (the
    owner can still DIAGNOSE) but can never RESOLVE."""
    if exec_rpc is None:
        return []
    out = [exec_rpc]
    raw = environ.get(cfg.trading.recovery_rpc_urls_env, "")
    shared = {str(u).strip() for u in
              (cfg.trading.paper_rpc_url, *getattr(getattr(cfg, "chain", None), "rpc_urls", []))}
    seen_hosts = {exec_rpc.host}
    for url in (u.strip() for u in raw.split(",")):
        if not url:
            continue
        if url in shared:
            raise ExecutionRpcConfigError("a recovery RPC must not be the scanner/paper RPC")
        try:
            rpc = ExecutionRpc(url)
        except ValueError:
            raise ExecutionRpcConfigError(
                f"{cfg.trading.recovery_rpc_urls_env} holds an invalid http(s) URL") from None
        if rpc.host in seen_hosts:
            raise ExecutionRpcConfigError("recovery RPC endpoints must have distinct hosts")
        seen_hosts.add(rpc.host)
        out.append(rpc)
    return out
