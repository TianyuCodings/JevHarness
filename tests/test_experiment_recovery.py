"""Crash-boundary regressions for the sealed-test experiment stage."""
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_jev import crypto, evolution, experiment, frozen, providers, storage
from auto_jev.crypto import EpisodeExecutionError
from auto_jev.providers import JevProviderError
from auto_jev.storage import RunStore, atomic_write_json


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    events, evaluations, baselines = [], Counter(), Counter()
    cfg = {"name": "recovery", "assets": ["BTC-USD", "ETH-USD"], "interval_seconds": 3600,
           "bars_per_split": 6, "train_batch_bars": 3, "evolution_rounds": 50,
           "costs": {}, "output_dir": str(tmp_path / "output"), "runs_dir": str(tmp_path / "runs")}

    def episode(asset, split, start, count):
        return {"id": f"{asset}-{split}", "asset": asset, "interval_seconds": 3600,
                "bars": [{"timestamp": start + i * 3600} for i in range(count)], "news": []}

    inputs = {"train": [episode(asset, f"train-{i}", i * 10800, 3) for asset in cfg["assets"] for i in range(2)],
              "validation": [episode(asset, "validation", 21600, 6) for asset in cfg["assets"]],
              "test": [episode(asset, "sealed", 43200, 6) for asset in cfg["assets"]],
              "proposer_config": {"kind": "mock"}, "seed_pipeline": {"test": "only"}}
    for name, value in inputs.items():
        path = tmp_path / f"{name}.json"
        atomic_write_json(path, value)
        cfg[name] = str(path)
    config = tmp_path / "config.json"
    atomic_write_json(config, cfg)
    store = RunStore(cfg["runs_dir"])

    class Jev:
        def __init__(self, **kwargs):
            self.stats = {}
            self.metadata = {"transport": "mock"}

    monkeypatch.setattr(providers, "JevClient", Jev)
    monkeypatch.setattr(providers, "make_proposer", lambda cfg: object())
    monkeypatch.setattr(frozen, "source_hash", lambda: "stable-runtime-contract")

    def evolve(train, validation, **kwargs):
        events.append("evolution")
        assert all("sealed" not in row["id"] for row in train + validation)
        assert kwargs["evolution_rounds"] == 50 and kwargs["reflection_batch_size"] == 1
        assert kwargs["max_metric_calls"] is None
        ident = kwargs["store"].create_run("recovery", {})
        kwargs["store"].update_run(ident, status="completed")
        return {"run_id": ident}

    def freeze(store, run_id):
        events.append("freeze")
        artifact = {"spec_hash": "a" * 64, "source_hash": "stable-runtime-contract",
                    "artifact_hash": f"freeze-{events.count('freeze')}"}
        store.save_frozen(run_id, artifact)
        return artifact

    def evaluate(artifact, episodes, jev):
        assert "freeze" in events
        ep = episodes[0]
        evaluations[ep["asset"]] += 1
        events.append("test:" + ep["asset"])
        return [{"episode_id": ep["id"], "asset": ep["asset"], "score": .1, "net_profit": 1000.,
                 "initial_cash": 10000., "final_equity": 11000., "fees_paid": 20., "decisions": 5,
                 "traces": [{"trace": [{"id": "proof", "output": "COMPLETE_TEST_EVIDENCE"}]}]}]

    def baseline(ep, **kwargs):
        baselines[ep["asset"]] += 1
        return {"cash": {"score": 0., "net_profit": 0.}}

    monkeypatch.setattr(evolution, "run_evolution", evolve)
    monkeypatch.setattr(evolution, "freeze_run", freeze)
    monkeypatch.setattr(frozen, "evaluate_frozen", evaluate)
    monkeypatch.setattr(crypto, "evaluate_baselines", baseline)
    return SimpleNamespace(config=config, cfg=cfg, store=store, output=Path(cfg["output_dir"]), events=events,
                           evaluations=evaluations, baselines=baselines, evaluate=evaluate, baseline=baseline)


def state(workflow):
    return json.loads((workflow.output / "state.json").read_text())


def assert_completed_once(workflow, result):
    assert result["status"] == "completed"
    assert workflow.events.count("evolution") == 1
    assert workflow.evaluations == {"BTC-USD": 1, "ETH-USD": 1}
    rows = workflow.store.list_holdout(result["run_id"])
    summaries = json.loads((workflow.output / "test_results.json").read_text())
    assert len(rows) == len(summaries) == 2
    assert rows == summaries
    assert {row["artifact_hash"] for row in rows} == {"freeze-1"}
    assert result["artifact_hash"] == "freeze-1"
    assert result["test_completed_ids"] == ["BTC-USD-sealed", "ETH-USD-sealed"]
    before = list(workflow.events)
    assert experiment.run_experiment(workflow.config, resume=True)["status"] == "completed"
    assert workflow.events == before


@pytest.mark.parametrize("boundary", ["baseline", "summary_write"])
def test_saved_test_trace_is_reused_after_local_postprocessing_failure(workflow, monkeypatch, boundary):
    failed = False
    if boundary == "baseline":
        def baseline(ep, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("injected baseline failure")
            return workflow.baseline(ep, **kwargs)
        monkeypatch.setattr(crypto, "evaluate_baselines", baseline)
    else:
        original = experiment.atomic_write_json
        def write(path, value):
            nonlocal failed
            if Path(path).name == "test_results.json" and not failed:
                failed = True
                raise OSError("injected summary failure")
            return original(path, value)
        monkeypatch.setattr(experiment, "atomic_write_json", write)
    with pytest.raises(OSError, match="injected"):
        experiment.run_experiment(workflow.config)
    assert workflow.evaluations == {"BTC-USD": 1}
    result = experiment.run_experiment(workflow.config, resume=True)
    assert_completed_once(workflow, result)
    first = json.loads((workflow.output / "test_results.json").read_text())[0]
    assert first["recovered_from_trace"] is True


@pytest.mark.parametrize("write_before_crash", [False, True])
def test_holdout_commit_is_reconciled_without_duplicate_evaluation_or_rows(workflow, monkeypatch, write_before_crash):
    original, failed = RunStore.append_holdout, False

    def append(self, run_id, metrics):
        nonlocal failed
        if not failed:
            failed = True
            if write_before_crash:
                original(self, run_id, metrics)
            raise OSError("injected holdout commit failure")
        return original(self, run_id, metrics)

    monkeypatch.setattr(RunStore, "append_holdout", append)
    with pytest.raises(OSError, match="injected"):
        experiment.run_experiment(workflow.config)
    assert workflow.evaluations == {"BTC-USD": 1}
    before = workflow.store.list_holdout(state(workflow)["run_id"])
    assert len(before) == int(write_before_crash)
    assert_completed_once(workflow, experiment.run_experiment(workflow.config, resume=True))


def test_final_state_commit_failure_reuses_all_existing_test_results(workflow, monkeypatch):
    original, failed = experiment.atomic_write_json, False

    def write(path, value):
        nonlocal failed
        if Path(path).name == "state.json" and value.get("status") == "completed" and not failed:
            failed = True
            raise OSError("injected final state failure")
        return original(path, value)

    monkeypatch.setattr(experiment, "atomic_write_json", write)
    with pytest.raises(OSError, match="injected"):
        experiment.run_experiment(workflow.config)
    assert workflow.evaluations == {"BTC-USD": 1, "ETH-USD": 1}
    assert_completed_once(workflow, experiment.run_experiment(workflow.config, resume=True))


@pytest.mark.parametrize("conflict", ["artifact_hash", "archived_metrics"])
def test_resume_rejects_conflicting_summary_or_holdout_archive(workflow, monkeypatch, conflict):
    original = RunStore.append_holdout

    def fail_after_write(self, run_id, metrics):
        original(self, run_id, metrics)
        raise OSError("pause before second test")

    with monkeypatch.context() as patch:
        patch.setattr(RunStore, "append_holdout", fail_after_write)
        with pytest.raises(OSError):
            experiment.run_experiment(workflow.config)
    if conflict == "artifact_hash":
        path = workflow.output / "test_results.json"
        records = json.loads(path.read_text())
        records[0]["artifact_hash"] = "another-freeze"
    else:
        path = workflow.store.run_dir(state(workflow)["run_id"]) / "holdout.json"
        records = json.loads(path.read_text())
        records[0]["net_profit"] += 1
    atomic_write_json(path, records)
    with pytest.raises(ValueError, match="different frozen artifact|differs from archived"):
        experiment.run_experiment(workflow.config, resume=True)
    assert workflow.evaluations == {"BTC-USD": 1}
    assert state(workflow)["status"] == "failed"


def test_test_bytes_are_sealed_before_search_but_json_is_parsed_only_after_freeze(workflow, monkeypatch):
    original_read, original_sha = experiment._read, experiment._sha

    def read(path):
        if Path(path) == Path(workflow.cfg["test"]):
            assert "freeze" in workflow.events
            workflow.events.append("test_json_read")
        return original_read(path)

    def sha(path):
        if Path(path) == Path(workflow.cfg["test"]):
            workflow.events.append("test_bytes_hash")
        return original_sha(path)

    monkeypatch.setattr(experiment, "_read", read)
    monkeypatch.setattr(experiment, "_sha", sha)
    experiment.run_experiment(workflow.config)
    assert workflow.events.index("test_bytes_hash") < workflow.events.index("evolution")
    assert workflow.events.index("freeze") < workflow.events.index("test_json_read")


def test_failed_test_partial_evidence_survives_resume_without_becoming_completion(workflow, monkeypatch):
    failure = EpisodeExecutionError("test Jev interrupted", cause=JevProviderError("test outage"),
        partial_result={"episode_id": "BTC-USD-sealed", "status": "error", "score": None,
                        "traces": [{"trace": [{"id": "failed", "output": "PARTIAL_TEST_EVIDENCE"}]}],
                        "trades": [{"side": "buy", "quantity": 1.}], "equity_curve": [{"equity": 9990.}]})
    first = True

    def evaluate(artifact, episodes, jev):
        nonlocal first
        if first:
            first = False
            raise failure
        return workflow.evaluate(artifact, episodes, jev)

    monkeypatch.setattr(frozen, "evaluate_frozen", evaluate)
    with pytest.raises(EpisodeExecutionError) as caught:
        experiment.run_experiment(workflow.config)
    assert caught.value is failure
    failed_state = state(workflow)
    assert failed_state["status"] == "failed" and failed_state["test_completed_ids"] == []
    archived = Path(failed_state["test_failure_trace"])
    assert json.loads(archived.read_text()) == failure.partial_result
    assert workflow.store.list_holdout(failed_state["run_id"]) == []
    assert_completed_once(workflow, experiment.run_experiment(workflow.config, resume=True))
    assert json.loads(archived.read_text()) == failure.partial_result


def test_trace_index_commit_failure_recovers_without_repeating_test(workflow, monkeypatch):
    # A real 1000-bar trace exceeds the small legacy fallback. Lower the bound
    # for this fixture to reproduce that recovery path without a huge allocation.
    monkeypatch.setattr(storage, "LEGACY_TRACE_LIMIT", 1)
    original, failed = storage.atomic_write_json, False

    def write(path, value):
        nonlocal failed
        if str(path).endswith(".index.json") and not failed:
            failed = True
            raise OSError("injected trace index failure")
        return original(path, value)

    monkeypatch.setattr(storage, "atomic_write_json", write)
    with pytest.raises(OSError, match="injected"):
        experiment.run_experiment(workflow.config)
    assert workflow.evaluations == {"BTC-USD": 1}
    assert_completed_once(workflow, experiment.run_experiment(workflow.config, resume=True))


def test_research_implementation_change_is_rejected_before_resume(workflow, monkeypatch):
    experiment.run_experiment(workflow.config)
    monkeypatch.setattr(experiment, '_research_source_hash', lambda: 'changed-research-code')
    before = list(workflow.events)
    with pytest.raises(ValueError, match='code changed'):
        experiment.run_experiment(workflow.config, resume=True)
    assert workflow.events == before
