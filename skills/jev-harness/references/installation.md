# Install in Codex or Claude Code

This repository ships one shared skill at `skills/jev-harness/`, available through a Claude Code plugin or a standalone skill installation. The skill teaches an agent to use the harness. Installing it does not install Python dependencies, configure model keys, grant tool permissions, or make a private repository publicly accessible.

## Claude Code: online plugin installation

Run inside Claude Code:

```text
/plugin marketplace add https://github.com/TianyuCodings/JevHarness.git
/plugin install jev-harness@jevharness
/reload-plugins
```

Then describe your task:

```text
/jev-harness:jev-harness Build a Jev harness for routing support tickets.
Start by clarifying the outcomes, inputs, available examples, and evaluation.
Do not run paid inference or write to our ticket system yet.
```

`/plugin` opens the plugin manager. The marketplace name is `jevharness`; the plugin name is `jev-harness`. Claude namespaces plugin skills, so this installation uses `/jev-harness:jev-harness`. The existing standalone skill uses `/jev-harness`. Installation fetches the skill and its references without a manual copy. See the [official plugin guide](https://code.claude.com/docs/en/plugins).

The equivalent terminal commands are:

```bash
claude plugin marketplace add https://github.com/TianyuCodings/JevHarness.git
claude plugin install jev-harness@jevharness --scope user
```

Open a Claude Code session after running the terminal commands, or run `/reload-plugins` in an existing session. Restart if your Claude Code version requires it. The default user scope makes the plugin available across projects; use `--scope project` to declare a shared project installation, or `--scope local` for a personal installation in that project.

If the repository is private, your account needs read access and working Git authentication. The HTTPS URL uses your existing Git credential helper, including a configured macOS Keychain or GitHub CLI helper. Do not put tokens in the install command or repository files. A public demo site does not grant repository access. See the [official private-marketplace guidance](https://code.claude.com/docs/en/plugin-marketplaces#private-repositories).

To update an installed plugin, run inside Claude Code:

```text
/plugin marketplace update jevharness
/plugin update jev-harness@jevharness
/reload-plugins
```

The terminal equivalents are `claude plugin marketplace update jevharness` and `claude plugin update jev-harness@jevharness`. For local development, `claude --plugin-dir /path/to/JevHarness` loads the checkout for that session. Maintainers must increment `.claude-plugin/plugin.json`'s version when releasing plugin content changes; pushing commits alone does not update an explicitly versioned install. Keep the version in that manifest rather than duplicating it in the marketplace entry. See the [official plugin reference](https://code.claude.com/docs/en/plugins-reference).

## Codex: online skill installation

In a Codex host that provides the built-in `skill-installer`, send:

```text
$skill-installer Install the jev-harness skill from https://github.com/TianyuCodings/JevHarness/tree/main/skills/jev-harness
```

Invoke `$jev-harness` with your task on the next turn after installation; restart the session if discovery has not refreshed. Private repository access is still required. If that host does not provide `skill-installer`, use the repository installer below. The Claude `/plugin` commands above are for Claude Code; Codex uses its own skill installation and invocation flow.

## Installer supplied by this repository

From a JevHarness checkout, preview then install into a chosen project:

```bash
python3 scripts/install-skill.py --target both --scope project --project /path/to/your-project --dry-run
python3 scripts/install-skill.py --target both --scope project --project /path/to/your-project
```

For personal discovery across projects:

```bash
python3 scripts/install-skill.py --target codex --scope user
python3 scripts/install-skill.py --target claude --scope user
```

The installer only copies this skill. It makes no network requests and refuses to overwrite a different existing installation. Identical contents are left unchanged. Review an existing installation before removing or replacing it. If copying manually, copy the whole `skills/jev-harness/` directory, including references; copying only `SKILL.md` breaks its links.

The plugin includes the repository sources, which the agent can locate for read-only inspection. It should use a writable checkout in the task workspace for implementation and runtime setup, not modify a cached plugin installation. A standalone skill installation contains instructions and references, so the agent first locates an existing checkout or obtains one in the task workspace. Users do not need to find or supply a plugin cache path; normal filesystem permissions still apply.

## Discovery paths and invocation

| Host | Project skill directory | Personal skill directory | Explicit invocation |
| --- | --- | --- | --- |
| Codex (repository installer) | `.agents/skills/jev-harness/` | `~/.agents/skills/jev-harness/` | `$jev-harness` |
| Claude Code standalone skill | `.claude/skills/jev-harness/` | `~/.claude/skills/jev-harness/` | `/jev-harness` |
| Claude Code plugin | Managed by Claude Code's plugin manager | Managed by Claude Code's plugin manager | `/jev-harness:jev-harness` |

The built-in Codex `skill-installer` chooses its host-managed destination, commonly `$CODEX_HOME/skills/jev-harness` (default `~/.codex/skills/jev-harness`). The table lists the destinations used by this repository's copy installer.

Codex scans project skill directories between the working directory and repository root. Use `/skills` to check discovery; restart if updates do not appear. Older installations may contain `~/.codex/skills`, but the installer uses the currently documented `.agents` locations. [Official Codex skill documentation](https://learn.chatgpt.com/docs/build-skills)

Claude Code discovers project and personal skill folders. Restart when creating a top-level skills directory that did not exist at session start; existing skill text is watched. Personal local skills do not automatically transfer to Cowork or cloud sessions. A cloud repository needs its project skill committed or another supported distribution method. Avoid duplicate personal/project names that can shadow the intended version. [Official Claude Code skill documentation](https://code.claude.com/docs/en/skills)

These paths and invocation conventions were checked on 2026-09-21. Host policy and version can restrict discovery. This package deliberately avoids host-specific shell injection, tool grants, hooks, or model overrides; `agents/openai.yaml` supplies Codex UI metadata only.

## First use

```text
Use the jev-harness skill. My task is to route support tickets into our queues.
Start by clarifying the outcomes, inputs, available examples, and evaluation.
Do not run paid inference or write to our ticket system yet.
```

In Codex, mention `$jev-harness`. For the Claude Code plugin, enter `/jev-harness:jev-harness`; for a standalone Claude Code installation, use `/jev-harness`. Follow the invocation with your task. Supply desired outcomes and examples rather than hand-authoring Jev criteria. The agent should establish the task contract before implementing a new adapter. If no defensible evaluator is available, begin with a prototype and an evaluation plan, not an unsupported optimization claim.
