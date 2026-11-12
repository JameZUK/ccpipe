"""Reading and writing ccpipe's small JSON state files safely.

Every one of them (remembered sessions, sticky pins, saved prompts,
layouts, drafts, push subscriptions) is changed the same way: read the
file, change the data, write it all back. Two things make that lose
data unless they're handled here:

- **A read that fails isn't an empty file.** Out of file descriptors, an
  I/O error, no permission: treating those as "nothing saved yet" means
  the next write replaces everything with just the new entry. So a failed
  read marks the path, and ``write`` refuses it until a read works again.
  Only a missing file is empty.
- **A file that isn't JSON** (cut short by a power loss, say) is moved
  aside as ``<name>.corrupt-<time>`` rather than overwritten, so what's
  left of it can be recovered by hand.

Writes go to a uniquely named temp file, are flushed to disk, and replace
the old file in one step.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Paths whose last read failed for a reason other than "no such file".
_unreadable: set[str] = set()
_lock = threading.Lock()


def read_json(path: Path) -> Any | None:
    """The parsed file, or None when there isn't one (or it wasn't JSON
    and has been moved aside). A read error also returns None, and blocks
    ``write`` for this path until a later read succeeds."""
    key = str(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        with _lock:
            _unreadable.discard(key)
        return None
    except OSError as exc:
        with _lock:
            first = key not in _unreadable
            _unreadable.add(key)
        if first:
            log.error("could not read %s (%s): leaving it alone, changes won't be saved "
                      "until it can be read", path, exc)
        return None
    with _lock:
        _unreadable.discard(key)
    try:
        return json.loads(raw)
    except ValueError as exc:
        aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, aside)
            log.error("%s isn't valid JSON (%s): moved to %s, starting empty", path, exc, aside)
        except OSError as move_exc:
            with _lock:
                _unreadable.add(key)
            log.error("%s isn't valid JSON (%s) and couldn't be moved aside (%s): "
                      "leaving it alone", path, exc, move_exc)
        return None


def write(path: Path, data: bytes) -> None:
    """Atomically replace *path* with *data*: 0600, in a 0700 directory,
    flushed to disk first. OSError when the file's last read failed (see
    the module docstring) or the write does."""
    with _lock:
        if str(path) in _unreadable:
            raise OSError(f"{path} could not be read; not overwriting it")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def reset() -> None:
    """For tests: forget which paths couldn't be read."""
    with _lock:
        _unreadable.clear()
