"""Portable advisory file locks for local JSON stores and experiment workers."""
from __future__ import annotations

import sys
from typing import IO, Union

_File = Union[IO[bytes], IO[str], int]


def _fd(file: _File) -> int:
    return file if isinstance(file, int) else file.fileno()


def _win_lock_position(file: _File) -> None:
    if isinstance(file, int):
        return
    if hasattr(file, "seek"):
        file.seek(0)


def _posix_flock(file: _File, *, exclusive: bool = True, non_blocking: bool = False) -> None:
    import fcntl

    flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if non_blocking:
        flags |= fcntl.LOCK_NB
    fcntl.flock(_fd(file), flags)


def _posix_funlock(file: _File) -> None:
    import fcntl

    fcntl.flock(_fd(file), fcntl.LOCK_UN)


def _win32_flock(file: _File, *, exclusive: bool = True, non_blocking: bool = False) -> None:
    import msvcrt

    if not exclusive:
        raise ValueError("Windows file locks support exclusive mode only")
    fd = _fd(file)
    _win_lock_position(file)
    mode = msvcrt.LK_NBLCK if non_blocking else msvcrt.LK_LOCK
    try:
        msvcrt.locking(fd, mode, 1)
    except OSError as exc:
        if non_blocking:
            raise BlockingIOError(exc.errno, exc.strerror, getattr(exc, "filename", None)) from exc
        raise


def _win32_funlock(file: _File) -> None:
    import msvcrt

    fd = _fd(file)
    _win_lock_position(file)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


if sys.platform == "win32":
    flock = _win32_flock
    funlock = _win32_funlock
else:
    flock = _posix_flock
    funlock = _posix_funlock
