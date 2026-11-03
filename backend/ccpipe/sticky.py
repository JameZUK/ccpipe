"""Sticky-session persistence.

A "sticky" session is one the user pinned: its name and cwd are stored in
a small JSON file, so it is remembered even with no other record of it,
it gets the longer idle-stop limit, and the most recently used few start
at boot (lifecycle.py — which also resumes it on its own conversation,
via session_meta.py).

Storage: ``$XDG_STATE_HOME/ccpipe/sticky_sessions.json`` (default
``~/.local/state/ccpipe/sticky_sessions.json``). 0600 perms, atomic
replace on save. Tests can override the path via
``CCPIPE_STICKY_FILE``.

Schema:
    {
      "<session_name>": {"cwd": "/abs/path"},
      ...
    }

We store only the cwd; how a session is started again is decided at
resume time (lifecycle.resume_command), so improvements there benefit
existing sticky sessions too.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from .paths import state_dir

log = logging.getLogger(__name__)

STICKY_FILE_ENV = "CCPIPE_STICKY_FILE"

# Serialises load → mutate → save inside set_sticky / clear / rename
# so two concurrent tabs flipping the same session's sticky flag at
# the same instant can't drop one update. The lock is held only for
# the duration of one small JSON file I/O, which is fine on the
# event loop because the file is tiny (a few KB max).
_state_lock = threading.Lock()


_state_dir = state_dir


def _default_path() -> Path:
    return _state_dir() / "sticky_sessions.json"


def path() -> Path:
    override = os.environ.get(STICKY_FILE_ENV)
    return Path(override) if override else _default_path()


def load() -> dict[str, dict[str, str]]:
    """Return the persisted sticky map. Robust to a missing or
    malformed file — returns ``{}`` rather than raising, so a corrupt
    file never blocks startup."""
    p = path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        log.warning("ignoring malformed sticky file at %s: %s", p, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for name, info in data.items():
        if not isinstance(name, str) or not isinstance(info, dict):
            continue
        cwd = info.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        out[name] = {"cwd": cwd}
    return out


def _save(data: dict[str, dict[str, str]]) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, indent=2, sort_keys=True).encode() + b"\n")
    finally:
        os.close(fd)
    os.replace(tmp, p)


def is_sticky(name: str) -> bool:
    return name in load()


def set_sticky(name: str, cwd: str) -> None:
    with _state_lock:
        data = load()
        data[name] = {"cwd": cwd}
        _save(data)


def clear(name: str) -> None:
    """Remove ``name`` from the sticky map. No-op if it wasn't sticky.
    Called by the kill/delete path so a user-initiated removal of a
    session also drops the sticky flag — otherwise the session would
    quietly come back on the next backend restart."""
    with _state_lock:
        data = load()
        if name in data:
            del data[name]
            _save(data)


def rename(old: str, new: str) -> None:
    """Move the sticky entry from ``old`` to ``new`` so a rename
    preserves the sticky flag. No-op if ``old`` wasn't sticky."""
    with _state_lock:
        data = load()
        if old in data:
            data[new] = data[old]
            del data[old]
            _save(data)


def sticky_names() -> set[str]:
    """Read-only snapshot of currently-sticky names. Cheap to call
    per-request; the file is small."""
    return set(load().keys())
