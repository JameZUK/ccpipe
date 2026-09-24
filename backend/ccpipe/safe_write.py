"""Atomic file replacement that doesn't change what the file *is*.

The classic "write a temp file, fsync, rename over the target" pattern is
crash-safe, but done naively it silently alters the file being saved:

  - the temp file is created with default permissions, so a private
    (0600) file such as a ``.env`` comes back world-readable, and an
    executable script loses its ``x`` bit;
  - renaming onto a symlink replaces the *link* with a plain file, so a
    dotfile managed from a dotfiles repo stops pointing at the repo;
  - a fixed temp name lets two concurrent saves of one file clobber each
    other (and truncates any real file that happens to have that name).

These helpers fix all three. ``routes/fs.py`` uses the dir_fd-level pieces
(it has its own jail / no-symlink-walk validation); ``atomic_write_text``
is the whole thing for ccpipe's own writes (e.g. ~/.claude settings).
"""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

# Files created from scratch (no existing target) keep the conventional
# default; the process umask still applies on top.
NEW_FILE_MODE = 0o644


def temp_name() -> str:
    """A unique, short temp-file name. Independent of the target's name, so
    a 255-byte basename can't overflow NAME_MAX, and random, so concurrent
    saves never share a temp file."""
    return f".ccpipe-{secrets.token_hex(8)}.tmp"


def existing_mode(name: str | os.PathLike[str], *, dir_fd: int | None = None) -> int | None:
    """Permission bits of the regular file *name* (not following a symlink
    at the leaf), or None if there's no such regular file."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return stat.S_IMODE(st.st_mode) if stat.S_ISREG(st.st_mode) else None


def open_temp(name: str, mode: int | None, *, dir_fd: int | None = None) -> int:
    """Create the temp file exclusively (O_EXCL, never following a symlink).

    When replacing an existing file (*mode* given) it's created 0600 and
    then set to exactly *mode* — fchmod ignores the umask, so the original
    permissions survive, and a private file's new content is never readable
    by others even for the moment before the chmod."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, 0o600 if mode is not None else NEW_FILE_MODE, dir_fd=dir_fd)
    if mode is not None:
        try:
            os.fchmod(fd, mode)
        except OSError:
            os.close(fd)
            os.unlink(name, dir_fd=dir_fd)
            raise
    return fd


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace *path* with *text*: writes through a symlink to
    the file it points at (the link survives), keeps the file's mode, and
    uses a unique temp file in the target's own directory."""
    target = path.resolve() if path.is_symlink() else path
    tmp = target.parent / temp_name()
    fd = open_temp(str(tmp), existing_mode(target))
    try:
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
