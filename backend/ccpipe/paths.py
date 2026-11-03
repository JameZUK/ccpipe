"""Filesystem locations shared across modules (kept import-free so any
module can use it without creating an import cycle)."""
from __future__ import annotations

import os
import re
from pathlib import Path


def state_dir() -> Path:
    """ccpipe's state directory: ``$XDG_STATE_HOME/ccpipe``, defaulting to
    ``~/.local/state/ccpipe`` (credentials, session secret, config,
    sticky sessions, revoked sessions)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ccpipe"


_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")


def claude_project_dir(cwd: str) -> Path:
    """Where Claude Code keeps the transcripts of conversations started in
    *cwd*: ``~/.claude/projects/<encoded>``, where every character other
    than an ASCII letter or digit becomes "-" — ``/home/u/my.site_x`` →
    ``-home-u-my-site-x`` (verified against the directories Claude Code
    2.1 creates; replacing only "/" missed any path with ".", "_" or a
    space in it)."""
    return Path.home() / ".claude" / "projects" / _NOT_ALNUM.sub("-", cwd)
