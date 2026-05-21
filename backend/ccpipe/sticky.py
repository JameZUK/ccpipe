"""Sticky-session persistence.

A "sticky" tmux session survives backend restart / reboot: its name and
cwd are stored in a small JSON file, and the lifespan hook recreates
any missing sticky session on startup. On restore we launch
``claude --continue`` so claude itself picks up the most recent
conversation for that cwd, then drop to an interactive shell when it
exits (same wrapper as freshly-created sessions).

Storage: ``$XDG_STATE_HOME/ccpipe/sticky_sessions.json`` (default
``~/.local/state/ccpipe/sticky_sessions.json``). 0600 perms, atomic
replace on save. Tests can override the path via
``CCPIPE_STICKY_FILE``.

Schema:
    {
      "<session_name>": {"cwd": "/abs/path"},
      ...
    }

We store only the cwd; the command used at restore time is a constant
(see ``build_restore_command()``) so we don't accidentally pin the
restore behaviour to whatever the original command was — improvements
to the wrapper benefit existing sticky sessions on the next restart.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
from pathlib import Path

log = logging.getLogger(__name__)

STICKY_FILE_ENV = "CCPIPE_STICKY_FILE"


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ccpipe"


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
        # fsync before close so a hard power loss can't publish a
        # zero-length sticky file. load() tolerates a missing or
        # malformed file (returns {}), but that silently drops every
        # sticky flag the user had set — losing intent across the
        # restart that prompted them to make sessions sticky.
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)


def is_sticky(name: str) -> bool:
    return name in load()


def set_sticky(name: str, cwd: str) -> None:
    data = load()
    data[name] = {"cwd": cwd}
    _save(data)


def clear(name: str) -> None:
    """Remove ``name`` from the sticky map. No-op if it wasn't sticky.
    Called by the kill/delete path so a user-initiated removal of a
    session also drops the sticky flag — otherwise the session would
    quietly come back on the next backend restart."""
    data = load()
    if name in data:
        del data[name]
        _save(data)


def rename(old: str, new: str) -> None:
    """Move the sticky entry from ``old`` to ``new`` so a rename
    preserves the sticky flag. No-op if ``old`` wasn't sticky."""
    data = load()
    if old in data:
        data[new] = data[old]
        del data[old]
        _save(data)


def sticky_names() -> set[str]:
    """Read-only snapshot of currently-sticky names. Cheap to call
    per-request; the file is small."""
    return set(load().keys())


def build_restore_command(shell: str | None = None) -> str:
    """Return the shell command used when restoring a sticky session.

    ``claude --continue`` picks up the most recent conversation for the
    pane's cwd (claude indexes conversations by cwd under
    ``~/.claude/projects/<encoded>/*.jsonl``), then ``exec $SHELL -i``
    replaces the shell process so the pane drops to an interactive
    prompt when claude exits instead of dying.
    """
    if shell is None:
        shell = os.environ.get("SHELL") or "/bin/bash"
    return f"claude --continue; exec {shlex.quote(shell)} -i"
