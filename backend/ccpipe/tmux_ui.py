"""Driving tmux from the browser: windows, panes, copy-mode.

The toolbar buttons map to a FIXED table of tmux commands here — the
client names an action, never a command line — so nothing typed in the
browser reaches tmux's command parser. Every command targets one session
by exact name (see tmux.session_target / pane_target).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from . import tmux

log = logging.getLogger(__name__)

_TIMEOUT_S = 5.0
_SEP = "\t"
# Window names: printable, no tabs/newlines (they'd break the list format).
_WINDOW_NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,40}")


async def run(*args: str) -> tuple[int, str]:
    """Run one tmux command; (returncode, stdout+stderr). -1 on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux.TMUX_BIN, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=tmux.tmux_env(),
        )
    except FileNotFoundError:
        return -1, "tmux not found"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return -1, "tmux timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def display(name: str, fmt: str) -> str | None:
    """Expand a tmux format against session *name*'s active pane."""
    code, out = await run("display-message", "-p", "-t", tmux.pane_target(name), fmt)
    return out.rstrip("\n") if code == 0 else None


async def window_size(name: str) -> tuple[int, int] | None:
    """(cols, rows) of the session's current window."""
    out = await display(name, "#{window_width}x#{window_height}")
    try:
        cols, rows = (int(v) for v in (out or "").split("x"))
    except ValueError:
        return None
    return (cols, rows) if cols > 0 and rows > 0 else None


async def window_panes(name: str) -> int:
    """Pane count of the session's current window (1 if unknown)."""
    try:
        return max(1, int(await display(name, "#{window_panes}") or 1))
    except ValueError:
        return 1


async def list_windows(name: str) -> list[dict[str, Any]]:
    fmt = _SEP.join(["#{window_index}", "#{window_active}", "#{window_panes}",
                     "#{window_zoomed_flag}", "#{window_name}"])
    code, out = await run("list-windows", "-t", tmux.session_target(name), "-F", fmt)
    if code != 0:
        return []
    windows = []
    for line in out.splitlines():
        parts = line.split(_SEP, 4)
        if len(parts) != 5:
            continue
        try:
            windows.append({"index": int(parts[0]), "active": parts[1] == "1",
                            "panes": int(parts[2]), "zoomed": parts[3] == "1",
                            "name": parts[4]})
        except ValueError:
            continue
    return windows


class ActionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


ACTIONS = (
    "split-h", "split-v", "pane-next", "pane-zoom", "pane-close",
    "window-new", "window-next", "window-prev", "window-select",
    "window-rename", "window-close",
    "copy-mode", "page-up", "page-down", "copy-exit",
)


async def perform(name: str, action: str, arg: Any = None) -> None:
    """Run toolbar *action* against session *name*. Raises ActionError."""
    sess = tmux.session_target(name)          # "=name"
    pane = tmux.pane_target(name)             # "=name:" (active pane)
    here = "#{pane_current_path}"
    match action:
        case "split-h":
            args = ["split-window", "-h", "-t", pane, "-c", here]
        case "split-v":
            args = ["split-window", "-v", "-t", pane, "-c", here]
        case "pane-next":
            args = ["select-pane", "-t", f"{pane}.+"]
        case "pane-zoom":
            args = ["resize-pane", "-Z", "-t", pane]
        case "pane-close":
            # Closing the last pane would close the window — and the last
            # window, the whole session. That's the window/session buttons'
            # job, with their own guards.
            if await window_panes(name) <= 1:
                raise ActionError(409, "only one pane in this window")
            args = ["kill-pane", "-t", pane]
        case "window-new":
            args = ["new-window", "-a", "-t", pane, "-c", here]
        case "window-next":
            args = ["next-window", "-t", sess]
        case "window-prev":
            args = ["previous-window", "-t", sess]
        case "window-select":
            if not isinstance(arg, int) or isinstance(arg, bool) or not 0 <= arg < 10_000:
                raise ActionError(400, "window index required")
            args = ["select-window", "-t", f"{sess}:{arg}"]
        case "window-rename":
            if not isinstance(arg, str) or not _WINDOW_NAME_RE.fullmatch(arg.strip()):
                raise ActionError(400, "invalid window name")
            args = ["rename-window", "-t", pane, "--", arg.strip()]
        case "window-close":
            if len(await list_windows(name)) <= 1:
                raise ActionError(409, "only one window in this session")
            args = ["kill-window", "-t", pane]
        case "copy-mode":
            args = ["copy-mode", "-t", pane]
        case "page-up":
            # -u enters copy-mode if needed and scrolls one page up.
            args = ["copy-mode", "-u", "-t", pane]
        case "page-down":
            args = ["send-keys", "-X", "-t", pane, "page-down"]
        case "copy-exit":
            args = ["send-keys", "-X", "-t", pane, "cancel"]
        case _:
            raise ActionError(400, "unknown action")
    code, out = await run(*args)
    if code != 0:
        # page-down / copy-exit outside copy-mode: "not in a mode" — harmless.
        if action in ("page-down", "copy-exit") and "not in a mode" in out:
            return
        log.info("tmux action %s on %s failed: %s", action, name, out.strip())
        raise ActionError(409 if code > 0 else 504, out.strip() or "tmux command failed")
