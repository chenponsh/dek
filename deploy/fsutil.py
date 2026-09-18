#!/usr/bin/python3
"""Shared fail-closed filesystem primitives for the deploy/ pipeline.

Every writer here across deploy/*.py independently reimplemented the same
"write to a same-directory tempfile, fsync, chmod, atomically rename over
the target, then fsync the parent directory" sequence, and every reader
independently reimplemented "open with O_NOFOLLOW, fstat-validate, read a
bounded number of bytes." This module centralizes both so a change to the
durability/safety sequence only needs to be made once. Callers that need a
genuinely different contract (e.g. release_bundle.py's _overwrite_in_place,
which must preserve an existing inode's owner/mode rather than replace it)
intentionally keep their own implementation instead of using this module.
"""
import json
import os
import stat
import tempfile
from pathlib import Path


def fsync_dir(path: Path, *, no_follow: bool = True) -> None:
    """fsync a directory's own metadata/entries.

    `no_follow=False` is required by callers that pass a /proc/self/fd/N
    magic-symlink path (itself always a symlink) rather than a real path.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | (getattr(os, "O_NOFOLLOW", 0) if no_follow else 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int, prefix: str = ".dek-atomic-",
                       ensure_parent_mode: int | None = None, parent_no_follow: bool = True) -> None:
    """Durably replace `path` with `payload` via same-directory tempfile + rename.

    The previous content (if any) remains intact and readable for the entire
    write; a crash at any point before the final os.replace() leaves the old
    file exactly as it was, never truncated or partially written.

    `parent_no_follow=False` is required by callers whose `path` lives under
    a /proc/self/fd/N magic-symlink root (see fsync_dir).
    """
    path = Path(path)
    if ensure_parent_mode is not None:
        path.parent.mkdir(mode=ensure_parent_mode, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = ""
        fsync_dir(path.parent, no_follow=parent_no_follow)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def atomic_write_json(path: Path, value, *, mode: int, prefix: str = ".dek-atomic-",
                      ensure_parent_mode: int | None = None, parent_no_follow: bool = True) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload, mode=mode, prefix=prefix,
                       ensure_parent_mode=ensure_parent_mode, parent_no_follow=parent_no_follow)


def read_bounded_regular(path: Path, *, maximum: int, minimum: int = 0,
                         require_nlink1: bool = True, require_uid: int | None = None,
                         require_mode: int | None = None) -> bytes:
    """Read a plain regular file up to `maximum` bytes, failing closed on anything unsafe.

    Refuses a symlink component (O_NOFOLLOW), and by default a hardlinked
    file (nlink != 1) -- another user could otherwise prepare content and
    swap it in via a second link to the same inode. `require_uid`/
    `require_mode`, when given, must match exactly. Raises OSError; callers
    translate it to their own domain exception.
    """
    path = Path(path)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode)
                or (require_nlink1 and details.st_nlink != 1)
                or details.st_size < minimum or details.st_size > maximum
                or (require_uid is not None and details.st_uid != require_uid)
                or (require_mode is not None and stat.S_IMODE(details.st_mode) != require_mode)):
            raise OSError("unsafe or oversized file")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise OSError("oversized file")
        return raw
    finally:
        if descriptor is not None:
            os.close(descriptor)
