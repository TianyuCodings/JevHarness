"""Flow contracts, verified without models, network, or latency thresholds."""
import copy
from threading import Barrier, Event, Lock

import pytest

from auto_jev.flow import compile_flow, expression_dependencies
from auto_jev.runtime import PipelineExecutionError, PipelineRuntime
from auto_jev.spec import validate_spec


def pipeline(nodes=(), *, version=2, output="nodes", memory_update=None):
    result = {
        "version": version,
        "name": "flow contract",
        "jev_model": "typesafe-ai/jev",
        "nodes": copy.deepcopy(list(nodes)),
        "output": output,
    }
    if memory_update is not None:
        result["memory_update"] = memory_update
    return result


def expr(ident, expression, **extra):
    return {"id": ident, "kind": "expression", "expression": expression, **extra}


def judge_node(ident, state=None, **extra):
    return {
        "id": ident,
        "kind": "jev",
        "state": state or repr({"id": ident}),
        "questions": {"q": {"type": "noul", "instructions": "Is the input valid?"}},
        **extra,
    }


def answer(value=1.0):
    return {"answers": {"q": {"noul": value}}}


class FunctionJev:
    def __init__(self, function):
        self.function = function

    def judge(self, state, questions, model=None):
        return self.function(state, questions)


class UnexpectedJev:
    def judge(self, *args, **kwargs):
        pytest.fail("This node must not call Jev")


def test_unordered_graph_unions_explicit_and_inferred_dependencies_without_mutation():
    spec = pipeline([
        expr("child", "nodes['a'] + nodes['b']", depends_on=["c"]),
        expr("c", "3"),
        expr("b", "2"),
        expr("a", "1"),
    ])
    original = copy.deepcopy(spec)
    assert compile_flow(spec) == {"child": ["c", "b", "a"], "c": [], "b": [], "a": []}
    assert expression_dependencies("nodes['a']['nested'] + nodes['b']") == {"a", "b"}
    assert validate_spec(spec) == original
    result = PipelineRuntime(spec, UnexpectedJev()).run({})
    assert result["nodes"]["child"] == 3
    assert spec == original


@pytest.mark.parametrize("nodes", [
    [expr("a", "nodes['missing']")],
    [expr("a", "1", depends_on=["missing"])],
    [expr("a", "1", depends_on=["a"])],
    [expr("a", "nodes['a']")],
    [expr("a", "1", depends_on=["b", "b"]), expr("b", "2")],
    [expr("a", "1", depends_on="b"), expr("b", "2")],
    [expr("a", "1", depends_on=[1])],
    [expr("a", "nodes['b']"), expr("b", "nodes['a']")],
    [expr("a", "1", depends_on=["b"]), expr("b", "1", depends_on=["a"])],
    [expr("a", "1"), expr("a", "2")],
])
def test_invalid_dependency_graph_rejected_before_execution(nodes):
    with pytest.raises(ValueError):
        PipelineRuntime(pipeline(nodes), UnexpectedJev())


@pytest.mark.parametrize("expression", [
    "nodes", "get(nodes, 'a')", "nodes[obs['key']]", "nodes['a' if obs else 'b']",
])
def test_v2_node_rejects_untraceable_dynamic_or_bare_nodes(expression):
    with pytest.raises(ValueError):
        validate_spec(pipeline([expr("a", "1"), expr("b", expression)]))


def test_independent_jev_nodes_really_overlap_and_trace_order_is_deterministic():
    rendezvous = Barrier(2)
    second_finished = Event()
    completions = []
    lock = Lock()

    def judge(state, questions):
        # A serial runtime cannot pass this rendezvous. Timeout only prevents a hang.
        rendezvous.wait(timeout=5)
        if state["id"] == "first":
            assert second_finished.wait(timeout=5)
            with lock:
                completions.append("first")
        else:
            with lock:
                completions.append("second")
            second_finished.set()
        return answer()

    result = PipelineRuntime(
        pipeline([judge_node("first"), judge_node("second")]), FunctionJev(judge), max_workers=2
    ).run({"immutable": [1]}, {"counter": 3})
    assert completions == ["second", "first"]
    assert list(result["nodes"]) == ["first", "second"]
    assert [record["id"] for record in result["trace"]] == ["first", "second"]
    assert result["execution"]["max_workers"] == 2
    assert result["execution"]["dependencies"] == {"first": [], "second": []}
    for record in result["trace"]:
        assert record["status"] == "ok"
        assert record["depends_on"] == []
        assert 0 <= record["started_ms"] <= record["ended_ms"]
        assert record["elapsed_ms"] >= 0
        assert record["input_context"] == {"obs": {"immutable": [1]}, "nodes": {}, "memory": {"counter": 3}}
        assert record["state_expression"] == repr({"id": record["id"]})
        assert record["state"] == {"id": record["id"]}
        assert record["questions"] == judge_node(record["id"])["questions"]
        assert record["response"] == answer()
        assert record["output"] == answer()["answers"]


def test_ready_descendant_starts_before_unrelated_slow_sibling_finishes():
    slow_started = Event()
    child_completed = Event()

    def judge(state, questions):
        if state["id"] == "slow":
            slow_started.set()
            # A whole-level barrier deadlocks here; the ready child must run now.
            assert child_completed.wait(timeout=5)
        elif state["id"] == "fast":
            assert slow_started.wait(timeout=5)
        else:
            assert state["parent_value"] == 1.0
            child_completed.set()
        return answer()

    spec = pipeline([
        judge_node("slow"),
        judge_node("fast"),
        judge_node("child", "{'id': 'child', 'parent_value': nodes['fast']['q']['noul']}"),
    ])
    result = PipelineRuntime(spec, FunctionJev(judge), max_workers=2).run({})
    assert child_completed.is_set()
    assert result["execution"]["dependencies"]["child"] == ["fast"]
    assert all(t["status"] == "ok" for t in result["trace"])


def test_v1_preserves_serial_execution_and_all_prior_outputs():
    calls = []

    def judge(state, questions):
        calls.append(state["id"])
        if state["id"] == "second":
            assert calls == ["first", "second"]
            assert state["seen"] == {"first": {"q": {"noul": 1.0}}}
        return answer()

    spec = pipeline([
        judge_node("first"),
        judge_node("second", "{'id': 'second', 'seen': nodes}"),
    ], version=1)
    result = PipelineRuntime(spec, FunctionJev(judge), max_workers=8).run({})
    assert calls == ["first", "second"]
    assert result["execution"]["dependencies"] == {"first": [], "second": ["first"]}
    assert result["trace"][1]["input_context"]["nodes"] == {"first": {"q": {"noul": 1.0}}}


def test_node_context_contains_only_direct_dependencies_and_inputs_are_isolated():
    obs = {"nested": [1]}
    memory = {"nested": [2]}
    spec = pipeline([
        expr("a", "10"),
        expr("b", "nodes['a'] + 1"),
        judge_node("c", "{'observation': obs, 'memory': memory}", depends_on=["b"]),
    ], memory_update="{'nested': memory['nested'], 'count': len(nodes)}")

    def judge(state, questions):
        state["observation"]["nested"].append(99)
        state["memory"]["nested"].append(99)
        return answer()

    result = PipelineRuntime(spec, FunctionJev(judge)).run(obs, memory)
    trace = {record["id"]: record for record in result["trace"]}
    assert trace["c"]["input_context"]["nodes"] == {"b": 11}
    assert trace["c"]["input_context"]["obs"] == {"nested": [1]}
    assert obs == {"nested": [1]}
    assert memory == {"nested": [2]}
    assert result["memory_before"] == {"nested": [2]}
    assert result["memory"] == {"nested": [2], "count": 3}
    assert isinstance(result["finalization"], dict) and result["finalization"]


def test_node_error_reports_blocked_and_cancelled_nodes_without_committing_memory():
    original_cause = RuntimeError("fake node failed")

    def judge(state, questions):
        assert state["id"] == "bad", "The runtime launched work after failure"
        raise original_cause

    spec = pipeline([
        expr("completed", "42"),
        judge_node("bad", depends_on=["completed"]),
        judge_node("blocked", depends_on=["bad"]),
        judge_node("cancelled", depends_on=["completed"]),
    ], memory_update="{'committed': True}")
    with pytest.raises(PipelineExecutionError) as caught:
        PipelineRuntime(spec, FunctionJev(judge), max_workers=1).run({}, {"original": 1})
    assert caught.value.cause is original_cause
    partial = caught.value.partial_result
    assert [t["id"] for t in partial["trace"]] == ["completed", "bad", "blocked", "cancelled"]
    assert {t["id"]: t["status"] for t in partial["trace"]} == {
        "completed": "ok", "bad": "error", "blocked": "blocked", "cancelled": "cancelled",
    }
    assert partial["nodes"] == {"completed": 42}
    assert partial["memory"] == partial["memory_before"] == {"original": 1}


def test_already_running_sibling_is_drained_and_included_after_a_failure():
    both_running = Barrier(2)
    cause = RuntimeError("one concurrent branch failed")

    def judge(state, questions):
        both_running.wait(timeout=5)
        if state["id"] == "bad":
            raise cause
        return answer(0.25)

    spec = pipeline([
        judge_node("bad"),
        judge_node("survivor"),
        expr("blocked", "nodes['bad']['q']['noul']"),
    ])
    with pytest.raises(PipelineExecutionError) as caught:
        PipelineRuntime(spec, FunctionJev(judge), max_workers=2).run({}, {"saved": 1})
    partial = caught.value.partial_result
    assert caught.value.cause is cause
    assert {t["id"]: t["status"] for t in partial["trace"]} == {
        "bad": "error", "survivor": "ok", "blocked": "blocked",
    }
    assert partial["nodes"] == {"survivor": {"q": {"noul": 0.25}}}
    assert partial["memory"] == {"saved": 1}


@pytest.mark.parametrize("output,memory_update", [("1 / 0", "{'new': True}"), ("nodes['a']", "[]")])
def test_finalization_failure_preserves_completed_nodes_and_original_memory(output, memory_update):
    spec = pipeline([expr("a", "7")], output=output, memory_update=memory_update)
    with pytest.raises(PipelineExecutionError) as caught:
        PipelineRuntime(spec, UnexpectedJev()).run({}, {"keep": [1]})
    partial = caught.value.partial_result
    assert partial["nodes"] == {"a": 7}
    assert [(t["id"], t["status"]) for t in partial["trace"]] == [("a", "ok")]
    assert partial["memory"] == partial["memory_before"] == {"keep": [1]}
    assert isinstance(partial["finalization"], dict) and partial["finalization"]
    assert isinstance(caught.value.cause, ValueError)


def test_base_exception_is_not_converted_to_candidate_failure():
    def interrupted(state, questions):
        raise KeyboardInterrupt("operator interruption")

    with pytest.raises(KeyboardInterrupt):
        PipelineRuntime(pipeline([judge_node("interrupted")]), FunctionJev(interrupted)).run({})


def test_provider_cannot_mutate_frozen_question_definition_or_returned_alias():
    shared={'answers':{'q':{'noul':.5}}}
    def mutate(state, questions):
        questions['q']['instructions']='malicious mutation'
        return shared
    spec=pipeline([judge_node('call')])
    runtime=PipelineRuntime(spec,FunctionJev(mutate))
    result=runtime.run({})
    shared['answers']['q']['noul']=.9
    assert runtime.spec==spec
    assert result['nodes']['call']['q']['noul']==.5


@pytest.mark.parametrize('memory',[[],[1],0,False,'text'])
def test_memory_requires_dictionary_even_without_update(memory):
    with pytest.raises(ValueError,match='memory'):
        PipelineRuntime(pipeline(),UnexpectedJev()).run({},memory)


def test_nested_sequence_expansion_has_total_budget():
    from auto_jev.spec import evaluate_expression
    with pytest.raises(ValueError,match='total JSON element'):
        evaluate_expression('[[0]*10000]*10000',{'obs':{},'nodes':{},'memory':{}})
