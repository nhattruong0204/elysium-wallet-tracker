"""Startup wiring for the execution RPC (issue #93). Nothing calls this by default.

Fails closed: with wallet execution enabled and no usable execution RPC there is no way to
reconcile what a wallet did, so the flag must not be honoured."""

from __future__ import annotations

import os
import re
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
        return ExecutionRpc(url, operator=(cfg.trading.execution_rpc_operator or None))
    except ValueError:
        raise ExecutionRpcConfigError(f"{name} is not a valid http(s) URL or operator label") from None


_LABELED = re.compile(r"^([a-z0-9][a-z0-9._-]{0,63})=(https?://.+)$", re.I)


def build_rpc_sources(cfg: Config, exec_rpc: ExecutionRpc | None,
                      environ: Mapping[str, str] = os.environ) -> list[ExecutionRpc]:
    """The independently configured chain sources (F-15): the execution RPC plus the endpoints in
    `corroboration_rpc_urls_env` (comma-separated SECRET urls, each optionally `label=url`). The
    same list feeds the receipt worker, the auto-reconciler and owner recovery. Every endpoint is
    read-only (ExecutionRpc allowlist). Refused at startup: the scanner/paper RPC, a duplicate URL,
    two endpoints with the same host, and two endpoints with the same OPERATOR (an explicit label,
    else the registrable domain). That is what code can check; whether two operators share an
    upstream node is the deployer's attestation (docs/TRADING_RPC_CORROBORATION.md). Fewer than
    two sources is allowed (owners can still DIAGNOSE) but nothing safety-critical is then ever
    recorded: the system fails closed."""
    if exec_rpc is None:
        return []
    out = [exec_rpc]
    raw = environ.get(cfg.trading.corroboration_rpc_urls_env, "")
    shared = {str(u).strip() for u in
              (cfg.trading.paper_rpc_url, *getattr(getattr(cfg, "chain", None), "rpc_urls", []))}
    seen_hosts, seen_ops, seen_urls = {exec_rpc.host}, {exec_rpc.operator}, set()
    for entry in (u.strip() for u in raw.split(",")):
        if not entry:
            continue
        m = _LABELED.match(entry)
        label, url = (m.group(1).lower(), m.group(2)) if m else (None, entry)
        if url in shared:
            raise ExecutionRpcConfigError("a corroboration RPC must not be the scanner/paper RPC")
        if url in seen_urls:
            raise ExecutionRpcConfigError("a corroboration RPC url is listed twice")
        seen_urls.add(url)
        try:
            rpc = ExecutionRpc(url, operator=label)
        except ValueError:
            raise ExecutionRpcConfigError(
                f"{cfg.trading.corroboration_rpc_urls_env} holds an invalid entry") from None
        if rpc.host in seen_hosts:
            raise ExecutionRpcConfigError("corroboration RPC endpoints must have distinct hosts")
        if rpc.operator in seen_ops:
            raise ExecutionRpcConfigError(
                f"two RPC endpoints share the operator '{rpc.operator}': they are not independent "
                "sources (use endpoints from different operators)")
        seen_hosts.add(rpc.host)
        seen_ops.add(rpc.operator)
        out.append(rpc)
    return out
