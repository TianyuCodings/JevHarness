# Evaluation and optional evolution

Read this before optimizing a pipeline. The user's desired outcome is the source of the objective; Jev's criteria and the reflector's prose are candidate components, not ground truth.

## Make a claim that the available evidence can support

For a single decision, keep the input seen by the policy separate from reference labels and scoring metadata. For an episode, define reset, allowed observations/actions, termination, delayed reward, and aggregation. Code checks legality and computes the fixed reward from the authoritative environment. A referee model can be a proxy, but its agreement is not automatically correctness or real-world utility.

When there is no reliable label, simulator, or measurable outcome, help define a human review protocol, pairwise preferences, operational feedback, or a narrow verifiable proxy. Describe what it establishes. Do not fabricate an evaluator, use Jev's own confidence as the reward for correctness, or claim objective optimization from a few self-judged examples. A pipeline can still be built as an unvalidated prototype; scaling search requires a defensible signal.

Fix hard constraints and score direction before search. If several outcomes matter, establish feasibility constraints, a scalarization, or a selection rule with the user. Do not quietly invent profit/risk or accuracy/latency weights. The current `run_evolution` consumes one scalar per episode. A custom multi-objective optimizer would be additional engineering, not an existing switch.

## Separate learning, selection, and final evidence

- **Training feedback:** Used to generate and compare mutations, including full trajectories from the selected parent episodes.
- **Validation:** Used repeatedly for candidate selection and the per-instance frontier. It is part of the optimization process and is not an untouched final benchmark.
- **Held-out test:** Isolated until all compared policies and selection rules are frozen. No test outcomes, hidden labels, or reconstructions of them belong in candidate prompts, generated code, early stopping, or a dashboard that can influence search.

Choose the split unit that prevents the actual leakage: shared customer/document/device, temporal overlap, repeated game seeds/opponents, environment families, or other correlated instances. For time-dependent inputs, record when information was observable, including publication versus later revision. These are task-specific choices; do not impose trading assumptions on unrelated tasks. If the user has only a tiny dataset, state the resulting evidence limit rather than manufacturing a test set.

## Select and compare candidates honestly

The implemented GEPA strategy keeps a **validation-instance frontier**. A candidate can be useful because it performs best on some instances even if it does not have the highest mean. Native selection prunes dominated programs and samples according to instance-front membership. `parent_selected` events record actual candidate hashes, frontier mappings, and probabilities; display those records rather than reconstructing history from final scores.

This frontier is different from a plot of latency versus quality. Track latency/cost separately unless the agreed scalar objective or a custom selector explicitly incorporates them. For a Jev-versus-code claim, an appropriate comparison can include both initial pipelines and both evolved pipelines under the same task/data protocol. A fixed weak code baseline alone does not establish the value of Jev. Match budgets relevant to the claim and disclose actual rounds, evaluations, time, calls, and tokens; identical round counts do not imply identical resource use.

Use paired instances and the same environment settings where possible. Record cache hits and measure uncached decisions separately when reporting live runtime latency. Candidate code overhead, environment overhead, Jev latency, end-to-end latency, and warm/cache replay timings are distinct quantities. Do not present zero recorded price as proof the service is free. Baselines and ablations should answer a specific uncertainty, not multiply the task indiscriminately.

## Reflect on complete selected episodes

A useful reflection record includes the immutable parent spec, task context, reward, episode identity, full decision-visible observations, legal actions, chosen actions, environment feedback, code outputs, exact Jev states/questions/responses, memory before/after, node dependencies/timing, and errors. Preserve intermediate failed steps and rejected actions; a final reward without the trajectory cannot explain their cause.

The current `full_feedback` requires one complete trajectory per evaluated episode. The batch size chooses episodes, not a prefix of each trajectory. Do not truncate observations, drop failed nodes, replace responses with summaries, or report a compact excerpt as the complete model input. If sensitive fields cannot leave the environment, define the permitted observation/feedback contract before collection; do not secretly alter an already archived input while retaining its hash or completeness claim.

`reflection_input` records full coverage, UTF-8 bytes, SHA-256, path, encoding, and whether the context fits. `complete=true` refers to coverage, not successful dispatch. Determine submission/completion by matching `reflection_dispatch` and `reflection_complete` to the same hash. `lossless_dag` encoding deduplicates structure; verify that unpacking reconstructs the full JSON and keep both original and transmitted archives. It does not promise that a model will reason equally well over every encoding.

If the input is too large, the implementation archives it and raises before calling the model. Resolve this by an explicit change in context capacity, selected episode batch, or task episode design, preserving the distinction between complete selected episodes and complete training coverage. Do not silently shorten an episode. Treat byte limits as one transport guard, not an exact token-capacity estimate.

## Bound the search and preserve failures

Distinguish proposed, valid, evaluated, accepted, and selected candidates. Preserve full code/specs, ancestry, rewards, evaluation results, and declared termination reasons. Count `evolution_rounds`, accepted rounds, and metric calls separately. A transport outage is not evidence that the candidate loses; an invalid candidate may legitimately fail under the fixed evaluator contract. Keep actual errors and partial trajectories available for diagnosis.

If no candidate improves the agreed validation objective, retaining the initial pipeline is a valid result. Stop at the agreed budget or supported convergence condition; do not extend search or loosen the evaluator to manufacture improvement. A changed objective, data policy, model, or runtime may require a new comparable run. Compatible resume can reuse archived responses/checkpoints, but a response lost before journaling may require another provider call; do not promise exactly-once billing.

Freeze before final testing and archive the comparison. Initial and final policies may have identical hashes and share recorded executions; state that directly. If a test result motivates another design change, move it into known development evidence and obtain new independent evidence for a new generalization claim. Deployed operation uses the frozen policy and Jev transport without an offline reflector; production mutations and tool permissions remain controlled outside the candidate graph.
