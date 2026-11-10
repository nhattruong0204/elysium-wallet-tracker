"""Canonical server-generated ERC-4361 messages; EOA recovery only, no signer."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from eth_hash.auto import keccak
from eth_keys import keys
from eth_keys.constants import SECPK1_N
from eth_keys.exceptions import BadSignature, ValidationError
from eth_utils import to_checksum_address

from ..chain import ReadRpc, address
from .telegram import AuthRefused


def timestamp(value: int) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def message(
    domain: str, uri: str, wallet: str, nonce: str, issued: int, expires: int, binding: str
) -> str:
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{to_checksum_address(wallet)}\n\n"
        "Verify ownership for this Telegram PAPER preview. No transactions or approvals.\n\n"
        f"URI: {uri}\nVersion: 1\nChain ID: 99901\nNonce: {nonce}\n"
        f"Issued At: {timestamp(issued)}\nExpiration Time: {timestamp(expires)}\n"
        f"Not Before: {timestamp(issued)}\nRequest ID: {binding}"
    )


def message_hash(text: str) -> bytes:
    raw = text.encode("utf-8")
    return keccak(b"\x19Ethereum Signed Message:\n" + str(len(raw)).encode() + raw)


def recover(text: str, signature: str) -> str:
    try:
        if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
            raise AuthRefused("Ownership proof refused")
        raw = bytes.fromhex(signature[2:])
        r, s, v = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:64], "big"), raw[64]
        # Deliberately only canonical 65-byte EIP-191 EOA signatures. No EIP-6492,
        # compact signatures, personal arbitrary messages or delegated code support.
        if v not in (27, 28) or not 0 < r < SECPK1_N or not 0 < s <= SECPK1_N // 2:
            raise AuthRefused("Ownership proof refused")
        return (
            keys.Signature(vrs=(v - 27, r, s))
            .recover_public_key_from_msg_hash(message_hash(text))
            .to_address()
            .lower()
        )
    except (BadSignature, ValidationError, ValueError, TypeError):
        raise AuthRefused("Ownership proof refused") from None


def require_eoa(rpc: ReadRpc, wallet: str) -> None:
    with rpc.budget(8):
        block, header = rpc.snapshot()  # verifies fresh head and chain 99901
        code = rpc.call("eth_getCode", [address(wallet), block])
        if code != "0x":
            raise AuthRefused(
                "Contract and delegated wallets are unsupported; ERC-1271 is not enabled"
            )
        rpc.check_block(block, header)
