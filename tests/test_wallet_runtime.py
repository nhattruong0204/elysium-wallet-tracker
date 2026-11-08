from types import SimpleNamespace

import pytest

from agora_tracker.config import TradingConfig
from agora_tracker.trading.wallet.runtime import ExecutionRpcConfigError, build_execution_rpc


def cfg(rpc_urls=("https://scanner.example",), **kw):
    return SimpleNamespace(trading=TradingConfig(**kw), chain=SimpleNamespace(rpc_urls=list(rpc_urls)))


ON = dict(wallet_execution_enabled=True, ledger_enabled=True)


def test_off_by_default_builds_nothing():
    assert build_execution_rpc(cfg(), {"TRADING_EXEC_RPC_URL": "https://x.example"}) is None


def test_enabled_without_a_url_is_fatal_and_names_only_the_variable():
    with pytest.raises(ExecutionRpcConfigError, match="TRADING_EXEC_RPC_URL"):
        build_execution_rpc(cfg(**ON), {})


def test_enabled_with_a_bad_url_never_echoes_it():
    with pytest.raises(ExecutionRpcConfigError) as e:
        build_execution_rpc(cfg(**ON), {"TRADING_EXEC_RPC_URL": "ftp://host/KEY123"})
    assert "KEY123" not in str(e.value)


def test_must_not_reuse_the_paper_rpc():
    with pytest.raises(ExecutionRpcConfigError, match="must not"):
        build_execution_rpc(cfg(**ON), {"TRADING_EXEC_RPC_URL": "https://rpc.agoradex.app"})


def test_must_not_reuse_the_scanner_rpc():
    with pytest.raises(ExecutionRpcConfigError, match="must not"):
        build_execution_rpc(cfg(**ON), {"TRADING_EXEC_RPC_URL": "https://scanner.example"})


def test_builds_an_isolated_rpc():
    rpc = build_execution_rpc(cfg(**ON), {"TRADING_EXEC_RPC_URL": "https://exec.example/KEY"})
    assert rpc is not None and rpc.host == "exec.example"


def test_variable_names_are_validated():
    with pytest.raises(ValueError):
        TradingConfig(execution_rpc_url_env="lower-case")
