"""Set sensible server-wide tmux options at backend startup.

Why: when the tmux server is first spawned (potentially from inside the
ccpipe container, where $SHELL may be unset), it locks in a default-shell
of /bin/sh. Subsequent new windows inherit that until the option is reset.
We apply our preferred default-shell + a couple of QoL options each time
the backend starts — idempotent and harmless if the values are already set.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

log = logging.getLogger(__name__)


async def _run_tmux(*args: str) -> tuple[int, str]:
    from . import tmux as _tmux
    proc = await asyncio.create_subprocess_exec(
        _tmux.TMUX_BIN, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace").strip()


def _resolve_shell() -> str:
    """Pick a shell. Honor CCPIPE_SHELL > $SHELL > /bin/bash > /bin/sh."""
    for candidate in (
        os.environ.get("CCPIPE_SHELL"),
        os.environ.get("SHELL"),
        "/bin/bash",
    ):
        if candidate and shutil.which(candidate.split()[0]):
            return candidate
    return "/bin/sh"


async def apply_server_defaults() -> None:
    # Lazy import to avoid the tmux <-> tmux_control import cycle.
    from .tmux_control import CONTROL_SESSION_NAME
    shell = _resolve_shell()
    # On a cold boot ccpipe's lifespan calls apply_server_defaults() BEFORE
    # anything else spawns the tmux server (sticky restore + control client
    # come afterwards). We must set the -g options on a server that will
    # still be alive when those later sessions are created — otherwise the
    # sessions come up with tmux's built-in defaults (alternate-screen ON,
    # history-limit 2000), which breaks scrollback.
    #
    # `tmux start-server` is NOT enough: a tmux server with zero sessions
    # exits immediately, so the options set on it evaporate and the next
    # `new-session` (sticky restore) spawns a fresh, unconfigured server.
    # Instead, create the long-lived control/anchor session FIRST (the same
    # detached `sleep infinity` session control_client uses). That keeps the
    # server alive so the -g options below persist, and every session
    # created afterwards inherits them. control_client.start() is
    # idempotent and reuses this session, so creating it here is safe.
    code, out = await _run_tmux(
        "new-session", "-d", "-s", CONTROL_SESSION_NAME, "sleep", "infinity",
    )
    if code != 0 and "duplicate session" not in out.lower():
        # Non-zero is expected only when the anchor already exists (warm
        # path). Anything else means the server may not be up — log it; the
        # set-option calls below will then also warn and the cause is clear.
        log.warning("tmux anchor session create (rc=%s): %s", code, out)
    options = [
        ("default-shell", shell),
        ("default-command", shell),       # so login-shell quirks don't bite
        ("history-limit", "50000"),
        ("aggressive-resize", "on"),
        # window-size 'latest' means the window resizes to match whichever
        # client most recently attached or resized. Without this, the default
        # 'smallest' clamps every browser tab to the size of the smallest
        # client currently attached (often a stale terminal you forgot
        # about) — making attached web sessions appear cropped.
        ("window-size", "latest"),
    ]
    # alternate-screen is a per-window option. With it OFF, tmux intercepts
    # the ?1049h escape sequence from TUI apps (Claude Code, vim, less)
    # and keeps their output in the main-screen buffer, which then flows
    # into scrollback. This is what lets the browser scroll back through
    # long Claude responses — in alt-screen mode the content lives in a
    # discarded buffer xterm can't scroll.
    window_options = [
        ("alternate-screen", "off"),
    ]
    for name, value in options:
        code, out = await _run_tmux("set-option", "-g", name, value)
        if code != 0:
            log.warning("tmux set-option -g %s %s failed: %s", name, value, out)
    for name, value in window_options:
        code, out = await _run_tmux("set-window-option", "-g", name, value)
        if code != 0:
            log.warning("tmux set-window-option -g %s %s failed: %s", name, value, out)
    log.info("tmux server defaults applied (shell=%s)", shell)
