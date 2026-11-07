import os
import sqlite3

import pytest
from test_paper_trading import FixtureProvider, bot, cq, flow, intent
from test_verified_paper import prepared

from agora_tracker.trading.journal import PaperJournal
from agora_tracker.trading.paper import TradeRefused


def test_rejected_amount_change_revokes_old_confirmation():
    f = flow(FixtureProvider())
    i = intent(f)
    f.review(1, 1, i.id)
    token = f.confirmation_token(1, 1, i.id)
    with pytest.raises(TradeRefused):
        f.select(1, 1, i.id, "1.1")
    with pytest.raises(TradeRefused):
        f.confirm(1, 1, i.id, token)


def test_review_return_does_not_alias_stored_unsigned_payload():
    f, i, _ = prepared()
    q = f.review(1, 1, i.id)
    q.transaction["to"] = "0x" + "ee" * 20
    assert f.reviewed(1, 1, i.id).transaction["to"] != q.transaction["to"]


def test_boolean_identity_cannot_pause_paper():
    b = bot()
    b.on_message(
        {"from": {"id": True}, "chat": {"id": True, "type": "private"}, "text": "/pause_trading"}
    )
    assert not b.paper_trades.paused


@pytest.mark.parametrize(
    "kind,chat", [("channel", -100123), ("unknown", -100123), ("supergroup", 1)]
)
def test_forged_initial_callback_context_is_refused(kind, chat):
    b = bot()
    b.on_callback(cq("tp|b|" + "0x" + "ab" * 20, kind=kind, chat=chat))
    assert not b.paper_trades._intents


def test_journal_rechecks_permissions_before_every_read(tmp_path):
    journal = PaperJournal(str(tmp_path / "private" / "paper.db"))
    os.chmod(journal.path, 0o644)
    with pytest.raises(TradeRefused):
        journal.recent(1)


def test_replaced_journal_symlink_is_refused_after_startup(tmp_path):
    journal = PaperJournal(str(tmp_path / "private" / "paper.db"))
    original = journal.path.with_suffix(".old")
    journal.path.rename(original)
    journal.path.symlink_to(original)
    with pytest.raises(TradeRefused):
        journal.recent(1)


def test_corrupt_journal_json_refuses_with_redacted_error(tmp_path):
    journal = PaperJournal(str(tmp_path / "private" / "paper.db"))
    with sqlite3.connect(journal.path) as c:
        c.execute(
            "INSERT INTO paper_orders VALUES (?,?,?,?)", ("x", 1, 1.0, "private malformed data")
        )
    with pytest.raises(TradeRefused, match="journal"):
        journal.recent(1)


def test_optimized_metadata_cache_never_caches_mutable_state():
    from test_verified_paper import E18, TOKEN, WALLET, RpcFixture

    from agora_tracker.trading.paper import PaperFlow
    from agora_tracker.trading.v2 import DIRECT_ROUTE, DirectV2Provider

    rpc = RpcFixture()
    provider = DirectV2Provider(rpc, now=lambda: 100.0)
    f = PaperFlow({1}, provider=provider, routes=frozenset({DIRECT_ROUTE}), now=lambda: 100.0)
    f.set_wallet(1, 1, WALLET)
    i = f.create(1, 1, TOKEN, "buy", 1, 1)
    f.select(1, 1, i.id, "0.1")
    a = f.review(1, 1, i.id)
    rpc.requests.clear()
    rpc.native = 8 * E18
    rpc.reserves = (1100 * E18, 1000 * E18)
    b = f.review(1, 1, i.id)
    assert b.balance_wei == 8 * E18 and a.amount_out_wei != b.amount_out_wei
    assert provider.metrics["metadata_cache_hit"] == 1
    assert len([x for x in rpc.requests if x[0] == "code_hash"]) == 3
    assert any(x[0] == "check_block" for x in rpc.requests)
    assert len([x for x in rpc.requests if x[0] == "view"]) == 3  # reserves, decimals, balance
    rpc.bad_hash = b.route.target
    with pytest.raises(TradeRefused, match="runtime"):
        f.review(1, 1, i.id)
    assert not provider._pairs


def test_reorg_recheck_refuses_even_with_warm_metadata():
    f, i, rpc = prepared()

    def reorg(*args):
        raise TradeRefused("Pinned block changed")

    rpc.check_block = reorg
    with pytest.raises(TradeRefused, match="Pinned block"):
        f.review(1, 1, i.id)
    assert not f._quotes


def test_oversized_or_malformed_response_is_bounded_and_closed():
    import time
    from unittest.mock import Mock

    from agora_tracker.trading.http import MAX_JSON_BYTES, bounded_json

    for chunks in [[b"x" * (MAX_JSON_BYTES + 1)], [b"private malformed data"]]:
        r = Mock(headers={})
        r.iter_content.return_value = iter(chunks)
        with pytest.raises(TradeRefused):
            bounded_json(r, deadline=time.monotonic() + 5)
        r.close.assert_called_once()


def test_expired_streaming_budget_refuses_json():
    import time
    from unittest.mock import Mock

    from agora_tracker.trading.http import bounded_json

    r = Mock(headers={})
    r.iter_content.return_value = iter([b"{}"])
    with pytest.raises(TradeRefused, match="budget"):
        bounded_json(r, deadline=time.monotonic() - 1)
    r.close.assert_called_once()


def test_canonical_block_hash_recheck_detects_number_pin_reorg(monkeypatch):
    from agora_tracker.trading.chain import ReadRpc

    rpc = ReadRpc("https://fixture.invalid")
    initial = {"hash": "0x" + "12" * 32, "number": "0xa", "timestamp": "0x64"}
    monkeypatch.setattr(rpc, "call", lambda *args: {**initial, "hash": "0x" + "34" * 32})
    with pytest.raises(TradeRefused, match="Pinned block"):
        rpc.check_block("0xa", initial)


@pytest.mark.parametrize(
    "field,value",
    [("created_at", float("nan")), ("expires_at", float("inf")), ("block_hash", "0x" + "zz" * 32)],
)
def test_malformed_quote_evidence_refuses(field, value):
    from dataclasses import replace

    f, i, _ = prepared()
    q = f.provider.quote(f.get(1, 1, i.id), 50)
    with pytest.raises(TradeRefused):
        f._validate(f.get(1, 1, i.id), replace(q, **{field: value}), 50)


def test_unicode_or_replayed_confirmation_refuses_without_crash():
    f = flow(FixtureProvider())
    i = intent(f)
    f.review(1, 1, i.id)
    for token in ["💥", None, True, "replayed"]:
        with pytest.raises(TradeRefused):
            f.confirm(1, 1, i.id, token)


def test_reverting_sell_with_existing_allowance_is_not_conditional():
    from test_verified_paper import RpcFixture

    rpc = RpcFixture(allowance=10**30)
    rpc.revert = True
    f, i, _ = prepared("sell", rpc=rpc)
    with pytest.raises(TradeRefused):
        f.review(1, 1, i.id)
    assert not f._quotes


def test_gas_and_wallet_state_are_not_cached_between_sell_reviews():
    from test_verified_paper import TOKEN, WALLET, RpcFixture

    from agora_tracker.trading.paper import PaperFlow
    from agora_tracker.trading.v2 import DIRECT_ROUTE, DirectV2Provider

    rpc = RpcFixture(allowance=10**30)
    provider = DirectV2Provider(rpc, now=lambda: 100.0)
    f = PaperFlow({1}, provider=provider, routes=frozenset({DIRECT_ROUTE}), now=lambda: 100.0)
    f.set_wallet(1, 1, WALLET)
    i = f.create(1, 1, TOKEN, "sell", 1, 1)
    f.select(1, 1, i.id, "1")
    q = f.review(1, 1, i.id)
    assert q.simulated is True
    rpc.allowance = 0
    q = f.review(1, 1, i.id)
    assert q.simulated is False and q.gas_wei is None
    assert provider.metrics["metadata_cache_hit"] == 1


def test_warm_metadata_cannot_skip_wrong_chain_verification():
    from test_verified_paper import TOKEN, WALLET, RpcFixture

    from agora_tracker.trading.paper import PaperFlow
    from agora_tracker.trading.v2 import DIRECT_ROUTE, DirectV2Provider

    rpc = RpcFixture()
    provider = DirectV2Provider(rpc, now=lambda: 100.0)
    f = PaperFlow({1}, provider=provider, routes=frozenset({DIRECT_ROUTE}), now=lambda: 100.0)
    f.set_wallet(1, 1, WALLET)
    i = f.create(1, 1, TOKEN, "buy", 1, 1)
    f.select(1, 1, i.id, "0.1")
    f.review(1, 1, i.id)

    def wrong_chain():
        raise TradeRefused("Paper RPC chain mismatch")

    rpc.snapshot = wrong_chain
    with pytest.raises(TradeRefused, match="chain mismatch"):
        f.review(1, 1, i.id)
    assert not f._quotes


def test_corrupted_cross_user_record_binding_is_not_displayed(tmp_path):
    import json

    journal = PaperJournal(str(tmp_path / "p" / "paper.db"))
    with sqlite3.connect(journal.path) as c:
        c.execute(
            "INSERT INTO paper_orders VALUES (?,?,?,?)",
            (
                "x",
                1,
                1.0,
                json.dumps(
                    {
                        "mode": "PAPER",
                        "status": "hypothetical_only",
                        "intent": {"id": "x", "user_id": 2},
                        "quote": {"intent_id": "x"},
                    }
                ),
            ),
        )
    with pytest.raises(TradeRefused, match="journal"):
        journal.recent(1)


def test_rejected_wallet_change_revokes_old_confirmation():
    f, i, _ = prepared()
    f.review(1, 1, i.id)
    token = f.confirmation_token(1, 1, i.id)
    with pytest.raises(TradeRefused):
        f.set_wallet(1, 1, "invalid")
    with pytest.raises(TradeRefused):
        f.confirm(1, 1, i.id, token)
