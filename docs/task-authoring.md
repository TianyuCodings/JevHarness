# Author a task-specific Jev harness

The main workflow is to let a coding LLM write the harness your task needs. Add reflection optimization when you have representative episodes and a trustworthy score; it is not a prerequisite for using JevHarness.

For a complete implemented task, read the [Pokémon adapter](../examples/pokemon/adapter.py), its [selected harness](../examples/pokemon/sample/selected-pipeline.json), and the [actual Jev call](jev-example.md). The ticket-routing example below illustrates the interfaces; it is not a measured experiment.

## 1. Define what the harness can observe and do

Write down the observation fields, legal outputs or actions, stopping condition, and the outcome that matters. Keep the evaluator and side effects in trusted application code. A task episode is a JSON-compatible dictionary with a unique `id`; the remaining fields belong to your task.

For a routing task, an episode can contain a subject, message, and expected queue. Only the subject and message belong in the runtime observation. The expected answer is available to the evaluator and can be included in training feedback after the decision.

For a sequential task, specify when information becomes visible. A game harness should receive player-visible information; a time-based task should not receive future events. Validate each returned action before executing it. The candidate must not be able to edit the reward function or invoke an unrestricted side-effecting tool.

## 2. Ask the LLM to write a PipelineSpec

Provide the task contract, representative observations, and the actual execution schema. Ask for a complete specification rather than a patch to an unspecified program:

```python
from auto_jev.spec import pipeline_schema, validate_spec

schema = pipeline_schema(version=3)
# Supply schema, task rules, and examples to your coding/authoring LLM.
# Parse its returned complete JSON object:
spec = validate_spec(generated_spec)
```

The repository's [agent skill](../skills/jev-harness/SKILL.md) guides this process. The [execution-contract helper](../examples/pokemon/execution_contract.py) shows how to include the actual validators, permitted names and methods, expression evaluator, and resource limits in a prompt. It exposes runtime rules; it does not prescribe a Pokémon strategy.

A `PipelineSpec` declares `version`, `name`, `jev_model`, `nodes`, and an `output` expression. `memory_update` is optional. Outputs and memory must be finite JSON values.

| Node kind | Role | Dependencies |
| --- | --- | --- |
| `expression` | Small deterministic transformations using the restricted expression language | Inferred from literal `nodes['id']` references, plus optional `depends_on` |
| `jev` | Build a state and ask a question map; receive structured answers | Inferred from `state` and, in v3, `questions_expression`, plus optional `depends_on` |
| `python` | Functions, loops, feature construction, dynamic criteria, and decision logic | v3 only; `depends_on` is mandatory and explicit |

Version 2 supports expression/Jev dependency graphs. Version 3 adds functional Python and dynamic questions. Independent nodes run concurrently, with up to eight workers by default. A node sees only its declared or inferred predecessors. The output and memory expressions run after the graph; memory commits once, after success. Cycles and dynamic graph references are rejected.

Python nodes define `run(obs, nodes, memory)` without annotations or default arguments. Module-level code is limited to function definitions and docstrings; put tables inside functions. Imports, classes, private identifiers (including `_`), files, networking, and process creation are unavailable. Complex Python belongs inside a Python node: the surrounding `output`, `state`, and `questions_expression` fields still use the smaller expression language.

Functional Python currently runs only through the native macOS sandbox. Limits include 64 KiB source, 5 seconds wall time, 2 CPU seconds, and a 512 MiB RSS supervision threshold sampled approximately every 20 ms; brief memory overshoot is possible. Input/output JSON wrappers count toward their byte limits. Read the current [validator and constants](../auto_jev/python_nodes.py) rather than assuming ordinary Python is accepted. There is no unrestricted execution fallback when isolation is unavailable.

## 3. Construct useful Jev questions

The repository's client accepts a state object and a question map:

```python
questions = {
    "queue": {
        "type": "choice",
        "instructions": "Which team should handle the request in subject and message?",
        "criteria": {
            "billing": "A payment, invoice, charge, or refund request.",
            "technical": "Troubleshooting a product failure or integration problem.",
            "general": "Other questions or insufficient information for the other teams.",
        },
    }
}
```

`choice` uses an option-to-description map. `score` uses an ordered list of 2–10 level descriptions. `noul` returns a value from 0 to 1 for a yes/no judgment. Under this repository's strict v3 validator, instructions are nonempty strings and `noul` has no `criteria`. These local restrictions are narrower than the full upstream TypeSafe API.

Use code to compute reliable quantities and identify legal options. Use Jev for judgments that remain after those calculations. Questions using the same state can share one request. If a later question needs an earlier answer, represent that dependency explicitly. Several visual feature cards or questions do not imply several API calls.

## 4. Execute a harness before adding an optimizer

This small version 2 harness routes a ticket:

```python
from auto_jev.providers import JevClient
from auto_jev.runtime import PipelineRuntime
from auto_jev.spec import validate_spec

spec = validate_spec({
    "version": 2,
    "name": "Ticket routing",
    "jev_model": "typesafe-ai/jev",
    "nodes": [{
        "id": "route", "kind": "jev", "state": "obs",
        "questions": questions,
    }],
    "output": "nodes['route']['queue']['choice']",
})
jev = JevClient(transport="vercel")  # Reads AI_GATEWAY_API_KEY.
runtime = PipelineRuntime(spec, jev)
step = runtime.run({"subject": "Invoice correction", "message": "The invoice lists the wrong company."})
queue = step["output"]
```

This makes a Jev request when executed. For a no-network inspection of an actual result, use the [archived Pokémon call](jev-example.md).

`step` includes `output`, `memory`, `memory_before`, node outputs, full node `trace`, execution dependencies/timing, and finalization records. For a multi-step task, pass the returned memory to the next call only after the action has been accepted. A rejected action may require rolling that memory back.

## 5. Add an evaluator when a score is available

The generic interface is a callable, not a task-specific base class:

```python
def evaluator(spec, episode, jev, *, capture_traces=True, **task_options):
    # Build the allowed observation, run the harness, validate/execute its action,
    # and compute a score in trusted code.
    return {"episode_id": episode["id"], "score": score, "traces": decisions}
```

Scores must be finite, with larger values better, and strictly greater than the optimizer's invalid-candidate sentinel `-1e9`. Define ties, invalid actions, truncation, and environment failures explicitly. In the Pokémon task, only an engine-confirmed win/loss/draw earns 1/0/0.5; reaching a step limit is not a draw.

For each decision, include the complete runtime result and the actual observation and action. Retain all earlier decisions and task outcomes if a later step fails. Catch `PipelineExecutionError` only to attach that episode evidence, then propagate its `partial_result`, `cause`, and `causes`. The cause chain lets evolution distinguish a bad candidate from a provider or infrastructure outage. Do not convert `KeyboardInterrupt` or `SystemExit` into a reward. The [Pokémon evaluator](../examples/pokemon/adapter.py) implements this pattern, including legal-action checks and memory rollback.

Keep runtime observations separate from labels, future events, and evaluator-private configuration. Full training feedback may include episode definitions and outcomes after execution; validation outcomes inform selection, and a held-out evaluation should wait until selection is complete.

## 6. Optionally optimize using full-trajectory reflection

Supply a seed harness, explicit task ID, evaluator, and task context. Omitting these can select the legacy trading defaults.

```python
from auto_jev.evolution import run_evolution
from auto_jev.frozen import build_task_contract
from auto_jev.providers import Proposer
from auto_jev.storage import RunStore

# evaluate_ticket is a trusted evaluator defined in an importable source file.
contract = build_task_contract(evaluate_ticket, files=["my_task/rules.json"])
proposer = Proposer({
    "kind": "openai",
    "model": "YOUR_AVAILABLE_MODEL",
    "api_key_env": "OPENAI_DIRECT_API_KEY",
    "max_prompt_bytes": 1_000_000,
    "reflection_encoding": "lossless_dag",
})
store = RunStore("runs")
result = run_evolution(
    train_episodes, validation_episodes,
    jev=jev, proposer=proposer, store=store,
    seed_pipeline=spec, evaluator=evaluate_ticket,
    task_id="ticket_routing", costs={},
    task_context={"objective": "Maximize correct ticket routing under the supplied rules."},
    task_contract=contract,
    reflection_batch_size=2, evolution_rounds=5, seed=0,
)
```

Replace the illustrative model name and supply an account authorized to use it. This code starts real evaluations and reflection requests. The default `max_prompt_bytes` is 1,000,000 UTF-8 bytes; a byte limit is not a token-context guarantee.

Every train/validation episode needs a unique ID across both splits. `reflection_batch_size` explicitly selects how many training episodes enter a reflection; each included episode stays complete. `evolution_rounds` counts actual proposal rounds. `max_metric_calls`, when supplied, limits episode evaluations rather than Jev requests, and a full batch can cross the requested budget. Use `skip_perfect_score=True` only when 1.0 is your task's meaningful perfect score.

The integration uses pinned GEPA 0.1.4, instance-frontier parent selection, strict improvement on a shared parent/child training batch, and full validation of admitted proposals. The final selection uses full-validation mean score. Different candidates can have different sampled training coverage, so their aggregate Train summaries are not interchangeable full-set scores.

Reflection receives the parent program, task/schema context, complete selected training episodes, results, and trajectories. `lossless_dag` deduplicates repeated JSON structure and can reconstruct the original feedback. Complete prompt/full-input files are saved before checking the byte cap; over-limit input fails without truncation or a model call. Input archives, dispatch events, and successful returned proposals are distinct evidence.

## 7. Freeze and evaluate through the same task adapter

```python
from auto_jev.evolution import freeze_run
from auto_jev.frozen import evaluate_frozen

artifact = freeze_run(store, result["run_id"])
outputs = evaluate_frozen(artifact, held_out_episodes, jev, evaluator=evaluate_ticket)
```

Only completed runs with full valid Eval coverage can be frozen. The framework checks the specification, current runtime source, Jev transport, and declared task contract. Include all runtime-relevant evaluator helpers, rule files, engines, and executables in `build_task_contract`; hashing a lockfile alone does not bind everything an engine actually loads.

Frozen execution does not call the proposer. It still calls Jev wherever the selected harness contains a Jev node. The contract is strict about the evaluated environment, including the Python worker identity; an archived frozen package is not automatically portable to another machine. Re-evaluate and freeze in the intended environment instead of silently changing its bindings.

If you have a response cache from the original run, `JevClient(cache_dir=..., cache_namespace=..., cache_only=True, transport="vercel")` reuses matching recorded responses and refuses a cache miss. This is distinct from fresh inference: model aliases and response timing can change. The bundled website snapshot renders archived records; it is neither that runtime cache nor a GEPA resume checkpoint.

## Test the boundary that matters

Before running expensive optimization, check valid/invalid actions, observation filtering, reward calculation, deterministic environment seeds where supported, and failure traces. Test that labels or private state never reach runtime observations. Exercise at least one complete episode and a controlled failure without invoking paid models, then do a small explicitly budgeted live run. Improvements on a selection set should be reported as selection-set results.
