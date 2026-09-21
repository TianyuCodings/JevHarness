import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

import auto_jev.evolution as evolution
from auto_jev.providers import JevClient
from auto_jev.storage import RunStore


def candidate():
    return {'version': 2, 'name': 'history fixture', 'jev_model': 'typesafe-ai/jev',
            'nodes': [], 'output': '0'}


class Proposer:
    config = {'reflection_encoding': 'lossless_dag'}

    def __init__(self, invalid=False):
        self.invalid = invalid
        self.stats = {'calls': 0}
        self.metadata = {'kind': 'offline-fixture', 'model': 'claude-opus-5',
                         'effort': 'xhigh', 'actual_models': ['claude-opus-5']}

    def __call__(self, prompt):
        self.stats['calls'] += 1
        return 'invalid JSON' if self.invalid else json.dumps(candidate())


def auditor():
    path = Path(__file__).resolve().parents[1] / '.work/pokemon/audit_reflections_v2.py'
    spec = importlib.util.spec_from_file_location('history_auditor', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_fixture(tmp_path, *, rounds=2, store=None, invalid=False):
    store = store or RunStore(tmp_path / 'runs')
    train = [{'id': 'train-1'}, {'id': 'train-2'}]
    validation = [{'id': 'validation'}]
    counts = {}

    def evaluate(spec, episode, jev, capture_traces=True):
        count = counts[episode['id']] = counts.get(episode['id'], 0) + 1
        return {'episode_id': episode['id'], 'score': 0., 'elapsed_ms': count,
                'traces': [{'obs': {'visible': 'complete'}, 'memory': {'a': [1, 2]},
                            'trace': [{'id': 'feature', 'output': 'same action',
                                       'elapsed_ms': count, 'response': {'cache_hit': count > 1}}]}]}

    proposer = Proposer(invalid)
    result = evolution.run_evolution(train, validation, jev=JevClient(mock=True), proposer=proposer,
        store=store, task_id='history-test', evaluator=evaluate, seed_pipeline=candidate(),
        evolution_rounds=rounds, reflection_batch_size=2)
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    for name, value in [('train.json', train), ('state.json', {'run_ids': [result['run_id']]}),
                        ('reflection.json', {'model': 'claude-opus-5', 'effort': 'xhigh'})]:
        (experiment / name).write_text(json.dumps(value))
    return store, result, experiment, proposer


def test_repeated_evaluations_keep_every_exact_result_and_each_reflection_remains_auditable(tmp_path):
    store, result, experiment, _ = run_fixture(tmp_path)
    ident = result['run_id']
    directory = store.run_dir(ident)
    histories = store.list_events(ident, 'evaluation_archived')
    assert len(histories) == 9  # Initial validation, then 2 * (2 parent + 2 child).
    assert len(list((directory / 'evaluation_history').glob('*.json'))) == 9
    train_one = [e['payload'] for e in histories if e['payload']['episode_id'] == 'train-1']
    assert len(train_one) == 4
    assert [json.loads((directory / r['path']).read_text())['elapsed_ms'] for r in train_one] == [1, 2, 3, 4]
    for record in train_one:
        raw = (directory / record['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record['sha256']
        assert len(raw) == record['bytes']
    latest = store.load_trace(ident, 'train', result['best_hash'], 'train-1')
    assert latest['elapsed_ms'] == 4
    assert json.loads((directory / train_one[0]['path']).read_text()) != latest
    report = auditor().audit(experiment, store.root)
    assert report['summary']['audit_passed'] and report['summary']['reflections'] == 2
    assert report['summary']['traces_checked'] == report['summary']['nodes_checked'] == 4
    assert not report['test_data_read']
    for row in report['runs'][0]['reflections']:
        assert row['verification_status'] == 'verified'
        assert not row['missing_evidence'] and row['integrity_passed']
        assert row['checks']['lossless_feedback_strict_equality']


def test_rejected_response_still_has_per_response_actual_model_metadata(tmp_path):
    store, result, experiment, proposer = run_fixture(tmp_path, rounds=1, invalid=True)
    event = store.list_events(result['run_id'], 'reflection_complete')[0]['payload']
    assert event['proposer'] == proposer.metadata
    assert event['stats']['calls'] == 1
    assert not store.list_events(result['run_id'], 'proposal')
    row = auditor().audit(experiment, store.root)['runs'][0]['reflections'][0]
    assert row['audit_passed'] and row['actual_model_confirmed']
    assert row['response_status'] == 'returned_with_completion_metadata'
    assert row['model_evidence'] == 'per_response_reflection_complete_event'
    assert not row['actual_effort_confirmed']


def archive(store, ident, payload):
    return evolution._archive_evaluation(store, ident, 'candidate-hash', 'episode', 'train', payload,
                                         capture_traces=True, round_number=1)


def test_history_is_lossless_beyond_ui_depth_limit_and_existing_object_is_never_replaced(tmp_path):
    store = RunStore(tmp_path)
    ident = store.create_run('history', {})
    payload = {'text': 'COMPLETE_DEEP_EVIDENCE'}
    for _ in range(60):
        payload = {'nested': payload}
    manifest = archive(store, ident, payload)
    path = store.run_dir(ident) / manifest['path']
    first_stat = path.stat()
    assert json.loads(path.read_text()) == payload
    assert archive(store, ident, payload) == manifest
    assert path.stat().st_ino == first_stat.st_ino
    assert path.stat().st_mtime_ns == first_stat.st_mtime_ns
    assert len(store.list_events(ident, 'evaluation_archived')) == 2
    path.write_text('{"corrupted":true}')
    with pytest.raises(ValueError, match='content hash'):
        archive(store, ident, payload)
    assert path.read_text() == '{"corrupted":true}'
    assert len(store.list_events(ident, 'evaluation_archived')) == 2
    assert not list(path.parent.glob('.evaluation-*'))


@pytest.mark.parametrize('stage', ['publish', 'event'])
def test_history_write_failure_is_fatal_before_reflection_or_success_record(tmp_path, monkeypatch, stage):
    class Store(RunStore):
        def append_event(self, run_id, kind, payload):
            if stage == 'event' and kind == 'evaluation_archived':
                raise OSError('history event failed')
            return super().append_event(run_id, kind, payload)

    store = Store(tmp_path / 'runs')
    if stage == 'publish':
        def failed_link(*args, **kwargs):
            raise OSError('history publication failed')
        monkeypatch.setattr(evolution.os, 'link', failed_link)
    with pytest.raises(OSError, match='history'):
        run_fixture(tmp_path, rounds=1, store=store)
    ident = store.list_runs()[0]['run_id']
    assert store.get_run(ident)['status'] == 'failed'
    assert not store.list_events(ident, 'reflection_dispatch')
    assert not store.list_events(ident, 'evaluation_archived')
    assert not store.list_evaluations(ident)
    assert not list((store.run_dir(ident) / 'evaluation_history').glob('.evaluation-*'))


def rewrite_first_input_without_history(store, run_id):
    """Build a valid legacy archive, without inventing immutable evidence."""
    from auto_jev.trace_codec import pack_json
    directory = store.run_dir(run_id)
    events = store.list_events(run_id)
    event = next(e for e in events if e['kind'] == 'reflection_input')
    manifest = event['payload']
    full = json.loads((directory / manifest['full_path']).read_text())
    for entry in full['training_feedback']['pipeline']:
        entry.pop('evaluation_history')
    encoded = {**full, 'training_feedback': pack_json(full['training_feedback'])}
    for value, path_key, sha_key, bytes_key in [(full, 'full_path', 'full_sha256', 'full_bytes'),
                                               (encoded, 'path', 'sha256', 'prompt_bytes')]:
        raw = auditor().canonical(value)
        (directory / manifest[path_key]).write_bytes(raw)
        manifest[sha_key], manifest[bytes_key] = hashlib.sha256(raw).hexdigest(), len(raw)
    # Input-only snapshot: no falsely rebound dispatch or model response metadata.
    prefix = events[:events.index(event) + 1]
    prefix = [e for e in prefix if e['kind'] != 'evaluation_archived']
    (directory / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in prefix))


def test_legacy_missing_history_is_missing_evidence_not_a_claim_of_dag_corruption(tmp_path):
    store, result, experiment, _ = run_fixture(tmp_path, rounds=1)
    rewrite_first_input_without_history(store, result['run_id'])
    report = auditor().audit(experiment, store.root)
    row = report['runs'][0]['reflections'][0]
    assert row['integrity_passed'] and row['checks']['lossless_feedback_strict_equality']
    assert row['verification_status'] == 'missing_evidence' and not row['audit_passed']
    assert row['errors'] == [] and len(row['missing_evidence']) == 2
    assert not report['summary']['audit_passed']
    assert report['summary']['missing_evidence_reflections'] == 1


@pytest.mark.parametrize('missing', ['object', 'prior_event'])
def test_auditor_requires_both_the_history_object_and_event_before_reflection(tmp_path, missing):
    store, result, experiment, _ = run_fixture(tmp_path, rounds=1)
    directory = store.run_dir(result['run_id'])
    events = store.list_events(result['run_id'])
    archive_event = next(e for e in events if e['kind'] == 'evaluation_archived'
                         and e['payload']['split'] == 'train')
    if missing == 'object':
        (directory / archive_event['payload']['path']).unlink()
    else:
        events.remove(archive_event)
        events.append(archive_event)  # A later event cannot prove the earlier input's provenance.
        (directory / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    row = auditor().audit(experiment, store.root)['runs'][0]['reflections'][0]
    assert row['verification_status'] == 'missing_evidence' and not row['audit_passed']
    assert row['integrity_passed'] and row['missing_evidence']


def test_auditor_detects_history_tampering_even_when_latest_trace_is_unchanged(tmp_path):
    store, result, experiment, _ = run_fixture(tmp_path, rounds=1)
    directory = store.run_dir(result['run_id'])
    event = next(e['payload'] for e in store.list_events(result['run_id'], 'evaluation_archived')
                 if e['payload']['split'] == 'train')
    path = directory / event['path']
    value = json.loads(path.read_text())
    value['score'] = 1.
    path.write_bytes(auditor().canonical(value))
    row = auditor().audit(experiment, store.root)['runs'][0]['reflections'][0]
    assert not row['integrity_passed'] and not row['audit_passed']
    assert row['verification_status'] == 'failed' and not row['missing_evidence']
    assert any(error.endswith('history_sha256') for error in row['errors'])


def test_history_refuses_symlinked_directory(tmp_path):
    store = RunStore(tmp_path / 'runs')
    ident = store.create_run('history', {})
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (store.run_dir(ident) / 'evaluation_history').symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        archive(store, ident, {'complete': True})
    assert not list(foreign.iterdir())
