---
name: jev-harness
description: Build, evaluate, and optionally evolve task-specific code and Jev decision pipelines with JevHarness. Use when a user wants a reusable Jev agent, a new task adapter, or trajectory-and-reward optimization; begin by clarifying the task and evaluation contract. Not for ordinary coding without a Jev workflow.
---

# JevHarness

Turn the user's intended outcome into a runnable, measurable decision workflow: ordinary code prepares observations and enforces actions; Jev supplies structured judgments; an optional reflection model proposes improved pipelines offline. This is task-level pipeline optimization, not model-weight training. The deployed pipeline can retain Jev while removing the reflection model.

## Establish the task before building

Read the user's request and existing authorized project material first. Reuse answers already given. Ask focused questions about missing information that would change the implementation or evaluation. Do not start a new task's implementation, generate its questions/criteria, access credentials, or run a paid experiment until enough of the following contract is known. Read-only inspection that helps identify those gaps can proceed.

- **Outcome and scope:** What should improve, for whom, and what counts as success? Is this one decision per input or a multi-step episode? What is outside scope?
- **Inputs and outputs:** Obtain representative input, desired output/action, available fields, legal actions, and failure behavior. Ask for a concrete example when a verbal goal leaves the semantics ambiguous.
- **Evidence:** Which examples, logs, labels, simulator, or real outcome signals exist? How may they be accessed and sent to providers? Which information is available at decision time?
- **Evaluation:** Who or what can score a result, on what timescale, and with what uncertainty? Fix reward direction, hard constraints, and any trade-off rule. The user may describe outcomes rather than supply a formal evaluator.
- **Environment and permissions:** Where will code run, which tools or external systems may it use, and which actions may actually be executed? Separate simulated actions, read-only integrations, and production writes.
- **Experiment resources:** Clarify a stopping condition and relevant limits: rounds, evaluations, money, time, latency, or concurrency. Keep Jev inference usage separate from reflection usage. An absent limit is unknown, not permission for indefinite spending; preserve an explicit authorization for unrestricted Jev calls within the agreed experiment.
- **Data separation:** Establish training/search feedback, validation for selection, and any final held-out test. Choose random, group, temporal, or environment splits based on the task's leakage risks, not a universal ratio.
- **Models and delivery:** Confirm the Jev transport and credential variable, optional reflector and model, deployment target, runtime latency needs, and whether Jev must remain in the final pipeline.

Adapt the conversation to what is missing; there is no required questionnaire length or number of rounds. Bundle related uncertainties, explain the decision each answer enables, and do not repeat settled questions. If the user delegates routine choices, make reasonable proposals and record the assumptions. If they cannot define a score, help operationalize the outcome using examples; do not require them to author prompts or criteria. A request to design only remains a design task.

Before implementation, state a concise task contract with the known inputs, output, objective, evaluator, permitted actions, data split, runtime, and experiment scope. This is a shared understanding, not an extra approval gate when the work is already authorized. An unresolved evaluator or permission can block the dependent experiment while useful contract or adapter work proceeds.

## Locate the actual integration

Read [references/integration.md](references/integration.md) before writing a pipeline or adapter. Locate the JevHarness checkout from the workspace or a user-supplied path; an installed copy of this skill is not the Python runtime. Verify `auto_jev` imports from the intended checkout and inspect its current signatures.

The repository currently exposes a reusable Python runtime and an evaluator callback for GEPA. Its existing command-line runners are domain examples; **do not invent a universal task CLI or a built-in `TaskAdapter` class**. Implement the new domain adapter and a small task runner using the real APIs when needed. Do not silently inherit cryptocurrency objectives, fees, datasets, Pokémon rules, pilot counts, or example model choices.

## Build the first pipeline

Keep task evaluation and authoritative environment interaction outside the candidate. The candidate may construct features/state, generate Jev instructions and criteria, combine answers, choose thresholds/actions, and update memory within the fixed task contract. It cannot rewrite reward, labels, hidden state, action permissions, or the evaluator to win the benchmark. If the task requires Jev in production, enforce that as a candidate constraint before evaluation and freezing; a prompt instruction alone is not enforcement.

Use a v3 `PipelineSpec` for functional Python and dynamic questions. Use restricted expression nodes when they suffice. Construct a real dependency DAG: independent judgments may run in parallel; dependent decisions wait for the required outputs. Python nodes require explicit `depends_on`, and their `run(obs, nodes, memory)` can see only those dependencies. Jev's `state` and `questions_expression` are restricted expressions; use a Python node to prepare more complex dynamic question dictionaries.

The agent writes task-specific instructions and criteria from the agreed outcomes and examples. The user need not supply them. Define choice criteria as meaningful option descriptions, score criteria as ordered descriptions, and noul as a probability of a clear proposition. Avoid treating any returned confidence as a validated likelihood of task success without checking calibration.

Run `validate_spec`, compile the graph, and verify observation/output contracts and legal actions before a live inference. Check current Python sandbox support; never replace an unavailable sandbox with host `exec`. Make an authorized, minimal real inference test to validate the selected transport and actual response schema. Mock tests establish plumbing only. Compare the initial pipeline with an appropriate simple or existing baseline and inspect complete decision traces before scaling.

## Optional trajectory-and-reward evolution

Use evolution only when requested or agreed within the task. Read [references/evaluation.md](references/evaluation.md) before running search. Establish the objective and allowed mutation surface once; keep them fixed for a comparable run.

Use the actual GEPA instance frontier, not a made-up global Pareto chart: different candidates may lead on different validation instances. Reflection receives **complete trajectories and rewards for every selected training episode**, including observations, actions, exact Jev states/questions/responses, code outputs, memory, errors, and node timing. A selected mini-batch may be smaller than the training set; no selected episode may be silently truncated or summarized as if complete.

Preserve full archived inputs, transmitted prompt bytes and hashes, and candidate ancestry. Lossless encoding is acceptable when reconstructable and verified. If full input exceeds the configured context limit, archive it and stop before dispatch; choose an explicitly revised batch/context policy without disguising the change. Do not put held-out results into prompts, code generation, candidate selection, or stopping decisions.

Report rounds completed/accepted, actual evaluations, Jev calls/cache hits, reflection model confirmation, wall time, and observed costs. Distinguish missing price data from zero cost. Keep infrastructure failures distinct from candidate failures; preserve partial traces. Resume only against compatible recorded contracts, not by changing a frozen policy or silently starting a different experiment.

## Freeze and hand over

Select by the agreed validation rule, freeze the specification plus runtime and task resources, and only then run a held-out test if one exists. For comparisons, freeze every compared policy before revealing the shared test. Once inspected, that test is no longer fresh evidence for further tuning.

A frozen configuration does not eliminate Jev calls. Production consists of the fixed observation adapter, permitted code, Jev judgments where retained, action validation, and memory policy; no reflection or fresh pipeline generation is needed. If all Jev nodes are removed, identify the result as code-only and check that this satisfies the user's goal. A gateway model alias does not pin underlying weights; distinguish live inference from exact recorded-response replay.

Deliver the task contract, runnable adapter/runner, validated specification, relevant checks, baseline and search evidence, frozen artifact, and exact run/deployment instructions appropriate to the task. Report no improvement, missing evaluation evidence, or a failed experiment plainly. Do not promise success on arbitrary tasks or competitive performance from a small pilot.

For installation or discovery issues, read [references/installation.md](references/installation.md). This shared skill supports Codex and Claude Code without granting new tool permissions or requiring host-specific prompt substitutions.
