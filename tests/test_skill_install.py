"""Skill installation copies complete bundles without replacing local changes."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'install-skill.py'
module_spec = importlib.util.spec_from_file_location('jev_skill_installer', SCRIPT)
installer = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(installer)


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / 'source' / 'jev-harness'
    (folder / 'references').mkdir(parents=True)
    (folder / 'agents').mkdir()
    (folder / 'SKILL.md').write_text('---\nname: jev-harness\ndescription: Test installation\n---\n')
    (folder / 'references' / 'integration.md').write_text('Complete reference content.\n')
    (folder / 'agents' / 'openai.yaml').write_text('interface:\n  display_name: "JevHarness"\n')
    return folder


def test_project_both_copies_complete_bundle(source, tmp_path):
    project = tmp_path / 'target project'
    project.mkdir()
    plan = installer.install_skill(source, target='both', scope='project', project=project)
    assert [action for action, _ in plan] == ['copy', 'copy']
    assert {path for _, path in plan} == {
        project / '.agents' / 'skills' / 'jev-harness',
        project / '.claude' / 'skills' / 'jev-harness',
    }
    for _, path in plan:
        assert installer.file_snapshot(path) == installer.file_snapshot(source)


def test_same_bundle_is_idempotent(source, tmp_path):
    installer.install_skill(source, target='codex', scope='project', project=tmp_path)
    target = tmp_path / '.agents' / 'skills' / 'jev-harness'
    before = (target / 'SKILL.md').stat().st_mtime_ns
    assert installer.install_skill(source, target='codex', scope='project', project=tmp_path) == [('unchanged', target)]
    assert (target / 'SKILL.md').stat().st_mtime_ns == before


def test_conflict_preflight_preserves_both_destinations(source, tmp_path):
    installer.install_skill(source, target='claude', scope='project', project=tmp_path)
    changed = tmp_path / '.claude' / 'skills' / 'jev-harness' / 'SKILL.md'
    changed.write_text('Local instructions that must survive.\n')
    with pytest.raises(ValueError, match='Refusing to overwrite'):
        installer.install_skill(source, target='both', scope='project', project=tmp_path)
    assert changed.read_text() == 'Local instructions that must survive.\n'
    assert not (tmp_path / '.agents').exists()


def test_dry_run_writes_nothing(source, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    plan = installer.install_skill(source, target='both', scope='project', project=project, dry_run=True)
    assert len(plan) == 2
    assert list(project.iterdir()) == []


@pytest.mark.parametrize('target,folder', [('codex', '.agents'), ('claude', '.claude')])
def test_personal_scope_uses_documented_host_path(source, tmp_path, target, folder):
    user_directory = tmp_path / 'personal'
    user_directory.mkdir()
    plan = installer.install_skill(source, target=target, scope='user', user_home=user_directory)
    assert plan == [('copy', user_directory / folder / 'skills' / 'jev-harness')]


def test_different_reference_is_a_conflict(source, tmp_path):
    installer.install_skill(source, target='codex', scope='project', project=tmp_path)
    reference = tmp_path / '.agents' / 'skills' / 'jev-harness' / 'references' / 'integration.md'
    reference.write_text('An independently edited integration.\n')
    with pytest.raises(ValueError, match='Refusing to overwrite'):
        installer.install_skill(source, target='codex', scope='project', project=tmp_path)
    assert reference.read_text() == 'An independently edited integration.\n'


def test_destination_symlink_is_not_followed(source, tmp_path):
    parent = tmp_path / '.agents' / 'skills'
    parent.mkdir(parents=True)
    (parent / 'jev-harness').symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match='regular skill directory'):
        installer.install_skill(source, target='codex', scope='project', project=tmp_path)
    assert (parent / 'jev-harness').is_symlink()


def test_source_symlink_is_not_copied(source, tmp_path):
    (source / 'external').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        installer.install_skill(source, target='both', scope='project', project=tmp_path)
    assert not (tmp_path / '.agents').exists()


def test_missing_skill_is_rejected_before_destination_creation(source, tmp_path):
    (source / 'SKILL.md').unlink()
    with pytest.raises(ValueError, match='SKILL.md is missing'):
        installer.install_skill(source, target='codex', scope='project', project=tmp_path)
    assert not (tmp_path / '.agents').exists()


def test_user_scope_rejects_ignored_project(source, tmp_path):
    with pytest.raises(ValueError, match='only to'):
        installer.install_skill(source, target='codex', scope='user', project=tmp_path)
