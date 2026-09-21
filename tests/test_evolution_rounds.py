import json
import pickle

import pytest

from auto_jev.evolution import freeze_run, run_evolution
from auto_jev.providers import JevClient, JevProviderError, ProposerError
from auto_jev.storage import RunStore


def spec(output='0'):
    return {'version': 2, 'name': 'round counting', 'jev_model': 'typesafe-ai/jev',
            'nodes': [], 'output': output}


def evaluate(pipeline, episode, jev, capture_traces=True):
    jev.stats['calls'] += 1
    return {'episode_id': episode['id'], 'score': float(pipeline['output']),
            'traces': [{'trace': [{'id': 'evidence', 'output': 'COMPLETE_EVIDENCE'}]}],
            'trades': [{'fill': 1}], 'equity_curve': [10, 11]}


class Proposer:
    def __init__(self, mode='same'):
        self.mode = mode
        self.config = {}
        self.stats = {'calls': 0}
        self.batches = []

    def __call__(self, prompt):
        self.stats['calls'] += 1
        payload = json.loads(prompt)
        feedback = payload['training_feedback']['pipeline']
        self.batches.append([row['episode_id'] for row in feedback])
        assert feedback[0]['result']['traces'][0]['trace'][0]['output'] == 'COMPLETE_EVIDENCE'
        if self.mode == 'invalid':
            return 'this is invalid JSON'
        if self.mode == 'error':
            raise ProposerError('provider is unavailable')
        parent = payload['parent']
        if self.mode == 'improve':
            parent['output'] = str(int(parent['output']) + 1)
        elif self.mode == 'sequence':
            parent['output'] = str(self.stats['calls'])
        return json.dumps(parent)


def setup_run(tmp_path, count=3):
    store = RunStore(tmp_path)
    train = [{'id': f'train-{i}', 'data': [i]} for i in range(count)]
    validation = [{'id': 'validation', 'data': ['never reflect']}]
    return store, train, validation


def run(store, train, validation, proposer, **kwargs):
    return run_evolution(train, validation, jev=kwargs.pop('jev', JevClient(mock=True, cache_namespace='test')),
                         proposer=proposer, store=store, task_id='round_test',
                         seed_pipeline=spec(), evaluator=kwargs.pop('evaluator', evaluate),
                         reflection_batch_size=kwargs.pop('reflection_batch_size', 1), **kwargs)


@pytest.mark.parametrize('mode', ['same', 'invalid'])
def test_fifty_actual_proposals_and_native_epoch_coverage(tmp_path, mode):
    store, train, validation = setup_run(tmp_path, 40)
    proposer = Proposer(mode)
    summary = run(store, train, validation, proposer, evolution_rounds=50)
    assert proposer.stats['calls'] == summary['rounds_completed'] == 50
    assert summary['rounds_accepted'] == 0
    assert summary['total_metric_calls'] == 101
    assert summary['goal_reached'] and summary['stop_reason'] == 'round_target'
    assert len({batch[0] for batch in proposer.batches[:40]}) == 40
    batches = store.list_events(summary['run_id'], 'round_batch')
    assert [event['payload']['episode_ids'] for event in batches] == proposer.batches
    completed = store.list_events(summary['run_id'], 'round_completed')
    assert [event['payload']['completed'] for event in completed] == list(range(1, 51))
    progress = store.get_run(summary['run_id'])['progress']
    assert progress['rounds_target'] == progress['rounds_completed'] == 50
    assert progress['rounds_accepted'] == 0


def test_round_commits_only_after_accepted_full_validation(tmp_path):
    store, train, validation = setup_run(tmp_path)
    validation.append({'id': 'validation-two'})
    seen = []

    def evaluator(pipeline, episode, jev, capture_traces=True):
        if pipeline['output'] == '1' and episode['id'].startswith('validation'):
            current = store.get_run(store.list_runs()[0]['run_id'])['progress']
            assert current['rounds_completed'] == current['rounds_accepted'] == 0
            assert current['status'] == 'evaluating'
            assert current['current_episode'] == episode['id']
            seen.append(episode['id'])
        return evaluate(pipeline, episode, jev)

    summary = run(store, train, validation, Proposer('improve'), evolution_rounds=1, evaluator=evaluator)
    assert seen == ['validation', 'validation-two']
    assert summary['rounds_completed'] == summary['rounds_accepted'] == 1


def test_metric_guard_pauses_before_round_goal_and_legacy_default_still_stops(tmp_path):
    store, train, validation = setup_run(tmp_path)
    guarded = run(store, train, validation, Proposer(), evolution_rounds=50, max_metric_calls=5)
    assert guarded['rounds_completed'] == 2 and not guarded['goal_reached']
    assert guarded['stop_reason'] == 'metric_budget'
    assert store.get_run(guarded['run_id'])['status'] == 'paused'
    with pytest.raises(ValueError, match='completed'):
        freeze_run(store, guarded['run_id'])
    legacy = run(store, train, validation, Proposer())
    assert legacy['rounds_target'] is None and legacy['total_metric_calls'] == 25
    assert store.get_run(legacy['run_id'])['status'] == 'completed'


def test_system_failure_is_not_counted_and_does_not_retry_model(tmp_path):
    store, train, validation = setup_run(tmp_path)
    proposer = Proposer('error')
    with pytest.raises(ProposerError, match='unavailable'):
        run(store, train, validation, proposer, evolution_rounds=50)
    saved = store.get_run(store.list_runs()[0]['run_id'])
    assert proposer.stats['calls'] == 1
    assert saved['progress']['rounds_completed'] == 0 and saved['status'] == 'failed'
    assert not store.list_events(saved['run_id'], 'round_completed')
    assert saved['jev_metadata']['cache_namespace'] == 'test'


@pytest.mark.parametrize('exception_type', [SystemExit, GeneratorExit, KeyboardInterrupt])
def test_baseexception_during_full_validation_propagates_without_completing_round(tmp_path, exception_type):
    store, train, validation = setup_run(tmp_path)
    interruption = exception_type('stop during validation')

    def interrupted(pipeline, episode, jev, capture_traces=True):
        if pipeline['output'] == '1' and episode['id'] == 'validation':
            raise interruption
        return evaluate(pipeline, episode, jev)

    proposer = Proposer('improve')
    with pytest.raises(exception_type) as caught:
        run(store, train, validation, proposer, evolution_rounds=1, evaluator=interrupted)
    assert caught.value is interruption
    saved = store.get_run(store.list_runs()[0]['run_id'])
    assert proposer.stats['calls'] == 1
    assert saved['status'] == 'failed'
    assert saved['progress']['rounds_completed'] == saved['progress']['rounds_accepted'] == 0
    assert not store.list_events(saved['run_id'], 'round_completed')


@pytest.mark.parametrize('skip_perfect_score', [False, True])
def test_empty_gepa_iteration_fails_instead_of_looping_without_round_progress(tmp_path, monkeypatch, skip_perfect_score):
    from gepa.proposer.reflective_mutation.reflective_mutation import ReflectiveMutationProposer
    monkeypatch.setattr(ReflectiveMutationProposer, 'propose', lambda self, state: [])
    store, train, validation = setup_run(tmp_path)
    proposer = Proposer()
    with pytest.raises(RuntimeError, match='without a reflection proposal response'):
        run(store, train, validation, proposer, evolution_rounds=50, skip_perfect_score=skip_perfect_score)
    saved = store.get_run(store.list_runs()[0]['run_id'])
    assert proposer.stats['calls'] == saved['progress']['rounds_completed'] == 0
    assert len(store.list_events(saved['run_id'], 'round_started')) == 1
    assert saved['status'] == 'failed'


def test_unrelated_exception_being_handled_by_caller_does_not_fail_a_clean_round(tmp_path):
    store, train, validation = setup_run(tmp_path)
    try:
        raise ValueError('unrelated caller recovery')
    except ValueError:
        summary = run(store, train, validation, Proposer(), evolution_rounds=1)
    assert summary['rounds_completed'] == 1 and summary['goal_reached']


def test_resume_restores_sampler_selector_counters_and_provider_stats(tmp_path):
    store, train, validation = setup_run(tmp_path / 'resumed', 7)
    first = Proposer('improve')
    part = run(store, train, validation, first, evolution_rounds=3, seed=71)
    old_stats = store.get_run(part['run_id'])['progress']['jev']['calls']
    second = Proposer('improve')
    final = run(store, train, validation, second, evolution_rounds=8, seed=71, resume_run_id=part['run_id'])
    assert second.stats['calls'] == final['rounds_completed'] == final['rounds_accepted'] == 8
    assert len(second.batches) == 5
    progress = store.get_run(final['run_id'])['progress']
    assert progress['jev']['calls'] == old_stats + 15  # no redundant seed validation
    assert store.list_events(final['run_id'], 'resume_seed_reused')
    baseline_store, baseline_train, baseline_val = setup_run(tmp_path / 'baseline', 7)
    baseline = Proposer('improve')
    uninterrupted = run(baseline_store, baseline_train, baseline_val, baseline, evolution_rounds=8, seed=71)
    assert first.batches + second.batches == baseline.batches
    assert final['best_hash'] == uninterrupted['best_hash']
    resumed_parents = [e['payload']['candidate'] for e in store.list_events(final['run_id'], 'parent_selected')]
    expected_parents = [e['payload']['candidate'] for e in baseline_store.list_events(uninterrupted['run_id'], 'parent_selected')]
    assert resumed_parents == expected_parents


def test_resume_replays_journaled_response_after_validation_failure(tmp_path):
    store, train, validation = setup_run(tmp_path)

    def failing(pipeline, episode, jev, capture_traces=True):
        if pipeline['output'] == '1' and episode['id'] == 'validation':
            raise JevProviderError('interrupted full validation')
        return evaluate(pipeline, episode, jev)

    first = Proposer('improve')
    with pytest.raises(JevProviderError, match='interrupted'):
        run(store, train, validation, first, evolution_rounds=1, evaluator=failing)
    failed = store.get_run(store.list_runs()[0]['run_id'])
    assert failed['progress']['rounds_completed'] == 0
    second = Proposer('error')  # must never be called while replaying returned proposal
    resumed = run(store, train, validation, second, evolution_rounds=1, resume_run_id=failed['run_id'])
    assert resumed['rounds_completed'] == resumed['rounds_accepted'] == 1
    assert second.batches == [] and second.stats['calls'] == 1
    assert store.list_events(resumed['run_id'], 'reflection_replayed')


def test_resume_preserves_random_parent_selection_with_competing_instance_frontiers(tmp_path):
    store, train, validation = setup_run(tmp_path / 'resumed')
    validation.append({'id': 'validation-two'})

    def competing(pipeline, episode, jev, capture_traces=True):
        result = evaluate(pipeline, episode, jev)
        if episode['id'].startswith('validation'):
            result['score'] = float(int(pipeline['output']) % 2 == int(episode['id'] == 'validation-two'))
        return result

    first = run(store, train, validation, Proposer('sequence'), evolution_rounds=3, seed=17, evaluator=competing)
    run(store, train, validation, Proposer('sequence'), evolution_rounds=8, seed=17,
        evaluator=competing, resume_run_id=first['run_id'])
    baseline_store = RunStore(tmp_path / 'baseline')
    baseline = run(baseline_store, train, validation, Proposer('sequence'), evolution_rounds=8,
                   seed=17, evaluator=competing)
    actual = [e['payload'] for e in store.list_events(first['run_id'], 'parent_selected')]
    expected = [e['payload'] for e in baseline_store.list_events(baseline['run_id'], 'parent_selected')]
    assert any(len(row['probabilities']) > 1 for row in actual)
    assert [row['candidate'] for row in actual] == [row['candidate'] for row in expected]
    assert [row['probabilities'] for row in actual] == [row['probabilities'] for row in expected]


def test_resume_initial_validation_without_checkpoint_reuses_finished_episodes(tmp_path):
    store, train, validation = setup_run(tmp_path)
    validation.append({'id': 'validation-two'})
    calls = []

    def interrupted(pipeline, episode, jev, capture_traces=True):
        calls.append(episode['id'])
        if episode['id'] == 'validation-two':
            raise JevProviderError('initial validation interrupted')
        return evaluate(pipeline, episode, jev)

    first = Proposer()
    with pytest.raises(JevProviderError):
        run(store, train, validation, first, evolution_rounds=1, evaluator=interrupted)
    ident = store.list_runs()[0]['run_id']
    assert not (store.run_dir(ident) / 'gepa' / 'gepa_state.bin').exists()
    assert first.stats['calls'] == 0
    resumed_calls = []

    def restored(pipeline, episode, jev, capture_traces=True):
        resumed_calls.append(episode['id'])
        return evaluate(pipeline, episode, jev)

    result = run(store, train, validation, Proposer(), evolution_rounds=1,
                 evaluator=restored, resume_run_id=ident)
    assert result['rounds_completed'] == 1
    assert 'validation' not in resumed_calls
    assert resumed_calls[0] == 'validation-two'
    assert store.list_events(ident, 'resume_seed_reused')


def test_resume_rejects_changed_episode_order_or_encoding(tmp_path):
    store, train, validation = setup_run(tmp_path)
    first = run(store, train, validation, Proposer(), evolution_rounds=1)
    with pytest.raises(ValueError, match='order/split'):
        run(store, train[::-1], validation, Proposer(), evolution_rounds=2, resume_run_id=first['run_id'])
    encoded = Proposer()
    encoded.config['reflection_encoding'] = 'lossless_dag'
    with pytest.raises(ValueError, match='encoding'):
        run(store, train, validation, encoded, evolution_rounds=2, resume_run_id=first['run_id'])


def test_checkpoint_validation_outputs_are_light_but_archive_is_complete(tmp_path):
    store, train, validation = setup_run(tmp_path)
    result = run(store, train, validation, Proposer('improve'), evolution_rounds=1)
    checkpoint = store.run_dir(result['run_id']) / 'gepa' / 'gepa_state.bin'
    raw = checkpoint.read_bytes()
    assert b'COMPLETE_EVIDENCE' not in raw
    assert b'equity_curve' not in raw
    assert pickle.loads(raw)['adapter_state']['completed'] == 1
    trace = store.load_trace(result['run_id'], 'validation', result['best_hash'], 'validation')
    assert trace['traces'][0]['trace'][0]['output'] == 'COMPLETE_EVIDENCE'
    assert trace['trades'] == [{'fill': 1}] and trace['equity_curve'] == [10, 11]


@pytest.mark.parametrize('target', [0, -1, True, 1.5])
def test_invalid_round_targets_fail_before_model(tmp_path, target):
    store, train, validation = setup_run(tmp_path)
    proposer = Proposer()
    with pytest.raises(ValueError, match='positive integer'):
        run(store, train, validation, proposer, evolution_rounds=target)
    assert proposer.stats['calls'] == 0


def perfect_train(pipeline, episode, jev, capture_traces=True):
    result = evaluate(pipeline, episode, jev)
    result['score'] = float(episode['id'].startswith('train-'))
    return result


def test_native_perfect_skips_stop_after_same_candidate_covers_training(tmp_path):
    store, train, validation = setup_run(tmp_path, 5)
    proposer = Proposer('error')  # No reflection should be dispatched.
    summary = run(store, train, validation, proposer, evolution_rounds=50,
                  reflection_batch_size=3, skip_perfect_score=True, evaluator=perfect_train)
    assert summary['rounds_completed'] == summary['rounds_accepted'] == proposer.stats['calls'] == 0
    assert summary['skipped_batches'] == 2  # Native sampler pads a 5-episode epoch to 6.
    assert summary['total_metric_calls'] == 7
    assert summary['stop_reason'] == 'training_perfect' and summary['converged']
    assert not summary['goal_reached'] and summary['rounds_target'] == 50
    assert summary['best_score'] == 0  # Training convergence does not promise validation success.
    saved = store.get_run(summary['run_id'])
    assert saved['status'] == 'completed'
    assert saved['config']['skip_perfect_score'] and saved['config']['perfect_score'] == 1.0
    assert saved['progress']['perfect_training_coverage'] == {summary['best_hash']: 5}
    events = store.list_events(summary['run_id'], 'round_skipped')
    assert {i for e in events for i in e['payload']['episode_ids']} == {e['id'] for e in train}
    assert [e['payload']['skipped_batches'] for e in events] == [1, 2]
    assert all(e['payload']['number'] == 1 and e['payload']['scores'] == [1, 1, 1] for e in events)
    assert not store.list_events(summary['run_id'], 'round_completed')
    assert not store.list_events(summary['run_id'], 'round_failed')
    assert not store.list_events(summary['run_id'], 'reflection_dispatch')
    assert len(store.list_events(summary['run_id'], 'training_perfect')) == 1
    assert freeze_run(store, summary['run_id'])['spec_hash'] == summary['best_hash']


def test_default_does_not_skip_perfect_batches(tmp_path):
    store, train, validation = setup_run(tmp_path)
    proposer = Proposer()
    summary = run(store, train, validation, proposer, evolution_rounds=3, evaluator=perfect_train)
    assert summary['rounds_completed'] == proposer.stats['calls'] == 3
    assert summary['skipped_batches'] == 0 and not summary['converged']
    assert summary['goal_reached']


def hard_episode(pipeline, episode, jev, capture_traces=True):
    result = evaluate(pipeline, episode, jev)
    result['score'] = float(episode['id'].startswith('train-') and episode['id'] != 'train-2')
    return result


@pytest.mark.parametrize('mode', ['same', 'invalid'])
def test_skipped_batches_do_not_reduce_actual_reflection_target(tmp_path, mode):
    store, train, validation = setup_run(tmp_path)
    proposer = Proposer(mode)
    summary = run(store, train, validation, proposer, evolution_rounds=4,
                  skip_perfect_score=True, evaluator=hard_episode)
    assert proposer.stats['calls'] == summary['rounds_completed'] == 4
    assert summary['rounds_accepted'] == 0
    assert summary['skipped_batches'] > 0 and not summary['converged']
    assert proposer.batches == [['train-2']] * 4
    events = store.list_events(summary['run_id'], 'round_skipped')
    assert summary['total_metric_calls'] == 1 + 2 * 4 + len(events)
    assert summary['stop_reason'] == 'round_target' and summary['goal_reached']


def test_skip_checkpoint_resume_preserves_sampler_rng_counters_and_coverage(tmp_path):
    store, train, validation = setup_run(tmp_path / 'resumed')
    part = run(store, train, validation, Proposer(), evolution_rounds=4, max_metric_calls=3,
               skip_perfect_score=True, evaluator=hard_episode)
    assert part['skipped_batches'] == 1 and part['rounds_completed'] == 1
    checkpoint = pickle.loads((store.run_dir(part['run_id']) / 'gepa' / 'gepa_state.bin').read_bytes())
    assert checkpoint['adapter_state']['skipped_batches'] == 1
    assert checkpoint['adapter_state']['perfect_coverage'] == {part['best_hash']: ['train-0']}
    final = run(store, train, validation, Proposer(), evolution_rounds=4,
                skip_perfect_score=True, evaluator=hard_episode, resume_run_id=part['run_id'])
    baseline_store = RunStore(tmp_path / 'baseline')
    baseline = run(baseline_store, train, validation, Proposer(), evolution_rounds=4,
                   skip_perfect_score=True, evaluator=hard_episode)
    assert final['skipped_batches'] == baseline['skipped_batches']
    assert final['total_metric_calls'] == baseline['total_metric_calls']
    assert final['rounds_completed'] == baseline['rounds_completed'] == 4
    for kind in ('round_batch', 'round_skipped', 'parent_selected'):
        actual = [e['payload'] for e in store.list_events(part['run_id'], kind)]
        expected = [e['payload'] for e in baseline_store.list_events(baseline['run_id'], kind)]
        assert actual == expected


def test_returned_proposal_journal_replays_after_skips_and_failure(tmp_path):
    store, train, validation = setup_run(tmp_path)

    def interrupted(pipeline, episode, jev, capture_traces=True):
        result = evaluate(pipeline, episode, jev)
        if episode['id'].startswith('train-') and episode['id'] != 'train-2':
            result['score'] = 1.0
        if pipeline['output'] == '1' and episode['id'] == 'validation':
            raise JevProviderError('interrupted after returned proposal')
        return result

    with pytest.raises(JevProviderError):
        run(store, train, validation, Proposer('improve'), evolution_rounds=1,
            skip_perfect_score=True, evaluator=interrupted)
    ident = store.list_runs()[0]['run_id']
    saved = store.get_run(ident)
    assert saved['progress']['skipped_batches'] == 1
    assert saved['progress']['rounds_completed'] == 0

    def recovered(pipeline, episode, jev, capture_traces=True):
        result = evaluate(pipeline, episode, jev)
        if episode['id'].startswith('train-') and episode['id'] != 'train-2':
            result['score'] = 1.0
        return result

    proposer = Proposer('error')
    summary = run(store, train, validation, proposer, evolution_rounds=1,
                  skip_perfect_score=True, evaluator=recovered, resume_run_id=ident)
    assert summary['rounds_completed'] == summary['rounds_accepted'] == 1
    assert summary['skipped_batches'] == 1 and not proposer.batches
    assert proposer.stats['calls'] == 1
    assert len(store.list_events(ident, 'reflection_replayed')) == 1


def test_skip_resume_cannot_change_setting_and_converged_resume_makes_no_calls(tmp_path):
    store, train, validation = setup_run(tmp_path)
    summary = run(store, train, validation, Proposer(), evolution_rounds=3,
                  skip_perfect_score=True, evaluator=perfect_train)
    with pytest.raises(ValueError, match='skip_perfect_score'):
        run(store, train, validation, Proposer(), evolution_rounds=5,
            evaluator=perfect_train, resume_run_id=summary['run_id'])
    resumed = run(store, train, validation, Proposer('error'), evolution_rounds=5,
                  skip_perfect_score=True, evaluator=perfect_train, resume_run_id=summary['run_id'])
    assert resumed['rounds_completed'] == 0 and not resumed['goal_reached']
    assert resumed['skipped_batches'] == summary['skipped_batches']
    assert resumed['total_metric_calls'] == summary['total_metric_calls']
    assert resumed['converged'] and resumed['stop_reason'] == 'training_perfect'


def rounds_tracker(tmp_path, *, enabled=True):
    from auto_jev.evolution import _EvolutionRounds
    store = RunStore(tmp_path)
    ident = store.create_run('skip callbacks', {})
    return _EvolutionRounds(store, ident, 5, ['a', 'b', 'c', 'd'], skip_perfect_score=enabled)


def start_parent_batch(rounds, parent, indices, scores, iteration=1):
    rounds.on_iteration_start({'iteration': iteration})
    rounds.active.update(parent=parent, candidate_idx=0)
    rounds.on_minibatch_sampled({'iteration': iteration, 'minibatch_ids': indices})
    rounds.parent_evaluated(parent, [rounds.train_ids[i] for i in indices], scores)


def end_skipped(rounds):
    from types import SimpleNamespace
    rounds.on_evaluation_skipped({'iteration': rounds.active['iteration'], 'candidate_idx': 0,
                                 'reason': 'all_scores_perfect', 'scores': rounds.active['scores']})
    rounds.on_iteration_end({'proposal_accepted': False, 'state': SimpleNamespace(total_num_evals=4)})


def test_training_coverage_is_per_candidate_and_nonperfect_observation_invalidates_it(tmp_path):
    rounds = rounds_tracker(tmp_path)
    start_parent_batch(rounds, 'parent-a', [0, 1], [1, 1])
    end_skipped(rounds)
    start_parent_batch(rounds, 'parent-b', [2, 3], [1, 1], 2)
    end_skipped(rounds)
    assert not rounds.stop(None)  # Different policies cannot share a convergence certificate.
    start_parent_batch(rounds, 'parent-a', [0, 2], [-1e9, 1], 3)
    rounds.update_perfect_coverage()
    assert rounds.perfect_coverage['parent-a'] == ['b', 'c']
    start_parent_batch(rounds, 'parent-a', [0, 3], [1, 1], 4)
    end_skipped(rounds)
    assert rounds.stop(None) and rounds.converged_candidate == 'parent-a'
    assert rounds.completed == 0


@pytest.mark.parametrize('change', [
    {'reason': 'no_trajectories'}, {'reason': 'unknown'}, {'scores': [0.5]},
    {'scores': [True]}, {'scores': []}, {'scores': [1, 1]},
    {'scores': [float('inf')]}, {'candidate_idx': 1}, {'iteration': 2},
])
def test_only_a_verified_native_perfect_skip_can_bypass_empty_iteration_error(tmp_path, change):
    from types import SimpleNamespace
    rounds = rounds_tracker(tmp_path)
    start_parent_batch(rounds, 'parent', [0], [1])
    event = {'iteration': 1, 'candidate_idx': 0, 'reason': 'all_scores_perfect', 'scores': [1]}
    event.update(change)
    if event['reason'] == 'all_scores_perfect':
        with pytest.raises(RuntimeError, match='Invalid GEPA'):
            rounds.on_evaluation_skipped(event)
    else:
        rounds.on_evaluation_skipped(event)
    rounds.on_iteration_end({'proposal_accepted': False, 'state': SimpleNamespace(total_num_evals=1)})
    assert rounds.error is not None and rounds.completed == rounds.skipped_batches == 0
    assert not rounds.perfect_coverage and rounds.converged_candidate is None


def test_skip_cannot_discard_a_journaled_reflection_response(tmp_path):
    rounds = rounds_tracker(tmp_path)
    start_parent_batch(rounds, 'parent', [0], [1])
    rounds.journal['pending'] = {'number': 1}
    with pytest.raises(RuntimeError, match='journaled reflection proposal'):
        end_skipped(rounds)
    assert rounds.skipped_batches == rounds.completed == 0


@pytest.mark.parametrize('setting', [None, 1, 'true'])
def test_skip_setting_must_be_boolean(tmp_path, setting):
    store, train, validation = setup_run(tmp_path)
    proposer = Proposer()
    with pytest.raises(ValueError, match='boolean'):
        run(store, train, validation, proposer, evolution_rounds=1, skip_perfect_score=setting)
    assert proposer.stats['calls'] == 0
