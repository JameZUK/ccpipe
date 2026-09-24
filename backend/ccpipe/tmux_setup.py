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


async def _run_tmux(*args: str, capture: bool = True) -> tuple[int, str]:
    # capture=False routes stdout to DEVNULL instead of a pipe. REQUIRED
    # for any tmux command that may SPAWN the server daemon on a cold boot
    # (the first new-session): the daemon inherits our stdout fd and holds
    # it open for its whole lifetime, so a pipe we drain via communicate()
    # never sees EOF and the call hangs forever (this wedged startup on the
    # 2026-06-06 reboot). stderr stays a pipe for error capture.
    from . import tmux as _tmux
    proc = await asyncio.create_subprocess_exec(
        _tmux.TMUX_BIN, *args,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        # When capture=False, stderr ALSO goes to DEVNULL — not a pipe.
        # communicate() waits for EOF on BOTH stdout and stderr, and a
        # server-spawning new-session leaves the daemon holding EVERY
        # inherited pipe fd open for its lifetime. Leaving stderr as a pipe
        # hangs communicate() exactly as a stdout pipe would.
        stderr=asyncio.subprocess.STDOUT if capture else asyncio.subprocess.DEVNULL,
        # This call may be the one that starts the server; see tmux_env().
        env=_tmux.tmux_env(),
    )
    out, err = await proc.communicate()
    blob = out if out is not None else err
    return proc.returncode or 0, (blob.decode(errors="replace").strip() if blob else "")


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
    from . import tmux as _tmux_mod
    from .tmux import CONTROL_SESSION_NAME
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
    # capture=False so the spawned server daemon can't hold our stdout pipe
    # open and hang startup (see _run_tmux).
    # Check first: has-session never spawns a server, and the create below
    # can't report WHY it failed (capture=False discards its output), so
    # without this every warm restart logged a bogus "create failed" for
    # the anchor that already existed.
    exists, _ = await _run_tmux("has-session", "-t",
                                _tmux_mod.session_target(CONTROL_SESSION_NAME))
    if exists != 0:
        code, _ = await _run_tmux(
            "new-session", "-d", "-s", CONTROL_SESSION_NAME, "sleep", "infinity",
            capture=False,
        )
        if code != 0:
            # The server may not be up — the set-option calls below will
            # then also warn, so the cause is clear.
            log.warning("tmux anchor session create failed (rc=%s)", code)
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
        # Pinned (they're tmux's defaults) because the browser mirrors any
        # OSC 52 it receives to the local clipboard: with "external" only
        # tmux's own copy-mode emits OSC 52, and programs in a pane can't
        # write the operator's clipboard. See the OSC 52 gate in terminal.ts.
        ("set-clipboard", "external"),
    ]
    # alternate-screen is a per-window option. With it OFF, tmux intercepts
    # the ?1049h escape sequence from TUI apps (Claude Code, vim, less)
    # and keeps their output in the main-screen buffer, which then flows
    # into scrollback. This is what lets the browser scroll back through
    # long Claude responses — in alt-screen mode the content lives in a
    # discarded buffer xterm can't scroll.
    window_options = [
        ("alternate-screen", "off"),
        # No raw escape passthrough: it would let pane programs bypass tmux
        # and reach the browser terminal directly (OSC 52 included).
        ("allow-passthrough", "off"),
    ]
    for name, value in options:
        code, out = await _run_tmux("set-option", "-g", name, value)
        if code != 0:
            log.warning("tmux set-option -g %s %s failed: %s", name, value, out)
    for name, value in window_options:
        code, out = await _run_tmux("set-window-option", "-g", name, value)
        if code != 0:
            log.warning("tmux set-window-option -g %s %s failed: %s", name, value, out)
    # A server started before tmux_env() existed (or by an older ccpipe)
    # still carries CCPIPE_* in its global environment, which new panes
    # inherit. Drop them; panes already running keep what they have.
    code, out = await _run_tmux("show-environment", "-g")
    if code == 0:
        for line in out.splitlines():
            var = line.split("=", 1)[0].lstrip("-")
            if var.startswith("CCPIPE_"):
                await _run_tmux("set-environment", "-g", "-u", var)
    log.info("tmux server defaults applied (shell=%s)", shell)
