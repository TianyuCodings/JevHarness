"""Reviewed highlights preserve exact evidence and fail closed on misalignment."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from auto_jev.storage import RunStore, TraceRevisionError
from examples.pokemon import highlights as h


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Small protocol fixture; production identifiers remain fixed and gated."""
    store = RunStore(tmp_path / 'runs')
    directory = store.root / h.REVIEWED_RUN
    directory.mkdir()
    (directory / 'run.json').write_text('{}')
    path = store._trace_path(h.REVIEWED_RUN, 'validation', h.REVIEWED_CANDIDATE, h.REVIEWED_EPISODE)
    path.parent.mkdir(parents=True)
    source = 'raise AssertionError("This archived Python must never be executed")'
    monkeypatch.setattr(h, 'REVIEWED_DECISION_SOURCE', hashlib.sha256(source.encode()).hexdigest())
    lines = ['|'] * 185
    markers = {1: 20, 2: 36, 3: 49, 4: 61, 5: 70, 6: 80, 7: 95,
               8: 103, 9: 113, 10: 124, 11: 135, 12: 145, 13: 154, 14: 164, 15: 178}
    for turn, index in markers.items():
        lines[index] = f'|turn|{turn}'
    lines[19] = '|switch|p1a: Dragonite|Dragonite, L50|100/100'
    lines[34] = '|-damage|p1a: Dragonite|32/100|[from] Sandstorm'
    lines[38] = '|switch|p2a: Magnezone|Magnezone, L50|100/100'
    lines[39] = '|move|p1a: Dragonite|Earthquake|p2a: Magnezone'
    lines[44] = '|-heal|p2a: Magnezone|26/100|[from] item: Sitrus Berry'
    lines[51] = '|move|p1a: Dragonite|Earthquake|p2a: Magnezone'
    lines[54] = '|faint|p2a: Magnezone'
    lines[147] = '|switch|p1a: Scizor|Scizor, L50|100/100'
    lines[148] = '|move|p2a: Gastrodon|Ice Beam|p1a: Scizor'
    lines[150] = '|-damage|p1a: Scizor|85/100'
    lines[180] = '|move|p1a: Scizor|X-Scissor|p2a: Gastrodon'
    lines[182] = '|faint|p2a: Gastrodon'
    lines[184] = '|win|AutoJev'
    records = []
    for index, turn in enumerate((1, 2, 3, 4, 5, 6, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)):
        selected_action = 'switch:2' if turn == 12 else 'move:2' if turn == 15 else 'move:3'
        action = {'id': selected_action, 'kind': 'switch' if turn == 12 else 'move'}
        if turn == 12:
            action['pokemon'] = {'ident': 'p1: Scizor', 'species': 'Scizor'}
        else:
            action['move'] = {'name': 'X-Scissor' if turn == 15 else 'Earthquake'}
        phase = 'switch' if index == 6 else 'move'
        history = list(lines[:markers[turn] + 1])
        # The alignment must compare public HP, without dropping other events.
        history[19] = '|switch|p1a: Dragonite|Dragonite, L50|166/166'
        if len(history) > 34:
            history[34] = '|-damage|p1a: Dragonite|53/166|[from] Sandstorm'
        features = {'candidates': [
            {'id': selected_action, 'value': 100, 'hiddenpen': 0, 'unsafe': False},
            {'id': 'other', 'value': 0, 'hiddenpen': 0, 'unsafe': False}],
            'finisher': selected_action if turn in (3, 15) else None}
        probability = {2: .95, 3: .89, 12: .72, 15: .99}.get(turn, .8)
        trace = [
            {'id': 'features', 'kind': 'python', 'status': 'ok', 'output': features},
            {'id': 'plan', 'kind': 'jev', 'status': 'ok', 'state': {'exact_context': 'Keep all of me'},
             'questions': {'switch_now': {'type': 'noul', 'instructions': 'A complete original instruction'}},
             'response': {'answers': {signal: {'type': 'noul', 'noul': .5}
                                     for signal in ('switch_now', 'recover_now', 'setup_now', 'hidden_coverage')}}},
            {'id': 'pick', 'kind': 'jev', 'status': 'ok', 'questions': {'action': {'type': 'choice', 'criteria': {selected_action: 'Exact criterion', 'other': 'Alternative'}}},
             'response': {'answers': {'action': {'type': 'choice', 'choice': selected_action,
                          'probabilities': {selected_action: probability, 'other': 1 - probability}}}}},
            {'id': 'decision', 'kind': 'python', 'status': 'ok', 'source': source, 'output': selected_action},
        ]
        records.append({'turn': turn, 'phase': phase, 'request_id': index + 1, 'status': 'ok',
                        'observation': {'turn': turn, 'phase': phase, 'history': history,
                                        'active': {'ident': 'p1: Scizor' if turn == 15 else 'p1: Dragonite'}},
                        'action': action, 'output': selected_action, 'trace': trace,
                        'memory_before': {'preserve': {'nested': [None, False, .123456789]}},
                        'finalization': {'output': selected_action}})
    game = {'episode_id': h.REVIEWED_EPISODE, 'status': 'completed', 'winner': 'player',
            'score': 1, 'turns': 15, 'replay_log': '\n'.join(lines), 'traces': records}

    def write(value, *, reviewed=True, replay=True):
        data = json.dumps(value).encode()
        path.write_bytes(data)
        if reviewed:
            monkeypatch.setattr(h, 'REVIEWED_REVISION', hashlib.sha256(data).hexdigest())
            monkeypatch.setattr(h, 'REVIEWED_BYTES', len(data))
        if replay:
            monkeypatch.setattr(h, 'REVIEWED_REPLAY_SHA256', hashlib.sha256(value['replay_log'].encode()).hexdigest())
        return data

    write(game)
    return store, path, game, write


def build(store, **overrides):
    arguments = dict(run_id=h.REVIEWED_RUN, split='validation', candidate=h.REVIEWED_CANDIDATE,
                     episode=h.REVIEWED_EPISODE, revision=h.REVIEWED_REVISION)
    arguments.update(overrides)
    return h.build_highlights(store, **arguments)


def test_exact_decisions_anchors_and_honest_finisher_semantics(archive):
    store, path, game, _ = archive
    report = build(store)
    assert report['binding']['trace_revision'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report['binding']['replay_sha256'] == hashlib.sha256(game['replay_log'].encode()).hexdigest()
    assert report['binding']['replay_line_count'] == 185
    assert (report['winner'], report['score'], report['turns']) == ('player', 1, 15)
    assert [(c['turn'], c['decision_index'], c['request_id']) for c in report['chapters']] == [
        (2, 1, 2), (3, 2, 3), (12, 12, 13), (15, 15, 16)]
    assert [(c['start_step'], c['end_step_exclusive']) for c in report['chapters']] == [
        (37, 49), (50, 61), (146, 154), (179, 185)]
    lines = game['replay_log'].split('\n')
    for chapter in report['chapters']:
        assert chapter['decision'] == game['traces'][chapter['decision_index']]
        for anchor in chapter['anchors'].values():
            if anchor is not None:
                assert anchor['line'] == lines[anchor['log_index']]
        fragment = chapter['log_fragment']
        assert fragment['lines'] == lines[fragment['start']:fragment['end_exclusive']]
        assert chapter['anchors']['action']['line'].split('|')[2].startswith('p1')
    assert [c['choice_usage']['mode'] for c in report['chapters']] == ['accepted_pick', 'finisher', 'accepted_pick', 'finisher']
    assert 'before consulting' in report['chapters'][3]['choice_usage']['caption']
    assert [c['gap_before'] for c in report['chapters'][:2]] == [None, None]
    assert report['chapters'][2]['gap_before'] == {'from_turn': 4, 'to_turn': 11, 'caption': 'Skipping turns 4–11'}
    assert report['chapters'][3]['gap_before']['caption'] == 'Skipping turns 13–14'
    assert report['chapters'][-1]['anchors']['stop_before'] is None
    assert 'replay_log' not in report


@pytest.mark.parametrize('overrides', [
    {'split': 'train'}, {'split': 'eval'}, {'split': 'holdout'}, {'split': 'test'},
    {'run_id': 'other-run'}, {'candidate': 'a' * 64}, {'episode': 'other-game'},
    {'episode': '../private'}, {'revision': '0' * 64},
])
def test_unreviewed_requests_are_rejected_before_any_archive_access(overrides):
    class UntouchableStore:
        def _trace_path(self, *args):
            raise AssertionError('An unreviewed request must not access the filesystem')
    with pytest.raises((h.HighlightUnavailable, TraceRevisionError)):
        build(UntouchableStore(), **overrides)


def test_stale_same_size_content_is_rejected_without_trusting_an_index(archive):
    store, path, game, write = archive
    altered = copy.deepcopy(game)
    altered['traces'][1]['trace'][2]['response']['answers']['action']['probabilities']['move:3'] = .94
    original_size = path.stat().st_size
    write(altered, reviewed=False)
    assert path.stat().st_size == original_size
    with pytest.raises(TraceRevisionError, match='differs'):
        build(store)


def test_oversize_or_replaced_archive_is_rejected(archive):
    store, path, _, _ = archive
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(TraceRevisionError, match='size changed'):
        build(store)


@pytest.mark.parametrize('fault', ['prefix', 'phase', 'extra_phase', 'boundary', 'wrong_actor', 'duplicate_action', 'missing_effect', 'missing_win', 'wrong_winner', 'source', 'finisher', 'gate', 'output', 'missing_answer'])
def test_semantic_mismatch_is_rejected_even_in_a_review_fixture(archive, fault):
    store, _, original, write = archive
    game = copy.deepcopy(original)
    first = game['traces'][1]
    if fault == 'prefix':
        first['observation']['history'][0] = '|message|wrong earlier position'
    elif fault == 'phase':
        first['phase'] = 'switch'
    elif fault == 'extra_phase':
        game['traces'][6]['turn'] = 2
    elif fault == 'boundary':
        game['traces'][2]['observation']['history'].insert(0, '|')
    elif fault in ('wrong_actor', 'duplicate_action', 'missing_effect', 'missing_win'):
        lines = game['replay_log'].split('\n')
        if fault == 'wrong_actor':
            lines[39] = lines[39].replace('p1a: Dragonite', 'p2a: Dragonite')
        elif fault == 'duplicate_action':
            lines[40] = lines[39]
        elif fault == 'missing_effect':
            lines[44] = '|'
        else:
            lines[184] = '|tie'
        game['replay_log'] = '\n'.join(lines)
    elif fault == 'wrong_winner':
        game['winner'] = 'opponent'
    elif fault == 'source':
        first['trace'][3]['source'] += '\n# altered logic'
    elif fault == 'finisher':
        game['traces'][15]['trace'][0]['output']['finisher'] = None
    elif fault == 'gate':
        first['trace'][0]['output']['candidates'][0]['value'] = -100
    elif fault == 'output':
        first['output'] = 'other'
    else:
        del first['trace'][2]['response']['answers']['action']['probabilities']
    write(game)
    with pytest.raises(h.HighlightUnavailable):
        build(store)


def test_wrong_public_log_hash_is_independently_rejected(archive):
    store, _, game, write = archive
    game = copy.deepcopy(game)
    game['replay_log'] += '\n'
    write(game, replay=False)
    with pytest.raises(h.HighlightUnavailable, match='public replay log changed'):
        build(store)


def test_archive_read_errors_are_classified(archive, monkeypatch):
    store, path, _, _ = archive
    original = Path.open

    def fail(self, *args, **kwargs):
        if self == path:
            raise PermissionError('fixture denied')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', fail)
    with pytest.raises(h.HighlightUnavailable, match='unavailable'):
        build(store)


def test_atomic_replacement_during_read_is_detected_even_for_identical_bytes(archive, monkeypatch):
    store, path, _, _ = archive
    original = Path.open

    class ReplacingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            data = self.stream.read(size)
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(data)
            replacement.replace(path)
            return data

    def open_replacing(self, *args, **kwargs):
        stream = original(self, *args, **kwargs)
        return ReplacingReader(stream) if self == path and args == ('rb',) else stream

    monkeypatch.setattr(Path, 'open', open_replacing)
    with pytest.raises(TraceRevisionError, match='changed while reading'):
        build(store)


def test_only_requested_eval_trace_is_read_and_no_source_is_executed(archive, monkeypatch):
    store, path, game, _ = archive
    original = Path.open
    reads = []

    def restricted(self, *args, **kwargs):
        assert self == path, f'Unexpected read: {self}'
        reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', restricted)
    report = build(store)
    assert reads == [path]
    assert report['chapters'][0]['decision']['trace'][3]['source'].startswith('raise AssertionError')
    assert report['chapters'][0]['decision'] == game['traces'][1]


def test_real_reviewed_eval_archive_when_present():
    root = Path(__file__).resolve().parents[1] / 'runs'
    path = root / h.REVIEWED_RUN / 'traces' / 'validation' / f'{h.REVIEWED_CANDIDATE}__{h.REVIEWED_EPISODE}.json'
    if not path.is_file():
        pytest.skip('The local reviewed Eval archive is not distributed with source-only checkouts')
    report = build(RunStore(root))
    original = json.loads(path.read_bytes())
    assert [c['decision'] for c in report['chapters']] == [original['traces'][i] for i in (1, 2, 12, 15)]
    assert [c['decision']['trace'][2]['response']['answers']['action']['probabilities'][c['choice_usage']['pick']]
            for c in report['chapters']] == [.95, .89, .72, .99]
    assert [c['anchors']['action']['log_index'] for c in report['chapters']] == [39, 51, 147, 180]
