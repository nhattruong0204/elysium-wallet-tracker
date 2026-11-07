import json

import pytest

from scripts.trade_rpc_bench import ReadOnlyRpc, benchmark, percentile, validate_workloads


def test_write_methods_cannot_be_dispatched():
    rpc = ReadOnlyRpc('https://example.test', pause=0)
    for method in ['eth_sendRawTransaction', 'eth_sendTransaction', 'personal_unlockAccount']:
        with pytest.raises(ValueError, match='forbidden'):
            rpc.call(method, [])
    assert rpc.count == 0


def test_workload_rejects_write_and_malformed_targets():
    with pytest.raises(ValueError):
        validate_workloads([{'method': 'eth_sendRawTransaction'}])
    with pytest.raises(ValueError):
        validate_workloads([{'name': 'gas', 'method': 'eth_estimateGas', 'params': [{}]}])


def test_wrong_chain_stops_endpoint_and_never_leaks_url(monkeypatch):
    calls = []
    def fake(self, method, params):
        calls.append(method)
        self.count += 1
        return '0x53b', 1., None  # unrelated chain 1339
    monkeypatch.setattr(ReadOnlyRpc, 'call', fake)
    result = benchmark(['https://user:secret@example.test/private?api_key=secret'], 2, [], 'test')
    assert calls == ['eth_chainId']
    assert result['endpoints'][0]['eligible'] is False
    assert 'secret' not in json.dumps(result)
    assert 'private' not in json.dumps(result)


def test_same_workloads_budget_and_statistics(monkeypatch):
    calls = []
    def fake(self, method, params):
        self.count += 1
        calls.append((self.url, method, params))
        return {'eth_chainId': hex(99901), 'eth_getBlockByNumber': {'number':'0x64', 'timestamp':'0x1'},
                'eth_getCode': '0x1234', 'eth_estimateGas': '0x5208'}[method], 10., None
    monkeypatch.setattr(ReadOnlyRpc, 'call', fake)
    workloads = [{'name':'reviewed_gas', 'method':'eth_estimateGas',
                  'params':[{'to':'0x'+'ab'*20}]}]
    result = benchmark(['https://a.test', 'https://b.test'], 3, workloads, 'fixture')
    assert len(calls) == 20  # chain + 3 samples * 3 workloads per endpoint
    assert [c[1:] for c in calls[:10]] == [c[1:] for c in calls[10:]]
    assert result['endpoints'][0]['methods']['reviewed_gas']['p95_ms'] == 10
    assert result['endpoints'][0]['freshness_qualified'] is False
    assert percentile([], .95) is None
    assert percentile([1, 2, 3], .95) == 3
