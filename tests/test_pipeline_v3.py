"""v3 integration contracts; Python execution is always a pure test double.

These tests exercise the real spec validator, DAG compiler, scheduler and trace
assembly. They do not run candidate Python, an OS sandbox, models or a network.
"""
import ast
import copy
import importlib
from threading import Barrier, Event

import pytest

import auto_jev.runtime as runtime_module
from auto_jev.flow import compile_flow
from auto_jev.runtime import PipelineExecutionError, PipelineRuntime
from auto_jev.spec import pipeline_schema, validate_spec


def pipeline(nodes=(), *, version=3, output='nodes', memory_update=None):
    spec = {'version': version, 'name': 'v3 integration contract',
            'jev_model': 'typesafe-ai/jev', 'nodes': copy.deepcopy(list(nodes)), 'output': output}
    if memory_update is not None:
        spec['memory_update'] = memory_update
    return spec


def source(label):
    return 'def run(obs, nodes, memory):\n    return ' + repr(label) + '\n'


def python_node(ident, *, depends_on=(), code=None):
    return {'id': ident, 'kind': 'python', 'source': code or source(ident), 'depends_on': list(depends_on)}


def expr(ident, expression, **extra):
    return {'id': ident, 'kind': 'expression', 'expression': expression, **extra}


def question_map(option='move-a'):
    return {'action': {'type': 'choice', 'instructions': 'Choose an available action.',
                       'criteria': {option: 'One currently legal action'}}}


def dynamic_node(ident='judge', expression="nodes['questions']", *, state='obs', **extra):
    return {'id': ident, 'kind': 'jev', 'state': state,
            'questions_expression': expression, **extra}


class RecordingJev:
    def __init__(self):
        self.calls = []

    def judge(self, state, questions, model=None):
        self.calls.append(copy.deepcopy({'state': state, 'questions': questions, 'model': model}))
        return {'answers': {qid: {'type': q['type'], 'choice': next(iter(q['criteria']))}
                            for qid, q in questions.items()}}


class UnexpectedJev:
    def judge(self, *args, **kwargs):
        pytest.fail('Malformed dynamic questions or code-only graphs must not call the provider')


@pytest.fixture
def stub_python(monkeypatch):
    """Patch both supported import styles before any PipelineRuntime executes."""
    module = importlib.import_module('auto_jev.python_nodes')

    def install(function):
        monkeypatch.setattr(module, 'evaluate_python', function)
        monkeypatch.setattr(runtime_module, 'evaluate_python', function, raising=False)

    return install


@pytest.mark.parametrize('version', [1, 2, 3])
def test_legacy_expression_and_static_question_graphs_keep_their_version(version):
    jev = RecordingJev()
    spec = pipeline([
        expr('features', "{'visible': obs['value']}"),
        {'id': 'judge', 'kind': 'jev', 'state': "nodes['features']", 'questions': question_map()},
    ], version=version, output="nodes['judge']['action']['choice']")
    original = copy.deepcopy(spec)
    result = PipelineRuntime(spec, jev).run({'value': 8})
    assert result['output'] == 'move-a'
    assert jev.calls == [{'state': {'visible': 8}, 'questions': question_map(), 'model': 'typesafe-ai/jev'}]
    assert validate_spec(spec)['version'] == version
    assert spec == original
    assert result['execution']['mode'] == ('sequential' if version == 1 else 'parallel')


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('node', [python_node('code'), dynamic_node(expression=repr(question_map()))])
def test_v3_capabilities_do_not_silently_change_v1_or_v2(version, node):
    with pytest.raises(ValueError):
        validate_spec(pipeline([node], version=version))


def test_schema_advertises_v3_python_and_dynamic_questions():
    schema = pipeline_schema(version=3)
    assert schema['properties']['version'] == {'const': 3}
    kinds = schema['properties']['nodes']['items']['oneOf']
    python = next(x for x in kinds if x['properties']['kind'].get('const') == 'python')
    assert {'source', 'depends_on'} <= set(python['required'])
    jev = next(x for x in kinds if x['properties']['kind'].get('const') == 'jev')
    assert 'questions_expression' in jev['properties']
    assert 'questions' not in jev.get('required', []), 'Static questions must not be required for the dynamic form'


@pytest.mark.parametrize('depends_on', [None, 'parent', ['absent'], ['parent', 'parent'], ['code'], [False]])
def test_python_dependencies_are_explicit_and_validated(depends_on):
    node = python_node('code')
    if depends_on is None:
        node.pop('depends_on')
    else:
        node['depends_on'] = depends_on
    with pytest.raises(ValueError):
        validate_spec(pipeline([expr('parent', '1'), node]))


def test_python_cycles_are_rejected_before_any_execution():
    with pytest.raises(ValueError, match='cycle'):
        validate_spec(pipeline([python_node('a', depends_on=['b']), python_node('b', depends_on=['a'])]))


def test_python_only_sees_explicit_direct_dependencies_and_snapshots(stub_python):
    obs, memory = {'list': [1]}, {'saved': [2]}
    observed = []

    def run_python(code, context):
        observed.append(copy.deepcopy(context))
        assert context['nodes'] == {'visible': {'list': [4]}}
        context['obs']['list'].append(99)
        context['memory']['saved'].append(99)
        context['nodes']['visible']['list'].append(99)
        return {'answer': 7}

    stub_python(run_python)
    spec = pipeline([
        expr('unrelated', "{'secret': 17}"),
        expr('ancestor', '4'),
        expr('visible', "{'list': [nodes['ancestor']]}"),
        python_node('code', depends_on=['visible']),
    ], output="nodes['code']['answer']", memory_update="{'saved': memory['saved'], 'answer': nodes['code']['answer']}")
    result = PipelineRuntime(spec, UnexpectedJev(), max_workers=1).run(obs, memory)
    record = result['trace'][-1]
    assert record['input_context'] == observed[0]
    assert record['depends_on'] == ['visible']
    assert record['source'] == spec['nodes'][-1]['source']
    assert result['nodes']['visible'] == {'list': [4]}
    assert obs == {'list': [1]} and memory == {'saved': [2]}
    assert result['memory_before'] == {'saved': [2]}
    assert result['memory'] == {'saved': [2], 'answer': 7}
    assert result['output'] == 7


def test_undeclared_python_reference_cannot_see_already_completed_sibling(stub_python):
    def run_python(code, context):
        return context['nodes']['hidden']

    stub_python(run_python)
    spec = pipeline([expr('hidden', '8'), python_node('code', code="def run(obs, nodes, memory):\n    return nodes['hidden']\n")])
    assert compile_flow(spec)['code'] == []
    with pytest.raises(PipelineExecutionError) as caught:
        PipelineRuntime(spec, UnexpectedJev(), max_workers=1).run({})
    assert isinstance(caught.value.cause, KeyError)
    assert caught.value.partial_result['trace'][1]['input_context']['nodes'] == {}


def test_dynamic_questions_add_dependencies_and_archive_actual_request(stub_python):
    expected = question_map('switch-2')
    shared_questions = copy.deepcopy(expected)

    def run_python(code, context):
        return shared_questions

    class MutatingJev(RecordingJev):
        def judge(self, state, questions, model=None):
            answer = super().judge(state, questions, model)
            questions['action']['criteria']['injected'] = 'Provider mutation must remain local'
            return answer

    stub_python(run_python)
    # A question-only dependency is forward-declared and must still unlock judge.
    spec = pipeline([
        dynamic_node(state="nodes['state']", depends_on=['extra']),
        expr('extra', '9'), expr('state', "{'turn': obs['turn']}"), python_node('questions'),
    ], output="nodes['judge']['action']['choice']")
    original = copy.deepcopy(spec)
    assert compile_flow(spec)['judge'] == ['extra', 'state', 'questions']
    jev = MutatingJev()
    result = PipelineRuntime(spec, jev).run({'turn': 5})
    trace = result['trace'][0]
    assert result['output'] == 'switch-2'
    assert trace['questions_expression'] == "nodes['questions']"
    assert trace['questions'] == expected
    assert trace['input_context']['nodes'] == {'extra': 9, 'state': {'turn': 5}, 'questions': expected}
    assert trace['response']['answers']['action']['choice'] == 'switch-2'
    assert jev.calls == [{'state': {'turn': 5}, 'questions': expected, 'model': 'typesafe-ai/jev'}]
    assert spec == original and shared_questions == expected


@pytest.mark.parametrize('node', [
    {**dynamic_node(expression=repr(question_map())), 'questions': question_map()},
    {'id': 'judge', 'kind': 'jev', 'state': 'obs'},
    dynamic_node(expression='nodes'),
    dynamic_node(expression="nodes[obs['selected_node']]"),
    dynamic_node(expression="nodes['missing']"),
    dynamic_node(expression="nodes['judge']"),
])
def test_dynamic_question_definition_rejects_ambiguous_or_untraceable_forms(node):
    with pytest.raises(ValueError):
        validate_spec(pipeline([node]))


@pytest.mark.parametrize('bad', [
    None, [], {}, {'action': 'not a question'},
    {'action': {'type': 'unknown', 'instructions': 'Choose.'}},
    {'action': {'type': 'noul', 'instructions': ''}},
    {'action': {'type': 'choice', 'instructions': 'Choose.', 'criteria': {}}},
    {'action': {'type': 'choice', 'instructions': 'Choose.', 'criteria': {'a': 1}}},
    {'action': {'type': 'score', 'instructions': 'Score.', 'criteria': ['one level']}},
    {'action': {'type': 'noul', 'instructions': 'Judge.', 'extra': 'unsupported'}},
    {'action': {'type': 'noul', 'instructions': '   '}},
    {'action': {'type': 'noul', 'instructions': 'Judge.', 'criteria': {}}},
    {'action': {'type': 'choice', 'instructions': 'Choose.', 'criteria': {'': 'Description'}}},
    {'action': {'type': 'choice', 'instructions': 'Choose.', 'criteria': {'a': '  '}}},
    {'action': {'type': 'choice', 'instructions': 'Choose.', 'criteria': {str(i): 'Option' for i in range(256)}}},
    {'action': {'type': 'score', 'instructions': 'Score.', 'criteria': ['level'] * 11}},
    {'action': {'type': 'score', 'instructions': 'Score.', 'criteria': ['low', ' ']}},
    {str(i): {'type': 'noul', 'instructions': 'Judge.'} for i in range(256)},
])
def test_invalid_dynamic_questions_fail_before_provider_with_complete_partial_trace(bad):
    spec = pipeline([
        expr('questions', "obs['bad_questions']"), dynamic_node(),
        expr('downstream', "nodes['judge']"),
    ], memory_update="{'should_not_commit': True}")
    memory = {'original': [1]}
    with pytest.raises(PipelineExecutionError) as caught:
        PipelineRuntime(spec, UnexpectedJev()).run({'bad_questions': bad}, memory)
    partial = caught.value.partial_result
    records = {r['id']: r for r in partial['trace']}
    assert isinstance(caught.value.cause, ValueError)
    assert records['judge']['status'] == 'error'
    assert records['judge']['questions_expression'] == "nodes['questions']"
    assert records['judge']['input_context']['nodes'] == {'questions': bad}
    assert records['downstream']['status'] == 'blocked'
    assert partial['memory'] == partial['memory_before'] == memory == {'original': [1]}
    assert partial['finalization']['memory_update']['status'] == 'cancelled'


def test_python_ready_queue_unlocks_child_before_unrelated_branch_finishes(stub_python):
    slow_started, child_completed = Event(), Event()

    def run_python(code, context):
        label = ast.literal_eval(ast.parse(code).body[0].body[0].value)
        if label == 'slow':
            slow_started.set()
            assert child_completed.wait(5), 'A level barrier prevented the ready child from running'
            return 'slow done'
        if label == 'fast':
            assert slow_started.wait(5)
            return 'fast done'
        assert context['nodes'] == {'fast': 'fast done'}
        child_completed.set()
        return 'child done'

    stub_python(run_python)
    result = PipelineRuntime(pipeline([
        python_node('slow'), python_node('fast'), python_node('child', depends_on=['fast']),
    ]), UnexpectedJev(), max_workers=2).run({})
    assert child_completed.is_set()
    assert result['execution']['mode'] == 'parallel'
    assert result['execution']['max_workers'] == 2
    assert [r['id'] for r in result['trace']] == ['slow', 'fast', 'child']
    records = {r['id']: r for r in result['trace']}
    assert records['child']['started_ms'] < records['slow']['ended_ms']
    assert all(r['status'] == 'ok' and r['started_ms'] <= r['ended_ms'] for r in records.values())


def test_python_failure_drains_running_sibling_and_keeps_atomic_memory(stub_python):
    together = Barrier(2)
    failure = RuntimeError('test Python execution failed')

    def run_python(code, context):
        label = ast.literal_eval(ast.parse(code).body[0].body[0].value)
        together.wait(5)
        context['memory']['saved'].append('node-local mutation')
        if label == 'bad':
            raise failure
        return {'survived': True}

    stub_python(run_python)
    spec = pipeline([
        python_node('bad'), python_node('survivor'),
        python_node('blocked', depends_on=['bad']),
    ], memory_update="{'committed': True}")
    memory = {'saved': [1]}
    with pytest.raises(PipelineExecutionError) as caught:
        PipelineRuntime(spec, UnexpectedJev(), max_workers=2).run({}, memory)
    partial = caught.value.partial_result
    assert caught.value.cause is failure
    assert [(r['id'], r['status']) for r in partial['trace']] == [('bad', 'error'), ('survivor', 'ok'), ('blocked', 'blocked')]
    assert partial['nodes'] == {'survivor': {'survived': True}}
    assert partial['trace'][0]['input_context']['memory'] == {'saved': [1]}
    assert partial['memory'] == partial['memory_before'] == memory == {'saved': [1]}
    assert partial['finalization']['memory_update']['status'] == 'cancelled'


@pytest.mark.parametrize('bad_result', [float('nan'), object()])
def test_python_output_is_validated_before_consumers_or_memory_commit(stub_python, bad_result):
    stub_python(lambda code, context: bad_result)
    spec = pipeline([python_node('code'), expr('consumer', "nodes['code']")], memory_update="{'changed': True}")
    with pytest.raises(PipelineExecutionError) as caught:
        PipelineRuntime(spec, UnexpectedJev()).run({}, {'saved': 1})
    partial = caught.value.partial_result
    assert [r['status'] for r in partial['trace']] == ['error', 'blocked']
    assert partial['nodes'] == {} and partial['memory'] == {'saved': 1}


@pytest.mark.parametrize('output,memory_update', [('1 / 0', "{'new': True}"), ("nodes['code']", '[]')])
def test_v3_finalization_failure_preserves_python_trace_and_original_memory(stub_python, output, memory_update):
    stub_python(lambda code, context: {'result': 3})
    memory = {'old': [1]}
    with pytest.raises(PipelineExecutionError) as caught:
        PipelineRuntime(pipeline([python_node('code')], output=output, memory_update=memory_update), UnexpectedJev()).run({}, memory)
    partial = caught.value.partial_result
    assert partial['nodes'] == {'code': {'result': 3}}
    assert partial['trace'][0]['status'] == 'ok'
    assert partial['memory'] == partial['memory_before'] == memory == {'old': [1]}
