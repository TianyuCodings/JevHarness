"""Trusted local battle adapter; candidate policies only receive a player view."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import time

from auto_jev.runtime import PipelineExecutionError, PipelineRuntime

HERE = Path(__file__).resolve().parent
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class PokemonInfrastructureError(RuntimeError):
    task_infrastructure_error = True


class Bridge:
    def __init__(self, episode, *, timeout=30):
        node = shutil.which('node')
        if not node:
            raise PokemonInfrastructureError('Node executable is unavailable')
        self.timeout = timeout
        self.buffer = bytearray()
        self.stderr = tempfile.TemporaryFile()
        self.selector = selectors.DefaultSelector()
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'TMPDIR', 'LANG', 'LC_ALL')}
        try:
            self.process = subprocess.Popen(
                [node, str(HERE / 'bridge.cjs')], cwd=HERE, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.stderr, start_new_session=True,
            )
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.send({'cmd': 'start', 'episode': episode})
        except OSError as exc:
            self.close()
            raise PokemonInfrastructureError('Game bridge could not start') from exc
        except BaseException:
            self.close()
            raise

    def send(self, message):
        try:
            data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode() + b'\n'
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise PokemonInfrastructureError('Game bridge input failed') from exc

    def receive(self):
        deadline = time.monotonic() + self.timeout
        while True:
            if b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise PokemonInfrastructureError('Game bridge returned invalid JSON') from exc
                if not isinstance(message, dict) or not isinstance(message.get('type'), str):
                    raise PokemonInfrastructureError('Game bridge returned an invalid message')
                if message['type'] == 'error':
                    raise PokemonInfrastructureError(str(message.get('error', 'Game engine error'))[:1000])
                return message
            remaining = deadline - time.monotonic()
            try:
                ready = remaining > 0 and self.selector.select(remaining)
            except OSError as exc:
                raise PokemonInfrastructureError('Game bridge output polling failed') from exc
            if not ready:
                raise PokemonInfrastructureError('Game bridge response timed out')
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except OSError as exc:
                raise PokemonInfrastructureError('Game bridge output failed') from exc
            if not chunk:
                self.stderr.seek(0)
                diagnostic = self.stderr.read(2000).decode(errors='replace')
                raise PokemonInfrastructureError('Game bridge exited before a terminal result: ' + diagnostic)
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_MESSAGE_BYTES:
                raise PokemonInfrastructureError('Game bridge message exceeded the transport limit')

    def close(self):
        process = getattr(self, 'process', None)
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
        self.selector.close()
        self.stderr.close()


def evaluate_episode(spec, episode, jev, *, capture_traces=True, bridge_timeout=30):
    """Return only engine-confirmed game rewards; archive partial traces on failure."""
    started = time.perf_counter()
    runtime = PipelineRuntime(spec, jev)
    result = {
        'episode_id': episode['id'], 'task_id': 'pokemon_singles',
        'status': 'running', 'score': None, 'winner': None, 'turns': 0,
        'replay_log': '', 'action_history': [], 'traces': [],
    }
    memory = {}
    bridge = None
    previous_request = -1
    current_observation = None
    try:
        bridge = Bridge(episode, timeout=bridge_timeout)
        while True:
            message = bridge.receive()
            if message['type'] == 'started':
                result.setdefault('bridge_events', []).append(copy.deepcopy(message))
                continue
            if message['type'] == 'action_rejected':
                result.setdefault('bridge_events', []).append(copy.deepcopy(message))
                if not result['traces'] or message.get('request_id') != result['traces'][-1].get('request_id'):
                    raise PokemonInfrastructureError('Rejection does not match the pending action')
                rejected = result['traces'][-1]
                rejected.update(status='rejected', rejection_reason=message.get('reason'))
                memory = copy.deepcopy(rejected['memory_before'])
                rejected['memory_rolled_back'] = True
                result['action_history'][-1]['status'] = 'rejected'
                continue
            if message['type'] == 'result':
                result.update({k: copy.deepcopy(message.get(k)) for k in
                               ('status', 'winner', 'score', 'turns', 'replay_log')})
                if isinstance(result['replay_log'], list) and all(isinstance(line, str) for line in result['replay_log']):
                    result['replay_log'] = '\n'.join(result['replay_log'])
                if not isinstance(result['replay_log'], str):
                    raise PokemonInfrastructureError('Game replay log is malformed')
                if result['status'] != 'completed':
                    if result['status'] != 'truncated' or message.get('reason') not in ('max_turns', 'max_requests'):
                        raise PokemonInfrastructureError('Game terminal status is malformed')
                    result['termination_reason'] = message.get('reason')
                    raise ValueError('Battle did not reach an engine-confirmed terminal result')
                expected = {'player': 1.0, 'opponent': 0.0, 'draw': 0.5}
                score = result['score']
                if (not isinstance(result['winner'], str) or result['winner'] not in expected or isinstance(score, bool)
                        or not isinstance(score, (int, float))
                        or score != expected[result['winner']]):
                    raise PokemonInfrastructureError('Game terminal outcome/reward mismatch')
                result['decisions'] = len(result['traces'])
                result['elapsed_ms'] = (time.perf_counter() - started) * 1000
                return result
            if message['type'] != 'observation':
                raise PokemonInfrastructureError('Unexpected game bridge message')
            request = message.get('request_id')
            if type(request) is not int or request <= previous_request:
                raise PokemonInfrastructureError('Game request IDs are stale or out of order')
            previous_request = request
            obs = message.get('observation')
            if not isinstance(obs, dict) or not isinstance(obs.get('legal_actions'), list):
                raise PokemonInfrastructureError('Game observation has no legal action catalog')
            current_observation = copy.deepcopy(obs)
            if any(not isinstance(a, dict) or not isinstance(a.get('id'), str) or not a['id']
                   for a in obs['legal_actions']):
                raise PokemonInfrastructureError('Game legal action catalog is malformed')
            actions = {a['id']: a for a in obs['legal_actions']}
            if not actions or len(actions) != len(obs['legal_actions']):
                raise PokemonInfrastructureError('Game legal action IDs are empty or duplicated')
            decision_start = time.perf_counter()
            before = copy.deepcopy(memory)
            try:
                decision = runtime.run(obs, memory)
            except PipelineExecutionError as exc:
                partial = copy.deepcopy(exc.partial_result)
                partial.update(observation=copy.deepcopy(obs), turn=obs.get('turn'),
                               phase=obs.get('phase'), request_id=request, status='error')
                result['traces'].append(partial)
                raise
            action_id = decision['output']
            record = {
                **decision, 'turn': obs.get('turn'), 'phase': obs.get('phase'),
                'request_id': request, 'observation': copy.deepcopy(obs),
                'memory_before': before, 'status': 'ok',
                'decision_ms': (time.perf_counter() - decision_start) * 1000,
            }
            result['traces'].append(record)
            if not isinstance(action_id, str) or action_id not in actions:
                record.update(status='error', error='Policy returned an unavailable action ID')
                raise ValueError('Policy returned an unavailable action ID')
            record['action'] = copy.deepcopy(actions[action_id])
            memory = copy.deepcopy(decision['memory'])
            result['action_history'].append({
                'decision_index': len(result['traces']) - 1, 'turn': obs.get('turn'),
                'action': copy.deepcopy(record['action']), 'status': 'ok',
                'elapsed_ms': record['decision_ms'],
            })
            bridge.send({'cmd': 'act', 'request_id': request, 'action_id': action_id})
    except BaseException as exc:
        result.update(status='error', error=f'{type(exc).__name__}: {str(exc)[:1000]}',
                      elapsed_ms=(time.perf_counter() - started) * 1000,
                      decisions=len(result['traces']))
        if current_observation is not None:
            result['last_observation'] = current_observation
        if isinstance(exc, PipelineExecutionError):
            raise PipelineExecutionError(str(exc), partial_result=result,
                                         cause=exc.cause, causes=exc.causes) from exc
        if isinstance(exc, Exception):
            raise PipelineExecutionError('Pokemon episode failed', partial_result=result, cause=exc) from exc
        raise
    finally:
        if bridge is not None:
            bridge.close()


def evaluate_code_episode(spec, episode, jev, *, capture_traces=True):
    """Matched ablation: the evolved code policy cannot invoke Jev."""
    if any(node.get('kind') == 'jev' for node in spec.get('nodes', [])):
        raise ValueError('The code-only experiment prohibits Jev nodes')
    return evaluate_episode(spec, episode, jev, capture_traces=capture_traces)
