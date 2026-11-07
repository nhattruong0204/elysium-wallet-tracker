#!/usr/bin/env python3
"""Bounded public Agora v1 indicative quotes; never requests calldata or signs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone

import requests

BASE = 'https://api.agoradex.app/api/v1'


def probe(tokens: list[str], amounts: list[str]) -> dict:
    if not 1 <= len(tokens) <= 3 or not 1 <= len(amounts) <= 3:
        raise ValueError('At most three tokens and three amounts')
    if any(not re.fullmatch(r'0x[0-9a-fA-F]{40}', t) for t in tokens):
        raise ValueError('Token address required')
    if any(not re.fullmatch(r'0(?:\.[0-9]{1,18})?|1(?:\.0+)?', a) or float(a) <= 0 for a in amounts):
        raise ValueError('Probe sizes must be positive and at most 1 HYPE')
    rows = []
    with requests.Session() as s:
        r = s.get(BASE + '/openapi.json', timeout=10)
        r.raise_for_status()
        spec_hash = hashlib.sha256(r.content).hexdigest()
        for token in tokens:
            for amount in amounts:
                time.sleep(2.1)  # documented unauthenticated budget: 30/minute/IP
                started = time.monotonic()
                row: dict = {'token': token, 'side': 'buy', 'amount_hype': amount}
                try:
                    r = s.get(BASE + '/quote', params={'token': token, 'side': 'buy',
                        'amount': amount, 'slippageBps': 50}, timeout=10)
                    row['http_status'] = r.status_code
                    if r.status_code == 429:
                        rows.append(row)
                        return {'quotes': rows, 'stopped': 'rate_limited'}
                    r.raise_for_status()
                    q = r.json()
                    # Record only public market observations. No calldata, wallets or errors.
                    row.update({k: q.get(k) for k in ('amountIn', 'amountOut', 'minAmountOut',
                        'venue', 'priceImpact', 'feeBps', 'venues')})
                    row['transaction_returned'] = q.get('transaction') is not None
                except (requests.RequestException, ValueError):
                    row['error'] = 'quote_unavailable'
                row['latency_ms'] = round((time.monotonic() - started) * 1000, 2)
                row['observed_at'] = datetime.now(timezone.utc).isoformat()
                rows.append(row)
    return {'source': BASE, 'openapi_sha256': spec_hash, 'location': 'WSL2 local network',
            'quotes': rows, 'limitations': ['Indicative provider data, not independently verified swaps.',
            'No recipient: wallet-specific fee eligibility unknown.',
            'venues are provider comparisons, not independent direct-router quotes.',
            'No calldata requested, no simulation, balances, signing or submission.']}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--token', action='append', required=True)
    p.add_argument('--amount', action='append', required=True)
    a = p.parse_args()
    print(json.dumps(probe(a.token, a.amount), indent=2))


if __name__ == '__main__':
    main()
