"""Reviewed replay chapters, built exclusively from one immutable Eval archive.

This is a presentation adapter, not a general battle-event aligner. A new game
or revised archive requires a new review. No policy or game code is executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from auto_jev.storage import StoreError, TraceRevisionError


REVIEWED_RUN = '20260921-012437-pokemon-mixed-expanded-405d85'
REVIEWED_CANDIDATE = '0617b0bb9505cddfc319231e6b2226924e1b1d40280b09486ee16d838342a502'
REVIEWED_EPISODE = 'pokemon-expanded-validation-05'
REVIEWED_REVISION = '9f52d96ac9717dca7a399de9ed4cc6f37018ef4c09bc01dcad5c8d9c459fb3ac'
REVIEWED_BYTES = 3621150
REVIEWED_REPLAY_SHA256 = 'ee0e100f0a4c7604773e526152f492533a4ce54c798eed8583c0e366ab11f26d'
REVIEWED_DECISION_SOURCE = '3576d189f531f3aaa3c05c54bfe3b29b5505378dc296cd57675e2f379b1ea7d2'

_CHAPTERS = (
    {'turn': 2, 'decision_index': 1, 'request_id': 2, 'mode': 'accepted_pick',
     'title': 'Jev chooses Earthquake',
     'caption': 'Jev assigns Earthquake 95%; the flow accepts its choice. Magnezone survives through Sturdy and heals to 26%.',
     'after_line': '|-heal|p2a: Magnezone|26/100|[from] item: Sitrus Berry'},
    {'turn': 3, 'decision_index': 2, 'request_id': 3, 'mode': 'finisher',
     'title': 'The flow finishes the knockout',
     'caption': 'The code finisher selects Earthquake, agreeing with Jev\'s 89% recommendation. Magnezone faints.',
     'after_line': '|faint|p2a: Magnezone'},
    {'turn': 12, 'decision_index': 12, 'request_id': 13, 'mode': 'accepted_pick',
     'title': 'Jev chooses a switch',
     'caption': 'Jev assigns switching to Scizor 72%; the flow accepts. Scizor resists Ice Beam and remains at 85% HP.',
     'after_line': '|-damage|p1a: Scizor|85/100'},
    {'turn': 15, 'decision_index': 15, 'request_id': 16, 'mode': 'finisher',
     'title': 'The final winning move',
     'caption': 'The code finisher selects X-Scissor, agreeing with Jev\'s 99% recommendation. Gastrodon faints and AutoJev wins.',
     'after_line': '|win|AutoJev'},
)


class HighlightUnavailable(StoreError):
    """The requested replay has no reviewed, unambiguous highlight sequence."""


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _binding(stat):
    return tuple(getattr(stat, key) for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns'))


def _public_line(line):
    """Map only our exact HP fields to the archived spectator percentage.

    All other tokens, player identities, event order and even blank protocol
    separators must match. Opponent HP and private request events are not
    silently removed or rewritten.
    """
    parts = line.split('|')
    if len(parts) < 4 or not parts[2].startswith('p1'):
        return line
    hp_index = 4 if parts[1] in ('switch', 'drag', 'replace') else 3
    if parts[1] not in ('switch', 'drag', 'replace', '-damage', '-heal') or len(parts) <= hp_index:
        return line
    match = re.fullmatch(r'(\d+)/(\d+)( .*)?', parts[hp_index])
    if match:
        hp, maximum = int(match[1]), int(match[2])
        if maximum <= 0 or hp > maximum:
            raise HighlightUnavailable('Invalid HP in the reviewed observation history')
        percentage = (100 * hp + maximum - 1) // maximum
        parts[hp_index] = f'{percentage}/100' + (match[3] or '')
    return '|'.join(parts)


def _anchor(lines, index):
    return {'log_index': index, 'line': lines[index]}


def _unique_index(lines, value, start, end):
    found = [index for index in range(start, end) if lines[index] == value]
    if len(found) != 1:
        raise HighlightUnavailable('The reviewed replay anchor is missing or ambiguous')
    return found[0]


def _player_ident(ident):
    return re.sub(r'^p1[a-c]: ', 'p1: ', ident)


def _action_index(lines, start, end, decision):
    action = decision['action']
    observation = decision['observation']
    found = []
    for index in range(start, end):
        parts = lines[index].split('|')
        if len(parts) < 5 or not parts[2].startswith('p1'):
            continue
        if action.get('kind') == 'move':
            matches = (parts[1] == 'move'
                       and _player_ident(parts[2]) == observation['active']['ident']
                       and parts[3] == action['move']['name'])
        elif action.get('kind') == 'switch':
            matches = (parts[1] == 'switch'
                       and _player_ident(parts[2]) == action['pokemon']['ident']
                       and parts[3].split(',')[0] == action['pokemon']['species'])
        else:
            matches = False
        if matches:
            found.append(index)
    if len(found) != 1:
        raise HighlightUnavailable('The executed player action is missing or ambiguous in the replay')
    return found[0]


def _choice_usage(decision, expected_mode):
    nodes = {node['id']: node for node in decision['trace']}
    if len(nodes) != len(decision['trace']) or set(nodes) != {'features', 'plan', 'pick', 'decision'}:
        raise HighlightUnavailable('The reviewed flow nodes changed')
    if any(node.get('status') != 'ok' for node in nodes.values()):
        raise HighlightUnavailable('A highlighted flow node did not complete')
    final = nodes['decision']
    if _digest(final['source'].encode()) != REVIEWED_DECISION_SOURCE:
        raise HighlightUnavailable('The reviewed final decision source changed')
    action = decision['action']['id']
    if action != decision['output'] or final.get('output') != action:
        raise HighlightUnavailable('Recorded final action and decision output disagree')
    features = nodes['features']['output']
    candidates = features['candidates']
    ids = [candidate['id'] for candidate in candidates]
    if len(ids) < 2 or len(set(ids)) != len(ids) or action not in ids:
        raise HighlightUnavailable('The reviewed action catalog changed')
    answer = nodes['pick']['response']['answers']['action']
    pick = answer['choice']
    if answer.get('type') != 'choice' or pick not in answer['probabilities']:
        raise HighlightUnavailable('The archived Jev choice is incomplete')
    finisher = features.get('finisher')
    if expected_mode == 'finisher':
        if finisher != action:
            raise HighlightUnavailable('The reviewed code finisher did not choose the recorded action')
        caption = 'The code finisher returns before consulting the Jev answers. Its action agrees with the recorded Jev recommendation.'
    else:
        if finisher is not None or pick != action:
            raise HighlightUnavailable('The reviewed Jev acceptance gate changed')
        # Check the gate using archived numeric features, without evaluating
        # generated Python. The source itself is independently hash-bound above.
        plan = nodes['plan']['response']['answers']
        signals = {key: plan[key]['noul'] for key in ('switch_now', 'recover_now', 'setup_now', 'hidden_coverage')}
        pool = [candidate for candidate in candidates if candidate.get('unsafe') is not True] or candidates
        scores = {}
        hidden = 2 * signals['hidden_coverage'] - 1
        if hidden < 0:
            hidden *= .5
        for candidate in pool:
            value = candidate['value'] - candidate['hiddenpen'] * hidden
            for flag, signal, weight in (('is_switch', 'switch_now', 30),
                                         ('is_heal', 'recover_now', 26), ('is_boost', 'setup_now', 24)):
                if candidate.get(flag) is True:
                    value += weight * (signals[signal] - .5)
            scores[candidate['id']] = value
        if pick not in scores or scores[pick] < max(scores.values()) - 10:
            raise HighlightUnavailable('The archived Jev choice did not pass the reviewed code gate')
        caption = 'The final code accepts the Jev choice after its score gate. This does not establish that Jev was necessary for the outcome.'
    if pick != action:
        raise HighlightUnavailable('The reviewed recommendation and executed action no longer agree')
    return {'mode': expected_mode, 'pick': pick, 'executed_action': action,
            'pick_matches': True, 'caption': caption}


def _chapter(game, lines, definition):
    index, turn = definition['decision_index'], definition['turn']
    records = game['traces']
    decision = records[index]
    observation = decision['observation']
    if (decision.get('status') != 'ok' or decision.get('turn') != turn
            or decision.get('phase') != 'move' or decision.get('request_id') != definition['request_id']
            or observation.get('turn') != turn or observation.get('phase') != 'move'):
        raise HighlightUnavailable('The reviewed decision identity or phase changed')
    if sum(record.get('turn') == turn for record in records) != 1:
        raise HighlightUnavailable('A highlighted turn contains additional decisions')
    history = observation['history']
    if not history or not all(isinstance(line, str) for line in history):
        raise HighlightUnavailable('The decision has no complete observation history')
    before = len(history) - 1
    if (history[-1] != f'|turn|{turn}' or before >= len(lines)
            or [_public_line(line) for line in history] != lines[:before + 1]):
        raise HighlightUnavailable('Decision history does not match the complete replay prefix')
    if _unique_index(lines, f'|turn|{turn}', 0, len(lines)) != before:
        raise HighlightUnavailable('The decision turn anchor is ambiguous')
    later = [position for position in range(before + 1, len(lines)) if lines[position].startswith('|turn|')]
    end = later[0] if later else len(lines)
    # Selected segments must stop before any later decision's snapshot, not
    # merely before an estimated elapsed time or a matching turn number.
    if index + 1 < len(records):
        following = records[index + 1]['observation']['history']
        if len(following) - 1 != end or following[-1] != lines[end]:
            raise HighlightUnavailable('The chapter would cross an unaligned decision boundary')
    elif end != len(lines):
        raise HighlightUnavailable('The final chapter is not the final archived decision')
    action = _action_index(lines, before + 1, end, decision)
    after = _unique_index(lines, definition['after_line'], action + 1, end)
    return {
        'id': f'turn-{turn}', 'title': definition['title'], 'caption': definition['caption'],
        'turn': turn, 'phase': 'move', 'decision_index': index,
        'request_id': decision['request_id'], 'decision': decision,
        'start_step': before + 1, 'end_step_exclusive': end,
        'choice_usage': _choice_usage(decision, definition['mode']),
        'anchors': {'before': _anchor(lines, before), 'action': _anchor(lines, action),
                    'after': _anchor(lines, after),
                    'stop_before': _anchor(lines, end) if end < len(lines) else None},
        'log_fragment': {'start': before, 'end_exclusive': end, 'lines': lines[before:end]},
        'gap_before': None,
    }


def build_highlights(store, run_id, split, candidate, episode, revision=None):
    """Return four reviewed chapters with exact original decision objects.

    Protocol indices address ``replay_log.split('\\n')`` without filtering.
    ``start_step`` follows the already-consumed decision-before turn marker;
    ``end_step_exclusive`` must not be consumed. It is the next turn marker or
    the queue length for the final chapter. Animation completion is a separate
    concern of the renderer, not inferred from these protocol indices.
    """
    if (run_id, split, candidate, episode) != (REVIEWED_RUN, 'validation', REVIEWED_CANDIDATE, REVIEWED_EPISODE):
        raise HighlightUnavailable('Highlights are available only for the reviewed selected Eval game')
    if revision is not None and revision != REVIEWED_REVISION:
        raise TraceRevisionError('The requested highlight revision has not been reviewed')
    path = store._trace_path(run_id, split, candidate, episode)
    try:
        with path.open('rb') as stream:
            initial = _binding(os.fstat(stream.fileno()))
            if initial[2] != REVIEWED_BYTES:
                raise TraceRevisionError('The reviewed highlight archive size changed')
            data = stream.read(REVIEWED_BYTES + 1)
            if initial != _binding(os.fstat(stream.fileno())) or initial != _binding(path.stat()):
                raise TraceRevisionError('The highlight archive changed while reading')
    except OSError as exc:
        raise HighlightUnavailable('The reviewed highlight archive is unavailable') from exc
    if _digest(data) != REVIEWED_REVISION:
        raise TraceRevisionError('The highlight archive differs from its reviewed revision')
    try:
        game = json.loads(data)
        if (game.get('episode_id') != episode or game.get('status') != 'completed'
                or game.get('winner') != 'player' or game.get('score') != 1 or game.get('turns') != 15):
            raise HighlightUnavailable('The reviewed terminal game result changed')
        log = game['replay_log']
        if not isinstance(log, str) or _digest(log.encode()) != REVIEWED_REPLAY_SHA256:
            raise HighlightUnavailable('The reviewed public replay log changed')
        lines = log.split('\n')
        if len(lines) != 185 or lines[-1] != '|win|AutoJev':
            raise HighlightUnavailable('The reviewed final victory event is missing')
        chapters = [_chapter(game, lines, definition) for definition in _CHAPTERS]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, StoreError):
            raise
        raise HighlightUnavailable('The reviewed highlight archive is incomplete or malformed') from exc
    for previous, chapter in zip(chapters, chapters[1:]):
        first, last = previous['turn'] + 1, chapter['turn'] - 1
        if first <= last:
            chapter['gap_before'] = {'from_turn': first, 'to_turn': last,
                                     'caption': f'Skipping turns {first}–{last}'}
    return {
        'schema': 'auto_jev.pokemon.highlights.v1',
        'binding': {'run_id': run_id, 'split': split, 'candidate_hash': candidate,
                    'episode_id': episode, 'trace_revision': REVIEWED_REVISION,
                    'replay_sha256': REVIEWED_REPLAY_SHA256, 'replay_line_count': len(lines)},
        'winner': game['winner'], 'score': game['score'], 'turns': game['turns'],
        'selection_note': 'Selected moments from one archived Eval victory, starting at turn 2. Intervening turns are explicitly skipped. Jev choice probabilities are not battle win probabilities.',
        'chapters': chapters,
    }
