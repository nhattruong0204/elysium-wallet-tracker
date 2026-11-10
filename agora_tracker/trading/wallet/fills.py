"""Realized-fill accounting from on-chain evidence only (issue #94).

A fill is `verified` only when two *independent* on-chain measurements agree; otherwise it is
`unknown`, and nothing downstream (positions, PnL) is allowed to guess. Never used as evidence:
the quote, `min_out`, the router's return value from a simulation, or a wallet-reported number.

BUY  (native in, token out)
  native spent  = the mined transaction's `value` (exact: the router consumes all of msg.value),
                  cross-checked with the wrapped-native Deposit log credited to the router;
  tokens in     = net ERC20 Transfer logs to the wallet  ==  balanceOf delta (block N vs N-1).
SELL (token out, native in)
  tokens out    = net ERC20 Transfer logs from the wallet  ==  balanceOf delta;
  native in     = wrapped-native Withdrawal log of the router  ==  native balance delta + gas.
Balance deltas are only trusted when no OTHER transaction in that block touches the wallet
(checked from the block's transaction list); otherwise the fill is `unknown` (reason says why).
Fee-on-transfer tokens therefore need logs and balances to agree on what the wallet really
received; any disagreement (hidden tax, rebase) is `unknown`, never a guessed number.
Gas is reported separately (`gasUsed * effectiveGasPrice`) and is not part of the trade amounts.
A reverted swap moved nothing but gas: verdict `reverted`. Approvals produce no fill at all."""

from __future__ import annotations

import re
from typing import Any, Protocol

from ..ledger.legs import FillRecord
from ..paper import TradeRefused
from ..v2 import WRAPPED
from .requests import WalletRequestError, decode_swap, tx_mismatches

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEPOSIT_TOPIC = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
WITHDRAWAL_TOPIC = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
HEX_WORD = re.compile(r"^0x[0-9a-f]{64}$")
ADVISORY = frozenset({"gas_limit", "fee_cap"})


class FillSource(Protocol):
    def block_transactions(self, number: int) -> list[dict]: ...
    def native_balance(self, address: str, block: int) -> int: ...
    def token_balance(self, token: str, address: str, block: int) -> int: ...


class ReceiptError(Exception):
    """The receipt/transaction pair is internally inconsistent: do not account for it."""


class EvidenceUnavailable(Exception):
    """Balance evidence could not be read (yet). Retry later; do not record a guess."""


def _int(v: Any, what: str) -> int:
    if not isinstance(v, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", v):
        raise ReceiptError(f"malformed {what}")
    return int(v, 16)


def _addr(topic: str) -> str:
    return "0x" + topic[-40:]


def parse_logs(receipt: dict) -> list[dict]:
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise ReceiptError("receipt has no logs list")
    out = []
    for lg in logs:
        if not isinstance(lg, dict) or not isinstance(lg.get("topics"), list):
            raise ReceiptError("malformed log")
        out.append({"address": str(lg.get("address", "")).lower(),
                    "topics": [str(t).lower() for t in lg["topics"]],
                    "data": str(lg.get("data", "0x")).lower()})
    return out


def _word(data: str) -> int:
    if not re.fullmatch(r"0x[0-9a-f]{64}", data):
        raise ReceiptError("unexpected log data")
    return int(data, 16)


def net_transfers(logs: list[dict], token: str, wallet: str) -> tuple[int, int]:
    """(received, sent) by `wallet` for ERC20 `token` in this transaction."""
    got = sent = 0
    for lg in logs:
        t = lg["topics"]
        if lg["address"] != token or len(t) != 3 or t[0] != TRANSFER_TOPIC:
            continue                                  # ERC721 (4 topics) and other events ignored
        amount = _word(lg["data"])
        if _addr(t[2]) == wallet:
            got += amount
        if _addr(t[1]) == wallet:
            sent += amount
    return got, sent


def wrapped_sum(logs: list[dict], topic: str, party: str) -> int:
    total = 0
    for lg in logs:
        t = lg["topics"]
        if lg["address"] == WRAPPED and len(t) == 2 and t[0] == topic and _addr(t[1]) == party:
            total += _word(lg["data"])
    return total


def validate(leg: dict, tx: dict, receipt: dict, tx_hash: str) -> list[str]:
    """Structural checks tying the receipt to the transaction and to what was requested."""
    problems: list[str] = []
    wallet, to = leg["wallet"], leg["expected_to"]
    if str(receipt.get("transactionHash", "")).lower() != tx_hash:
        problems.append("receipt_hash")
    if str(receipt.get("from", "")).lower() != wallet:
        problems.append("receipt_from")
    if str(receipt.get("to", "")).lower() != to:
        problems.append("receipt_to")
    if receipt.get("contractAddress") not in (None, ""):
        problems.append("receipt_creates_contract")
    for k in ("blockHash", "blockNumber"):
        if receipt.get(k) != tx.get(k):
            problems.append(f"block_mismatch_{k}")
    if not HEX_WORD.fullmatch(str(receipt.get("blockHash", "")).lower()):
        problems.append("block_hash_malformed")
    if receipt.get("status") not in ("0x0", "0x1"):
        problems.append("status_malformed")
    exp = _Exp(leg)
    bad = [m for m in tx_mismatches(exp, tx, None) if m not in ADVISORY]
    problems += [f"tx_{m}" for m in bad]
    return problems


class _Exp:
    def __init__(self, leg: dict) -> None:
        self.sender, self.to = leg["wallet"], leg["expected_to"]
        self.value_wei, self.data = int(leg["expected_value_wei"]), leg["expected_data"]
        self.chain_id = leg["chain_id"]
        self.gas_limit = self.gas_price_wei = None


def gas_cost(tx: dict, receipt: dict) -> tuple[int, int]:
    used = _int(receipt.get("gasUsed"), "gasUsed")
    price = receipt.get("effectiveGasPrice", tx.get("gasPrice"))
    return used, used * _int(price, "gas price")


def _isolated(src: FillSource, block: int, wallet: str, tx_hash: str) -> bool:
    for t in src.block_transactions(block):
        if str(t.get("hash", "")).lower() == tx_hash:
            continue
        if wallet in (str(t.get("from", "")).lower(), str(t.get("to") or "").lower()):
            return False
    return True


def compute_fill(leg: dict, tx: dict, receipt: dict, tx_hash: str, src: FillSource, *,
                 strict: bool = True) -> FillRecord:
    """FillRecord for a SWAP leg (never call it for approvals).

    `strict`: unreadable balance evidence raises EvidenceUnavailable (the caller retries) instead of
    being recorded as `unknown`; a fill row is immutable once written, so a transient RPC error
    must not become a permanent verdict. The caller turns strict off after bounded retries."""
    value = int(leg["expected_value_wei"])
    side = "buy" if value > 0 else "sell"
    try:
        path = decode_swap(leg["expected_data"], side)["path"]
    except WalletRequestError as e:
        raise ReceiptError("leg payload is not a swap") from e
    token = path[-1] if side == "buy" else path[0]
    wallet, router = leg["wallet"], leg["expected_to"]
    used, gas = gas_cost(tx, receipt)

    def unknown(reason: str) -> FillRecord:
        return FillRecord("unknown", side, token, None, None, gas, None, reason[:190])

    if receipt["status"] == "0x0":
        return FillRecord("reverted", side, token, None, None, gas, "receipt status 0", None)

    block = _int(receipt["blockNumber"], "blockNumber")
    logs = parse_logs(receipt)
    got, sent = net_transfers(logs, token, wallet)
    try:
        if not _isolated(src, block, wallet, tx_hash):
            return unknown("other transactions touched the wallet in the same block")
        before = src.token_balance(token, wallet, block - 1)
        after = src.token_balance(token, wallet, block)
    except TradeRefused as exc:
        if strict:
            raise EvidenceUnavailable("balance evidence unavailable") from exc
        return unknown("balance evidence unavailable")

    if side == "buy":
        if _int(tx.get("value"), "value") != value:
            raise ReceiptError("transaction value differs from the request")
        if wrapped_sum(logs, DEPOSIT_TOPIC, router) != value:
            return unknown("wrapped-native deposit log does not match the amount sent")
        log_in = got - sent
        delta = after - before
        if log_in <= 0 or delta != log_in:
            return unknown("token transfer logs and balance delta disagree")
        return FillRecord("verified", side, token, value, delta, gas,
                          "transfer logs == token balance delta; native == tx value", None)

    log_out = sent - got
    delta_out = before - after
    if log_out <= 0 or delta_out != log_out:
        return unknown("token transfer logs and balance delta disagree")
    withdrawn = wrapped_sum(logs, WITHDRAWAL_TOPIC, router)
    try:
        native_delta = src.native_balance(wallet, block) - src.native_balance(wallet, block - 1) + gas
    except TradeRefused as exc:
        if strict:
            raise EvidenceUnavailable("native balance evidence unavailable") from exc
        return unknown("native balance evidence unavailable")
    if withdrawn <= 0 or native_delta != withdrawn:
        return unknown("native proceeds: withdrawal log and balance delta disagree")
    return FillRecord("verified", side, token, withdrawn, log_out, gas,
                      "transfer logs == token delta; withdrawal log == native delta + gas", None)
