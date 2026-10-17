import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

from auto_jev.file_lock import _win32_flock, flock, funlock


def test_exclusive_lock_blocks_second_non_blocking_holder(tmp_path):
    path = tmp_path / "store.lock"
    with path.open("a+") as first:
        flock(first)
        with path.open("a+") as second:
            with pytest.raises(BlockingIOError):
                flock(second, non_blocking=True)
        funlock(first)


def test_exclusive_lock_serializes_writers(tmp_path):
    path = tmp_path / "counter.txt"
    path.write_text("0")
    errors = []

    def worker():
        lock_path = tmp_path / "counter.lock"
        try:
            with lock_path.open("a+") as lock:
                flock(lock)
                value = int(path.read_text())
                path.write_text(str(value + 1))
                funlock(lock)
        except Exception as exc:  # pragma: no cover - surfaced through barrier
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert int(path.read_text()) == 8


def test_windows_non_blocking_maps_oserror_to_blocking_io_error(tmp_path):
    path = tmp_path / "win.lock"
    fake_msvcrt = mock.Mock(LK_NBLCK=2, LK_LOCK=0, LK_UNLCK=3)
    fake_msvcrt.locking.side_effect = OSError(13, "locked")
    with path.open("a+") as stream:
        with mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
            with pytest.raises(BlockingIOError):
                _win32_flock(stream, non_blocking=True)


def test_providers_importable_when_fcntl_missing():
    code = (
        "import sys\n"
        "sys.modules['fcntl'] = None\n"
        "from auto_jev.providers import JevClient\n"
        "print(JevClient.__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "JevClient"
