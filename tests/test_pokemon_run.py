"""Offline orchestration checks; all providers, battles and optimizers are fakes."""
import copy
import fcntl
from collections import Counter
from itertools import combinations
from types import SimpleNamespace

import pytest

from auto_jev import frozen
from auto_jev.spec import spec_hash
from auto_jev.storage import RunStore, atomic_write_json, read_json
from examples.pokemon import run as runner


def policy(branch, evolved=False):
    return {'version': 2, 'name': branch + (' evolved' if evolved else ' seed'),
            'jev_model': 'typesafe-ai/jev', 'nodes': [], 'output': '1' if evolved else '0'}


@pytest.fixture
def offline_runner(tmp_path, monkeypatch):
    (tmp_path / 'configs').mkdir()
    atomic_write_json(tmp_path / 'configs/opus5_pokemon.json', {'kind': 'offline-test'})
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'load_dotenv', lambda *a, **kw: None)
    monkeypatch.setattr(runner, 'source_hash', lambda: 'fixed-test-runtime')
    monkeypatch.setattr(runner, 'seed_spec', lambda: policy('mixed'))
    monkeypatch.setattr(runner, 'code_baseline_spec', lambda: policy('code'))
    events, evaluations, evolutions, clients = [], [], [], []
    control = {'fail_test': None}

    class FakeJev:
        def __init__(self, **kwargs):
            self.stats = {'calls': 0, 'cache_hits': 0}
            self.metadata = {'cache_namespace': kwargs['cache_namespace']}
            clients.append(self)

    def evaluate(spec, episode, jev, **kwargs):
        key = (spec['name'], episode['id'])
        evaluations.append(key)
        if control['fail_test'] == key:
            raise RuntimeError('planned battle transport failure')
        if spec['name'].startswith('mixed'):
            jev.stats['calls'] += 1
        events.append(('game', key))
        return {'episode_id': episode['id'], 'score': 1.0, 'winner': 'player',
                'turns': 1, 'decisions': 1, 'status': 'completed', 'elapsed_ms': 3.0,
                'traces': [{'trace': [{'id': 'full', 'status': 'ok', 'output': 'evidence'}],
                            'decision_ms': 2.0}], 'replay_log': '|win|AutoJev'}

    def evolve(train, validation, **kwargs):
        branch = kwargs['task_id'].removeprefix('pokemon_singles_')
        events.append(('evolve', branch))
        evolutions.append({'branch': branch, 'train': copy.deepcopy(train),
                           'validation': copy.deepcopy(validation), 'kwargs': kwargs})
        assert kwargs['resume_run_id'] is None
        store, jev = kwargs['store'], kwargs['jev']
        ident = store.create_run('offline-' + branch, {})
        seed, best = policy(branch), policy(branch, True)
        for spec in (seed, best):
            store.save_candidate(ident, spec, spec_hash(spec))
        if branch == 'mixed':
            jev.stats['calls'] += 10
        result = {'run_id': ident, 'best_hash': spec_hash(best), 'best_score': 1.0,
                  'rounds_completed': kwargs['evolution_rounds']}
        store.update_run(ident, status='completed', result=result,
                         progress={'jev': dict(jev.stats), 'proposer': {'calls': kwargs['evolution_rounds']}})
        return result

    def freeze(store, ident):
        run = store.get_run(ident)
        spec = store.get_candidate(ident, run['result']['best_hash'])['spec']
        events.append(('freeze', spec['name']))
        return {'spec': spec, 'spec_hash': spec_hash(spec), 'task_contract': {'test': 'fixed'}}

    original_read = runner.read_json
    def tracked_read(path, *args, **kwargs):
        if path.name == 'test.json':
            events.append(('test_read', None))
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(runner, 'JevClient', FakeJev)
    monkeypatch.setattr(runner, 'make_proposer', lambda config: SimpleNamespace(stats={'calls': 0}))
    monkeypatch.setattr(runner, 'evaluate_episode', evaluate)
    monkeypatch.setattr(runner, 'evaluate_code_episode', evaluate)
    monkeypatch.setattr(runner, 'run_evolution', evolve)
    monkeypatch.setattr(runner, 'freeze_run', freeze)
    monkeypatch.setattr(runner, 'evaluate_frozen', lambda artifact, episodes, jev, evaluator:
                        [evaluator(artifact['spec'], ep, jev) for ep in episodes])
    monkeypatch.setattr(runner, 'task_contract', lambda evaluator, output: {'test': 'fixed'})
    monkeypatch.setattr(runner, 'verify_task_contract', lambda *args, **kwargs: None)
    monkeypatch.setattr(frozen, 'validate_artifact', lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, 'read_json', tracked_read)
    return SimpleNamespace(root=tmp_path, output=tmp_path / 'experiment', events=events,
                           evaluations=evaluations, evolutions=evolutions,
                           clients=clients, control=control)


def test_paired_branches_share_data_and_round_budget_and_seal_until_both_freeze(offline_runner):
    env = offline_runner
    state = runner.run(env.output, rounds=2)
    assert state['status'] == 'completed' and state['test_released']
    mixed, code = env.evolutions
    assert mixed['branch'] == 'mixed' and code['branch'] == 'code'
    assert mixed['train'] == code['train'] and mixed['validation'] == code['validation']
    for entry in env.evolutions:
        assert entry['kwargs']['seed'] == 17
        assert entry['kwargs']['reflection_batch_size'] == 1
        assert entry['kwargs']['evolution_rounds'] == 2
        assert all('-test-' not in ep['id'] for ep in entry['train'] + entry['validation'])
    read_at = next(i for i, event in enumerate(env.events) if event[0] == 'test_read')
    assert [e[1] for e in env.events[:read_at] if e[0] == 'freeze'] == ['mixed evolved', 'code evolved']
    summary = read_json(env.output / 'summary.json')
    assert set(summary['policies']) == {'evolved', 'initial', 'code_evolved', 'code_initial'}
    assert all(p['games'] == 4 for p in summary['policies'].values())
    assert len(env.clients) == 3
    assert len({c.metadata['cache_namespace'] for c in env.clients}) == 3
    assert summary['jev_by_branch']['code']['calls'] == 0
    assert summary['jev_by_branch']['mixed']['calls'] == 18
    assert summary['jev_by_branch']['preflight']['calls'] == 1
    assert summary['jev']['calls'] == 19
    code_run = RunStore(env.root / 'runs').get_run(state['branches']['code']['run_id'])
    assert code_run['progress']['jev']['calls'] == 0


def test_preflight_only_evaluates_engineering_and_resume_continues_search(offline_runner):
    env = offline_runner
    state = runner.run(env.output, rounds=2, preflight_only=True)
    assert state['status'] == 'paused' and not state['test_released']
    assert not env.evolutions and all('-engineering-' in ep for _, ep in env.evaluations)
    assert not any(e[0] == 'test_read' for e in env.events)
    runner.run(env.output, rounds=2, resume=True)
    assert sum('-engineering-' in ep for _, ep in env.evaluations) == 1


def test_holdout_crash_resume_reuses_committed_games_and_completed_search(offline_runner):
    env = offline_runner
    env.control['fail_test'] = ('mixed evolved', 'pokemon-test-02')
    with pytest.raises(RuntimeError, match='planned battle'):
        runner.run(env.output, rounds=2)
    state = read_json(env.output / 'state.json')
    assert state['status'] == 'failed' and not state['test_released']
    assert len(env.evolutions) == 2
    env.control['fail_test'] = None
    resumed = runner.run(env.output, rounds=2, resume=True)
    assert resumed['status'] == 'completed' and len(env.evolutions) == 2
    assert env.evaluations.count(('mixed evolved', 'pokemon-test-01')) == 1
    assert env.evaluations.count(('mixed evolved', 'pokemon-test-02')) == 2
    assert sum('-engineering-' in ep for _, ep in env.evaluations) == 1
    for ident in resumed['run_ids']:
        rows = RunStore(env.root / 'runs').list_holdout(ident)
        assert len(rows) == 8
        assert len({(r['policy'], r['episode_id']) for r in rows}) == 8
    summary = read_json(env.output / 'summary.json')
    assert summary['jev']['calls'] == 19
    assert summary['jev_by_branch']['code']['calls'] == 0


def test_resume_rejects_changed_prepared_inputs_or_round_target(offline_runner):
    env = offline_runner
    runner.run(env.output, rounds=2, preflight_only=True)
    with pytest.raises(ValueError, match='round target changed'):
        runner.run(env.output, rounds=3, resume=True)
    old = env.output / 'train.json'
    original = old.read_bytes()
    old.write_bytes(original + b'\n')
    with pytest.raises(ValueError, match='Prepared input changed'):
        runner.run(env.output, rounds=2, resume=True)
    assert not env.evolutions


def test_completed_run_recovers_usage_ahead_of_last_experiment_heartbeat(offline_runner):
    env = offline_runner
    env.control['fail_test'] = ('mixed evolved', 'pokemon-test-01')
    with pytest.raises(RuntimeError, match='planned battle'):
        runner.run(env.output, rounds=2)
    state_path = env.output / 'state.json'
    state = read_json(state_path)
    run = RunStore(env.root / 'runs').get_run(state['branches']['mixed']['run_id'])
    assert run['status'] == 'completed' and run['progress']['jev']['calls'] == 10
    # Model the last experiment heartbeat preceding the already committed run.
    # A kill after completion must still recover its more recent durable stats.
    state['jev_by_branch']['mixed']['calls'] = 0
    state['jev']['calls'] = 1
    atomic_write_json(state_path, state)
    env.control['fail_test'] = None
    runner.run(env.output, rounds=2, resume=True)
    summary = read_json(env.output / 'summary.json')
    assert summary['jev_by_branch']['mixed']['calls'] == 18
    assert summary['jev']['calls'] == 19


def test_resume_rejects_legacy_state_without_research_binding(offline_runner):
    env = offline_runner
    runner.run(env.output, rounds=2, preflight_only=True)
    path = env.output / 'state.json'
    state = read_json(path)
    state.pop('research_source_hash')
    atomic_write_json(path, state)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='no research source binding'):
        runner.run(env.output, rounds=2, resume=True)
    assert not env.evolutions and path.read_bytes() == before


def test_resume_rejects_changed_search_implementation(offline_runner, monkeypatch):
    env = offline_runner
    runner.run(env.output, rounds=2, preflight_only=True)
    path = env.output / 'state.json'
    before = path.read_bytes()
    monkeypatch.setattr(runner, '_research_source_hash', lambda: 'changed-search-implementation')
    with pytest.raises(ValueError, match='code or round target changed'):
        runner.run(env.output, rounds=2, resume=True)
    assert not env.evolutions and path.read_bytes() == before


def test_research_hash_binds_local_sources_and_actual_gepa_code(tmp_path, monkeypatch):
    local = tmp_path / 'auto_jev'
    package = tmp_path / 'gepa'
    local.mkdir()
    package.mkdir()
    driver = tmp_path / 'run.py'
    driver.write_text('runner source')
    authoring = tmp_path / 'execution_contract.py'
    authoring.write_text('authoring contract source')
    for name in ('evolution.py', 'reflection.py', 'trace_codec.py'):
        (local / name).write_text(name)
    module = package / '__init__.py'
    module.write_text('gepa implementation')
    monkeypatch.setattr(runner, '__file__', str(driver))
    monkeypatch.setattr(runner, 'execution_contract', SimpleNamespace(__file__=str(authoring)))
    monkeypatch.setattr(runner, 'evolution_module', SimpleNamespace(__file__=str(local / 'evolution.py')))
    monkeypatch.setattr(runner, 'gepa', SimpleNamespace(__file__=str(module)))
    monkeypatch.setattr(runner.importlib.metadata, 'version', lambda name: '0.1.4')
    original = runner._research_source_hash()
    for path in [driver, authoring, *(local / name for name in ('evolution.py', 'reflection.py', 'trace_codec.py')), module]:
        old = path.read_text()
        path.write_text(old + '\nchanged')
        assert runner._research_source_hash() != original
        path.write_text(old)
        assert runner._research_source_hash() == original
    # A same-version local GEPA source edit or added module still changes the
    # contract; distribution metadata alone cannot catch it.
    extra = package / 'new_selector.py'
    extra.write_text('new selector implementation')
    assert runner._research_source_hash() != original
    extra.unlink()
    assert runner._research_source_hash() == original


def test_expanded_data_is_fresh_disjoint_reproducible_and_covers_opponents(offline_runner):
    env = offline_runner
    pilot = env.root / 'old-pilot'
    runner.prepare(pilot)
    first = runner.prepare(env.output, profile='expanded')
    other = env.root / 'replica'
    runner.prepare(other, profile='expanded')
    assert first['split_counts'] == {'train': 18, 'validation': 12, 'test': 12, 'engineering': 1}
    triple = lambda e: (e['player_team_id'], e['opponent_team_id'], e['opponent'])
    old = [e for split in ('train', 'validation', 'test', 'engineering')
           for e in read_json(pilot / f'{split}.json')]
    old_cases = {triple(e) for e in old}
    old_seeds = {tuple(e['seed']) for e in old}
    old_opponent_seeds = {e['opponent_seed'] for e in old}
    splits = {}
    seeds = set()
    opponent_seeds = set()
    ids = set()
    for split in ('train', 'validation', 'test', 'engineering'):
        games = read_json(env.output / f'{split}.json')
        assert games == read_json(other / f'{split}.json')
        for game in games:
            assert tuple(game['seed']) not in old_seeds | seeds
            assert game['opponent_seed'] not in old_opponent_seeds | opponent_seeds
            assert game['id'] not in ids
            seeds.add(tuple(game['seed']))
            opponent_seeds.add(game['opponent_seed'])
            ids.add(game['id'])
        if split == 'engineering':
            assert {triple(e) for e in games}.issubset(old_cases)
            continue
        counts = Counter(triple(e) for e in games)
        assert set(counts.values()) == {2}
        splits[split] = set(counts)
        assert not splits[split] & old_cases
        assert {e['player_team_id'] for e in games} == {'balanced', 'tempo', 'resilient', 'sand'}
        assert {e['opponent_team_id'] for e in games} == {'balanced', 'tempo', 'resilient', 'sand'}
        assert {e['opponent'] for e in games} == {'random', 'max_power', 'heuristic'}
    assert all(not a & b for a, b in combinations(splits.values(), 2))
    assert len(set.union(*splits.values())) == 21
    assert runner.prepare(env.output, profile='expanded') == first


def test_expanded_runner_uses_two_full_games_and_skip_without_releasing_test_early(offline_runner):
    env = offline_runner
    state = runner.run(env.output, profile='expanded', rounds=5)
    assert state['status'] == 'completed'
    assert state['split_counts']['test'] == 12
    for entry in env.evolutions:
        assert len(entry['train']) == 18 and len(entry['validation']) == 12
        assert entry['kwargs']['reflection_batch_size'] == 2
        assert entry['kwargs']['skip_perfect_score'] is True
        assert entry['kwargs']['evolution_rounds'] == 5
        contract = entry['kwargs']['task_context']['execution_contract']
        assert 'Even the conventional throwaway variable _' in contract['python']['names']
        assert "item.id.startswith('_')" in contract['python']['source_validator']
        assert 'getattr' in contract['python']['forbidden_identifiers']
        assert 'enumerate' in contract['python']['builtins']
        assert contract['expressions']['allowed_functions'] == sorted(runner.execution_contract.spec.FUNCS)
    read_at = next(i for i, event in enumerate(env.events) if event[0] == 'test_read')
    assert [e[1] for e in env.events[:read_at] if e[0] == 'freeze'] == ['mixed evolved', 'code evolved']
    summary = read_json(env.output / 'summary.json')
    assert all(policy['games'] == 12 for policy in summary['policies'].values())


def test_profile_and_reflection_rules_are_immutable(offline_runner):
    env = offline_runner
    runner.prepare(env.output, profile='expanded')
    with pytest.raises(ValueError, match='profile changed'):
        runner.prepare(env.output)
    rules = read_json(env.output / 'rules.json')
    rules['skip_perfect_score'] = False
    atomic_write_json(env.output / 'rules.json', rules)
    with pytest.raises(ValueError, match='Prepared input changed: rules.json'):
        runner.prepare(env.output, profile='expanded')


def test_concurrent_preparation_cannot_rewrite_manifest(offline_runner):
    env = offline_runner
    runner.prepare(env.output, profile='expanded')
    before = (env.output / 'manifest.json').read_bytes()
    with (env.output / 'prepare.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='preparation worker'):
            runner.prepare(env.output, profile='expanded')
    assert (env.output / 'manifest.json').read_bytes() == before
