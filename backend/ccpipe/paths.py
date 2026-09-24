"""Filesystem locations shared across modules (kept import-free so any
module can use it without creating an import cycle)."""
from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """ccpipe's state directory: ``$XDG_STATE_HOME/ccpipe``, defaulting to
    ``~/.local/state/ccpipe`` (credentials, session secret, config,
    sticky sessions, revoked sessions)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ccpipe"
