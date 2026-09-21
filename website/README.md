# JevHarness demonstration website

[Open the public demonstration](https://jev-harness.tianyuchen99.chatgpt.site/?autoplay=1#comparison).

This is the original Pokémon interface backed by a complete, read-only response snapshot. It preserves battle replays, Jev arguments, the evolution tree, Train/Eval metrics, latency, decision inspection, and full reflection inputs. No model keys or new inference are required.

## Local preview

Use Node.js 20 or later, from the repository root:

```bash
node website/build.mjs
node website/preview.mjs --port 8768
```

Open http://localhost:8768. The build verifies every compressed and uncompressed payload hash. It uses the committed `examples/pokemon/sample/archive` directory, without installing Node dependencies or requiring Python. Battle animations load the official Pokémon Showdown assets over HTTPS.

```bash
node --test website/test/*.test.mjs
```

## Archive contract

`archive/routes.json` maps allowed request paths and canonical query parameters to gzip-compressed payloads. Ordinary and highlight replays, Train and Eval, and exact trace revisions remain distinct. Unknown resources and mutations are rejected. Reflection downloads preserve their original byte hashes. No active evaluator, model credentials, local RunStore, or filesystem endpoint is exposed.

`build.mjs` emits a small Worker module and manifest under `dist/server/`, plus compressed assets under `dist/client/archive/blobs/`. The Worker streams each requested asset through an `ASSETS` binding; it does not load the whole data archive into memory. Precompressed responses set `encodeBody: 'manual'` to avoid double compression in Cloudflare Workers.

The local preview emulates this binding with files. It is a demonstration server, not a general production application server.

## Refreshing the snapshot

A maintainer with the original reviewed experiment can export the current FastAPI GET responses:

```bash
uv run python scripts/export_pokemon_site.py --help
```

The exporter validates the reviewed run and source artifacts, includes the complete published Train/Eval games and reflections, and rejects credential-shaped data. It makes no model, game-engine, or remote HTTP calls. Fresh checkouts already contain the resulting archive and do not need the original research directory.

The selected harness and trace revisions retain their original identities; a response snapshot is not a GEPA checkpoint or a frozen execution environment.

## ChatGPT Sites deployment

The published site uses a separate Site checkout containing this directory's source plus `archive/` copied from the committed sample. Build there with:

```bash
node build.mjs --archive archive
```

The Site owner registers the project through ChatGPT Sites and keeps the returned `project_id` in that checkout's `.openai/hosting.json`. The build copies that manifest into its output. Its Worker asset configuration is emitted as `dist/server/wrangler.json` with the `ASSETS` binding.

Commit and push the exact Site source to the source repository supplied by Sites. Package only validated `dist/` output with the platform's hosting helper, save the version against that pushed commit, and deploy the saved version. Use the returned production URL; do not put short-lived source credentials into configuration, files, or Git remotes. A new fork needs its own Site identity.
