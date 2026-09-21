import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from auto_jev import storage
from auto_jev.server import create_app
from auto_jev.storage import RunStore, StoreError, TraceRevisionError


@pytest.fixture
def archive(tmp_path):
    store = RunStore(tmp_path / 'runs')
    run = store.create_run('paging', {})
    payload = {'episode_id': 'BTC-validation', 'asset': 'BTC-USD', 'score': .01,
               'market_bars': [{'timestamp': i, 'open': 1, 'high': 2, 'low': 1, 'close': 2} for i in range(3)],
               'trades': [], 'equity_curve': [], 'traces': []}
    for i in range(3):
        payload['traces'].append({'timestamp': i, 'target': i / 2, 'trade_index': None,
            'obs': {'headline': '市場 🚀 quote " slash \\ newline\n'},
            'memory_before': {}, 'finalization': {'output': {'output': i / 2}},
            'trace': [{'id': 'signal', 'kind': 'jev', 'input_context': {'obs': {'text': '全文' * 1000}},
                       'output': {'bull': {'type': 'noul', 'noul': .7}}}]})
    return store, run, 'candidate123', 'BTC-validation', payload


def test_unicode_byte_ranges_are_lossless_and_summary_has_no_context(archive, monkeypatch):
    store, run, candidate, episode, payload = archive
    store.save_trace(run, 'validation', candidate, episode, payload)
    assert store.load_trace(run, 'validation', candidate, episode) == payload
    raw_path = store._trace_path(run, 'validation', candidate, episode)
    original_load = storage.json.load

    def forbid_full_parse(stream, *args, **kwargs):
        assert str(getattr(stream, 'name', '')) != str(raw_path), 'paged reads must not parse the full trace'
        return original_load(stream, *args, **kwargs)

    monkeypatch.setattr(storage.json, 'load', forbid_full_parse)
    summary = store.load_trace_summary(run, 'validation', candidate, episode)
    assert summary['trace_count'] == 3
    assert summary['market_bars'] == payload['market_bars']
    assert 'input_context' not in json.dumps(summary)
    assert 'obs' not in summary['traces'][0]
    for i in range(3):
        result = store.load_trace_decision(run, 'validation', candidate, episode, i, summary['trace_revision'])
        assert result['decision'] == payload['traces'][i]


def test_revision_change_rejects_a_stale_selection(archive):
    store, run, candidate, episode, payload = archive
    store.save_trace(run, 'validation', candidate, episode, payload)
    revision = store.load_trace_summary(run, 'validation', candidate, episode)['trace_revision']
    changed = copy.deepcopy(payload)
    changed['traces'][0]['target'] = .42
    store.save_trace(run, 'validation', candidate, episode, changed)
    with pytest.raises(TraceRevisionError):
        store.load_trace_decision(run, 'validation', candidate, episode, 0, revision)
    path = store._trace_path(run, 'validation', candidate, episode)
    path.write_text(json.dumps(payload))
    with pytest.raises(TraceRevisionError):
        store.load_trace_summary(run, 'validation', candidate, episode)


def test_legacy_small_trace_and_invalid_indices(archive, monkeypatch):
    store, run, candidate, episode, payload = archive
    path = store._trace_path(run, 'validation', candidate, episode)
    storage.atomic_write_json(path, payload)
    summary = store.load_trace_summary(run, 'validation', candidate, episode)
    assert store.load_trace_decision(run, 'validation', candidate, episode, 2, summary['trace_revision'])['decision'] == payload['traces'][2]
    for index in (-1, 3, True):
        with pytest.raises(StoreError):
            store.load_trace_decision(run, 'validation', candidate, episode, index)
    monkeypatch.setattr(storage, 'LEGACY_TRACE_LIMIT', 1)
    with pytest.raises(StoreError, match='no paging index'):
        store.load_trace_summary(run, 'validation', candidate, episode)
    assert store.load_trace(run, 'validation', candidate, episode) == payload


def test_search_http_keeps_sealed_data_private_and_supports_revisions(archive):
    store, run, candidate, episode, payload = archive
    store.save_trace(run, 'validation', candidate, episode, payload)
    store.save_trace(run, 'holdout', candidate, episode, payload)
    with TestClient(create_app(store.root)) as client:
        base = f'/api/runs/{run}/trace/{candidate}/{episode}'
        summary = client.get(base + '/summary').json()
        assert client.get(base).json() == payload
        response = client.get(base + '/decision/1', params={'revision': summary['trace_revision']})
        assert response.status_code == 200
        assert response.json()['decision'] == payload['traces'][1]
        assert client.get(base + '/decision/1', params={'revision': 'stale'}).status_code == 409
        for suffix in ('', '/summary', '/decision/0'):
            for split in ('holdout', 'test'):
                assert client.get(base + suffix, params={'split': split}).status_code == 400


def test_concurrent_same_trace_writers_publish_a_matching_index(archive):
    store, run, candidate, episode, payload = archive
    variants = []
    for i in range(4):
        changed = copy.deepcopy(payload)
        changed['traces'][0]['target'] = i / 4
        variants.append(changed)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: store.save_trace(run, 'validation', candidate, episode, value), variants))
    summary = store.load_trace_summary(run, 'validation', candidate, episode)
    result = store.load_trace_decision(run, 'validation', candidate, episode, 0, summary['trace_revision'])
    assert result['decision'] == store.load_trace(run, 'validation', candidate, episode)['traces'][0]


def test_thousand_decisions_stay_out_of_the_summary(archive):
    store, run, candidate, episode, payload = archive
    payload['traces'] = [dict(payload['traces'][0], timestamp=i) for i in range(1000)]
    store.save_trace(run, 'validation', candidate, episode, payload)
    summary = store.load_trace_summary(run, 'validation', candidate, episode)
    assert len(summary['traces']) == 1000
    full_size = store._trace_path(run, 'validation', candidate, episode).stat().st_size
    assert len(json.dumps(summary).encode()) < full_size / 10
    assert store.load_trace_decision(run, 'validation', candidate, episode, 999)['decision']['timestamp'] == 999
