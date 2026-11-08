"""Fill accounting, positions and labels from on-chain evidence only (#94). No database, no
network: every number is built independently of the code under test."""

from __future__ import annotations

import pytest
from keccak_ref import keccak256
from walletkit import (
    DEPOSIT,
    E18,
    TOKEN,
    TRANSFER,
    WALLET,
    WITHDRAWAL,
    ReceiptChain,
    block_hash,
    deposit_log,
    make_plan,
    transfer_log,
    withdrawal_log,
)

from agora_tracker.config import TradingConfig
from agora_tracker.trading.v2 import ROUTER
from agora_tracker.trading.wallet import fills as F
from agora_tracker.trading.wallet.status import LABELS, order_status, positions, render_text

PAIR = "0x" + "03" * 20
FEE = "0x" + "ee" * 20
H = "0x" + "cd" * 32
AMT = E18 // 2
GAS = 150_000 * 10**9


def leg_of(plan) -> dict:
    t = plan.transaction
    return {"id": 1, "kind": "swap", "wallet": t.sender, "expected_to": t.target,
            "expected_value_wei": str(t.value_wei), "expected_data": t.calldata, "chain_id": 99901,
            "gas_limit": "210000"}


def buy_world(*, tokens=3 * E18, extra_logs=(), balance_gain=None, deposit=AMT):
    plan = make_plan("buy")
    leg = leg_of(plan)
    ch = ReceiptChain()
    logs = [deposit_log(ROUTER, deposit), transfer_log(TOKEN, PAIR, WALLET, tokens), *extra_logs]
    tx, rc = ch.mine(leg, H, 1000, logs=logs)
    ch.buy_balances(1000, tokens if balance_gain is None else balance_gain)
    return leg, tx, rc, ch


def sell_world(*, sold=AMT, proceeds=8 * E18 // 10, native_gain=None, logs_proceeds=None):
    plan = make_plan("sell", amount_in=AMT, out=8 * E18 // 10)
    leg = leg_of(plan)
    ch = ReceiptChain()
    logs = [transfer_log(TOKEN, WALLET, PAIR, sold),
            withdrawal_log(ROUTER, proceeds if logs_proceeds is None else logs_proceeds)]
    tx, rc = ch.mine(leg, H, 1000, logs=logs)
    ch.tok[(TOKEN, WALLET, 999)] = 5 * E18
    ch.tok[(TOKEN, WALLET, 1000)] = 5 * E18 - sold
    ch.nat[(WALLET, 999)] = 10 * E18
    ch.nat[(WALLET, 1000)] = 10 * E18 + (proceeds if native_gain is None else native_gain) - GAS
    return leg, tx, rc, ch


# ------------------------------------------------------------------ constants

def test_event_topics_and_selectors_are_derived_not_remembered():
    assert keccak256(b"") .hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert "0x" + keccak256(b"Transfer(address,address,uint256)").hex() == TRANSFER == F.TRANSFER_TOPIC
    assert "0x" + keccak256(b"Deposit(address,uint256)").hex() == DEPOSIT == F.DEPOSIT_TOPIC
    assert "0x" + keccak256(b"Withdrawal(address,uint256)").hex() == WITHDRAWAL == F.WITHDRAWAL_TOPIC


# ------------------------------------------------------------------ BUY

def test_buy_is_verified_only_when_logs_and_balances_agree():
    leg, tx, rc, ch = buy_world()
    f = F.compute_fill(leg, tx, rc, H, ch)
    assert (f.verdict, f.side, f.token) == ("verified", "buy", TOKEN)
    assert (f.native_wei, f.token_wei, f.gas_cost_wei) == (AMT, 3 * E18, GAS)


def test_fee_on_transfer_token_uses_the_net_amount_both_sources_agree_on():
    tax = 3 * E18 // 20
    leg, tx, rc, ch = buy_world(extra_logs=[transfer_log(TOKEN, WALLET, FEE, tax)],
                                balance_gain=3 * E18 - tax)
    f = F.compute_fill(leg, tx, rc, H, ch)
    assert f.verdict == "verified" and f.token_wei == 3 * E18 - tax


def test_a_hidden_tax_that_logs_do_not_show_is_unknown_not_guessed():
    leg, tx, rc, ch = buy_world(balance_gain=3 * E18 - 10**15)
    f = F.compute_fill(leg, tx, rc, H, ch)
    assert f.verdict == "unknown" and f.token_wei is None and "disagree" in f.reason


def test_a_deposit_log_that_does_not_match_the_value_sent_is_unknown():
    leg, tx, rc, ch = buy_world(deposit=AMT - 1)
    assert F.compute_fill(leg, tx, rc, H, ch).verdict == "unknown"


def test_another_transaction_touching_the_wallet_in_the_block_makes_balances_unusable():
    leg, tx, rc, ch = buy_world()
    ch.blocks[1000].append({"hash": "0x" + "99" * 32, "from": "0x" + "55" * 20, "to": WALLET})
    f = F.compute_fill(leg, tx, rc, H, ch)
    assert f.verdict == "unknown" and "same block" in f.reason


def test_unrelated_transactions_in_the_block_do_not_matter():
    leg, tx, rc, ch = buy_world()
    ch.blocks[1000].append({"hash": "0x" + "99" * 32, "from": "0x" + "55" * 20, "to": "0x" + "66" * 20})
    assert F.compute_fill(leg, tx, rc, H, ch).verdict == "verified"


def test_erc721_transfers_and_other_tokens_are_ignored():
    nft = {"address": TOKEN, "topics": [TRANSFER, "0x" + PAIR[2:].rjust(64, "0"),
                                        "0x" + WALLET[2:].rjust(64, "0"), "0x" + "0" * 63 + "1"],
           "data": "0x"}
    other = transfer_log("0x" + "44" * 20, PAIR, WALLET, 7 * E18)
    leg, tx, rc, ch = buy_world(extra_logs=[nft, other])
    assert F.compute_fill(leg, tx, rc, H, ch).token_wei == 3 * E18


def test_unreadable_balances_raise_in_strict_mode_and_become_unknown_when_not():
    leg, tx, rc, ch = buy_world()
    del ch.tok[(TOKEN, WALLET, 999)]
    with pytest.raises(F.EvidenceUnavailable):
        F.compute_fill(leg, tx, rc, H, ch)
    f = F.compute_fill(leg, tx, rc, H, ch, strict=False)
    assert f.verdict == "unknown" and "unavailable" in f.reason


def test_reverted_swap_moved_nothing_but_gas():
    leg, tx, rc, ch = buy_world()
    rc["status"] = "0x0"
    rc["logs"] = []
    f = F.compute_fill(leg, tx, rc, H, ch)
    assert (f.verdict, f.native_wei, f.token_wei, f.gas_cost_wei) == ("reverted", None, None, GAS)


# ------------------------------------------------------------------ SELL

def test_sell_is_verified_from_transfer_withdrawal_and_both_balance_deltas():
    leg, tx, rc, ch = sell_world()
    f = F.compute_fill(leg, tx, rc, H, ch)
    assert (f.verdict, f.side) == ("verified", "sell")
    assert (f.token_wei, f.native_wei) == (AMT, 8 * E18 // 10)


def test_sell_proceeds_that_the_wallet_balance_does_not_confirm_are_unknown():
    leg, tx, rc, ch = sell_world(native_gain=8 * E18 // 10 - 10**12)
    assert F.compute_fill(leg, tx, rc, H, ch).verdict == "unknown"


def test_sell_without_a_withdrawal_log_is_unknown():
    leg, tx, rc, ch = sell_world(logs_proceeds=0)
    assert F.compute_fill(leg, tx, rc, H, ch).verdict == "unknown"


def test_sell_token_delta_must_match_the_transfer_logs():
    leg, tx, rc, ch = sell_world()
    ch.tok[(TOKEN, WALLET, 1000)] += 1
    assert F.compute_fill(leg, tx, rc, H, ch).verdict == "unknown"


# ------------------------------------------------------------------ validation

def test_a_perfect_pair_validates():
    leg, tx, rc, _ = buy_world()
    assert F.validate(leg, tx, rc, H) == []


@pytest.mark.parametrize("tx_change", [{"input": "0x00"}, {"to": "0x" + "77" * 20},
                                       {"value": "0x1"}, {"from": "0x" + "77" * 20}])
def test_a_transaction_that_is_not_the_reviewed_request_is_rejected(tx_change):
    leg, tx, rc, _ = buy_world()
    tx.update(tx_change)
    assert F.validate(leg, tx, rc, H)


@pytest.mark.parametrize("rc_change", [
    {"from": "0x" + "77" * 20}, {"to": "0x" + "77" * 20}, {"transactionHash": "0x" + "12" * 32},
    {"contractAddress": "0x" + "88" * 20}, {"status": "0x2"}, {"blockHash": block_hash(1000, 5)},
    {"blockNumber": "0x3e9"}])
def test_a_receipt_that_does_not_match_is_rejected(rc_change):
    leg, tx, rc, _ = buy_world()
    rc.update(rc_change)
    assert F.validate(leg, tx, rc, H)


def test_wallet_chosen_gas_differs_without_invalidating_the_fill():
    leg, tx, rc, _ = buy_world()
    tx["gas"], tx["gasPrice"] = hex(500_000), hex(7 * 10**9)
    assert F.validate(leg, tx, rc, H) == []


# ------------------------------------------------------------------ positions

def fill(i, side, native, tokens, *, verdict="verified", status="final", block=None,
         token=TOKEN, wallet=WALLET):
    return {"id": i, "wallet": wallet, "token": token, "side": side, "verdict": verdict,
            "status": status, "block_number": block or 1000 + i, "gas_cost_wei": str(GAS), "reason": None,
            "native_wei": None if verdict != "verified" else str(native),
            "token_wei": None if verdict != "verified" else str(tokens)}


def test_average_cost_realized_pnl_is_exact():
    p, = positions([fill(1, "buy", 2 * E18, 10), fill(2, "buy", 4 * E18, 10),
                    fill(3, "sell", 5 * E18, 10)])
    assert p["quantity_wei"] == "10" and p["cost_basis_wei"] == str(3 * E18)
    assert p["realized_pnl_wei"] == str(2 * E18) and p["complete"] is True


def test_an_unknown_fill_makes_quantity_and_pnl_unknown_never_zero():
    p, = positions([fill(1, "buy", E18, 10), fill(2, "buy", 0, 0, verdict="unknown")])
    assert p["quantity_wei"] is None and p["realized_pnl_wei"] is None and p["complete"] is False
    assert "unknown" in p["note"]


def test_selling_more_than_the_verified_basis_is_unknown():
    p, = positions([fill(1, "buy", E18, 10), fill(2, "sell", E18, 11)])
    assert p["quantity_wei"] is None and p["realized_pnl_wei"] is None


def test_a_sell_with_no_verified_buy_has_no_basis():
    p, = positions([fill(1, "sell", E18, 5)])
    assert p["realized_pnl_wei"] is None


def test_rolled_back_and_reverted_fills_are_excluded():
    assert positions([fill(1, "buy", E18, 10, status="rolled_back"),
                      fill(2, "buy", E18, 10, verdict="reverted")]) == []


def test_provisional_fills_are_flagged():
    p, = positions([fill(1, "buy", E18, 10, status="provisional")])
    assert p["provisional"] is True and p["quantity_wei"] == "10"


def test_positions_are_per_wallet_and_token():
    ps = positions([fill(1, "buy", E18, 10), fill(2, "buy", E18, 5, token="0x" + "22" * 20),
                    fill(3, "buy", E18, 7, wallet="0x" + "c3" * 20)])
    assert len(ps) == 3


# ------------------------------------------------------------------ labels

ORDER = {"id": "11111111-2222-3333-4444-555555555555"}


def leg(kind="swap", state="hash_reported", **kw):
    base = {"kind": kind, "state": state, "tx_hash": H, "final_tx_hash": None, "block_number": None,
            "receipt_status": None, "gas_cost_wei": None, "finalized_at": None}
    return {**base, **kw}


def test_a_reported_hash_is_never_called_a_fill():
    st = order_status({**ORDER, "state": "submitted"}, [leg()], [])
    assert "not filled" in st["label"] and st["fill"] is None and st["provisional"] is False


def test_approval_confirmation_says_refresh_the_quote_and_not_that_a_trade_happened():
    st = order_status({**ORDER, "state": "requires_approval"}, [leg("approve", "included")], [])
    assert "refresh quote required" in st["label"] and "nothing was bought or sold" in st["detail"]


def test_approval_pending_is_not_confirmed():
    st = order_status({**ORDER, "state": "requires_approval"}, [leg("approve", "hash_reported")], [])
    assert "waiting for the chain" in st["label"]


def test_reverted_says_no_tokens_moved_and_shows_gas():
    f = fill(1, "buy", 0, 0, verdict="reverted", status="final")
    st = order_status({**ORDER, "state": "reverted"}, [leg(state="reverted")], [f])
    assert "no tokens moved" in st["label"] and st["gas_cost_wei"] == str(GAS) and st["fill"] is None


def test_unknown_fill_explains_why_there_is_no_number():
    f = fill(1, "buy", 0, 0, verdict="unknown", status="provisional")
    st = order_status({**ORDER, "state": "included"}, [leg(state="included")], [f])
    assert st["fill"]["verdict"] == "unknown" and "could not be verified" in st["detail"]


def test_rolled_back_fills_are_not_shown():
    f = fill(1, "buy", E18, 5, status="rolled_back")
    st = order_status({**ORDER, "state": "orphaned"}, [leg(state="orphaned")], [f])
    assert st["fill"] is None and st["gas_cost_wei"] is None


def test_every_ledger_state_has_a_label():
    from agora_tracker.trading.ledger import OrderState
    assert {s.value for s in OrderState} <= set(LABELS)


def test_rendered_text_escapes_everything_dynamic():
    evil = "<script>alert(1)</script>"
    items = [{"order_id": "abcdefgh1234", "label": evil, "detail": evil, "fill": None,
              "gas_cost_wei": None, "legs": [{"kind": "swap", "state": evil, "final": False,
                                              "tx_hash": H, "tx_url": "https://x/\"><i>"}]}]
    out = render_text(items, [{"token": evil, "quantity_wei": None, "realized_pnl_wei": None,
                               "provisional": False}], __import__("html").escape)
    assert "<script>" not in out and "<i>" not in out


# ------------------------------------------------------------------ config

@pytest.mark.parametrize("bad", [0, 257, True, 1.5, "12", None])
def test_finality_confirmations_is_validated(bad):
    with pytest.raises(ValueError):
        TradingConfig(finality_confirmations=bad)


def test_finality_default_is_documented_and_conservative():
    assert TradingConfig().finality_confirmations == 12


# ------------------------------------------------------------------ browser/server constants

def test_browser_constants_match_the_server_constants():
    """wallet.js re-checks the target/chain independently; a drift would silently weaken or break it."""
    import re
    from pathlib import Path

    from agora_tracker.trading.paper import CHAIN
    js = (Path(F.__file__).parent / "static" / "wallet.js").read_text()
    assert re.search(rf"const ROUTER = '{ROUTER}'", js)
    assert re.search(rf"const CHAIN_HEX = '{hex(CHAIN)}'", js)
