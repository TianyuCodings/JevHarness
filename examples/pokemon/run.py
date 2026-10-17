"""Reproducible paired code/Jev evolution pilot with a sealed game test set."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import random
import shutil
import statistics
import threading
import time
import uuid

import gepa
from dotenv import load_dotenv

from auto_jev import evolution as evolution_module

from auto_jev.evolution import run_evolution, freeze_run
from auto_jev.file_lock import flock
from auto_jev.frozen import build_task_contract, evaluate_frozen, source_hash, verify_task_contract
from auto_jev.providers import JevClient, make_proposer
from auto_jev.spec import spec_hash
from auto_jev.storage import RunStore, atomic_write_json, now_iso, read_json
from .adapter import evaluate_episode, evaluate_code_episode
from .seed import TASK_CONTEXT, seed_spec, code_baseline_spec
from . import execution_contract

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


PILOT_COMBINATIONS = {
    'train': [('balanced', 'tempo', 'random'), ('tempo', 'resilient', 'max_power'),
              ('resilient', 'sand', 'heuristic'), ('sand', 'balanced', 'random'),
              ('balanced', 'resilient', 'max_power'), ('tempo', 'sand', 'heuristic')],
    'validation': [('balanced', 'sand', 'heuristic'), ('tempo', 'balanced', 'max_power'),
                   ('resilient', 'tempo', 'random'), ('sand', 'resilient', 'heuristic')],
    'test': [('balanced', 'tempo', 'heuristic'), ('tempo', 'resilient', 'random'),
             ('resilient', 'sand', 'max_power'), ('sand', 'balanced', 'heuristic')],
    'engineering': [('balanced', 'resilient', 'heuristic')],
}


def expanded_combinations():
    # Fixed before observing any scores. Each split covers all four player
    # teams, all four opponent teams and all three opponent policies.
    return {
        'train': [('resilient', 'balanced', 'random'), ('balanced', 'sand', 'max_power'),
                  ('tempo', 'balanced', 'heuristic'), ('tempo', 'resilient', 'heuristic'),
                  ('sand', 'resilient', 'max_power'), ('resilient', 'sand', 'random'),
                  ('resilient', 'tempo', 'max_power'), ('balanced', 'sand', 'random'),
                  ('sand', 'tempo', 'max_power')],
        'validation': [('tempo', 'balanced', 'random'), ('balanced', 'tempo', 'max_power'),
                       ('tempo', 'sand', 'random'), ('resilient', 'balanced', 'max_power'),
                       ('sand', 'resilient', 'random'), ('sand', 'tempo', 'heuristic')],
        'test': [('tempo', 'sand', 'max_power'), ('resilient', 'tempo', 'heuristic'),
                 ('resilient', 'balanced', 'heuristic'), ('balanced', 'resilient', 'random'),
                 ('sand', 'tempo', 'random'), ('sand', 'balanced', 'max_power')],
        # Reused only for infrastructure preflight, with a fresh random seed.
        'engineering': copy.deepcopy(PILOT_COMBINATIONS['engineering']),
    }


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _research_source_hash():
    """Bind the search implementation as well as the executable policy runtime."""
    source_root = Path(evolution_module.__file__).resolve().parent
    gepa_root = Path(gepa.__file__).resolve().parent
    gepa_files = sorted(gepa_root.rglob('*.py'))
    if not gepa_files:
        raise ValueError('Installed GEPA sources are unavailable for the research contract')
    contract = {
        'runner': sha(__file__),
        'authoring_contract': sha(execution_contract.__file__),
        'sources': {name: sha(source_root / name) for name in
                    ('evolution.py', 'reflection.py', 'trace_codec.py')},
        'gepa_version': importlib.metadata.version('gepa'),
        'gepa_root': str(gepa_root),
        'gepa_sources': {path.relative_to(gepa_root).as_posix(): sha(path)
                         for path in gepa_files},
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def prepare(output, *, profile='pilot'):
    """Fix all inputs before the first score is observed; never rewrite a manifest."""
    if profile not in ('pilot', 'expanded'):
        raise ValueError('Unknown Pokemon experiment profile')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'prepare.lock').open('a') as lock:
        try:
            flock(lock, non_blocking=True)
        except BlockingIOError:
            raise ValueError('This Pokemon experiment already has a preparation worker') from None
        return _prepare_inputs(output, profile=profile)


def _prepare_inputs(output, *, profile):
    manifest_path = output / 'manifest.json'
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest.get('profile', 'pilot') != profile:
            raise ValueError('Prepared experiment profile changed')
        for name, expected in manifest['input_hashes'].items():
            if sha(output / name) != expected:
                raise ValueError('Prepared input changed: ' + name)
        if sha(HERE / 'teams.json') != manifest['teams_hash']:
            raise ValueError('Preset teams changed since data preparation')
        return manifest
    teams_file = read_json(HERE / 'teams.json')
    teams = {t['id']: t['team'] for t in teams_file['teams']}
    # Held-out (player team, opponent team, opponent policy) combinations;
    # seeds are also disjoint. This tests these preset teams, not unseen species.
    combinations = copy.deepcopy(PILOT_COMBINATIONS)
    repeats = 1
    if profile == 'expanded':
        combinations = expanded_combinations()
        repeats = 2
    rng = random.Random(20260920 if profile == 'pilot' else 20260921)
    used_seeds = set()
    used_opponent_seeds = set()
    if profile == 'expanded':
        # Reproduce the old pilot's seed allocation without consuming its
        # results or exposing the new test games to the search loop.
        prior_rng = random.Random(20260920)
        for _ in range(sum(map(len, PILOT_COMBINATIONS.values()))):
            used_seeds.add(tuple(prior_rng.randrange(65536) for _ in range(4)))
            used_opponent_seeds.add(prior_rng.randrange(2**31))
    split_counts = {}
    for split, cases in combinations.items():
        episodes = []
        expanded_cases = [case for case in cases for _ in range(1 if split == 'engineering' else repeats)]
        for i, (ours, theirs, policy) in enumerate(expanded_cases):
            seed = [rng.randrange(65536) for _ in range(4)]
            if tuple(seed) in used_seeds:
                raise RuntimeError('Duplicate engine seed')
            used_seeds.add(tuple(seed))
            opponent_seed = rng.randrange(2**31)
            if opponent_seed in used_opponent_seeds:
                raise RuntimeError('Duplicate opponent seed')
            used_opponent_seeds.add(opponent_seed)
            prefix = 'pokemon' if profile == 'pilot' else 'pokemon-expanded'
            episodes.append({'id': f'{prefix}-{split}-{i + 1:02}', 'format': teams_file['format'],
                'player_team': teams[ours], 'opponent_team': teams[theirs],
                'player_team_id': ours, 'opponent_team_id': theirs, 'opponent': policy,
                'seed': seed, 'opponent_seed': opponent_seed,
                'max_turns': 100, 'max_requests': 300,
                'provenance': {'source': 'official Pokemon Showdown local battle simulation',
                               'split': split, 'engine_package': 'pokemon-showdown@0.11.11'}})
        split_counts[split] = len(episodes)
        atomic_write_json(output / f'{split}.json', episodes)
    atomic_write_json(output / 'seed_mixed.json', seed_spec())
    atomic_write_json(output / 'seed_code.json', code_baseline_spec())
    reflection = read_json(ROOT / 'configs/opus5_pokemon.json')
    if profile == 'expanded':
        reflection['max_prompt_bytes'] = 2_000_000
    atomic_write_json(output / 'reflection.json', reflection)
    atomic_write_json(output / 'rules.json', {**teams_file['task_rules'],
        'engine_debug': False, 'reward': {'win': 1, 'loss': 0, 'draw': .5},
        'incomplete_battle': 'candidate failure; infrastructure errors abort the run',
        'max_turns': 100, 'max_requests': 300,
        'reflection_batch_games': 2 if profile == 'expanded' else 1,
        'skip_perfect_score': profile == 'expanded',
        'early_stop': 'An immutable candidate observed perfect on every training episode may end search before the reflection target; report actual rounds.',
        'paired_comparison': 'same data, initial code features, search seed and reflection round target; actual metric calls and cost reported separately'})
    names = ['train.json', 'validation.json', 'test.json', 'engineering.json',
             'seed_mixed.json', 'seed_code.json', 'reflection.json', 'rules.json']
    manifest = {'schema': 'auto_jev.pokemon.data.v1', 'created_at': now_iso(),
                'profile': profile,
                'split_unit': 'directed player team, opponent team, opponent policy combination',
                'seeds_per_combination': repeats,
                'teams_hash': sha(HERE / 'teams.json'),
                'input_hashes': {name: sha(output / name) for name in names},
                'split_counts': split_counts}
    if profile == 'expanded':
        manifest['allocation_version'] = 'pokemon-unused21-v1'
        manifest['excluded_previous_combinations'] = [list(case) for cases in PILOT_COMBINATIONS.values() for case in cases]
        prior_dir = ROOT / 'artifacts/pokemon_pilot'
        manifest['prior_pilot_input_hashes'] = {
            f'{split}.json': sha(prior_dir / f'{split}.json')
            for split in PILOT_COMBINATIONS if (prior_dir / f'{split}.json').is_file()}
        manifest['scope'] = 'Fresh combinations of the same preset teams and opponent policies; not unseen species or human opponents.'
    atomic_write_json(manifest_path, manifest)
    return manifest


def task_contract(evaluator, output):
    engine = HERE / 'node_modules/pokemon-showdown'
    return build_task_contract(evaluator,
        files=[HERE / 'adapter.py', HERE / 'bridge.cjs', HERE / 'seed.py', HERE / 'teams.json',
               HERE / 'package-lock.json', engine / 'package.json', Path(shutil.which('node')).resolve(),
               *[output / name for name in ('manifest.json', 'rules.json', 'train.json', 'validation.json', 'test.json')]],
        trees=[engine / 'dist', HERE / 'node_modules/ts-chacha20', HERE / 'node_modules/sql-template-strings'],
        executables=['node'])


def percentiles(values):
    values = sorted(values)
    return {'median': statistics.median(values) if values else None,
            'p95': values[min(len(values) - 1, int(.95 * len(values)))] if values else None}


def game_metrics(result, candidate, policy):
    values = [t['decision_ms'] for t in result.get('traces', []) if 'decision_ms' in t]
    uncached = []
    live_calls = cache_hits = 0
    for decision in result.get('traces', []):
        responses = [node['response'] for node in decision.get('trace', [])
                     if node.get('kind') == 'jev' and isinstance(node.get('response'), dict)]
        cache_hits += sum(response.get('cache_hit') is True for response in responses)
        live_calls += sum(response.get('cache_hit') is False for response in responses)
        if not any(response.get('cache_hit') is True for response in responses) and 'decision_ms' in decision:
            uncached.append(decision['decision_ms'])
    return {**{key: result.get(key) for key in
                   ('episode_id', 'score', 'winner', 'turns', 'decisions', 'elapsed_ms', 'status')},
            'candidate': candidate, 'policy': policy, 'decision_latency_ms': percentiles(values),
            'decision_times_ms': values, 'uncached_decision_times_ms': uncached,
            'decision_latency_uncached_ms': percentiles(uncached),
            'trace_live_jev_calls': live_calls, 'trace_jev_cache_hits': cache_hits}


def run(output, *, rounds=2, resume=False, preflight_only=False, profile='pilot'):
    load_dotenv(ROOT / '.env', override=False)
    output = Path(output).resolve()
    manifest = prepare(output, profile=profile)
    rules = read_json(output / 'rules.json')
    with (output / 'experiment.lock').open('a') as lock:
        try:
            flock(lock, non_blocking=True)
        except BlockingIOError:
            raise ValueError('This Pokemon experiment already has a worker') from None
        state_path = output / 'state.json'
        state = read_json(state_path)
        expected = {'manifest_hash': sha(output / 'manifest.json'), 'source_hash': source_hash(),
                    'runner_hash': sha(__file__), 'research_source_hash': _research_source_hash(),
                    'rounds_target': rounds}
        if state:
            if 'research_source_hash' not in state:
                raise ValueError('Existing experiment has no research source binding; '
                                 'continue with its original source environment or use a new experiment directory')
            if any(state.get(k) != v for k, v in expected.items()):
                raise ValueError('Inputs, code or round target changed; use a new experiment directory')
            if state['status'] == 'completed':
                return state
            if not resume:
                raise ValueError('Use --resume for this existing experiment')
        else:
            if resume:
                raise ValueError('No experiment exists to resume')
            state = {'schema': 'auto_jev.pokemon.experiment.v1', 'created_at': now_iso(),
                     **expected, 'phase': 'setup', 'status': 'created', 'run_id': None,
                     'run_ids': [], 'branches': {}, 'test_released': False,
                     'cache_namespace': uuid.uuid4().hex, 'split_counts': manifest['split_counts']}
        state_lock = threading.RLock()

        def update(**fields):
            with state_lock:
                state.update(fields, updated_at=now_iso())
                atomic_write_json(state_path, state)

        clients = {name: JevClient(cache_dir=ROOT / '.cache/jev', transport='vercel',
                                  cache_namespace=state['cache_namespace'] + name)
                   for name in ('preflight', 'mixed', 'code')}
        for name, client in clients.items():
            client.stats.update(state.get('jev_by_branch', {}).get(name, {}))

        def usage():
            snapshots = {name: dict(client.stats) for name, client in clients.items()}
            totals = {key: sum(stats.get(key, 0) for stats in snapshots.values())
                      for key in set().union(*(set(stats) for stats in snapshots.values()))}
            return {'jev': totals, 'jev_by_branch': snapshots}
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(10):
                update(heartbeat_at=now_iso(), **usage())

        thread = threading.Thread(target=heartbeat, daemon=True)
        update(status='running', worker_pid=os.getpid(), error=None)
        thread.start()
        store = RunStore(ROOT / 'runs')
        try:
            # No held-out game is evaluated in preflight.
            if not state.get('preflight_completed'):
                update(phase='preflight')
                episode = read_json(output / 'engineering.json')[0]
                engineering = read_json(output / 'preflight.json')
                if not engineering or engineering.get('status') != 'completed':
                    engineering = evaluate_episode(read_json(output / 'seed_mixed.json'), episode, clients['preflight'])
                    engineering['provider_totals'] = dict(clients['preflight'].stats)
                    atomic_write_json(output / 'preflight.json', engineering)
                for key, value in engineering.get('provider_totals', {}).items():
                    clients['preflight'].stats[key] = max(value, clients['preflight'].stats.get(key, 0))
                update(preflight_completed=True, preflight_seconds=engineering['elapsed_ms'] / 1000,
                       **usage())
            if preflight_only:
                update(status='paused', phase='preflight')
                return state
            train = read_json(output / 'train.json')
            validation = read_json(output / 'validation.json')
            evaluators = {'mixed': evaluate_episode, 'code': evaluate_code_episode}
            artifacts = {}
            for branch in ('mixed', 'code'):
                evaluator = evaluators[branch]
                jev = clients[branch]
                contract = task_contract(evaluator, output)
                branch_state = state['branches'].setdefault(branch, {})
                ident = branch_state.get('run_id')

                class TrackingStore(RunStore):
                    def create_run(self, *args, **kwargs):
                        created = super().create_run(*args, **kwargs)
                        branch_state['run_id'] = created
                        state['run_ids'].append(created)
                        update(**({'run_id': created} if branch == 'mixed' else {'code_run_id': created}))
                        return created

                branch_store = TrackingStore(ROOT / 'runs')
                current = store.get_run(ident) if ident else None
                if current:
                    for key, value in current.get('progress', {}).get('jev', {}).items():
                        jev.stats[key] = max(value, jev.stats.get(key, 0))
                if not current or current['status'] != 'completed':
                    context = copy.deepcopy(TASK_CONTEXT)
                    context['execution_contract'] = execution_contract.authoring_contract()
                    if branch == 'code':
                        context['search_space'] = ('Code-only ablation: evolve Python and expression nodes, graph, output and memory. '
                            'Jev nodes are forbidden and the evaluator rejects them. No external calls or hidden state access. '
                            'All battle logic must be computed by the frozen code.')
                    update(phase='evolution', active_branch=branch)
                    proposer = make_proposer(read_json(output / 'reflection.json'))
                    result = run_evolution(train, validation, jev=jev, proposer=proposer,
                        store=branch_store, run_name=f'pokemon-{branch}-{profile}', seed=17,
                        seed_pipeline=read_json(output / f'seed_{branch}.json'), evaluator=evaluator,
                        task_id='pokemon_singles_' + branch, task_context=context, task_contract=contract,
                        reflection_batch_size=rules['reflection_batch_games'],
                        skip_perfect_score=rules.get('skip_perfect_score', False),
                        evolution_rounds=rounds, resume_run_id=ident)
                    ident = result['run_id']
                    current = store.get_run(ident)
                    if current['status'] != 'completed':
                        update(status='paused')
                        return state
                branch_state.update(rounds_completed=current['result']['rounds_completed'],
                                    result=current['result'])
                update(phase='freeze', rounds_completed=state['branches'].get('mixed', {}).get('rounds_completed', 0), **usage())
                path = output / f'frozen_{branch}.json'
                artifact = read_json(path)
                if artifact is None:
                    artifact = freeze_run(store, ident)
                    atomic_write_json(path, artifact)
                else:
                    from auto_jev.frozen import validate_artifact
                    validate_artifact(artifact, jev, evaluator=evaluator)
                    if artifact['spec_hash'] != current['result']['best_hash']:
                        raise ValueError('Frozen candidate no longer matches completed selection')
                artifacts[branch] = artifact
                branch_state['frozen_path'] = str(path)
                update(**({'frozen_path': str(path)} if branch == 'mixed' else {}))

            # BOTH selections and freezes precede reading any test episode here.
            if sha(output / 'test.json') != manifest['input_hashes']['test.json']:
                raise ValueError('Sealed test input changed')
            test = read_json(output / 'test.json')
            if {e['id'] for e in test} & {e['id'] for e in train + validation}:
                raise ValueError('Test IDs overlap search data')
            update(phase='test', active_branch=None)
            policies = [('evolved', 'mixed', artifacts['mixed']['spec'], True),
                        ('initial', 'mixed', read_json(output / 'seed_mixed.json'), False),
                        ('code_evolved', 'code', artifacts['code']['spec'], True),
                        ('code_initial', 'code', read_json(output / 'seed_code.json'), False)]
            summaries = {}
            for label, branch, spec, frozen in policies:
                jev = clients[branch]
                ident = state['branches'][branch]['run_id']
                candidate = spec_hash(spec)
                store.save_candidate(ident, spec, candidate, origin='test_baseline' if not frozen else 'selected')
                verify_task_contract(artifacts[branch]['task_contract'], evaluator=evaluators[branch])
                rows = []
                for episode in test:
                    update(current_test_policy=label, current_test_episode=episode['id'])
                    path = store._trace_path(ident, 'holdout', candidate, episode['id'])
                    result = read_json(path)
                    if result is None or result.get('status') != 'completed':
                        try:
                            result = (evaluate_frozen(artifacts[branch], [episode], jev,
                                      evaluator=evaluators[branch])[0] if frozen else
                                      evaluators[branch](spec, episode, jev, capture_traces=True))
                        except Exception as exc:
                            partial = getattr(exc, 'partial_result', None)
                            if partial:
                                store.save_trace(ident, 'holdout', candidate,
                                                 episode['id'] + '-failed-' + uuid.uuid4().hex[:8], partial)
                            raise
                        result['provider_totals'] = dict(jev.stats)
                        store.save_trace(ident, 'holdout', candidate, episode['id'], result)
                    for key, value in result.get('provider_totals', {}).items():
                        jev.stats[key] = max(value, jev.stats.get(key, 0))
                    # Reindex complete JSON after a crash between JSON and index publication.
                    try:
                        store.load_trace_summary(ident, 'holdout', candidate, episode['id'])
                    except ValueError:
                        store.save_trace(ident, 'holdout', candidate, episode['id'], result)
                    row = game_metrics(result, candidate, label)
                    rows.append(row)
                    previous = [r for r in store.list_holdout(ident) if r.get('policy') == label and r.get('episode_id') == episode['id']]
                    if not previous:
                        store.append_holdout(ident, row)
                    elif previous != [row]:
                        raise ValueError('Archived holdout metrics changed')
                    update(**usage())
                summaries[label] = {'candidate': candidate, 'run_id': ident, 'games': len(rows),
                    'mean_score': statistics.mean(r['score'] for r in rows),
                    'wins': sum(r['winner'] == 'player' for r in rows),
                    'losses': sum(r['winner'] == 'opponent' for r in rows),
                    'draws': sum(r['winner'] == 'draw' for r in rows),
                    'decision_latency_ms': percentiles([v for r in rows for v in r['decision_times_ms']]),
                    'decision_latency_uncached_ms': percentiles([v for r in rows for v in r['uncached_decision_times_ms']]),
                    'trace_live_jev_calls': sum(r['trace_live_jev_calls'] for r in rows),
                    'trace_jev_cache_hits': sum(r['trace_jev_cache_hits'] for r in rows),
                    'episodes': rows}
            summary = {'schema': 'auto_jev.pokemon.comparison.v1', 'created_at': now_iso(),
                       'policies': summaries, 'branches': state['branches'], **usage(),
                       'interpretation': 'Paired preset-team experiment, not evidence of general competitive strength. '
                         'Wall time includes sandbox startup and any recorded Jev cache hits. '
                         'Reflection round targets are matched; actual completed rounds, stopping reasons, '
                         'evaluations and model usage are reported per branch.'}
            atomic_write_json(output / 'summary.json', summary)
            update(status='completed', phase='completed', test_released=True,
                   summary_path=str(output / 'summary.json'), completed_at=now_iso(),
                   current_test_episode=None, current_test_policy=None, **usage())
            return state
        except BaseException as exc:
            update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:1000]}', **usage())
            raise
        finally:
            stop.set()
            thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/pokemon_pilot')
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--profile', choices=('pilot', 'expanded'), default='pilot')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('--rounds must be positive')
    result = prepare(args.output, profile=args.profile) if args.prepare_only else run(
        args.output, rounds=args.rounds, resume=args.resume, preflight_only=args.preflight_only, profile=args.profile)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
