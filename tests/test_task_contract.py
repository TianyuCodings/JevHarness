import hashlib
import inspect
import json
import subprocess

import pytest

from auto_jev.evolution import freeze_run, run_evolution
from auto_jev.frozen import build_task_contract, evaluate_frozen, validate_artifact, verify_task_contract
from auto_jev.providers import JevClient
from auto_jev.runtime import PipelineExecutionError
from auto_jev.spec import pipeline_schema, spec_hash, validate_spec
from auto_jev.storage import RunStore


EVALUATIONS = []


def pipeline(output='0', version=2):
    return {'version': version, 'name': 'contract test', 'jev_model': 'typesafe-ai/jev',
            'nodes': [], 'output': output}


def evaluator(spec, episode, jev, capture_traces=True):
    EVALUATIONS.append(episode['id'])
    return {'episode_id': episode['id'], 'score': float(spec['output']),
            'traces': [{'trace': [{'id': 'preserved', 'output': 'full task evidence'}]}]}


def different_evaluator(spec, episode, jev, capture_traces=True):
    pytest.fail('Wrong evaluator must be rejected before executing')


class Proposer:
    def __init__(self, proposed=None):
        self.prompts = []
        self.proposed = proposed

    def __call__(self, prompt):
        self.prompts.append(json.loads(prompt))
        return json.dumps(self.proposed or pipeline('1'))


def evolve(store, proposer, **kwargs):
    return run_evolution([{'id': 'train'}], [{'id': 'validation'}],
                         jev=JevClient(mock=True, cache_namespace='contract-test'), proposer=proposer,
                         store=store, task_id='custom_task', evaluator=kwargs.pop('evaluator', evaluator),
                         seed_pipeline=kwargs.pop('seed_pipeline', pipeline()),
                         evolution_rounds=kwargs.pop('evolution_rounds', 1), **kwargs)


def test_context_reaches_proposer_and_contract_survives_freeze_and_execution(tmp_path):
    rule = tmp_path / 'rules.json'
    rule.write_text('{"reward":"win"}')
    contract = build_task_contract(evaluator, files=[rule])
    context = {'objective': 'maximize independently verified wins', 'description': 'A generic game',
               'observation_contract': {'allowed': ['public_state']}, 'action_contract': {'output': 'legal action id'},
               'task_semantics': {'hidden_state': 'never visible'}}
    proposer = Proposer()
    store = RunStore(tmp_path / 'runs')
    result = evolve(store, proposer, task_context=context, task_contract=contract)
    task = proposer.prompts[0]['task']
    assert task['objective'] == context['objective'] and task['context'] == context
    assert 'task_contract' not in proposer.prompts[0]  # runtime bindings are not reflection input
    artifact = freeze_run(store, result['run_id'])
    assert artifact['task_context'] == context
    assert artifact['task_contract'] == contract
    result = evaluate_frozen(artifact, [{'id': 'sealed'}], JevClient(mock=True), evaluator=evaluator)
    assert result[0]['score'] == 1


def test_false_declared_source_hash_is_rejected_before_any_evaluation(tmp_path):
    contract = build_task_contract(evaluator)
    contract['evaluator']['sha256'] = '0' * 64
    before = len(EVALUATIONS)
    proposer = Proposer()
    with pytest.raises(ValueError, match='evaluator source changed'):
        evolve(RunStore(tmp_path), proposer, task_contract=contract)
    assert not proposer.prompts and len(EVALUATIONS) == before


def test_actual_callable_is_checked_and_missing_evaluator_is_not_trusted(tmp_path):
    contract = build_task_contract(evaluator)
    with pytest.raises(ValueError, match='actual evaluator'):
        verify_task_contract(contract, evaluator=different_evaluator)
    store = RunStore(tmp_path)
    result = evolve(store, Proposer(), task_contract=contract)
    artifact = freeze_run(store, result['run_id'])
    with pytest.raises(ValueError, match='contracted evaluator'):
        validate_artifact(artifact, JevClient(mock=True))
    with pytest.raises(ValueError, match='actual evaluator'):
        evaluate_frozen(artifact, [{'id': 'sealed'}], JevClient(mock=True), evaluator=different_evaluator)


def test_mutated_rule_is_rejected_at_freeze_and_replay(tmp_path):
    rule = tmp_path / 'rules.json'
    rule.write_text('{"reward":"win"}')
    contract = build_task_contract(evaluator, files=[rule])
    store = RunStore(tmp_path / 'runs')
    result = evolve(store, Proposer(), task_contract=contract)
    artifact = freeze_run(store, result['run_id'])
    rule.write_text('{"reward":"always win"}')
    with pytest.raises(ValueError, match='files changed'):
        freeze_run(store, result['run_id'])
    before = len(EVALUATIONS)
    with pytest.raises(ValueError, match='files changed'):
        evaluate_frozen(artifact, [{'id': 'sealed'}], JevClient(mock=True), evaluator=evaluator)
    assert len(EVALUATIONS) == before


@pytest.mark.parametrize('mutation', ['modify', 'add', 'remove'])
def test_actual_engine_tree_is_bound_even_when_package_lock_is_unchanged(tmp_path, mutation):
    engine = tmp_path / 'engine'
    engine.mkdir()
    module = engine / 'battle.js'
    module.write_text('const engineVersion = 1;')
    lock = tmp_path / 'package-lock.json'
    lock.write_text('{"version":"fixed"}')
    contract = build_task_contract(evaluator, files=[lock], trees=[engine])
    assert contract['files'][str(lock)] == hashlib.sha256(lock.read_bytes()).hexdigest()
    if mutation == 'modify':
        module.write_text('const engineVersion = 2;')
    elif mutation == 'add':
        (engine / 'new.js').write_text('new engine code')
    else:
        module.unlink()
    with pytest.raises(ValueError, match='trees changed'):
        verify_task_contract(contract, evaluator=evaluator)


def test_manifest_does_not_follow_unbound_tree_symlinks(tmp_path):
    tree = tmp_path / 'engine'
    tree.mkdir()
    outside = tmp_path / 'other-code'
    outside.write_text('outside')
    (tree / 'linked').symlink_to(outside)
    with pytest.raises(ValueError, match='symlinks'):
        build_task_contract(evaluator, trees=[tree])


def test_git_commit_and_clean_tracked_source_are_independently_verified(tmp_path):
    repo = tmp_path / 'game'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)
    git('init')
    source = repo / 'engine.py'
    source.write_text('VERSION = 1\n')
    git('add', 'engine.py')
    git('-c', 'user.name=Contract Test', '-c', 'user.email=contract@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-m', 'engine v1')
    contract = build_task_contract(evaluator, git=[repo])
    source.write_text('VERSION = 2\n')
    with pytest.raises(ValueError, match='tracked changes'):
        verify_task_contract(contract)
    git('add', 'engine.py')
    git('-c', 'user.name=Contract Test', '-c', 'user.email=contract@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-m', 'engine v2')
    with pytest.raises(ValueError, match='git revision changed'):
        verify_task_contract(contract)


def test_executable_binding_re_resolves_path_before_freeze_and_replay(tmp_path, monkeypatch):
    original_bin = tmp_path / 'original-bin'
    replacement_bin = tmp_path / 'replacement-bin'
    original_bin.mkdir()
    replacement_bin.mkdir()
    for directory in (original_bin, replacement_bin):
        command = directory / 'contract-game-engine'
        command.write_text('not executed; identical bytes deliberately isolate PATH identity')
        command.chmod(0o700)
    monkeypatch.setenv('PATH', str(original_bin))
    contract = build_task_contract(evaluator, executables=['contract-game-engine'])
    assert contract['executables']['contract-game-engine']['path'] == str(original_bin / 'contract-game-engine')
    store = RunStore(tmp_path / 'runs')
    result = evolve(store, Proposer(), task_contract=contract)
    artifact = freeze_run(store, result['run_id'])
    monkeypatch.setenv('PATH', str(replacement_bin))
    assert (original_bin / 'contract-game-engine').is_file()
    with pytest.raises(ValueError, match='executable changed'):
        verify_task_contract(contract, evaluator=evaluator)
    with pytest.raises(ValueError, match='executable changed'):
        freeze_run(store, result['run_id'])
    before = len(EVALUATIONS)
    with pytest.raises(ValueError, match='executable changed'):
        evaluate_frozen(artifact, [{'id': 'sealed'}], JevClient(mock=True), evaluator=evaluator)
    assert len(EVALUATIONS) == before


def test_executable_binding_detects_changed_bytes_and_unavailable_command(tmp_path, monkeypatch):
    command = tmp_path / 'contract-game-engine'
    command.write_text('version one')
    command.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    contract = build_task_contract(evaluator, executables=['contract-game-engine'])
    command.write_text('version two')
    with pytest.raises(ValueError, match='executable changed'):
        verify_task_contract(contract)
    command.unlink()
    with pytest.raises(ValueError):
        verify_task_contract(contract)


def test_legacy_task_contract_keeps_no_executable_requirement(tmp_path, monkeypatch):
    contract = build_task_contract(evaluator)
    assert 'executables' not in contract
    monkeypatch.setenv('PATH', str(tmp_path))
    assert verify_task_contract(contract, evaluator=evaluator) == contract


def test_executable_identity_does_not_retarget_a_stored_canonical_path(tmp_path, monkeypatch):
    command = tmp_path / 'contract-game-engine'
    other = tmp_path / 'other-engine'
    for path in (command, other):
        path.write_text('same content')
        path.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    contract = build_task_contract(evaluator, executables=['contract-game-engine'])
    command.unlink()
    command.symlink_to(other)
    with pytest.raises(ValueError, match='executable changed'):
        verify_task_contract(contract)


def test_resume_rejects_changed_task_context_or_new_resource_contract(tmp_path):
    resource = tmp_path / 'rules.json'
    resource.write_text('{}')
    original = build_task_contract(evaluator)
    store = RunStore(tmp_path / 'runs')
    first = evolve(store, Proposer(), task_context={'objective': 'wins'}, task_contract=original)
    with pytest.raises(ValueError, match='task_context'):
        evolve(store, Proposer(), evolution_rounds=2, resume_run_id=first['run_id'],
               task_context={'objective': 'damage'}, task_contract=original)
    changed = build_task_contract(evaluator, files=[resource])
    with pytest.raises(ValueError, match='task_contract'):
        evolve(store, Proposer(), evolution_rounds=2, resume_run_id=first['run_id'],
               task_context={'objective': 'wins'}, task_contract=changed)
    resumed = evolve(store, Proposer(), evolution_rounds=2, resume_run_id=first['run_id'],
                     task_context={'objective': 'wins'}, task_contract=original)
    assert resumed['rounds_completed'] == 2


def test_task_infrastructure_failure_archives_partial_trace_and_aborts_round(tmp_path):
    class InfrastructureError(RuntimeError):
        task_infrastructure_error = True
    def interrupted(spec, episode, jev, capture_traces=True):
        if spec['output'] == '1' and episode['id'] == 'validation':
            cause = InfrastructureError('game engine disconnected')
            raise PipelineExecutionError('battle interrupted', cause=cause,
                partial_result={'traces': [{'trace': [{'id': 'played-turn', 'output': 'retain'}]}]})
        return evaluator(spec, episode, jev, capture_traces)
    store = RunStore(tmp_path)
    proposer = Proposer()
    with pytest.raises(InfrastructureError, match='disconnected'):
        evolve(store, proposer, evaluator=interrupted)
    ident = store.list_runs()[0]['run_id']
    run = store.get_run(ident)
    assert run['status'] == 'failed' and run['progress']['rounds_completed'] == 0
    assert len(proposer.prompts) == 1
    trace = store.load_trace(ident, 'validation', spec_hash(pipeline('1')), 'validation')
    assert trace['traces'][0]['trace'][0]['id'] == 'played-turn'


def test_ordinary_bad_policy_is_still_a_candidate_failure(tmp_path):
    def invalid_action(spec, episode, jev, capture_traces=True):
        if spec['output'] == '1':
            raise PipelineExecutionError('illegal action', cause=ValueError('illegal action'),
                                         partial_result={'traces': []})
        return evaluator(spec, episode, jev, capture_traces)
    result = evolve(RunStore(tmp_path), Proposer(), evaluator=invalid_action)
    assert result['rounds_completed'] == 1 and result['rounds_accepted'] == 0
    assert result['best_score'] == 0


def test_runtime_hash_includes_optional_python_node_implementation(tmp_path, monkeypatch):
    import auto_jev.frozen as frozen
    for name in ('spec.py', 'flow.py', 'runtime.py', 'crypto.py', 'providers.py', 'vercel.py',
                 'paper.py', 'observations.py', 'data.py', 'storage.py', 'frozen.py'):
        (tmp_path / name).write_text(name)
    monkeypatch.setattr(frozen, '__file__', str(tmp_path / 'frozen.py'))
    before = frozen.source_hash()
    (tmp_path / 'python_nodes.py').write_text('version one')
    added = frozen.source_hash()
    assert added != before
    (tmp_path / 'python_nodes.py').write_text('version two')
    assert frozen.source_hash() != added


def test_v3_prompt_and_downgrade_guard_when_runtime_is_available(tmp_path):
    seed = pipeline(version=3)
    if 'version' not in inspect.signature(pipeline_schema).parameters:
        pytest.skip('v3 runtime is being supplied independently')
    validate_spec(seed)
    proposer = Proposer(pipeline(version=2))
    store = RunStore(tmp_path)
    result = evolve(store, proposer, seed_pipeline=seed)
    instruction = proposer.prompts[0]['instruction']
    assert 'run(obs, nodes, memory)' in instruction
    assert 'questions_expression' in instruction and 'explicit depends_on' in instruction
    assert result['rounds_completed'] == 1 and result['rounds_accepted'] == 0
    assert 'Version 3' in store.list_events(result['run_id'], 'proposal_rejected')[0]['payload']['reason']


@pytest.mark.parametrize('context', [[], {'objective': ''}, {'objective': 1}, {'bad': float('nan')}])
def test_invalid_task_context_fails_before_model(tmp_path, context):
    proposer = Proposer()
    with pytest.raises(ValueError):
        evolve(RunStore(tmp_path), proposer, task_context=context)
    assert not proposer.prompts
