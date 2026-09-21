"""Complete episode evidence and transactional paper-account auditing."""
import copy
import json
import threading

import httpx
import pytest

from auto_jev import crypto, paper
from auto_jev.crypto import EpisodeExecutionError, evaluate_episode
from auto_jev.frozen import digest, source_hash
from auto_jev.observations import OBSERVATION_CONFIG
from auto_jev.providers import JevClient, JevProviderError
from auto_jev.runtime import PipelineExecutionError
from auto_jev.spec import spec_hash


def pipeline(*, version=2, risky="1.0", output="nodes['risky']"):
    return {"version": version, "name": "trace regression", "jev_model": "typesafe-ai/jev",
            "nodes": [{"id": "first", "kind": "expression", "expression": "1.0"},
                      {"id": "risky", "kind": "expression", "expression": risky},
                      {"id": "dependent", "kind": "expression", "expression": "nodes['risky']"}],
            "output": output, "memory_update": "{'count': get(memory, 'count', 0) + 1}"}


def flat_episode():
    return {"id": "flat-trace", "asset": "BTC-USD", "interval_seconds": 3600, "news": [],
            "bars": [{"timestamp": i * 3600, "open": 100., "high": 100., "low": 100.,
                      "close": 100., "volume": 10.} for i in range(4)]}


@pytest.mark.parametrize("version", [1, 2])
def test_success_decisions_keep_runtime_graph_finalization_and_memory(version):
    result = evaluate_episode(pipeline(version=version), flat_episode(), JevClient(mock=True))
    assert len(result["traces"]) == 3
    for index, decision in enumerate(result["traces"]):
        assert decision["status"] == "ok"
        assert decision["execution"]["mode"] == ("parallel" if version == 2 else "sequential")
        assert decision["execution"]["dependencies"]["dependent"][-1] == "risky"
        assert decision["memory_before"] == ({"count": index} if index else {})
        assert decision["memory"] == {"count": index + 1}
        assert decision["finalization"]["output"]["status"] == "ok"
        assert decision["finalization"]["memory_update"]["status"] == "ok"
        assert [node["id"] for node in decision["trace"]] == ["first", "risky", "dependent"]
        assert all("input_context" in node for node in decision["trace"])
        assert decision["obs"]["timestamp"] == (index + 1) * 3600
    assert result["final_equity"] == pytest.approx(10000 * .9995 * .999 / (1.0005 * 1.001))
    hidden = evaluate_episode(pipeline(version=version), flat_episode(), JevClient(mock=True), capture_traces=False)
    assert hidden["traces"] == []
    assert hidden["final_equity"] == result["final_equity"]


@pytest.mark.parametrize("capture", [True, False])
def test_node_failure_keeps_prior_decisions_ledger_and_failed_nodes(capture):
    spec = pipeline(version=1, risky="1.0 if len(obs['history']) == 1 else 1 / 0")
    with pytest.raises(EpisodeExecutionError) as caught:
        evaluate_episode(spec, flat_episode(), JevClient(mock=True), capture_traces=capture)
    error = caught.value
    assert isinstance(error, PipelineExecutionError)
    partial = error.partial_result
    assert partial["completed_decisions"] == 1
    assert partial["score"] is None and partial["net_profit"] is None
    assert len(partial["traces"]) == 2
    assert [row["status"] for row in partial["traces"]] == ["ok", "error"]
    assert len(partial["trades"]) == 1 and partial["trades"][0]["side"] == "buy"
    assert len(partial["equity_curve"]) == 2
    assert partial["memory"] == {"count": 1}
    assert partial["quantity"] > 0  # A failed episode is not silently liquidated.
    failed = partial["failed_decision"]
    assert failed["obs"]["timestamp"] == 7200
    assert failed["memory_before"] == failed["memory"] == {"count": 1}
    assert [row["status"] for row in failed["trace"]] == ["ok", "error", "blocked"]
    assert failed["execution"] == partial["runtime_partial_result"]["execution"]


def test_finalization_failure_retains_successful_node_evidence():
    spec = pipeline(output="1.0 if len(obs['history']) == 1 else 1 / 0")
    with pytest.raises(EpisodeExecutionError) as caught:
        evaluate_episode(spec, flat_episode(), JevClient(mock=True))
    failed = caught.value.partial_result["failed_decision"]
    assert all(node["status"] == "ok" for node in failed["trace"])
    assert failed["finalization"]["output"]["status"] == "error"
    assert failed["finalization"]["memory_update"]["status"] in ("blocked", "cancelled", "skipped")
    assert failed["memory"] == {"count": 1}


def test_invalid_trading_output_keeps_completed_run_without_committing_memory():
    spec = pipeline(output="1.0 if len(obs['history']) == 1 else 2.0")
    with pytest.raises(EpisodeExecutionError) as caught:
        evaluate_episode(spec, flat_episode(), JevClient(mock=True))
    partial = caught.value.partial_result
    assert partial["failure"]["phase"] == "output"
    assert partial["memory"] == {"count": 1}
    assert partial["runtime_partial_result"]["memory"] == {"count": 2}
    assert partial["failed_decision"]["output"] == 2.0
    assert len(partial["trades"]) == 1


def test_episode_preserves_all_parallel_causes_including_system_failure():
    barrier = threading.Barrier(2)
    candidate_error, system_error = ValueError("invalid candidate"), JevProviderError("Jev request failed")

    class Client:
        def judge(self, state, questions, **kwargs):
            barrier.wait(timeout=5)
            raise candidate_error if state == "candidate" else system_error

    spec = {"version": 2, "name": "multiple causes", "jev_model": "typesafe-ai/jev", "output": "0.0",
            "nodes": [{"id": name, "kind": "jev", "state": repr(name),
                       "questions": {"q": {"type": "noul", "instructions": "Check."}}}
                      for name in ("candidate", "system")]}
    with pytest.raises(EpisodeExecutionError) as caught:
        evaluate_episode(spec, flat_episode(), Client())
    assert set(caught.value.causes) == {candidate_error, system_error}
    assert caught.value.cause in caught.value.causes
    assert all(row["status"] == "error" for row in caught.value.partial_result["failed_decision"]["trace"])


def test_evolution_archives_parallel_failures_before_aborting_system_error(tmp_path, monkeypatch):
    from auto_jev.evolution import run_evolution
    from auto_jev.runtime import PipelineRuntime
    from auto_jev.storage import RunStore

    barrier, candidate_recorded = threading.Barrier(2), threading.Event()
    candidate_error = ValueError("candidate failed first")
    system_error = JevProviderError("parallel provider failed later")
    original_execute = PipelineRuntime._execute

    def execute(self, node, *args, **kwargs):
        result = original_execute(self, node, *args, **kwargs)
        if node["id"] == "candidate":
            candidate_recorded.set()
        return result

    monkeypatch.setattr(PipelineRuntime, "_execute", execute)

    class Client:
        mock = True
        stats = {}
        metadata = {"transport": "mock"}

        def judge(self, state, questions, **kwargs):
            barrier.wait(timeout=5)
            if state == "candidate":
                raise candidate_error
            assert candidate_recorded.wait(timeout=5)
            raise system_error

    spec = {"version": 2, "name": "mixed failures", "jev_model": "typesafe-ai/jev", "output": "0.0",
            "nodes": [{"id": name, "kind": "jev", "state": repr(name),
                       "questions": {"q": {"type": "noul", "instructions": "Check."}}}
                      for name in ("candidate", "system")]}
    train, validation = flat_episode(), copy.deepcopy(flat_episode())
    train["id"], validation["id"] = "train", "validation"
    for bar in validation["bars"]:
        bar["timestamp"] += 86400
    store = RunStore(tmp_path / "runs")

    def unexpected_proposer(prompt):
        raise AssertionError("System failure must stop before reflection")

    with pytest.raises(JevProviderError) as caught:
        run_evolution([train], [validation], jev=Client(), proposer=unexpected_proposer,
                      store=store, max_metric_calls=4, seed_pipeline=spec)
    assert caught.value is system_error
    runs = store.list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    run_id = runs[0]["run_id"]
    evaluations = store.list_evaluations(run_id)
    assert len(evaluations) == 1 and evaluations[0]["status"] == "error"
    trace_paths = [path for path in store.run_dir(run_id).glob("traces/**/*.json")
                   if not path.name.endswith('.index.json')]
    assert len(trace_paths) == 1
    saved = json.loads(trace_paths[0].read_text())
    assert saved["traces"]
    failed = saved["failed_decision"]
    assert [row["status"] for row in failed["trace"]] == ["error", "error"]
    assert "candidate failed first" in saved["error"]
    assert "parallel provider failed later" in json.dumps(saved)


def frozen(spec):
    artifact = {"version": 1, "task_id": "crypto_spot", "spec": spec, "spec_hash": spec_hash(spec),
                "source_hash": source_hash(), "observation_config": OBSERVATION_CONFIG.copy(),
                "costs": {"initial_cash": 10000., "fee_bps": 10., "slippage_bps": 5.},
                "jev_metadata": {"transport": "mock"}}
    artifact["artifact_hash"] = digest(artifact)
    return artifact


@pytest.fixture
def paper_market(monkeypatch):
    clock = [1_800_000_123.]

    def market(*args, **kwargs):
        end = int(clock[0]) // 3600 * 3600
        return {"bars": [{"timestamp": end - (3 - i) * 3600, "open": 100., "high": 100.,
                          "low": 100., "close": 100., "volume": 10.} for i in range(3)]}

    monkeypatch.setattr(paper.time, "time", lambda: clock[0])
    monkeypatch.setattr(paper, "fetch_coinbase", market)
    monkeypatch.setattr(paper, "_ticker", lambda asset: {"mid": 100., "received_at": clock[0] + 1})
    monkeypatch.setattr(paper, "fetch_rss", lambda url: [])
    return clock


def test_paper_success_has_complete_step_archive(tmp_path, paper_market):
    path = tmp_path / "account.json"
    result = paper.paper_step(frozen(pipeline()), JevClient(mock=True), state_path=path)
    trace = result["state"]["traces"][-1]
    audit = json.loads((tmp_path / "account.traces" / result["audit_path"].split("/")[-1]).read_text())
    assert trace["execution"]["mode"] == "parallel"
    assert trace["memory_before"] == {}
    assert trace["memory"] == {"count": 1}
    assert trace["runtime_trace"] == trace["trace"]
    assert trace["finalization"]["memory_update"]["status"] == "ok"
    assert audit["trace"] == trace
    assert audit["account_before"]["quantity"] == 0
    assert audit["account_after"]["quantity"] > 0
    assert audit["account_after"]["cash"] == pytest.approx(0., abs=1e-10)


def test_paper_node_failure_keeps_previous_account_and_full_failure(tmp_path, paper_market):
    path = tmp_path / "account.json"
    spec = pipeline(risky="1.0 if get(memory, 'count', 0) == 0 else 1 / 0")
    artifact = frozen(spec)
    first = paper.paper_step(artifact, JevClient(mock=True), state_path=path)
    before = path.read_bytes()
    paper_market[0] += 3600
    with pytest.raises(paper.PaperExecutionError) as caught:
        paper.paper_step(artifact, JevClient(mock=True), state_path=path)
    assert path.read_bytes() == before
    failure = json.loads((tmp_path / "account.failure.json").read_text())
    assert failure["account_committed"] is False
    assert failure["trades"] == first["state"]["trades"]
    assert failure["equity_curve"] == first["state"]["equity"]
    assert len(failure["traces"]) == 2
    assert failure["memory"] == {"count": 1}
    assert failure["runtime_partial_result"]["finalization"]["output"]["status"] != "ok"
    assert len(list((tmp_path / "account.traces").glob("*.json"))) == 2
    assert caught.value.partial_result["parent_audit_path"] == first["audit_path"]


def test_paper_quote_failure_retains_completed_pipeline_and_http_cause(tmp_path, paper_market, monkeypatch):
    path = tmp_path / "account.json"
    artifact = frozen(pipeline())
    paper.paper_step(artifact, JevClient(mock=True), state_path=path)
    before = path.read_bytes()
    paper_market[0] += 3600
    cause = httpx.ReadTimeout("public quote timed out")

    def fail_quote(asset):
        raise cause

    monkeypatch.setattr(paper, "_ticker", fail_quote)
    with pytest.raises(paper.PaperExecutionError) as caught:
        paper.paper_step(artifact, JevClient(mock=True), state_path=path)
    assert caught.value.cause is cause and caught.value.causes == (cause,)
    assert path.read_bytes() == before
    failure = caught.value.partial_result
    assert failure["failure"]["phase"] == "quote"
    assert all(row["status"] == "ok" for row in failure["failed_decision"]["trace"])
    assert failure["memory"] == {"count": 1}
    assert failure["runtime_partial_result"]["memory"] == {"count": 2}


def test_paper_account_write_failure_does_not_report_committed_fills(tmp_path, paper_market, monkeypatch):
    path = tmp_path / "account.json"
    original_write = paper.atomic_write_json

    def failing_write(destination, value):
        if destination == path:
            raise OSError("test account write failure")
        return original_write(destination, value)

    monkeypatch.setattr(paper, "atomic_write_json", failing_write)
    with pytest.raises(paper.PaperExecutionError) as caught:
        paper.paper_step(frozen(pipeline()), JevClient(mock=True), state_path=path)
    assert not path.exists()
    partial = caught.value.partial_result
    assert partial["failure"]["phase"] == "account_commit"
    assert partial["account_committed"] is False
    assert partial["trades"] == [] and len(partial["attempted_trades"]) == 1
    archived = json.loads(next((tmp_path / "account.traces").glob("*.json")).read_text())
    assert archived["status"] == "error" and not archived["account_committed"]


def test_paper_retains_legacy_evidence_when_display_window_rolls(tmp_path, paper_market):
    path = tmp_path / "account.json"
    artifact = frozen(pipeline())
    legacy = paper._load(path, artifact["artifact_hash"], "BTC-USD", artifact["costs"])
    legacy["traces"] = [{"legacy_decision": index} for index in range(200)]
    path.write_text(json.dumps(legacy))
    result = paper.paper_step(artifact, JevClient(mock=True), state_path=path)
    assert len(result["state"]["traces"]) == 200
    assert result["state"]["traces"][0] == {"legacy_decision": 1}
    archived = json.loads(next((tmp_path / "account.traces").glob("*.json")).read_text())
    assert archived["initial_state"]["traces"] == legacy["traces"]
    assert archived["trace"] == result["state"]["traces"][-1]


@pytest.mark.parametrize("adapter", ["episode", "paper"])
def test_adapters_do_not_catch_base_exceptions(adapter, tmp_path, paper_market, monkeypatch):
    class InterruptedRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            raise KeyboardInterrupt("operator stop")

    monkeypatch.setattr(crypto if adapter == "episode" else paper, "PipelineRuntime", InterruptedRuntime)
    with pytest.raises(KeyboardInterrupt, match="operator stop"):
        if adapter == "episode":
            evaluate_episode(pipeline(), flat_episode(), JevClient(mock=True))
        else:
            paper.paper_step(frozen(pipeline()), JevClient(mock=True), state_path=tmp_path / "account.json")
    assert not (tmp_path / "account.json").exists()
    assert not (tmp_path / "account.failure.json").exists()
