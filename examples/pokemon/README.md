# Pokémon: an evolved Jev harness

This example turns a player-visible battle observation into features, two parallel Jev judgments, and a validated action. An authoring LLM can change the harness's code, questions, criteria, and graph. Reward and complete training trajectories support optional offline reflection.

[Live demonstration](https://jev-harness.tianyuchen99.chatgpt.site/?autoplay=1#comparison) · [Actual Jev call](../../docs/jev-example.md) · [Selected harness](sample/selected-pipeline.json)

## View the recorded experiment

From the repository root, using Node.js 20 or later:

```bash
node website/build.mjs
node website/preview.mjs --port 8768
```

Open http://localhost:8768. The bundled [response archive](sample/README.md) supplies the complete published website. No API key, Python environment, local simulator, or inference request is needed. The official battle renderer downloads its scripts and images over HTTPS.

The website contains:

- A short replay of turns 2, 3, 12, and 15, ending in the actual engine-confirmed victory. Jev action probabilities, the final executed action, and recorded call latency follow the corresponding decision.
- Exact `state`, `instructions`, applicable `criteria`, and results, expanded by default. Switch between Jev nodes and questions, or copy/download the full argument JSON.
- The initial and evolved policies' complete paired Eval replays, in an expandable archive.
- A six-node evolution tree on the left and the selected candidate's feature flow on the right. Features have separate cells; cells sharing a Jev execution node share its request.
- Train/Eval performance, measured latency, every published decision, frontier snapshots, and all five reflections' full inputs.

Use `?autoplay=0` to disable automatic playback or `?autoplay=1` to explicitly enable it. The default respects reduced-motion preferences. Playback supports pause, restart, chapter navigation, and 1×/2× speed. The preview pauses used to read probabilities are separate from measured Jev latency.

## Recorded result and provenance

| Item | Recorded value |
| --- | --- |
| Environment | Local Pokémon Showdown 0.11.11, Generation 9 custom 3v3 preset-team singles |
| Rules | Fixed initial order; no Terastallization; four preset team configurations |
| Train | 18 defined games; each reflection uses two complete sampled games |
| Eval | 12 games with different matchup combinations and random seeds |
| Search | Five reflections, three admitted proposals, six total candidates |
| Initial Eval | 3/12 wins (25%) |
| Selected Eval | 9/12 wins (75%) |
| Selected observed Train | 2/4 wins; coverage 4/18 games |
| Selected candidate | `0617b0bb9505cddfc319231e6b2226924e1b1d40280b09486ee16d838342a502` |
| Run | `20260921-012437-pokemon-mixed-expanded-405d85` |

Eval guided selection, so the improvement is a selection-set result. Different candidates saw different Train subsets. The experiment does not establish performance on arbitrary opponents or unseen Pokémon.

The featured game is `pokemon-expanded-validation-05`. At turn 12, Jev assigned switching to Scizor a probability of 72%, and the final code accepted that action. At turns 3 and 15, a deterministic knockout rule selected the move; Jev recommended the same action. The page distinguishes these cases. Choice probabilities are not whole-battle win probabilities, and one successful game does not isolate the causal contribution of one call.

The [pipeline provenance](sample/selected-pipeline.provenance.json), [call provenance](../../docs/examples/pokemon-turn12-jev.json), response archive manifest, and screenshot records preserve their source identities. Original trace revisions, HTTP response hashes, and compressed-file hashes have distinct meanings.

## Understand the implementation

- `adapter.py` owns the legal observation, episode loop, action validation, and reward.
- `bridge.cjs` runs the official local engine. Opponent hidden moves, items, seeds, and future actions are excluded from the runtime observation.
- `seed.py` supplies an initial mixed code/Jev pipeline. `sample/selected-pipeline.json` is the full evolved specification.
- `execution_contract.py` exposes the actual runtime restrictions to the authoring model.
- `demo.py` serves a live local RunStore; `presentation.py`, `flow_annotations.py`, `latency.py`, and `highlights.py` project its recorded evidence.
- `static/` contains the English interface and synchronized replay controller.
- `../../website/` serves the immutable demonstration archive without a live RunStore.

The selected pipeline executes a Python feature node, parallel `plan` and `pick` Jev nodes, then a Python decision node. `plan` contains four judgment questions; `pick` contains one action-choice question. Visual feature cells do not add hidden API calls.

A frozen pipeline still needs Jev where its Jev nodes remain. The reflection model is unnecessary during fixed-policy execution. The bundled selected JSON is inspectable source, not a portable frozen runtime or a resumable GEPA checkpoint.

## Run new work

Install framework and engine dependencies from the repository root:

```bash
uv sync --locked --extra dev
npm ci --prefix examples/pokemon
```

Functional Python nodes require the native macOS sandbox; unavailable isolation fails closed. Jev can use `AI_GATEWAY_API_KEY` through Vercel or an explicitly configured direct TypeSafe transport. Authoring/reflection can use an available local Claude CLI or a supported API configuration. Choose a model your account can access rather than assuming the archived model name is available.

Use the [task-authoring guide](../../docs/task-authoring.md) to invoke the reusable runtime and evolution APIs with `adapter.evaluate_episode`, an explicit task contract, and the desired evaluation budget. `run.py` preserves the original research workflow and its dataset preparation; it is not the entrypoint for the read-only website. Do not use the website snapshot as a resume checkpoint or change old frozen hashes to make them match a new installation.

With your own full RunStore and experiment state, the live archive viewer remains available:

```bash
uv run python -m examples.pokemon.demo \
  --root runs --state artifacts/your_experiment/state.json \
  --host 127.0.0.1 --port 8768
```

The reviewed highlight package is intentionally bound to its original run and exact replay; a new experiment needs its own reviewed chapter selection.

## Verification

```bash
node --test tests/test_highlight_replay.cjs
node --test website/test/*.test.mjs
uv run pytest tests/test_pokemon_highlights.py tests/test_pokemon_demo_highlights.py \
  tests/test_pokemon_site_export.py tests/test_pokemon_flow_annotations.py \
  tests/test_pokemon_latency.py tests/test_pokemon_overview.py
```

The recorded battle frames use the [official Pokémon Showdown client](https://github.com/smogon/pokemon-showdown-client). Pokémon characters and artwork belong to their respective owners; this is an independent experiment.
