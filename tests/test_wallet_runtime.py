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


# ------------------------------------------------------------------ F-15: the corroboration sources
from agora_tracker.trading.wallet.runtime import build_rpc_sources  # noqa: E402

EXEC = "https://exec.alpha.example/KEY"
KEYS = "TRADING_CORROBORATION_RPC_URLS"


def exec_rpc(**kw):
    return build_execution_rpc(cfg(**ON, **kw), {"TRADING_EXEC_RPC_URL": EXEC})


def test_no_execution_rpc_means_no_sources():
    assert build_rpc_sources(cfg(**ON), None, {KEYS: "https://b.example"}) == []


def test_the_execution_rpc_alone_is_one_source_and_the_pair_requirement_is_enforced_downstream():
    out = build_rpc_sources(cfg(**ON), exec_rpc(), {})
    assert len(out) == 1


def test_two_distinct_operators_make_two_sources():
    out = build_rpc_sources(cfg(**ON), exec_rpc(), {KEYS: "https://rpc.beta.example/K2"})
    assert [x.operator for x in out] == ["alpha.example", "beta.example"]
    assert all(x.chain_id == 99901 for x in out)


def test_labels_name_the_operator_and_override_the_domain():
    out = build_rpc_sources(cfg(**ON), exec_rpc(execution_rpc_operator="alpha"),
                            {KEYS: "bravo=https://x.cloudgw.example/K, charlie=https://y.cloudgw.example/K"})
    assert [x.operator for x in out] == ["alpha", "bravo", "charlie"]
    assert out[1].host == "x.cloudgw.example"


@pytest.mark.parametrize("value,why", [
    ("https://other.alpha.example/K", "share the operator"),           # same registrable domain
    ("https://exec.alpha.example/OTHER", "distinct hosts"),            # same host
    ("https://b.example/K,https://b.example/K", "twice"),
    ("https://scanner.example", "scanner/paper"),
    ("ftp://b.example/K", "invalid entry"),
    ("bad label=https://b.example/K", "invalid entry")])
def test_dependent_or_unusable_sources_are_refused_at_startup(value, why):
    with pytest.raises(ExecutionRpcConfigError, match=why) as e:
        build_rpc_sources(cfg(**ON), exec_rpc(), {KEYS: value})
    assert "KEY" not in str(e.value).replace("TRADING", "")


def test_a_label_can_only_separate_hosts_the_deployer_attests_are_independent():
    # same registrable domain but explicit, different labels: accepted (the deployer's attestation)
    out = build_rpc_sources(cfg(**ON), exec_rpc(execution_rpc_operator="one"),
                            {KEYS: "two=https://b.alpha.example/K"})
    assert [x.operator for x in out] == ["one", "two"]
    # the same label twice is the same operator
    with pytest.raises(ExecutionRpcConfigError, match="share the operator"):
        build_rpc_sources(cfg(**ON), exec_rpc(execution_rpc_operator="one"),
                          {KEYS: "one=https://b.example/K"})


def test_the_old_recovery_key_is_gone_and_the_new_one_is_validated():
    with pytest.raises(TypeError):
        TradingConfig(recovery_rpc_urls_env="TRADING_RECOVERY_RPC_URLS")
    with pytest.raises(ValueError):
        TradingConfig(corroboration_rpc_urls_env="lower")
    for bad in (5, 5000, True, "x"):
        with pytest.raises(ValueError):
            TradingConfig(corroboration_calls_per_pass=bad)
    with pytest.raises(ValueError):
        TradingConfig(execution_rpc_operator="Bad Label!")
    t = TradingConfig()
    assert (t.corroboration_rpc_urls_env, t.corroboration_calls_per_pass) == (
        "TRADING_CORROBORATION_RPC_URLS", 120) and t.finality_confirmations == 12


def test_finality_policy_defaults_to_the_finalized_tag_and_is_validated():
    t = TradingConfig()
    assert t.finality_require_finalized_tag is True and t.finality_confirmations == 12
    assert t.corroboration_max_head_skew_blocks == 64
    for bad in ("yes", 1, None):
        with pytest.raises(ValueError):
            TradingConfig(finality_require_finalized_tag=bad)
    for bad in (0, 10_001, True, "64"):
        with pytest.raises(ValueError):
            TradingConfig(corroboration_max_head_skew_blocks=bad)
