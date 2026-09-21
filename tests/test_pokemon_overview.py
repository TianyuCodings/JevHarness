import builtins
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auto_jev.storage import RunStore, atomic_write_json
from examples.pokemon.demo import create_app


@pytest.fixture
def archive(tmp_path):
    store = RunStore(tmp_path / 'runs')
    hashes = [str(i) * 64 for i in range(6)]
    train = [{'id': f'train-{i}', 'player_team_id': 'tempo', 'opponent_team_id': 'balanced',
              'opponent': 'random', 'seed': [i, 2, 3, 4], 'opponent_seed': i + 20} for i in range(1, 4)]
    evaluation = [{'id': f'eval-{i}', 'player_team_id': 'balanced', 'opponent_team_id': 'tempo',
                   'opponent': 'random', 'seed': [i + 10, 2, 3, 4], 'opponent_seed': i + 30} for i in range(1, 3)]
    config = {'seed_pipeline_hash': hashes[0], 'evolution_rounds': 5, 'reflection': {'batch_size': 2},
              'episodes': [{'id': e['id'], 'split': split} for split, rows in [('train', train), ('validation', evaluation)] for e in rows],
              'data_hashes': {e['id']: hashlib.sha256(json.dumps(e, sort_keys=True, allow_nan=False).encode()).hexdigest()
                              for e in train + evaluation}}
    run = store.create_run('mixed', config)
    other = store.create_run('PRIVATE_CANCELLED_BRANCH', {'private': 'DO_NOT_READ'})
    state = tmp_path / 'state.json'
    atomic_write_json(state, {'run_id': run, 'run_ids': [run, other], 'code_run_id': other,
                             'active_branch': 'code', 'test_released': True, 'status': 'PRIVATE_GLOBAL_STATUS'})
    atomic_write_json(tmp_path / 'manifest.json', {'profile': 'expanded', 'split_counts': {'train': 3, 'validation': 2, 'test': 99}})
    atomic_write_json(tmp_path / 'rules.json', {'reflection_batch_games': 2, 'skip_perfect_score': True})
    atomic_write_json(tmp_path / 'train.json', train)
    atomic_write_json(tmp_path / 'validation.json', evaluation)
    atomic_write_json(tmp_path / 'test.json', {'secret': 'SEALED_INPUT'})
    atomic_write_json(tmp_path / 'summary.json', {'secret': 'SEALED_OUTCOME'})
    parent_indices = [None, 0, 1, 1, 3, 1]
    accepted = {1, 3, 5}
    for index, ident in enumerate(hashes):
        spec = {'version': 3, 'name': f'Policy {index}', 'jev_model': 'typesafe-ai/jev',
                'nodes': [{'id': 'feature', 'kind': 'python', 'depends_on': [],
                           'source': 'def run(obs, nodes, memory):\n    return 1\n'}],
                'output': "nodes['feature']"}
        store.save_candidate(run, spec, ident, parents=[] if not index else [hashes[parent_indices[index]]],
                             origin='accepted' if index in accepted or not index else 'proposal',
                             gepa_idx=index if index in accepted or not index else None)
    events = []
    for number in range(1, 6):
        clock = number * 10
        parent, child = hashes[parent_indices[number]], hashes[number]
        events += [{'timestamp': clock, 'kind': 'round_started', 'payload': {'number': number, 'iteration': number + 2}},
                   {'timestamp': clock, 'kind': 'parent_selected', 'payload': {'candidate': parent, 'gepa_idx': 0,
                     'probabilities': {'0': 1.}, 'candidate_hashes': {'0': parent}, 'frontier': {'0': [0]}}},
                   {'timestamp': clock, 'kind': 'reflection_input', 'payload': {'sha256': str(number) * 64,
                     'path': f'reflections/{number:04d}.prompt.json', 'prompt_bytes': 1000, 'full_bytes': 2000,
                     'parent': parent, 'episode_ids': ['train-1', 'train-2'], 'trace_count': 20, 'node_count': 80}},
                   {'timestamp': clock + 1, 'kind': 'proposal', 'payload': {'candidate': child, 'parent': parent}},
                   {'timestamp': clock + 4, 'kind': 'round_completed', 'payload': {'number': number, 'parent': parent,
                     'episode_ids': ['train-1', 'train-2'], 'scores': [1, 0], 'valid': True, 'accepted': number in accepted}}]
        for index, score in enumerate([1, 1] if number in accepted else [1, 0]):
            store.append_evaluation(run, {'candidate': child, 'episode_id': f'train-{index + 1}', 'split': 'train',
                'score': score, 'status': 'ok', 'timestamp': clock + 2 + index})
    events.append({'timestamp': 55, 'kind': 'reflection_complete', 'payload': {'proposer': {'model': 'claude-opus-5', 'effort': 'xhigh'}}})
    (store.run_dir(run) / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    for index in (0, 1, 3, 5):
        for episode in evaluation:
            score = 0 if index == 0 and episode['id'] == 'eval-1' else 1
            store.append_evaluation(run, {'candidate': hashes[index], 'episode_id': episode['id'], 'split': 'validation',
                'score': score, 'status': 'ok', 'evaluation_history': {'sha256': str(index) * 64,
                    'path': f'evaluation_history/{index}.json', 'bytes': 123}})
    # Duplicate observations count once; an error is neither a win nor a loss.
    for score, status in [(0, 'ok'), (1, 'ok')]:
        store.append_evaluation(run, {'candidate': hashes[0], 'episode_id': 'train-1', 'split': 'train', 'score': score, 'status': status})
    store.append_evaluation(run, {'candidate': hashes[0], 'episode_id': 'train-3', 'split': 'train', 'score': -1e9, 'status': 'error'})
    store.update_run(run, status='completed', frozen_hash=hashes[3], result={'best_hash': hashes[3],
        'rounds_completed': 5, 'rounds_accepted': 3, 'skipped_batches': 3})
    return {'store': store, 'state': state, 'run': run, 'other': other, 'hashes': hashes,
            'client': TestClient(create_app(store.root, state))}


def test_overview_contains_all_candidates_actual_branches_and_observed_metrics(archive):
    response = archive['client'].get('/api/overview')
    assert response.status_code == 200
    data = response.json()
    candidates = data['candidates']
    assert len(candidates) == 6
    assert [c['status'] for c in candidates] == ['seed', 'accepted', 'rejected', 'accepted', 'rejected', 'accepted']
    assert [c['round'] for c in candidates] == list(range(6))
    assert candidates[5]['parent_hash'] == candidates[1]['hash']  # Not a fabricated linear chain.
    assert candidates[4]['parent_hash'] == candidates[3]['hash']
    assert [c['hash'] for c in candidates if c['selected']] == [archive['hashes'][3]]
    assert len(data['edges']) == 5 and candidates[3]['spec']['nodes'][0]['kind'] == 'python'
    assert candidates[0]['metrics']['train'] == {'wins': 1, 'losses': 0, 'draws': 0, 'errors': 1,
                                              'completed': 1, 'observed': 2, 'total': 3}
    assert candidates[2]['metrics']['eval']['observed'] == candidates[2]['metrics']['eval']['completed'] == 0
    assert candidates[2]['metrics']['eval']['total'] == 2
    assert candidates[2]['minibatch'] == {'episodes': ['train-1', 'train-2'], 'parent_scores': [1, 0], 'child_scores': [1, 0]}
    assert data['run']['rounds_completed'] == 5 and data['run']['skipped_batches'] == 3
    assert data['run']['reflection_model'] == 'claude-opus-5' and data['run']['reflection_effort'] == 'xhigh'
    assert len(data['run']['reflection_inputs']) == len(data['run']['frontier_selections']) == 5
    assert data['run']['reflection_inputs'][2]['round'] == 3
    assert set(data['splits']) == set(data['episodes']) == {'train', 'eval'}


def test_paired_examples_match_the_same_eval_identity_and_put_failure_to_success_first(archive):
    data = archive['client'].get('/api/overview').json()
    example = data['paired_examples'][0]
    assert example['episode_id'] == 'eval-1' and example['improved']
    assert example['before']['score'] == 0 and example['after']['score'] == 1
    assert example['before']['hash'] == archive['hashes'][0]
    assert example['after']['hash'] == archive['hashes'][3]
    assert example['before']['metadata'] == example['after']['metadata'] == example['metadata']
    assert example['metadata'] == {'player_team_id': 'balanced', 'opponent_team_id': 'tempo',
                                   'opponent': 'random', 'seed': [11, 2, 3, 4], 'opponent_seed': 31}
    assert example['before']['history']['path'] != example['after']['history']['path']
    assert data['paired_examples'][1]['improved'] is False


def test_overview_never_reads_games_private_partitions_other_branch_or_final_summary(archive, monkeypatch):
    original = Path.open
    builtin_open = builtins.open
    reads = []

    def check(path):
        path = Path(path)
        assert path.name not in {'test.json', 'summary.json', 'holdout.json', 'sealed_episodes.json'}
        assert archive['other'] not in path.parts
        assert not any(part in path.parts for part in ('traces', 'evaluation_history', 'reflections'))
        reads.append(path)

    def guarded(path, *args, **kwargs):
        check(path)
        return original(path, *args, **kwargs)

    def guarded_builtin(path, *args, **kwargs):
        if not isinstance(path, int):
            check(path)
        return builtin_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded)
    monkeypatch.setattr(builtins, 'open', guarded_builtin)
    response = archive['client'].get('/api/overview?run_id=' + archive['other'])
    assert response.status_code == 200
    encoded = response.text
    for forbidden in ('SEALED_INPUT', 'SEALED_OUTCOME', 'PRIVATE_CANCELLED_BRANCH', 'PRIVATE_GLOBAL_STATUS',
                      archive['other'], 'test_released', 'code_run_id', '"test"', '"holdout"'):
        assert forbidden not in encoded
    assert reads and archive['client'].post('/api/overview').status_code == 405


def test_tampered_public_episode_cannot_claim_the_original_evaluated_identity(archive):
    path = archive['state'].parent / 'validation.json'
    value = json.loads(path.read_text())
    value[0]['seed'][0] += 1
    path.write_text(json.dumps(value))
    response = archive['client'].get('/api/overview')
    assert response.status_code == 404 and 'input hash' in response.json()['detail']


def test_pending_candidate_is_not_presented_as_rejected(archive):
    path = archive['store'].run_dir(archive['run']) / 'events.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows = [e for e in rows if not (e['kind'] == 'round_completed' and e['payload']['number'] == 4)]
    path.write_text(''.join(json.dumps(e) + '\n' for e in rows))
    data = archive['client'].get('/api/overview').json()
    candidate = next(c for c in data['candidates'] if c['hash'] == archive['hashes'][4])
    assert candidate['status'] == 'pending' and candidate['minibatch'] is None


@pytest.mark.parametrize('score', [None, True, -1e9, 0.25, 2])
def test_invalid_score_never_inflates_completed_denominator(archive, score):
    archive['store'].append_evaluation(archive['run'], {
        'candidate': archive['hashes'][0], 'episode_id': 'train-1', 'split': 'train',
        'status': 'ok', 'score': score})
    metrics = archive['client'].get('/api/overview').json()['candidates'][0]['metrics']['train']
    assert metrics['observed'] == 2 and metrics['errors'] == 2 and metrics['completed'] == 0
    assert metrics['wins'] == metrics['losses'] == metrics['draws'] == 0


def test_overview_before_run_only_presents_public_preparation_counts(tmp_path):
    atomic_write_json(tmp_path / 'manifest.json', {'profile': 'expanded', 'split_counts': {'train': 18, 'validation': 12, 'test': 12}})
    client = TestClient(create_app(tmp_path / 'runs', tmp_path / 'state.json'))
    data = client.get('/api/overview').json()
    assert data['run']['run_id'] is None and data['candidates'] == data['edges'] == data['paired_examples'] == []
    assert data['splits']['train']['total'] == 18 and data['splits']['eval']['total'] == 12
    assert data['state']['phase'] == 'setup' and '"test"' not in json.dumps(data)
