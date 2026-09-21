import copy
import json
from pathlib import Path

import pytest

from auto_jev.experiment import _validate_windows, run_experiment
from auto_jev.storage import atomic_write_json


def episode(ident, start, count, asset='BTC-USD'):
    return {'id': ident, 'asset': asset, 'interval_seconds': 3600,
            'bars': [{'timestamp': start + i * 3600} for i in range(count)], 'news': []}


def test_partition_validation_catches_overlap_gap_asset_and_window_size():
    eps = [episode('a', 0, 2), episode('b', 7200, 2)]
    kw = dict(assets=['BTC-USD'], bars_per_asset=4, window_bars=2, interval=3600)
    assert _validate_windows(eps, **kw) == {'BTC-USD': (0, 14400)}
    for bad in [eps + [eps[0]], [eps[0], episode('b', 3600, 2)],
                [eps[0], episode('b', 10800, 2)], [episode('a', 0, 4)],
                [eps[0], episode('b', 7200, 2, 'ETH-USD')]]:
        with pytest.raises(ValueError):
            _validate_windows(bad, **kw)


def setup_experiment(tmp_path):
    cfg = {'name': 'test', 'assets': ['BTC-USD'], 'interval_seconds': 3600,
           'bars_per_split': 4, 'train_batch_bars': 2, 'evolution_rounds': 50,
           'costs': {}, 'output_dir': str(tmp_path / 'output'), 'runs_dir': str(tmp_path / 'runs')}
    files = {'train': [episode('t0', 0, 2), episode('t1', 7200, 2)],
             'validation': [episode('v', 14400, 4)], 'test': [episode('sealed', 28800, 4)],
             'proposer_config': {'kind': 'test'}, 'seed_pipeline': {'seed': True}}
    for key, data in files.items():
        path = tmp_path / f'{key}.json'
        atomic_write_json(path, data)
        cfg[key] = str(path)
    config = tmp_path / 'config.json'
    atomic_write_json(config, cfg)
    return config, cfg


def test_workflow_keeps_test_out_of_search_and_freezes_before_test(tmp_path, monkeypatch):
    import auto_jev.evolution as evolution
    import auto_jev.frozen as frozen
    import auto_jev.providers as providers
    import auto_jev.crypto as crypto
    config, cfg = setup_experiment(tmp_path)
    order = []
    class Jev:
        stats = {}
        def __init__(self, **kwargs): pass
    monkeypatch.setattr(providers, 'JevClient', Jev)
    monkeypatch.setattr(providers, 'make_proposer', lambda _: object())
    def evolve(train, val, **kw):
        order.append('evolution')
        assert {e['id'] for e in train + val} == {'t0', 't1', 'v'}
        assert kw['evolution_rounds'] == 50 and kw['reflection_batch_size'] == 1
        assert kw['max_metric_calls'] is None
        store = kw['store']
        ident = store.create_run('test', {})
        store.update_run(ident, status='completed')
        return {'run_id': ident}
    monkeypatch.setattr(evolution, 'run_evolution', evolve)
    artifact = {'spec_hash': 'abcdef', 'source_hash': frozen.source_hash(), 'artifact_hash': 'artifact'}
    def freeze(*_):
        order.append('freeze')
        return copy.deepcopy(artifact)
    monkeypatch.setattr(evolution, 'freeze_run', freeze)
    def evaluate(art, eps, _):
        assert order == ['evolution', 'freeze']
        assert eps[0]['id'] == 'sealed' and art == artifact
        order.append('test')
        return [{'episode_id': 'sealed', 'asset': 'BTC-USD', 'score': .1, 'traces': []}]
    monkeypatch.setattr(frozen, 'evaluate_frozen', evaluate)
    monkeypatch.setattr(crypto, 'evaluate_baselines', lambda *_args, **_kw: {'cash': {'score': 0}})
    result = run_experiment(config)
    assert result['status'] == 'completed'
    assert result['test_completed_ids'] == ['sealed']
    assert run_experiment(config)['status'] == 'completed'
    assert order == ['evolution', 'freeze', 'test']  # Completed execution is idempotent.
    Path(cfg['test']).write_text('[]')
    with pytest.raises(ValueError, match='changed'):
        run_experiment(config, resume=True)
