"""Functional Python nodes and capability boundaries; no model calls."""
import copy
import ctypes
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

if importlib.util.find_spec('auto_jev.python_nodes') is None:
    pytest.skip('Python sandbox implementation has not arrived', allow_module_level=True)

from auto_jev.python_nodes import PythonSandboxUnavailable, evaluate_python, validate_python_source
import auto_jev.python_nodes as python_nodes


CONTEXT = {'obs': {'values': [1, 2, 3], '_score': 7},
           'nodes': {'feature': {'value': 9}}, 'memory': {'seen': 0}}


def source(body):
    return 'def run(obs, nodes, memory):\n' + '\n'.join('    ' + line for line in body.splitlines()) + '\n'


def test_sandbox_unavailability_is_an_infrastructure_error():
    assert issubclass(PythonSandboxUnavailable, Exception)
    assert PythonSandboxUnavailable.task_infrastructure_error is True


def test_functional_helpers_comprehension_lambda_and_safe_math_facades():
    code = '''def doubled(value):
    return value * 2

def run(obs, nodes, memory):
    values = [doubled(x) for x in obs["values"]]
    ranked = sorted(values, key=lambda value: -value)
    return {"sum": sum(values), "ranked": ranked,
            "root": math.sqrt(nodes["feature"]["value"]),
            "mean": statistics.mean(values), "_score": obs["_score"]}
'''
    validate_python_source(code)
    assert evaluate_python(code, copy.deepcopy(CONTEXT)) == {
        'sum': 12, 'ranked': [6, 4, 2], 'root': 3.0, 'mean': 4, '_score': 7}


def test_input_mutation_remains_local_and_state_does_not_survive_calls():
    code = source('obs["values"].append(9)\nnodes["feature"]["value"] = 20\n'
                  'memory["seen"] = 99\nreturn len(obs["values"])')
    context = copy.deepcopy(CONTEXT)
    assert evaluate_python(code, context) == 4
    assert context == CONTEXT
    assert evaluate_python(code, context) == 4
    assert context == CONTEXT


def test_underscore_dictionary_keys_are_data_not_python_private_attributes():
    code = source('local = {"_value": 2, "__label": "public data"}\n'
                  'return [local["_value"], local["__label"], obs["_score"]]')
    validate_python_source(code)
    assert evaluate_python(code, copy.deepcopy(CONTEXT)) == [2, 'public data', 7]


@pytest.mark.parametrize('code', [
    'import os\n' + source('return 1'),
    source('from os import environ\nreturn 1'),
    source('return __import__("os").getpid()'),
    source('return eval("1 + 1")'),
    source('return exec("a = 1")'),
    source('return compile("1", "candidate", "eval")'),
    source('return globals()'),
    source('return locals()'),
    source('return vars(obs)'),
    source('return getattr(obs, "__class__")'),
    source('return obs.__class__.__name__'),
    source('return ().__class__.__mro__[1].__subclasses__()'),
    source('return len.__self__.__dict__'),
    source('return statistics.mean.__globals__'),
    source('return (x for x in []).gi_frame'),
    source('return "{0.__class__.__name__}".format(0)'),
    source('return "{x.__class__.__name__}".format_map({"x": 0})'),
    source('return "{0.__self__.__loader__.exec_module.__globals__[sys].version}".format(len)'),
    source('return open("synthetic-canary.txt").read()'),
    '@print\n' + source('return 1'),
    'class Leak:\n    pass\n' + source('return 1'),
])
def test_static_validation_rejects_host_capabilities_and_indirect_attribute_reading(code):
    with pytest.raises((ValueError, SyntaxError)):
        validate_python_source(code)


@pytest.mark.parametrize('code', [
    '', 'def wrong(obs, nodes, memory):\n    return 1\n',
    'def run(obs):\n    return 1\n',
    'async def run(obs, nodes, memory):\n    return 1\n',
])
def test_entrypoint_contract_is_checked_before_worker_launch(code):
    with pytest.raises((ValueError, SyntaxError)):
        validate_python_source(code)


@pytest.mark.parametrize('body', ['return run', 'return float("nan")', 'return float("inf")',
                                'value = []\nvalue.append(value)\nreturn value'])
def test_non_json_outputs_are_candidate_errors_not_infrastructure_failures(body):
    with pytest.raises(Exception) as caught:
        evaluate_python(source(body), copy.deepcopy(CONTEXT))
    assert not getattr(caught.value, 'task_infrastructure_error', False)


def test_complete_json_inputs_are_required():
    code = source('return 1')
    for context in ({'obs': object(), 'nodes': {}, 'memory': {}},
                    {'obs': float('nan'), 'nodes': {}, 'memory': {}}):
        with pytest.raises((TypeError, ValueError)):
            evaluate_python(code, context)


def test_worker_transport_error_is_infrastructure_failure_without_launching_os_probe(monkeypatch):
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
                              returncode=0, poll=lambda: 0, wait=lambda timeout: 0)
    monkeypatch.setattr(python_nodes, '_sandbox_command', lambda: ['not-executed'])
    monkeypatch.setattr(python_nodes.subprocess, 'Popen', lambda *a, **kw: process)
    def exchange(*args):
        raise OSError('synthetic broken worker pipe')
    monkeypatch.setattr(python_nodes, '_exchange', exchange)
    with pytest.raises(PythonSandboxUnavailable):
        evaluate_python(source('return 1'), copy.deepcopy(CONTEXT))
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


def test_bootstrap_stderr_overflow_is_infrastructure_failure_without_launching_os_probe(monkeypatch):
    streams = [SimpleNamespace(fileno=lambda fd=fd: fd) for fd in (10, 11, 12)]
    process = SimpleNamespace(stdin=streams[0], stdout=streams[1], stderr=streams[2])
    class Selector:
        def __init__(self):
            self.entries = {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def register(self, stream, events, data):
            self.entries[data] = SimpleNamespace(fileobj=stream, data=data)
        def get_map(self):
            return self.entries
        def select(self, timeout):
            return [(self.entries['stderr'], 1)]
    monkeypatch.setattr(python_nodes.selectors, 'DefaultSelector', Selector)
    monkeypatch.setattr(python_nodes, '_check_memory', lambda process: None)
    monkeypatch.setattr(python_nodes.os, 'set_blocking', lambda *args: None)
    monkeypatch.setattr(python_nodes.os, 'read', lambda *args: b'x' * 8193)
    with pytest.raises(PythonSandboxUnavailable):
        python_nodes._exchange(process, b'input')


def test_native_rusage_v0_layout_matches_the_platform_header():
    # sys/resource.h: 16-byte UUID followed by ten uint64_t counters.
    assert ctypes.sizeof(python_nodes._Rusage) == 96
    assert python_nodes._Rusage.resident_size.offset == 64
    assert python_nodes._Rusage.phys_footprint.offset == 72


def test_memory_reader_failure_is_closed_for_a_live_worker(monkeypatch):
    monkeypatch.setattr(python_nodes, '_usage_reader', lambda: lambda *args: -1)
    live = SimpleNamespace(pid=123, poll=lambda: None)
    with pytest.raises(PythonSandboxUnavailable, match='supervise'):
        python_nodes._check_memory(live)
    # A vanished worker no longer needs memory accounting; its exit is checked
    # separately by evaluate_python and must not mask the underlying result.
    exited = SimpleNamespace(pid=123, poll=lambda: 0)
    python_nodes._check_memory(exited)


def test_memory_threshold_is_a_candidate_error(monkeypatch):
    def read_usage(pid, flavor, pointer):
        assert flavor == 0
        ctypes.cast(pointer, ctypes.POINTER(python_nodes._Rusage)).contents.resident_size = python_nodes.MEMORY_BYTES + 1
        return 0
    monkeypatch.setattr(python_nodes, '_usage_reader', lambda: read_usage)
    with pytest.raises(ValueError, match='memory') as caught:
        python_nodes._check_memory(SimpleNamespace(pid=123, poll=lambda: None))
    assert not getattr(caught.value, 'task_infrastructure_error', False)


def capture_launch_contract(monkeypatch):
    """Capture the real launch arguments without starting any subprocess."""
    captured = {}
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
                              returncode=0, poll=lambda: 0, wait=lambda timeout: 0)
    def launch(command, **kwargs):
        captured.update(command=list(command), kwargs=kwargs)
        return process
    with monkeypatch.context() as patched:
        patched.setattr(python_nodes.subprocess, 'Popen', launch)
        patched.setattr(python_nodes, '_exchange', lambda *args: (b'{"ok":true,"value":1}', b''))
        assert evaluate_python(source('return 1'), copy.deepcopy(CONTEXT)) == 1
    return captured


def test_launch_contract_discards_host_environment_and_fds(monkeypatch):
    monkeypatch.setenv('AUTO_JEV_SANDBOX_TEST_MARKER', 'synthetic-value')
    captured = capture_launch_contract(monkeypatch)
    command, kwargs = captured['command'], captured['kwargs']
    assert command[:2] == ['/usr/bin/sandbox-exec', '-p']
    assert '(deny default)' in command[2]
    assert '-I' in command and '-S' in command and '-B' in command
    assert kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LANG': 'C'}
    assert kwargs['close_fds'] is True and kwargs['start_new_session'] is True
    assert kwargs.get('shell', False) is False
    assert kwargs.get('pass_fds', ()) == ()
    assert Path(kwargs['cwd']).resolve() != Path(__file__).resolve().parents[1]


def test_actual_runtime_os_profile_denies_files_network_and_fork(tmp_path, monkeypatch):
    """Use the implementation's exact OS profile; deliberately bypass its AST.

    Only synthetic local files and a socket owned by this test are probed. The
    trusted probe imports system APIs to distinguish OS denial from AST denial.
    """
    monkeypatch.setenv('AUTO_JEV_SANDBOX_TEST_MARKER', 'synthetic-value')
    captured = capture_launch_contract(monkeypatch)
    canary = tmp_path / 'synthetic-canary.txt'
    canary.write_text('synthetic-value')
    linked = tmp_path / 'linked-canary.txt'
    linked.symlink_to(canary)
    writable = tmp_path / 'must-not-be-written.txt'
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    probe = '''import errno, json, os, socket
paths = PATHS
results = {}
def fork_probe():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
def read_path(path):
    with open(path) as stream:
        stream.read()
def write_path():
    with open(paths[2], "w") as stream:
        stream.write("unexpected write")
def connect():
    connection = socket.create_connection(("127.0.0.1", PORT), timeout=1)
    connection.close()
for label, operation in [("read", lambda: read_path(paths[0])),
                         ("symlink_read", lambda: read_path(paths[1])),
                         ("write", write_path), ("network", connect), ("fork", fork_probe)]:
    try:
        operation()
    except OSError as exc:
        results[label] = "denied" if exc.errno in (errno.EPERM, errno.EACCES) else "unexpected_errno:" + str(exc.errno)
    else:
        results[label] = "ALLOWED"
results["environment"] = "clean" if "AUTO_JEV_SANDBOX_TEST_MARKER" not in os.environ else "inherited"
print(json.dumps(results))
'''.replace('PATHS', repr([str(canary), str(linked), str(writable)])).replace('PORT', str(port))
    command = captured['command']
    assert command[-2] == '-c'
    command[-1] = probe
    process = subprocess.Popen(command, cwd=tmp_path, env=captured['kwargs']['env'],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, close_fds=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        pytest.fail('OS containment probe timed out')
    finally:
        listener.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    assert process.returncode == 0, stderr[:1500]
    assert json.loads(stdout) == {'read': 'denied', 'symlink_read': 'denied', 'write': 'denied',
                                 'network': 'denied', 'fork': 'denied', 'environment': 'clean'}
    assert canary.read_text() == 'synthetic-value'
    assert not writable.exists()


_HARNESS = '''import json, os, subprocess, sys
from auto_jev.python_nodes import evaluate_python
real_popen = subprocess.Popen
def track_popen(*args, **kwargs):
    process = real_popen(*args, **kwargs)
    with open(os.environ["PYTHON_NODE_TEST_PIDS"], "a") as stream:
        stream.write(str(process.pid) + "\\n")
    return process
subprocess.Popen = track_popen
payload = json.load(sys.stdin)
try:
    result = evaluate_python(payload["source"], payload["context"])
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__,
                     "infrastructure": bool(getattr(exc, "task_infrastructure_error", False))}))
else:
    print(json.dumps({"result": result}))
'''


def bounded_worker_probe(code, tmp_path):
    """An outer timeout also cleans workers if the implementation's timeout fails."""
    children_path = tmp_path / 'children.txt'
    env = {'PATH': os.defpath, 'LANG': 'en_US.UTF-8', 'PYTHON_NODE_TEST_PIDS': str(children_path)}
    process = subprocess.Popen([sys.executable, '-c', _HARNESS],
        cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(json.dumps({'source': code, 'context': CONTEXT}), timeout=15)
    except subprocess.TimeoutExpired:
        timed_out = True
        stdout = stderr = ''
    finally:
        children = [int(x) for x in children_path.read_text().splitlines()] if children_path.exists() else []
        survivors = []
        for pid in children:
            try:
                group = os.getpgid(pid)
            except ProcessLookupError:
                continue
            survivors.append(pid)
            try:
                if group == pid:
                    os.killpg(group, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    assert not timed_out, 'Python node exceeded the outer test deadline'
    assert not survivors, 'Python node left live worker processes behind'
    assert process.returncode == 0, stderr[:1500]
    assert children, 'The resource probe did not actually start an isolated worker'
    return json.loads(stdout)


def test_infinite_candidate_is_terminated_and_worker_reaped(tmp_path):
    code = source('while True:\n    pass')
    validate_python_source(code)  # Exercise execution limits, not static refusal.
    result = bounded_worker_probe(code, tmp_path)
    assert 'error' in result and result['infrastructure'] is False
    assert evaluate_python(source('return 1'), copy.deepcopy(CONTEXT)) == 1


def test_large_output_fails_with_bounded_worker_result(tmp_path):
    code = source('return "x" * (10 * 1024 * 1024)')
    validate_python_source(code)
    result = bounded_worker_probe(code, tmp_path)
    assert 'error' in result and result['infrastructure'] is False
