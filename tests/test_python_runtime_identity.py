"""Runtime provenance checks only: no Python worker or sandbox probe is launched."""
import copy
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace

import pytest

from auto_jev import frozen, python_nodes


@pytest.fixture
def installation(tmp_path, monkeypatch):
    base = tmp_path / 'framework' / 'Versions' / '3.12'
    executable = base / 'Resources/Python.app/Contents/MacOS/Python'
    stdlib = base / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}'
    extension = stdlib / 'lib-dynload' / 'math.cpython-test.so'
    source = stdlib / 'math_helpers.py'
    framework = base / 'Python'
    sandbox = tmp_path / 'sandbox-exec'
    system = tmp_path / 'SystemVersion.plist'
    for path, contents in [(executable,b'python-app'),(framework,b'python-framework'),
                           (extension,b'native-extension'),(source,b'def value(): return 1\n'),
                           (sandbox,b'sandbox-executable')]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    system.write_bytes(plistlib.dumps({'ProductName':'test OS','ProductVersion':'27.0',
                                     'ProductBuildVersion':'TEST-BUILD'}))
    original_path = Path

    def mapped_path(value):
        if str(value) == '/System/Library/CoreServices/SystemVersion.plist':
            return system
        return original_path(value)

    command = [str(sandbox),'-p','(version 1)\n(deny default)\n(deny file-read-data (regex #"\\.pyc$"))',
               str(executable),'-I','-S','-B']
    monkeypatch.setattr(python_nodes.sys, 'base_prefix', str(base))
    monkeypatch.setattr(python_nodes, 'Path', mapped_path)
    monkeypatch.setattr(python_nodes, '_sandbox_command', lambda: list(command))

    def no_process(*args, **kwargs):
        raise AssertionError('Hashing must not launch a process')

    monkeypatch.setattr(python_nodes.subprocess, 'Popen', no_process)
    return SimpleNamespace(base=base, executable=executable, stdlib=stdlib, extension=extension,
                           source=source, framework=framework, sandbox=sandbox, system=system,
                           command=command)


def test_runtime_identity_records_actual_interpreter_libraries_profile_and_os_without_execution(installation):
    result = python_nodes.runtime_identity()
    assert result['available'] is True
    assert result['executable']['path'] == str(installation.executable.resolve())
    assert result['framework']['path'] == str(installation.framework.resolve())
    assert result['sandbox_executable']['path'] == str(installation.sandbox.resolve())
    assert result['stdlib']['files'] == result['native_extensions']['files'] == 1
    assert result['os_build']['ProductBuildVersion'] == 'TEST-BUILD'
    assert result['python_version'] == sys.version
    assert result['interpreter_flags'] == ['-I','-S','-B']
    assert result['stdlib_archive']['present'] is False
    assert python_nodes.runtime_identity() == result


@pytest.mark.parametrize('resource,identity_field', [
    ('source','stdlib'),('extension','native_extensions'),('executable','executable'),
    ('framework','framework'),('sandbox','sandbox_executable'),
])
def test_current_runtime_resource_changes_are_detected(installation, resource, identity_field):
    before = python_nodes.runtime_identity()
    path = getattr(installation, resource)
    path.write_bytes(path.read_bytes() + b'changed')
    after = python_nodes.runtime_identity()
    assert before[identity_field] != after[identity_field]


def test_stdlib_added_sources_and_profile_changes_are_detected(installation):
    before = python_nodes.runtime_identity()
    (installation.stdlib / 'new_module.py').write_text('VALUE = 3\n')
    after = python_nodes.runtime_identity()
    assert after['stdlib']['sha256'] != before['stdlib']['sha256']
    installation.command[2] += '\n(allow sysctl-read)'
    assert python_nodes.runtime_identity()['profile_sha256'] != after['profile_sha256']


def test_disabled_site_packages_and_pyc_caches_do_not_churn_runtime_hash(installation):
    before = python_nodes.runtime_identity()
    for relative in ('site-packages/external.py','__pycache__/math_helpers.cpython-312.pyc',
                     '__pycache__/ignored.py','legacy.pyc'):
        path = installation.stdlib / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'non-executable excluded fixture')
    assert python_nodes.runtime_identity() == before


def test_runtime_selection_is_resolved_again_even_when_old_binary_still_exists(installation):
    before = python_nodes.runtime_identity()
    second = installation.executable.with_name('Python-second')
    second.write_bytes(installation.executable.read_bytes())
    installation.command[3] = str(second)
    after = python_nodes.runtime_identity()
    assert before['executable']['sha256'] == after['executable']['sha256']
    assert before['executable']['path'] != after['executable']['path']
    assert installation.executable.is_file()


def test_framework_alias_is_reresolved_to_current_installation(installation, tmp_path, monkeypatch):
    # An executable may remain unchanged while the parent interpreter selects a
    # different base installation. The base identity itself must also change.
    alias = tmp_path / 'current-framework'
    alias.symlink_to(installation.base, target_is_directory=True)
    monkeypatch.setattr(python_nodes.sys, 'base_prefix', str(alias))
    first = python_nodes.runtime_identity()
    assert first['base_prefix'] == str(installation.base.resolve())
    second_base = tmp_path / 'second-base'
    second_base.mkdir()
    (second_base / 'Python').write_bytes(installation.framework.read_bytes())
    # Linking the stdlib root is explicit and gets resolved in its tree identity.
    (second_base / 'lib').symlink_to(installation.base / 'lib', target_is_directory=True)
    alias.unlink()
    alias.symlink_to(second_base, target_is_directory=True)
    second = python_nodes.runtime_identity()
    assert first['base_prefix'] != second['base_prefix']
    assert first['framework']['path'] != second['framework']['path']


def test_system_build_and_optional_stdlib_archive_are_bound(installation):
    before = python_nodes.runtime_identity()
    data = plistlib.loads(installation.system.read_bytes())
    data['ProductBuildVersion'] = 'NEXT-BUILD'
    installation.system.write_bytes(plistlib.dumps(data))
    after = python_nodes.runtime_identity()
    assert after['system_version'] != before['system_version']
    assert after['os_build']['ProductBuildVersion'] == 'NEXT-BUILD'
    archive = installation.base / 'lib' / f'python{sys.version_info.major}{sys.version_info.minor}.zip'
    archive.write_bytes(b'archive fixture')
    assert python_nodes.runtime_identity()['stdlib_archive']['present'] is True


def test_missing_native_sandbox_has_stable_unavailable_identity(monkeypatch):
    def unavailable():
        raise python_nodes.PythonSandboxUnavailable('not on macOS')

    monkeypatch.setattr(python_nodes, '_sandbox_command', unavailable)
    first = python_nodes.runtime_identity()
    assert first['available'] is False
    assert first == python_nodes.runtime_identity()
    # Legacy expression-only callers can still compute a source hash.
    assert len(frozen.source_hash()) == 64


def test_source_hash_includes_current_runtime_identity(monkeypatch):
    identity = {'available':False,'schema':'test-runtime','revision':'first'}
    monkeypatch.setattr(python_nodes, 'runtime_identity', lambda: copy.deepcopy(identity))
    before = frozen.source_hash()
    identity['revision'] = 'second'
    assert frozen.source_hash() != before


def test_installed_runtime_can_be_fingerprinted_without_starting_workers(monkeypatch):
    def no_process(*args, **kwargs):
        raise AssertionError('Runtime identity must only read files')

    monkeypatch.setattr(python_nodes.subprocess, 'Popen', no_process)
    identity = python_nodes.runtime_identity()
    if identity['available']:
        assert identity['stdlib']['files'] > 0
        assert identity['native_extensions']['files'] > 0
        assert identity['os_build']['ProductBuildVersion']
        assert identity['executable']['sha256']
