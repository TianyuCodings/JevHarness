#!/usr/bin/env python3
"""Copy the shared JevHarness skill into Codex or Claude Code discovery paths."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

SKILL_NAME = 'jev-harness'


def file_snapshot(directory: Path) -> dict[str, bytes]:
    """Compare the complete bundle; refuse links rather than following them."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f'Expected a regular skill directory: {directory}')
    snapshot = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Skill bundle contains a symlink: {path}')
        if path.is_file():
            snapshot[path.relative_to(directory).as_posix()] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError(f'Skill bundle contains an unsupported file: {path}')
    if 'SKILL.md' not in snapshot:
        raise ValueError(f'SKILL.md is missing: {directory}')
    return snapshot


def install_skill(source: Path, *, target: str, scope: str,
                  project: Path | None = None, user_home: Path | None = None,
                  dry_run: bool = False) -> list[tuple[str, Path]]:
    """Preflight every destination before copying; leave existing content intact."""
    if target not in ('codex', 'claude', 'both') or scope not in ('project', 'user'):
        raise ValueError('Unknown target or scope')
    if scope == 'user' and project is not None:
        raise ValueError('--project applies only to --scope project')
    base = (project if project is not None else Path.cwd()) if scope == 'project' else (
        user_home if user_home is not None else Path.home())
    base = base.expanduser().resolve()
    if not base.is_dir():
        raise ValueError(f'The installation root must already be a directory: {base}')
    source = source.absolute()
    expected = file_snapshot(source)
    clients = ('codex', 'claude') if target == 'both' else (target,)
    plan = []
    for client in clients:
        destination = base / ('.agents' if client == 'codex' else '.claude') / 'skills' / SKILL_NAME
        if destination.exists() or destination.is_symlink():
            if file_snapshot(destination) != expected:
                raise ValueError(f'Refusing to overwrite a different installation: {destination}')
            plan.append(('unchanged', destination))
        else:
            plan.append(('copy', destination))
    if not dry_run:
        for action, destination in plan:
            if action == 'copy':
                destination.parent.mkdir(parents=True, exist_ok=True)
                # copytree refuses an existing target, including one created
                # after preflight. It never merges or overwrites an installation.
                shutil.copytree(source, destination)
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('codex', 'claude', 'both'), required=True)
    parser.add_argument('--scope', choices=('project', 'user'), default='project')
    parser.add_argument('--project', type=Path,
                        help='Existing project directory (default: current directory)')
    parser.add_argument('--dry-run', action='store_true', help='Show paths without writing files')
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1] / 'skills' / SKILL_NAME
    try:
        plan = install_skill(source, target=args.target, scope=args.scope,
                             project=args.project, dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        print(f'Installation failed: {exc}', file=sys.stderr)
        return 1
    for action, path in plan:
        label = 'Would copy' if args.dry_run and action == 'copy' else 'Installed' if action == 'copy' else 'Unchanged'
        print(f'{label}: {path}')
    if not args.dry_run:
        print('Invoke $jev-harness in Codex or /jev-harness in Claude Code.')
        print('If discovery has not updated, restart the host session.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
