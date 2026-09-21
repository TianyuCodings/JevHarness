# Install in Codex or Claude Code

This repository ships one shared skill at `skills/jev-harness/`. Copy the whole directory, including references; copying only `SKILL.md` breaks its links. The skill teaches an agent to use the harness. It does not bundle Python dependencies, install a model, grant tool permissions, or make a private repository publicly accessible.

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

The installer only copies this skill. It makes no network requests and refuses to overwrite a different existing installation. Identical contents are left unchanged. Review an existing installation before removing or replacing it. Installing into another directory does not grant the agent access to the JevHarness checkout; provide its path and authorize required filesystem access normally.

## Discovery paths and invocation

| Host | Project skill directory | Personal skill directory | Explicit invocation |
| --- | --- | --- | --- |
| Codex | `.agents/skills/jev-harness/` | `~/.agents/skills/jev-harness/` | `$jev-harness` |
| Claude Code | `.claude/skills/jev-harness/` | `~/.claude/skills/jev-harness/` | `/jev-harness` |

Codex scans project skill directories between the working directory and repository root. Use `/skills` to check discovery; restart if updates do not appear. Older installations may contain `~/.codex/skills`, but the installer uses the currently documented `.agents` locations. [Official Codex skill documentation](https://learn.chatgpt.com/docs/build-skills)

Claude Code discovers project and personal skill folders. Restart when creating a top-level skills directory that did not exist at session start; existing skill text is watched. Personal local skills do not automatically transfer to Cowork or cloud sessions. A cloud repository needs its project skill committed or another supported distribution method. Avoid duplicate personal/project names that can shadow the intended version. [Official Claude Code skill documentation](https://code.claude.com/docs/en/skills)

These paths and invocation conventions were checked on 2026-09-21. Host policy and version can restrict discovery. This package deliberately avoids host-specific shell injection, tool grants, hooks, or model overrides; `agents/openai.yaml` supplies Codex UI metadata only.

## First use

```text
Use the jev-harness skill. My task is to route support tickets into our queues.
The JevHarness checkout is at /path/to/JevHarness.
Start by clarifying the outcomes, inputs, available examples, and evaluation.
Do not run paid inference or write to our ticket system yet.
```

In Codex, mention `$jev-harness`; in Claude Code, enter `/jev-harness` followed by the task. Supply desired outcomes and examples rather than hand-authoring Jev criteria. The agent should establish the task contract before implementing a new adapter. If no defensible evaluator is available, begin with a prototype and an evaluation plan, not an unsupported optimization claim.
