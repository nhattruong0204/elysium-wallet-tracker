#!/usr/bin/env python3
"""Bounded read-only execution research; never signs or submits a transaction.

Run with the SAME workload file for every endpoint. Workloads may contain only eth_call
and eth_estimateGas; no arbitrary RPC method dispatch is permitted. URLs/exception text
are never included in results. Each endpoint has its own persistent HTTP connection pool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

CHAIN = 99901
PUBLIC_HOSTS = frozenset({'rpc.thekleos.pro', 'rpc.thekleos.xyz', 'rpc-live.boxa.club',
                          'rpc-live.elyspump.com', 'rpc.agoradex.app', 'rpc.eleos.fun'})
ROUTER = '0xaf8dec958df2bd664aa661716a8de5fe0417a295'
READS = frozenset({'eth_chainId', 'eth_blockNumber', 'eth_getBlockByNumber',
                  'eth_getCode', 'eth_call', 'eth_estimateGas'})


def percentile(values: list[float], q: float) -> float | None:
    return round(sorted(values)[max(0, math.ceil(q * len(values)) - 1)], 2) if values else None


def validate_workloads(workloads: list[dict]) -> None:
    if len(workloads) > 4:
        raise ValueError('at most four explicitly reviewed workloads')
    for w in workloads:
        if w.get('method') not in ('eth_call', 'eth_estimateGas'):
            raise ValueError('workloads must be read-only calls or gas estimates')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', w.get('name', '')):
            raise ValueError('workload needs a non-sensitive name')
        p = w.get('params')
        if not isinstance(p, list) or not p or not isinstance(p[0], dict):
            raise ValueError('workload must include the actual unsigned transaction object')
        if not re.fullmatch(r'0x[0-9a-fA-F]{40}', p[0].get('to', '')):
            raise ValueError('workload requires a target address')


class ReadOnlyRpc:
    def __init__(self, url: str, timeout: float = 5, pause: float = 0.5):
        self.url, self.timeout, self.pause = url, timeout, pause
        self.session = requests.Session()
        self.last = 0.0
        self.count = 0

    def call(self, method: str, params: list) -> tuple[object, float, str | None]:
        if method not in READS:
            raise ValueError('write RPC methods forbidden')
        time.sleep(max(0, self.pause - (time.monotonic() - self.last)))
        started = time.monotonic()
        self.count += 1
        try:
            response = self.session.post(self.url, json={
                'jsonrpc': '2.0', 'id': self.count, 'method': method, 'params': params},
                timeout=self.timeout)
            if response.status_code == 429:
                return None, (time.monotonic() - started) * 1000, 'rate_limited'
            response.raise_for_status()
            data = response.json()
            if data.get('id') != self.count or data.get('jsonrpc') != '2.0':
                return None, (time.monotonic() - started) * 1000, 'invalid_rpc_response'
            if 'error' in data:
                return None, (time.monotonic() - started) * 1000, 'rpc_error'
            if 'result' not in data:
                return None, (time.monotonic() - started) * 1000, 'missing_result'
            return data['result'], (time.monotonic() - started) * 1000, None
        except requests.Timeout:
            return None, (time.monotonic() - started) * 1000, 'timeout'
        except (requests.RequestException, ValueError):
            return None, (time.monotonic() - started) * 1000, 'transport_or_json_error'
        finally:
            self.last = time.monotonic()


def benchmark(endpoints: list[str], samples: int, workloads: list[dict], location: str) -> dict:
    validate_workloads(workloads)
    if not 1 <= samples <= 20 or not 1 <= len(endpoints) <= 8:
        raise ValueError('bounded to 1-20 samples and 1-8 endpoints')
    rows = []
    for index, url in enumerate(endpoints):
        if urlsplit(url).scheme != 'https':
            raise ValueError('HTTPS endpoints required')
        rpc = ReadOnlyRpc(url)
        row = {'endpoint': f'candidate_{index + 1}', 'host': (urlsplit(url).hostname if urlsplit(url).hostname in PUBLIC_HOSTS
                        else 'redacted_custom_endpoint'),
               'chain_id': None, 'eligible': False, 'head': None, 'methods': {}}
        chain, _, err = rpc.call('eth_chainId', [])
        try:
            row['chain_id'] = int(str(chain), 16) if not err else None
        except ValueError:
            pass
        if row['chain_id'] != CHAIN:
            row['rejected'] = err or 'wrong_chain_or_invalid_chain'
            row['requests'] = rpc.count
            rows.append(row)
            rpc.session.close()
            continue
        row['eligible'] = True
        mix = [{'name': 'latest_block', 'method': 'eth_getBlockByNumber', 'params': ['latest', False]},
               {'name': 'router_code', 'method': 'eth_getCode', 'params': [ROUTER, 'latest']}] + workloads
        for w in mix:
            times, errors = [], {}
            for _ in range(samples):
                result, ms, error = rpc.call(w['method'], w['params'])
                if error:
                    errors[error] = errors.get(error, 0) + 1
                    if error == 'rate_limited':
                        break  # no retry storm; stop this endpoint below
                else:
                    times.append(ms)
                    if w['name'] == 'latest_block':
                        try:
                            row['head'] = int(result['number'], 16)
                            row['head_timestamp'] = int(result['timestamp'], 16)
                            row['observed_at'] = int(time.time())
                        except (KeyError, TypeError, ValueError):
                            errors['invalid_block'] = errors.get('invalid_block', 0) + 1
                            times.pop()
                    elif w['name'] == 'router_code':
                        row['router_code_present'] = isinstance(result, str) and len(result) > 2
                        if row['router_code_present']:
                            row['router_code_sha256'] = hashlib.sha256(str(result).encode()).hexdigest()

            row['methods'][w['name']] = {'method': w['method'], 'successes': len(times),
                'errors': errors, 'p50_ms': percentile(times, .5),
                'p95_ms': percentile(times, .95), 'p99_ms': percentile(times, .99)}
            if errors.get('rate_limited'):
                break
        row['requests'] = rpc.count
        rows.append(row)
        rpc.session.close()
    best = max((r['head'] for r in rows if r['head'] is not None), default=None)
    for row in rows:
        row['sampled_head_lag'] = best - row['head'] if best is not None and row['head'] is not None else None
        row['head_age_s_at_observation'] = (row['observed_at'] - row['head_timestamp']
            if 'observed_at' in row else None)
        row['freshness_qualified'] = bool(row['eligible'] and row['head'] is not None
            and 0 <= row['head_age_s_at_observation'] <= 60 and row['sampled_head_lag'] <= 50)
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'location': location,
            'chain_id': CHAIN, 'samples_per_method': samples, 'router': ROUTER,
            'phase': 'pre-public-mainnet' if datetime.now(timezone.utc).date().isoformat() < '2026-11-09' else 'revalidate-post-launch',
            'endpoints': rows, 'workloads': [w['name'] for w in workloads],
            'limitations': ['Sequential head observations; sampled lag includes timing skew.',
                'Small samples: p95/p99 are descriptive, not reliable tail estimates.',
                'Router bytecode presence does not verify ABI, ownership or execution safety.',
                'No signing, send acknowledgment, inclusion or settlement measurements.',
                'Quote/net-output comparison unrun unless explicit verified workloads supplied.']}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', help='read configured chain.rpc_urls; never prints credentials')
    ap.add_argument('--rpc', action='append', default=[])
    ap.add_argument('--samples', type=int, default=3)
    ap.add_argument('--workloads', help='reviewed read-only JSON workload list')
    ap.add_argument('--location', default='unspecified')
    args = ap.parse_args()
    endpoints = list(args.rpc)
    if args.config:
        import yaml
        with open(args.config) as f:
            endpoints = (yaml.safe_load(f).get('chain') or {}).get('rpc_urls', []) + endpoints
    endpoints = list(dict.fromkeys(endpoints))
    with open(args.workloads) if args.workloads else open('/dev/null') as f:
        workloads = json.load(f) if args.workloads else []
    print(json.dumps(benchmark(endpoints, args.samples, workloads, args.location), indent=2))


if __name__ == '__main__':
    main()
