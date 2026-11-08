"""Noncustodial, wallet-managed approval and swap requests (Stage C2, issue #92).

The server builds, binds and verifies requests; the user's own wallet signs and broadcasts them
through EIP-1193 `eth_sendTransaction`. There is no key, signer, signing endpoint or server-side
broadcast anywhere in this package. See docs/TRADING_WALLET_APPROVALS.md.
"""
