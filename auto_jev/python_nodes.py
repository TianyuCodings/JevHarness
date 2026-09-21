"""Functional Python in a fresh macOS OS sandbox; no host execution fallback."""
from __future__ import annotations

import ast
import ctypes
from functools import lru_cache
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

SOURCE_BYTES = 65536
AST_NODES = 10000
OUTPUT_BYTES = 1024 * 1024
INPUT_BYTES = 2 * 1024 * 1024
WALL_SECONDS = 5
CPU_SECONDS = 2
MEMORY_BYTES = 512 * 1024 * 1024


class PythonSandboxUnavailable(RuntimeError):
    task_infrastructure_error = True


_BUILTINS = ('abs all any bool dict enumerate filter float int isinstance len list map max min '
             'next range reversed round set sorted str sum tuple zip ValueError TypeError KeyError '
             'IndexError ZeroDivisionError Exception').split()
_MATH = ('ceil copysign cos degrees exp expm1 fabs floor fmod frexp fsum hypot isclose isfinite '
         'isinf isnan ldexp log log10 log1p log2 modf radians sin sqrt tan trunc pi e tau inf nan').split()
_STATISTICS = ('mean fmean geometric_mean harmonic_mean median median_low median_high '
               'mode multimode pstdev pvariance stdev variance').split()
_METHODS = set(('get keys values items copy update setdefault pop popitem clear append extend insert '
                'remove index count sort reverse lower upper strip lstrip rstrip replace split rsplit '
                'join startswith endswith isdigit isalpha isalnum find rfind capitalize title '
                'removeprefix removesuffix add discard union intersection difference symmetric_difference '
                'issubset issuperset isdisjoint intersection_update difference_update symmetric_difference_update').split())
_FORBIDDEN = set(('open exec eval compile input print format globals locals vars getattr setattr delattr '
                  'hasattr dir type object super help breakpoint memoryview bytearray bytes exit quit '
                  'classmethod staticmethod property').split())
_AST = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
        ast.AugAssign, ast.Expr, ast.If, ast.For, ast.While, ast.Break, ast.Continue,
        ast.Pass, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
        ast.comprehension, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set,
        ast.Name, ast.Load, ast.Store, ast.Subscript, ast.Slice, ast.Attribute, ast.Call,
        ast.keyword, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt,
        ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
        ast.Try, ast.ExceptHandler, ast.Raise, ast.JoinedStr, ast.FormattedValue,
        ast.Starred, ast.NamedExpr)


def validate_python_source(source):
    if not isinstance(source, str) or not source.strip() or len(source.encode('utf-8')) > SOURCE_BYTES:
        raise ValueError('Python source must be nonempty and at most 64KB')
    try:
        tree = ast.parse(source, mode='exec')
    except (SyntaxError, RecursionError, ValueError) as exc:
        raise ValueError('Invalid Python source') from exc
    items = list(ast.walk(tree))
    if len(items) > AST_NODES:
        raise ValueError('Python source AST limit exceeded')
    for item in items:
        if not isinstance(item, _AST):
            raise ValueError('Disallowed Python syntax: ' + type(item).__name__)
        if isinstance(item, ast.Name) and (item.id.startswith('_') or item.id in _FORBIDDEN):
            raise ValueError('Disallowed Python name: ' + item.id)
        if isinstance(item, ast.arg) and (item.arg.startswith('_') or item.annotation is not None):
            raise ValueError('Private argument names and annotations are not supported')
        if isinstance(item, ast.FunctionDef):
            if item.name.startswith('_') or item.name in _FORBIDDEN or item.decorator_list or item.returns is not None:
                raise ValueError('Decorators, annotations and private function names are not supported')
        if isinstance(item, ast.Attribute):
            if not isinstance(item.ctx, ast.Load):
                raise ValueError('Attribute writes are not supported')
            allowed = _METHODS | set(_MATH) | set(_STATISTICS)
            if item.attr not in allowed:
                raise ValueError('Disallowed Python attribute: ' + item.attr)
        if isinstance(item, ast.comprehension) and item.is_async:
            raise ValueError('Async comprehensions are not supported')
        if isinstance(item, ast.Constant) and not isinstance(item.value, (str, int, float, bool, type(None))):
            raise ValueError('Only JSON-compatible Python literals are supported')
    for item in tree.body:
        if not isinstance(item, ast.FunctionDef) and not (
                isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str)):
            raise ValueError('Python source may only define functions at module scope')
    entrypoints = [item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == 'run']
    if len(entrypoints) != 1:
        raise ValueError('Define exactly one run(obs, nodes, memory) entrypoint')
    args = entrypoints[0].args
    if (args.posonlyargs or [arg.arg for arg in args.args] != ['obs', 'nodes', 'memory'] or
            args.vararg or args.kwarg or args.kwonlyargs or args.defaults):
        raise ValueError('Entrypoint must be run(obs, nodes, memory) without defaults')
    return source


def _sandbox_command():
    """Return the actual interpreter plus the reviewed dyld bootstrap profile."""
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise PythonSandboxUnavailable('Functional Python requires the macOS native sandbox')
    base = Path(sys.base_prefix).resolve()
    executable = base / 'Resources/Python.app/Contents/MacOS/Python'
    if not executable.is_file():
        executable = Path(sys._base_executable).resolve()
    libraries = [str(base), '/System/Library', '/usr/lib',
                 '/System/Volumes/Preboot/Cryptexes/OS/System/Library',
                 '/System/Cryptexes/OS/System/Library',
                 '/System/Volumes/Preboot/Cryptexes/OS/usr/lib']
    paths = ' '.join('(subpath ' + json.dumps(path) + ')' for path in libraries)
    profile = '\n'.join([
        '(version 1)', '(deny default)',
        '(allow process-exec (literal ' + json.dumps(str(executable)) + '))',
        '(allow sysctl-read)', '(allow file-read-metadata)',
        '(allow file-read* file-map-executable ' + paths + ')',
        # -B prevents writes only. Refuse cached bytecode reads so frozen source
        # hashes also describe what the isolated interpreter actually imports.
        '(deny file-read-data (regex ' + json.dumps(r'\.pyc$') + '))',
        '(allow file-read* (literal "/dev/urandom") (literal "/dev/null"))',
        # libignition opens / as a directory handle. This is not subpath "/".
        '(allow file-read* file-test-existence (literal "/"))',
    ])
    return ['/usr/bin/sandbox-exec', '-p', profile, str(executable), '-I', '-S', '-B']


def runtime_identity():
    """Hash the currently selected Python runtime without starting a process.

    ``-S`` excludes site-packages, and the reviewed sandbox profile explicitly
    denies reading .pyc files; ``-B`` alone would only prevent cache writes.
    Consequently generated __pycache__/.pyc files are excluded, while .py
    sources and native extensions are bound. This records runtime provenance,
    not protection against an active attacker modifying the local installation.
    The system build identifies the OS libraries supplied by the dyld cache.
    """
    import hashlib
    import plistlib

    identity = {'schema': 'auto_jev.python.runtime.v1', 'available': False,
                'platform': sys.platform, 'python_version': sys.version,
                'base_prefix': str(Path(sys.base_prefix).resolve())}
    try:
        command = _sandbox_command()
    except PythonSandboxUnavailable:
        return identity

    def file_identity(path):
        path = Path(path).resolve(strict=True)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'path': str(path), 'sha256': digest}

    def tree_identity(path, suffixes):
        root = Path(path).resolve(strict=True)
        if not root.is_dir():
            raise PythonSandboxUnavailable('Python runtime library directory is unavailable')
        entries = {}
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in ('site-packages', '__pycache__'))
            # Do not silently omit executable libraries behind a directory link.
            if any((Path(directory) / name).is_symlink() for name in dirs):
                raise PythonSandboxUnavailable('Python runtime library has an unsupported directory symlink')
            for name in sorted(names):
                if not name.endswith(suffixes):
                    continue
                member = Path(directory) / name
                entries[member.relative_to(root).as_posix()] = file_identity(member)
        digest = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'path': str(root), 'sha256': digest, 'files': len(entries)}

    try:
        base = Path(sys.base_prefix).resolve(strict=True)
        version = f'{sys.version_info.major}.{sys.version_info.minor}'
        stdlib = base / 'lib' / f'python{version}'
        system_version = Path('/System/Library/CoreServices/SystemVersion.plist')
        system_data = plistlib.loads(system_version.read_bytes())
        # CPython also searches this optional archive before the stdlib directory.
        # Record absence too, so adding a shadowing archive changes the identity.
        archive = base / 'lib' / f'python{version.replace(".", "")}.zip'
        identity.update(
            available=True,
            executable=file_identity(command[3]),
            framework=file_identity(base / 'Python'),
            sandbox_executable=file_identity(command[0]),
            profile_sha256=hashlib.sha256(command[2].encode()).hexdigest(),
            interpreter_flags=list(command[4:]),
            stdlib=tree_identity(stdlib, ('.py',)),
            native_extensions=tree_identity(stdlib / 'lib-dynload', ('.so', '.dylib')),
            stdlib_archive=({'present': True, **file_identity(archive)} if archive.is_file() else
                            {'present': False, 'path': str(archive.resolve())}),
            system_version=file_identity(system_version),
            os_build={key: system_data.get(key) for key in
                      ('ProductName', 'ProductVersion', 'ProductBuildVersion')},
            excluded={'site-packages': 'not loaded with -S',
                      '__pycache__ and .pyc': 'reading .pyc is denied by the sandbox profile'},
        )
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise PythonSandboxUnavailable('Cannot fingerprint the selected Python runtime') from exc
    return identity


# Trusted bootstrap executes only after sandbox-exec has applied the OS profile.
# It never imports the project; candidate globals contain only explicit facades.
_BOOTSTRAP = r'''
import builtins, json, math, os, resource, signal, statistics, sys, types
def fail_cpu(signum, frame):
    os.write(1, b'{"ok":false,"error":"CPU limit exceeded"}\n')
    os._exit(124)
signal.signal(signal.SIGXCPU, fail_cpu)
resource.setrlimit(resource.RLIMIT_CPU, (LIMIT_CPU, LIMIT_CPU + 1))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
# Darwin rejects a finite RLIMIT_AS on this runtime. The parent supervises RSS;
# RLIMIT_DATA is an additional kernel limit where the platform supports it.
try:
    resource.setrlimit(resource.RLIMIT_DATA, (LIMIT_MEMORY, LIMIT_MEMORY))
except ValueError:
    pass
sys.setrecursionlimit(200)
def check(value, depth=0, budget=None):
    if budget is None: budget=[100000,1000000]
    budget[0]-=1
    if depth>30 or budget[0]<0: raise ValueError('JSON nesting or element limit exceeded')
    if value is None or type(value) is bool: return
    if type(value) is int:
        if value.bit_length()>1024: raise ValueError('Integer too large')
    elif type(value) is float:
        if not math.isfinite(value): raise ValueError('Non-finite number')
    elif type(value) is str:
        budget[1]-=len(value)
        if len(value)>100000 or budget[1]<0: raise ValueError('JSON text limit exceeded')
    elif type(value) in (list,dict):
        if len(value)>10000: raise ValueError('JSON collection limit exceeded')
        for key,item in (value.items() if type(value) is dict else enumerate(value)):
            if type(value) is dict:
                if type(key) is not str: raise ValueError('JSON keys must be strings')
                budget[1]-=len(key)
                if budget[1]<0: raise ValueError('JSON text limit exceeded')
            check(item,depth+1,budget)
    else: raise ValueError('Result is not a JSON value')
try:
    packet=json.loads(sys.stdin.buffer.read(LIMIT_INPUT+1))
    namespace={'__builtins__':{name:getattr(builtins,name) for name in BUILTIN_NAMES},
               'math':types.SimpleNamespace(**{name:getattr(math,name) for name in MATH_NAMES}),
               'statistics':types.SimpleNamespace(**{name:getattr(statistics,name) for name in STAT_NAMES})}
    # The only execution of candidate source occurs in this isolated worker.
    exec(compile(packet['source'],'<functional-python-node>','exec'),namespace,namespace)
    context=packet['context']
    value=namespace['run'](context['obs'],context['nodes'],context['memory'])
    check(value)
    output=json.dumps({'ok':True,'value':value},ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(output)>LIMIT_OUTPUT: raise ValueError('Python output limit exceeded')
except Exception as exc:
    output=json.dumps({'ok':False,'error':type(exc).__name__+': '+str(exc)[:1000]}).encode('utf-8')
output+=b'\n'
position=0
while position<len(output):
    position+=os.write(1,output[position:])
'''


def _bootstrap():
    settings = {'LIMIT_CPU': CPU_SECONDS, 'LIMIT_MEMORY': MEMORY_BYTES,
                'LIMIT_OUTPUT': OUTPUT_BYTES, 'LIMIT_INPUT': INPUT_BYTES,
                'BUILTIN_NAMES': _BUILTINS, 'MATH_NAMES': _MATH, 'STAT_NAMES': _STATISTICS}
    return '\n'.join(key + '=' + repr(value) for key, value in settings.items()) + '\n' + _BOOTSTRAP


def _kill(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=3)


class _Rusage(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in
        ('user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins',
         'wired_size', 'resident_size', 'phys_footprint', 'proc_start_abstime', 'proc_exit_abstime')]


@lru_cache(maxsize=1)
def _usage_reader():
    try:
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        reader = library.proc_pid_rusage
        reader.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_Rusage)]
        reader.restype = ctypes.c_int
        return reader
    except (OSError, AttributeError) as exc:
        raise PythonSandboxUnavailable('Native process memory supervision is unavailable') from exc


def _check_memory(process):
    usage = _Rusage()
    if _usage_reader()(process.pid, 0, ctypes.byref(usage)):
        if process.poll() is None:
            raise PythonSandboxUnavailable('Cannot supervise Python worker memory')
    elif usage.resident_size > MEMORY_BYTES:
        raise ValueError('Python worker exceeded the memory supervision limit')


def _exchange(process, packet):
    """Bound all pipes and elapsed time; do not use unbounded communicate()."""
    deadline = time.monotonic() + WALL_SECONDS
    stdout, stderr = bytearray(), bytearray()
    position = 0
    with selectors.DefaultSelector() as selector:
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        while selector.get_map():
            _check_memory(process)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('Python node exceeded wall time limit')
            for key, _ in selector.select(min(remaining, .02)):
                stream, name = key.fileobj, key.data
                if name == 'stdin':
                    try:
                        position += os.write(stream.fileno(), packet[position:position + 65536])
                    except BrokenPipeError:
                        position = len(packet)
                    except BlockingIOError:
                        continue
                    if position == len(packet):
                        selector.unregister(stream)
                        stream.close()
                else:
                    try:
                        chunk = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    buffer = stdout if name == 'stdout' else stderr
                    buffer.extend(chunk)
                    if len(buffer) > (OUTPUT_BYTES if name == 'stdout' else 8192):
                        if name == 'stderr':
                            raise PythonSandboxUnavailable('Python sandbox produced excessive startup diagnostics')
                        raise ValueError('Python worker output limit exceeded')
        try:
            process.wait(timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise ValueError('Python node exceeded wall time limit') from None
    return bytes(stdout), bytes(stderr)


def evaluate_python(source, context):
    validate_python_source(source)
    from .spec import _json
    _json(context)
    if not isinstance(context, dict) or set(context) != {'obs', 'nodes', 'memory'}:
        raise ValueError('Python context requires exactly obs, nodes and memory')
    packet = json.dumps({'source': source, 'context': context}, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(packet) > INPUT_BYTES:
        raise ValueError('Python worker input limit exceeded')
    command = _sandbox_command() + ['-c', _bootstrap()]
    with tempfile.TemporaryDirectory(prefix='auto-jev-python-') as directory:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=directory,
                                       env={'PATH': '/usr/bin:/bin', 'LANG': 'C'},
                                       start_new_session=True, close_fds=True)
        except OSError as exc:
            raise PythonSandboxUnavailable('Cannot start the native Python sandbox') from exc
        try:
            try:
                output, diagnostic = _exchange(process, packet)
            except OSError as exc:
                raise PythonSandboxUnavailable('Native Python worker transport failed') from exc
            if process.returncode == -signal.SIGABRT:
                raise PythonSandboxUnavailable('Native Python sandbox aborted during startup; stop and review platform bootstrap permissions')
            if not output and (b'sandbox' in diagnostic.lower() or process.returncode == 1):
                raise PythonSandboxUnavailable('Native Python sandbox failed to initialize: ' + diagnostic.decode(errors='replace')[:500])
            if process.returncode not in (0, 124):
                raise ValueError('Python worker terminated: ' + str(process.returncode))
            try:
                response = json.loads(output)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError('Python worker returned invalid JSON') from exc
            if not isinstance(response, dict) or response.get('ok') is not True:
                raise ValueError(str(response.get('error', 'Python node failed')) if isinstance(response, dict) else 'Python node failed')
            return _json(response['value'])
        finally:
            _kill(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
