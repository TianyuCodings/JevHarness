# Recorded Pokémon example

This directory contains the selected harness and the website's complete response snapshot.

- `selected-pipeline.json`: the unchanged, validated four-node mixed code/Jev specification.
- `selected-pipeline.provenance.json`: source file hash, candidate hash, ancestry, and export identity.
- `archive/routes.json`: an allowlist of website responses, their source trace revisions, and both uncompressed and gzip hashes.
- `archive/blobs/`: deterministic gzip payloads addressed by the hash of their exact uncompressed response bytes.

The snapshot contains 74 recorded Train/Eval games, 1,032 complete decision records, the six candidates, paired replays, latency summaries, and ten original reflection files (full input and transmitted prompt for each of five rounds). It includes only the current mixed-policy presentation; other research runs and evaluation partitions are not part of the website archive.

The archive is about 26 MiB compressed. It is a browsable record of completed work, not a GEPA resume checkpoint, an inference cache, or a portable frozen executable. Original reflection bytes and trace identities remain intact; filenames or process paths appearing inside a historical trace describe the original run and are not installation instructions.

From the repository root, run `node website/build.mjs` followed by `node website/preview.mjs --port 8768`. This requires no credentials and performs no new inference. See [the example guide](../README.md) and [the actual turn-12 Jev call](../../../docs/jev-example.md).
