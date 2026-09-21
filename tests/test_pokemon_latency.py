"""Archived latency is measured, split-scoped, and revision-bound."""
import builtins
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auto_jev.storage import RunStore, atomic_write_json
from examples.pokemon.demo import create_app
from examples.pokemon.latency import LatencyArchive, _stats


def decision(milliseconds, flags=(False, False), *, status='ok'):
    nodes = [{'id': 'features', 'kind': 'python', 'status': 'ok', 'elapsed_ms': 10}]
    for index, flag in enumerate(flags):
        response = {'elapsed_ms': 40 + 20 * index, 'request_elapsed_ms': 999999,
                    'authorization': 'PRIVATE_RESPONSE'}
        if flag != 'missing':
            response['cache_hit'] = flag
        nodes.append({'id': ('plan', 'pick')[index], 'kind': 'jev', 'status': 'ok',
                      'elapsed_ms': 70 + 10 * index, 'response': response})
    nodes.append({'id': 'decision', 'kind': 'python', 'status': 'ok', 'elapsed_ms': 20})
    return {'decision_ms': milliseconds, 'elapsed_ms': 777777, 'status': status,
            'trace': nodes, 'observation': {'private': 'DO_NOT_EXPOSE'}}


@pytest.fixture
def archive(tmp_path):
    store = RunStore(tmp_path / 'runs')
    hashes = ['a' * 64, 'b' * 64, 'c' * 64]
    run_id = store.create_run('mixed-latency', {
        'seed_pipeline_hash': hashes[0],
        'episodes': [{'id': 'eval-1', 'split': 'validation'}, {'id': 'eval-2', 'split': 'validation'},
                     {'id': 'train-1', 'split': 'train'}, {'id': 'test-private', 'split': 'holdout'}]})
    other = store.create_run('PRIVATE_OTHER_BRANCH', {})
    state = tmp_path / 'state.json'
    atomic_write_json(state, {'run_id': run_id, 'code_run_id': other, 'run_ids': [run_id, other],
                             'test_released': True})
    atomic_write_json(tmp_path / 'test.json', {'private': 'PRIVATE_TEST'})
    atomic_write_json(tmp_path / 'summary.json', {'private': 'PRIVATE_SUMMARY'})
    for ident in hashes:
        spec = {'name': ident[0], 'nodes': [{'id': node['id'], 'kind': node['kind']}
                                            for node in decision(1)['trace']]}
        store.save_candidate(run_id, spec, ident, parents=[] if ident == hashes[0] else [hashes[0]],
                             origin='seed' if ident == hashes[0] else 'proposal')
    store.update_run(run_id, frozen_hash=hashes[1], result={'best_hash': hashes[1]})
    seed_decisions = [decision(100), decision(200), decision(5, (True, True)),
                      decision(9, (True, False)), decision(300, ('missing', False)),
                      decision(400, status='error'), decision(None)]
    for candidate, split, episode, records in [
            (hashes[0], 'validation', 'eval-1', seed_decisions),
            (hashes[1], 'validation', 'eval-1', [decision(80), decision(120)]),
            (hashes[0], 'train', 'train-1', [decision(999)]),
            (hashes[0], 'holdout', 'test-private', [decision(888888)])]:
        store.save_trace(run_id, split, candidate, episode, {'episode_id': episode, 'status': 'ok', 'traces': records})
        if split != 'holdout':
            store.append_evaluation(run_id, {'candidate': candidate, 'split': split, 'episode_id': episode,
                                             'status': 'ok', 'score': 1})
    # Duplicate evaluation rows must not duplicate the latest trace's timings.
    store.append_evaluation(run_id, {'candidate': hashes[0], 'split': 'validation', 'episode_id': 'eval-1', 'status': 'ok', 'score': 1})
    store.append_evaluation(run_id, {'candidate': hashes[2], 'split': 'validation', 'episode_id': 'eval-2', 'status': 'error', 'score': -1e9})
    return {'store': store, 'state': state, 'run_id': run_id, 'other': other, 'hashes': hashes,
            'client': TestClient(create_app(store.root, state))}


def test_real_decision_timing_uncached_cohort_and_parallel_nodes(archive):
    response = archive['client'].get('/api/latency')
    assert response.status_code == 200
    report = response.json()
    assert report['split'] == 'eval' and report['run_id'] == archive['run_id']
    assert report['initial_hash'] == archive['hashes'][0] and report['selected_hash'] == archive['hashes'][1]
    seed, selected, missing = report['candidates']
    assert seed['coverage'] == {'games': 1, 'total': 2, 'missing_games': 0, 'missing_episode_ids': []}
    assert seed['decision'] == {'count': 2, 'median_ms': 150., 'p95_ms': 195., 'mean_ms': 150.,
                               'observed': 7, 'excluded_cached': 2, 'excluded_unknown': 1,
                               'excluded_failed': 1, 'excluded_missing_timing': 1, 'without_jev': 0}
    assert selected['decision']['median_ms'] == 100 and selected['selected']
    assert seed['jev'] == {'calls': 14, 'uncached_calls': 10, 'cache_hits': 3,
                           'unknown_cache': 1, 'errors': 0, 'count': 10,
                           'median_ms': 60., 'p95_ms': 60., 'mean_ms': 52.}
    assert seed['python'] == {'count': 14, 'median_ms': 15., 'p95_ms': 20., 'mean_ms': 15.}
    nodes = {node['id']: node for node in seed['nodes']}
    assert nodes['plan']['median_ms'] == 70 and nodes['pick']['median_ms'] == 80
    assert nodes['plan']['cache_hits'] == 2 and nodes['pick']['cache_hits'] == 1
    # Neither node duration sums, runtime elapsed_ms, nor historical cached
    # request_elapsed_ms can substitute for the measured decision/client times.
    assert seed['decision']['median_ms'] != sum(node['median_ms'] for node in seed['nodes'])
    assert max(seed['jev'][key] for key in ('median_ms', 'p95_ms', 'mean_ms')) < 100
    assert missing['decision']['count'] == 0 and missing['decision']['median_ms'] is None
    assert missing['coverage']['missing_games'] == 1
    assert missing['coverage']['missing_episode_ids'] == ['eval-2']
    assert len(seed['revisions']) == 1 and len(seed['revisions'][0]['revision']) == 64


def test_split_and_read_boundary_never_opens_private_or_other_branch(archive, monkeypatch):
    original, builtin = Path.open, builtins.open
    reads = []

    def check(path):
        if isinstance(path, int):
            return
        path = Path(path)
        assert path.name not in ('test.json', 'summary.json', 'holdout.json', 'sealed_episodes.json')
        assert archive['other'] not in path.parts and 'holdout' not in path.parts
        if 'traces' in path.parts:
            assert 'train' in path.parts
        reads.append(path)

    def path_open(path, *args, **kwargs):
        check(path)
        return original(path, *args, **kwargs)

    def builtin_open(path, *args, **kwargs):
        check(path)
        return builtin(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', path_open)
    monkeypatch.setattr(builtins, 'open', builtin_open)
    response = archive['client'].get('/api/latency?split=train&run_id=' + archive['other'])
    assert response.status_code == 200 and reads
    report = response.json()
    assert report['candidates'][0]['decision']['median_ms'] == 999
    assert report['candidates'][1]['decision']['count'] == 0
    for private in ('PRIVATE_', 'DO_NOT_EXPOSE', 'authorization', '"test"', '"holdout"', archive['other']):
        assert private not in response.text
    for invalid in ('test', 'holdout', 'validation', '../test'):
        assert archive['client'].get('/api/latency', params={'split': invalid}).status_code == 400
    assert archive['client'].post('/api/latency').status_code == 405


def test_warm_cache_does_not_parse_decisions_and_replacement_invalidates_it(archive, monkeypatch):
    from examples.pokemon import latency
    service = LatencyArchive(archive['store'], archive['state'])
    original = latency._compact
    calls = []

    def count(value):
        calls.append(1)
        return original(value)

    monkeypatch.setattr(latency, '_compact', count)
    first = service.report()
    cold_count = len(calls)
    assert cold_count == 9
    assert service.report() == first and len(calls) == cold_count
    store, candidate = archive['store'], archive['hashes'][0]
    store.save_trace(archive['run_id'], 'validation', candidate, 'eval-1', {'traces': [decision(700)]})
    updated = service.report()
    assert len(calls) == cold_count + 1
    assert updated['candidates'][0]['decision']['median_ms'] == 700
    assert first['candidates'][0]['revisions'] != updated['candidates'][0]['revisions']


def test_trace_replaced_during_scan_returns_conflict_not_partial_metrics(archive, monkeypatch):
    from examples.pokemon import latency
    original = latency._compact
    replaced = False

    def replace(value):
        nonlocal replaced
        if not replaced:
            replaced = True
            archive['store'].save_trace(archive['run_id'], 'validation', archive['hashes'][0],
                                        'eval-1', {'traces': [decision(700)]})
        return original(value)

    monkeypatch.setattr(latency, '_compact', replace)
    response = archive['client'].get('/api/latency')
    assert response.status_code == 409 and 'changed' in response.json()['detail']


@pytest.mark.parametrize('flags', [('missing', False), (0, False), (None, False)])
def test_non_boolean_or_missing_cache_flags_are_unknown(archive, flags):
    archive['store'].save_trace(archive['run_id'], 'validation', archive['hashes'][0], 'eval-1',
                                {'traces': [decision(100, flags)]})
    metric = archive['client'].get('/api/latency').json()['candidates'][0]['decision']
    assert metric['count'] == 0 and metric['excluded_unknown'] == 1 and metric['excluded_cached'] == 0


def test_incomplete_or_absent_jev_trace_is_not_assumed_uncached(archive):
    missing, absent = decision(100), decision(100)
    missing['trace'].pop(1)
    del absent['trace']
    archive['store'].save_trace(archive['run_id'], 'validation', archive['hashes'][0], 'eval-1',
                                {'traces': [missing, absent]})
    metric = archive['client'].get('/api/latency').json()['candidates'][0]['decision']
    assert metric['count'] == 0 and metric['excluded_unknown'] == 2


def test_no_jev_decision_is_reported_separately_from_uncached():
    from examples.pokemon.latency import _candidate_metrics, _compact
    record = decision(100)
    record['trace'] = [node for node in record['trace'] if node['kind'] == 'python']
    metric = _candidate_metrics([_compact(record)], record['trace'])['decision']
    assert metric['count'] == 0 and metric['without_jev'] == 1


@pytest.mark.parametrize('bad', [True, -1, float('nan'), float('inf'), None])
def test_non_finite_boolean_or_negative_timings_never_enter_statistics(bad):
    assert _stats([bad, 10, 30]) == {'count': 2, 'median_ms': 20., 'p95_ms': 29., 'mean_ms': 20.}


def test_corrupt_index_fails_closed_instead_of_returning_cached_values(archive):
    first = archive['client'].get('/api/latency')
    assert first.status_code == 200
    path = archive['store']._trace_path(archive['run_id'], 'validation', archive['hashes'][0], 'eval-1').with_suffix('.index.json')
    index = json.loads(path.read_text())
    index['decisions'][0] = [0, index['binding']['st_size'] + 1]
    atomic_write_json(path, index)
    response = archive['client'].get('/api/latency')
    assert response.status_code == 404 and 'byte index' in response.json()['detail']


def test_waiting_experiment_has_empty_latency_without_opening_games(tmp_path):
    response = TestClient(create_app(tmp_path / 'runs', tmp_path / 'state.json')).get('/api/latency')
    assert response.status_code == 200
    assert response.json()['run_id'] is None and response.json()['candidates'] == []
