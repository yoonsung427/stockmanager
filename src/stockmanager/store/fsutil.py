"""Durable file primitives shared by the store (AS §3.0, §8.5).

Every write is either tmp -> full fsync -> rename -> directory fsync, or ``O_APPEND`` with an fsync per line.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


def full_fsync(fd: int) -> None:
    """Flush ``fd`` to stable storage. On macOS plain ``fsync`` does not flush the drive cache, so use ``F_FULLFSYNC``."""
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass  # filesystem without F_FULLFSYNC support: fall back
    os.fsync(fd)


def fsync_dir(path: Path) -> None:
    """Make a rename or create in ``path`` durable."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        full_fsync(fd)
    finally:
        os.close(fd)


def tmp_path(path: Path) -> Path:
    """The only temp name used for ``path``: ``<name>.tmp`` in the same directory."""
    return path.with_name(path.name + ".tmp")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data``: write ``<name>.tmp``, full fsync, rename, fsync the directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        full_fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)
    fsync_dir(path.parent)


def atomic_write_text(path: Path, text: str) -> None:
    """``atomic_write_bytes`` for UTF-8 text (LF newlines, never translated)."""
    atomic_write_bytes(path, text.encode("utf-8"))


def append_lines(path: Path, lines: list[str]) -> None:
    """Append ``lines`` (each gets a trailing ``\\n``) with ``O_APPEND``: one ``write`` and one fsync per line.

    If the file's last byte is not ``\\n`` (a torn line from a crash mid-write, TS CP7) the first write is prefixed
    with ``\\n`` so the new line cannot fuse with the torn one. Existing content is never truncated or rewritten.
    """
    if any("\n" in line for line in lines):
        raise ValueError("log line contains a newline")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        needs_newline = _last_byte(path) not in (None, b"\n")
        for line in lines:
            payload = ("\n" if needs_newline else "") + line + "\n"
            needs_newline = False
            os.write(fd, payload.encode("utf-8"))
            full_fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)


def _last_byte(path: Path) -> bytes | None:
    """Last byte of ``path``, or ``None`` if it is empty."""
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        if size == 0:
            return None
        f.seek(size - 1)
        return f.read(1)
