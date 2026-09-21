"""Game protocol boundaries, without a live Jev service or reflection model."""
import copy
import io
from types import SimpleNamespace

import pytest

from auto_jev.runtime import PipelineExecutionError
from examples.pokemon import adapter


def observation(request=1, turn=1, action='move-1', phase='move'):
    return {'type': 'observation', 'request_id': request, 'observation': {
        'turn': turn, 'phase': phase,
        'legal_actions': [{'id': action, 'kind': phase, 'name': 'visible action'}],
    }}


def terminal(winner='player', score=1.0, status='completed', **fields):
    return {'type': 'result', 'status': status, 'winner': winner, 'score': score,
            'turns': 2, 'replay_log': ['|turn|1', '|turn|2'], **fields}


def policy(output="obs['legal_actions'][0]['id']", expression="obs['turn']"):
    return {'version': 2, 'name': 'adapter contract', 'jev_model': 'typesafe-ai/jev',
            'nodes': [{'id': 'visible_turn', 'kind': 'expression', 'expression': expression}],
            'output': output, 'memory_update': "{'decisions': get(memory, 'decisions', 0) + 1}"}


@pytest.fixture
def scripted_bridge(monkeypatch):
    scripts = []
    bridges = []

    class FakeBridge:
        def __init__(self, episode, *, timeout):
            self.messages = list(scripts.pop(0))
            self.sent = []
            self.closed = False
            bridges.append(self)

        def receive(self):
            if not self.messages:
                raise AssertionError('Adapter requested an unplanned protocol message')
            message = self.messages.pop(0)
            if isinstance(message, BaseException):
                raise message
            return copy.deepcopy(message)

        def send(self, message):
            self.sent.append(copy.deepcopy(message))

        def close(self):
            self.closed = True

    monkeypatch.setattr(adapter, 'Bridge', FakeBridge)
    return SimpleNamespace(scripts=scripts, bridges=bridges)


def play(scripted_bridge, messages, spec=None, *, capture_traces=True):
    scripted_bridge.scripts.append(messages)
    return adapter.evaluate_episode(spec or policy(), {'id': 'battle-test'}, None,
                                    capture_traces=capture_traces)


@pytest.mark.parametrize('winner,score', [('player', 1.0), ('opponent', 0.0), ('draw', 0.5)])
def test_engine_terminal_result_and_complete_policy_trace(scripted_bridge, winner, score):
    result = play(scripted_bridge, [observation(), terminal(winner, score)])
    assert result['status'] == 'completed' and result['score'] == score
    assert result['winner'] == winner and result['decisions'] == 1
    record = result['traces'][0]
    assert record['trace'][0]['output'] == 1
    assert record['finalization']['output']['status'] == 'ok'
    assert record['observation']['legal_actions'][0]['id'] == record['output']
    assert result['replay_log'] == '|turn|1\n|turn|2'
    assert scripted_bridge.bridges[0].sent == [{'cmd': 'act', 'request_id': 1, 'action_id': 'move-1'}]
    assert scripted_bridge.bridges[0].closed


def test_memory_updates_per_decision_and_resets_between_battles(scripted_bridge):
    result = play(scripted_bridge, [observation(), observation(2, 2, 'switch-2', 'switch'), terminal()],
                  capture_traces=False)
    assert result['traces'][0]['memory_before'] == {}
    assert result['traces'][0]['memory'] == {'decisions': 1}
    assert result['traces'][1]['memory_before'] == {'decisions': 1}
    assert result['traces'][1]['memory'] == {'decisions': 2}
    assert result['traces'][1]['action']['id'] == 'switch-2'
    again = play(scripted_bridge, [observation(), terminal()])
    assert again['traces'][0]['memory_before'] == {}


@pytest.mark.parametrize('output', ["'not-an-action'", '0', "{'id': 'move-1'}"])
def test_policy_cannot_submit_commands_or_actions_outside_current_catalog(scripted_bridge, output):
    with pytest.raises(PipelineExecutionError) as caught:
        play(scripted_bridge, [observation()], policy(output))
    exc = caught.value
    assert isinstance(exc.cause, ValueError)
    assert not getattr(exc.cause, 'task_infrastructure_error', False)
    assert exc.partial_result['traces'][0]['status'] == 'error'
    assert exc.partial_result['traces'][0]['trace'][0]['output'] == 1
    assert scripted_bridge.bridges[0].sent == []
    assert scripted_bridge.bridges[0].closed


@pytest.mark.parametrize('reason', ['max_turns', 'max_requests'])
def test_truncated_battle_is_a_failed_candidate_not_a_draw(scripted_bridge, reason):
    with pytest.raises(PipelineExecutionError) as caught:
        play(scripted_bridge, [observation(), terminal(None, None, 'truncated', reason=reason)])
    exc = caught.value
    assert isinstance(exc.cause, ValueError)
    assert not getattr(exc.cause, 'task_infrastructure_error', False)
    assert exc.partial_result['status'] == 'error'
    assert exc.partial_result['termination_reason'] == reason
    assert exc.partial_result['winner'] is None and exc.partial_result['score'] is None
    assert len(exc.partial_result['traces']) == 1


def test_disconnect_preserves_all_previous_decisions_and_is_fatal(scripted_bridge):
    cause = adapter.PokemonInfrastructureError('engine connection lost')
    with pytest.raises(PipelineExecutionError) as caught:
        play(scripted_bridge, [observation(), observation(2, 2), cause])
    exc = caught.value
    assert exc.cause is cause and exc.cause.task_infrastructure_error
    assert len(exc.partial_result['traces']) == 2
    assert len(exc.partial_result['action_history']) == 2
    assert exc.partial_result['last_observation']['turn'] == 2
    assert scripted_bridge.bridges[0].closed


def test_failed_pipeline_appends_its_full_partial_result_after_previous_turns(scripted_bridge):
    with pytest.raises(PipelineExecutionError) as caught:
        play(scripted_bridge, [observation(), observation(2, 2)],
             policy(expression="1 / (2 - obs['turn'])"))
    result = caught.value.partial_result
    assert len(result['traces']) == 2 and result['traces'][0]['status'] == 'ok'
    failed = result['traces'][1]
    assert failed['status'] == 'error' and failed['request_id'] == 2
    assert failed['memory'] == {'decisions': 1}
    assert failed['trace'][0]['status'] == 'error'
    assert failed['finalization']['output']['status'] == 'cancelled'
    assert len(scripted_bridge.bridges[0].sent) == 1


def test_parallel_pipeline_causes_remain_available_to_fatal_detection(scripted_bridge, monkeypatch):
    infrastructure = adapter.PokemonInfrastructureError('task transport failed')
    arithmetic = ValueError('bad policy expression')
    class FailingRuntime:
        def __init__(self, spec, jev):
            pass
        def run(self, obs, memory):
            raise PipelineExecutionError('parallel failure', cause=arithmetic,
                causes=(arithmetic, infrastructure), partial_result={
                    'trace': [{'id': 'bad', 'status': 'error'}, {'id': 'network', 'status': 'error'}],
                    'memory': {}, 'finalization': {'output': {'status': 'skipped'}},
                })
    monkeypatch.setattr(adapter, 'PipelineRuntime', FailingRuntime)
    with pytest.raises(PipelineExecutionError) as caught:
        play(scripted_bridge, [observation()])
    assert caught.value.causes == (arithmetic, infrastructure)
    assert len(caught.value.partial_result['traces'][0]['trace']) == 2


@pytest.mark.parametrize('message', [
    terminal('player', 0.0), terminal('player', True), terminal('unknown', 1.0),
    terminal('draw', float('nan')), terminal(['player'], 1.0),
    terminal('player', 1.0, 'unknown-status'), terminal(replay_log=['valid', None]),
    {'type': 'unrecognized'},
])
def test_invalid_protocol_outcome_is_fatal(scripted_bridge, message):
    with pytest.raises(PipelineExecutionError) as caught:
        play(scripted_bridge, [message])
    assert isinstance(caught.value.cause, adapter.PokemonInfrastructureError)


@pytest.mark.parametrize('actions', [[], [{'id': 'same'}, {'id': 'same'}], [None], [{}],
                                      [{'id': []}], [{'id': 1}], [{'id': ''}]])
def test_malformed_legal_catalog_is_infrastructure_failure_before_policy(scripted_bridge, actions):
    message = observation()
    message['observation']['legal_actions'] = actions
    with pytest.raises(PipelineExecutionError) as caught:
        play(scripted_bridge, [message])
    assert isinstance(caught.value.cause, adapter.PokemonInfrastructureError)
    assert not caught.value.partial_result['traces']
    assert not scripted_bridge.bridges[0].sent


@pytest.mark.parametrize('request_id', [1, 0, -1, True, None, '2'])
def test_stale_or_invalid_request_never_executes_a_second_action(scripted_bridge, request_id):
    with pytest.raises(PipelineExecutionError) as caught:
        play(scripted_bridge, [observation(), observation(request_id, 2)])
    assert isinstance(caught.value.cause, adapter.PokemonInfrastructureError)
    assert len(scripted_bridge.bridges[0].sent) == 1


def test_rejected_action_rolls_back_memory_and_replans_from_new_request(scripted_bridge):
    rejected = {'type': 'action_rejected', 'request_id': 1,
                'reason': '[Unavailable choice] newly revealed trapping ability'}
    result = play(scripted_bridge, [observation(action='switch-2', phase='switch'), rejected,
                                   observation(2, 1, 'move-1'), terminal()])
    assert result['traces'][0]['status'] == 'rejected'
    assert result['action_history'][0]['status'] == 'rejected'
    assert result['traces'][1]['memory_before'] == {}
    assert result['traces'][1]['memory'] == {'decisions': 1}
    assert result['bridge_events'][0] == rejected
    assert [x['action_id'] for x in scripted_bridge.bridges[0].sent] == ['switch-2', 'move-1']


def test_unmatched_action_rejection_is_a_protocol_failure(scripted_bridge):
    with pytest.raises(PipelineExecutionError) as caught:
        play(scripted_bridge, [observation(), {'type': 'action_rejected', 'request_id': 99, 'reason': 'bad request'}])
    assert isinstance(caught.value.cause, adapter.PokemonInfrastructureError)


@pytest.mark.parametrize('payload', [b'not-json\n', b'[]\n', b'{"type":"error","error":"engine failed"}\n'])
def test_bridge_rejects_corrupt_messages_and_engine_error(payload):
    bridge = adapter.Bridge.__new__(adapter.Bridge)
    bridge.timeout = 1
    bridge.buffer = bytearray(payload)
    with pytest.raises(adapter.PokemonInfrastructureError):
        bridge.receive()


def test_bridge_timeout_is_fatal_without_waiting(monkeypatch):
    bridge = adapter.Bridge.__new__(adapter.Bridge)
    bridge.timeout = 1
    bridge.buffer = bytearray()
    bridge.selector = SimpleNamespace(select=lambda timeout: [])
    with pytest.raises(adapter.PokemonInfrastructureError, match='timed out'):
        bridge.receive()


def test_bridge_selector_failure_is_fatal(monkeypatch):
    bridge = adapter.Bridge.__new__(adapter.Bridge)
    bridge.timeout = 1
    bridge.buffer = bytearray()
    def select(timeout):
        raise OSError('broken selector')
    bridge.selector = SimpleNamespace(select=select)
    with pytest.raises(adapter.PokemonInfrastructureError):
        bridge.receive()


@pytest.mark.parametrize('failure', ['eof', 'oserror'])
def test_bridge_read_failures_are_classified_as_infrastructure(monkeypatch, failure):
    bridge = adapter.Bridge.__new__(adapter.Bridge)
    bridge.timeout = 1
    bridge.buffer = bytearray()
    bridge.selector = SimpleNamespace(select=lambda timeout: [True])
    bridge.process = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: 10))
    bridge.stderr = io.BytesIO(b'local engine exited')
    def read(fd, count):
        if failure == 'oserror':
            raise OSError('broken descriptor')
        return b''
    monkeypatch.setattr(adapter.os, 'read', read)
    with pytest.raises(adapter.PokemonInfrastructureError):
        bridge.receive()


def test_process_launch_failure_is_fatal_and_releases_adapter_resources(monkeypatch):
    monkeypatch.setattr(adapter.shutil, 'which', lambda name: '/nonexistent/test-node')
    def launch(*args, **kwargs):
        raise OSError('unable to start engine')
    monkeypatch.setattr(adapter.subprocess, 'Popen', launch)
    with pytest.raises(PipelineExecutionError) as caught:
        adapter.evaluate_episode(policy(), {'id': 'battle-test'}, None)
    assert isinstance(caught.value.cause, adapter.PokemonInfrastructureError)
    assert caught.value.partial_result['traces'] == []


def test_code_only_evaluator_rejects_jev_before_starting_game_or_calling_provider(scripted_bridge):
    spec = policy()
    spec['nodes'].append({'id': 'not_allowed', 'kind': 'jev', 'state': "{}",
                         'questions': {'choice': {'type': 'choice', 'instruction': 'choose',
                                                  'criteria': ['move-1']}}})
    class NoProviderCalls:
        def __getattr__(self, name):
            pytest.fail('Pure code evaluator must not reach the Jev client')
    with pytest.raises(ValueError, match='prohibits Jev nodes'):
        adapter.evaluate_code_episode(spec, {'id': 'battle-test'}, NoProviderCalls())
    assert not scripted_bridge.bridges


def test_code_only_evaluator_runs_same_engine_reward_and_trace(scripted_bridge):
    scripted_bridge.scripts.append([observation(), terminal()])
    class NoProviderCalls:
        def __getattr__(self, name):
            pytest.fail('Pure code policy unexpectedly attempted a provider operation')
    result = adapter.evaluate_code_episode(policy(), {'id': 'battle-test'}, NoProviderCalls())
    assert result['score'] == 1 and result['traces'][0]['trace'][0]['output'] == 1
