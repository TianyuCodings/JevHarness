"""Frozen execution: this module does not import GEPA or a reflection provider."""
import hashlib
import inspect
import json
import re
import shutil
import subprocess
from pathlib import Path

from .spec import spec_hash, _json
from .crypto import evaluate_episode
from .observations import OBSERVATION_CONFIG


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()


def source_hash():
    root = Path(__file__).parent
    files = ("spec.py", "flow.py", "runtime.py", "crypto.py", "providers.py", "vercel.py", "paper.py", "observations.py", "data.py", "storage.py", "frozen.py")
    if (root / 'python_nodes.py').is_file():
        files += ('python_nodes.py',)
    identity = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    if (root / 'python_nodes.py').is_file():
        from .python_nodes import runtime_identity
        identity['python_runtime'] = runtime_identity()
    return digest(identity)


def _file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _tree_digest(path):
    root = Path(path).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Task contract tree must be a directory')
    files = {}
    for entry in sorted(root.rglob('*')):
        if entry.is_symlink():
            raise ValueError('Task contract trees cannot contain symlinks')
        if entry.is_file():
            files[entry.relative_to(root).as_posix()] = _file_digest(entry)
        elif not entry.is_dir():
            raise ValueError('Task contract tree contains a non-regular entry')
    return digest(files)


def _evaluator_identity(evaluator):
    target = evaluator if inspect.isfunction(evaluator) or inspect.ismethod(evaluator) else type(evaluator).__call__
    try:
        path = inspect.getsourcefile(target)
        if path is None:
            raise ValueError('Evaluator has no source file')
        path = str(Path(path).resolve(strict=True))
        return {'module': target.__module__, 'qualname': target.__qualname__,
                'path': path, 'sha256': _file_digest(path)}
    except (OSError, TypeError, AttributeError) as exc:
        raise ValueError('Task contract evaluator must have inspectable Python source') from exc


def _git_revision(path):
    root = str(Path(path).resolve(strict=True))
    result = subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'],
                            check=True, capture_output=True, text=True, timeout=10)
    clean = subprocess.run(['git', '-C', root, 'diff', '--no-ext-diff', '--no-textconv', '--quiet', 'HEAD', '--'],
                           capture_output=True, timeout=10)
    if clean.returncode != 0:
        raise ValueError('Task contract git checkout has tracked changes or cannot be inspected')
    return {'commit': result.stdout.strip(), 'clean': True}


def _executable_identity(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*', name):
        raise ValueError('Task contract executable must be a command name')
    path = shutil.which(name)
    if path is None:
        raise ValueError(f'Task contract executable is unavailable: {name}')
    path = str(Path(path).resolve(strict=True))
    return {'path': path, 'sha256': _file_digest(path)}


def build_task_contract(evaluator, *, files=(), trees=(), git=(), executables=()):
    """Snapshot actual task resources, independently of a caller's version labels.

    Include rule/config files and the code actually executed, not only a package
    lock. Directory hashes include relative paths and every regular file. The
    evaluator's source is always included; stateful evaluator configuration must
    additionally be represented by the listed immutable resources.
    """
    contract = {'version': 1, 'evaluator': _evaluator_identity(evaluator),
                'files': {str(Path(p).resolve(strict=True)): _file_digest(p) for p in files},
                'trees': {str(Path(p).resolve(strict=True)): _tree_digest(p) for p in trees},
                'git': {str(Path(p).resolve(strict=True)): _git_revision(p) for p in git}}
    if executables:
        contract['executables'] = {name: _executable_identity(name) for name in executables}
    return verify_task_contract(contract, evaluator=evaluator)


def verify_task_contract(contract, *, evaluator=None):
    """Re-read bound resources; reject a changed evaluator, rules, lock or engine.

    Does not import or execute code referenced by a manifest. A live evaluator is
    supplied at evaluation time; freeze-time checks verify its persisted source.
    """
    try:
        _json(contract)
        if not isinstance(contract, dict) or type(contract.get('version')) is not int or contract['version'] != 1:
            raise ValueError('Unsupported task contract version')
        if set(contract) - {'version', 'evaluator', 'files', 'trees', 'git', 'executables'}:
            raise ValueError('Unknown task contract fields')
        identity = contract.get('evaluator')
        if not isinstance(identity, dict) or set(identity) != {'module', 'qualname', 'path', 'sha256'}:
            raise ValueError('Task contract requires a concrete evaluator identity')
        if not all(isinstance(identity[k], str) and identity[k] for k in identity):
            raise ValueError('Invalid evaluator identity')
        identity = dict(identity)
        identity['path'] = str(Path(identity['path']).resolve(strict=True))
        if not re.fullmatch('[0-9a-f]{64}', identity['sha256']) or _file_digest(identity['path']) != identity['sha256']:
            raise ValueError('Task contract evaluator source changed')
        if evaluator is not None and _evaluator_identity(evaluator) != identity:
            raise ValueError('Task contract does not match the actual evaluator')
        verified = {'version': 1, 'evaluator': identity}
        for field, compute in [('files', _file_digest), ('trees', _tree_digest)]:
            entries = contract.get(field, {})
            if not isinstance(entries, dict):
                raise ValueError(f'Task contract {field} must be a path/hash mapping')
            checked = {}
            for path, expected in entries.items():
                if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
                    raise ValueError(f'Invalid task contract {field} digest')
                absolute = str(Path(path).resolve(strict=True))
                if compute(absolute) != expected:
                    raise ValueError(f'Task contract {field} changed: {path}')
                if absolute in checked:
                    raise ValueError('Duplicate resolved task contract path')
                checked[absolute] = expected
            verified[field] = checked
        repos = contract.get('git', {})
        if not isinstance(repos, dict):
            raise ValueError('Task contract git must be a repository/revision mapping')
        verified['git'] = {}
        for path, expected in repos.items():
            if not isinstance(expected, dict) or set(expected) != {'commit', 'clean'} or expected['clean'] is not True:
                raise ValueError('Task contract git requires a clean concrete revision')
            if not isinstance(expected['commit'], str) or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', expected['commit']):
                raise ValueError('Invalid task contract git revision')
            absolute = str(Path(path).resolve(strict=True))
            if _git_revision(absolute) != expected:
                raise ValueError(f'Task contract git revision changed: {path}')
            if absolute in verified['git']:
                raise ValueError('Duplicate resolved task contract git path')
            verified['git'][absolute] = dict(expected)
        if 'executables' in contract:
            entries = contract['executables']
            if not isinstance(entries, dict):
                raise ValueError('Task contract executables must be a command/identity mapping')
            verified['executables'] = {}
            for name, expected in entries.items():
                if (not isinstance(expected, dict) or set(expected) != {'path', 'sha256'}
                        or not isinstance(expected['path'], str) or not expected['path']
                        or not isinstance(expected['sha256'], str)
                        or not re.fullmatch('[0-9a-f]{64}', expected['sha256'])):
                    raise ValueError('Invalid task contract executable identity')
                # The stored path is already the resolved command identity.
                # Re-resolving it into a different target would silently bless
                # a replaced symlink even if the replacement has equal bytes.
                identity = dict(expected)
                if not Path(identity['path']).is_absolute():
                    raise ValueError('Task contract executable path must be absolute')
                if _executable_identity(name) != identity:
                    raise ValueError(f'Task contract executable changed: {name}')
                verified['executables'][name] = identity
        return verified
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f'Task contract resource verification failed: {type(exc).__name__}') from exc


def validate_artifact(artifact, jev, *, evaluator=None):
    if artifact.get("artifact_hash") != digest({k: v for k, v in artifact.items() if k != "artifact_hash"}):
        raise ValueError("Frozen artifact integrity check failed")
    if spec_hash(artifact["spec"]) != artifact["spec_hash"]:
        raise ValueError("Frozen specification hash mismatch")
    if source_hash() != artifact["source_hash"]:
        raise ValueError("Runtime source changed since freezing; review and freeze a new artifact")
    if artifact.get("observation_config") != OBSERVATION_CONFIG:
        raise ValueError("Observation contract differs from the frozen artifact")
    expected = artifact.get("jev_metadata", {}).get("transport")
    actual = "mock" if jev.mock else jev.transport
    if expected and expected != actual:
        raise ValueError("Jev transport differs from the frozen artifact")
    if artifact.get('task_contract') is not None:
        if evaluator is None:
            raise ValueError('This frozen artifact requires its contracted evaluator')
        verify_task_contract(artifact['task_contract'], evaluator=evaluator)


def evaluate_frozen(artifact, episodes, jev, *, evaluator=None):
    if artifact["task_id"] != "crypto_spot" and evaluator is None:
        raise ValueError("This task requires its evaluator")
    task = evaluator or evaluate_episode
    validate_artifact(artifact, jev, evaluator=task)
    return [task(artifact["spec"], episode, jev, capture_traces=True, **artifact["costs"]) for episode in episodes]
