"""Thin libtmux wrapper for session management.

We use libtmux for one-shot operations (list/has/create) because it gives us
proper error types and avoids tab-parsed shell output. For the persistent
event channel see tmux_control.py. For the attached client see pty_relay.py
(spawns `tmux attach-session` in a PTY).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import libtmux
from libtmux.exc import LibTmuxException

from .tmux_control import CONTROL_SESSION_NAME

log = logging.getLogger(__name__)

# Resolve once so subsequent invocations don't depend on $PATH ordering
# changing under the process. Falls back to "tmux" if not on PATH yet —
# the lookup will then fail loudly at first use.
TMUX_BIN: str = shutil.which("tmux") or "tmux"


@dataclass(frozen=True)
class TmuxSession:
    name: str
    windows: int
    attached: bool
    created: int  # unix timestamp


def _server() -> libtmux.Server:
    # libtmux.Server is cheap to construct; no persistent state.
    return libtmux.Server()


def _sync_list_sessions() -> list[TmuxSession]:
    try:
        sessions = _server().sessions
    except LibTmuxException:
        return []
    result: list[TmuxSession] = []
    for s in sessions:
        if s.session_name == CONTROL_SESSION_NAME:
            continue  # hidden ccpipe control session — see tmux_control.py
        result.append(TmuxSession(
            name=s.session_name or "",
            windows=int(s.session_windows or 0),
            attached=int(s.session_attached or 0) > 0,
            created=int(s.session_created or 0),
        ))
    return result


def _sync_session_exists(name: str) -> bool:
    try:
        return _server().has_session(name)
    except LibTmuxException:
        return False


def _sync_create_session(name: str, command: str, cwd: str | None = None) -> None:
    server = _server()
    start_dir = cwd or os.environ.get("HOME") or "/tmp"
    try:
        server.new_session(
            session_name=name,
            attach=False,
            window_command=command,
            start_directory=start_dir,
        )
    except LibTmuxException as exc:
        # Two near-simultaneous WS connections for the same session name
        # can each pass session_exists()==False then both reach this
        # function. The loser of the race sees "duplicate session" —
        # benign: the session is there, attach can proceed.
        if "duplicate session" in str(exc).lower() or _sync_session_exists(name):
            return
        raise


def _sync_kill_session(name: str) -> bool:
    server = _server()
    try:
        s = server.sessions.get(session_name=name)
    except LibTmuxException:
        return False
    if s is None:
        return False
    # libtmux still exposes Session.kill_session() in dir() but the method
    # is a deprecated stub that raises DeprecatedError on call. Use kill()
    # which is the actual implementation in libtmux 0.30+. Belt-and-braces
    # the BaseException catch since DeprecatedError doesn't derive from
    # LibTmuxException in all versions.
    try:
        s.kill()
    except (LibTmuxException, Exception) as exc:
        log.warning("kill failed for %r: %s", name, exc)
        # If the session is already gone post-failure, treat as success.
        return not _sync_session_exists(name)
    return True


def _sync_rename_session(old: str, new: str) -> bool:
    server = _server()
    try:
        s = server.sessions.get(session_name=old)
    except LibTmuxException:
        return False
    if s is None:
        return False
    try:
        s.rename_session(new)
    except LibTmuxException:
        return False
    return True


async def list_sessions() -> list[TmuxSession]:
    return await asyncio.to_thread(_sync_list_sessions)


_TMUX_QUERY_TIMEOUT_S = 5.0


async def _pane_pid(name: str) -> int | None:
    """Active-pane PID for a tmux session, or None if the lookup failed."""
    try:
        proc = await asyncio.create_subprocess_exec(
            TMUX_BIN, "display-message", "-t", name, "-p", "#{pane_pid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    # Wrap communicate in a timeout — a hung tmux server would otherwise
    # pin the WS attach indefinitely. 5s is generous; display-message
    # against a healthy tmux is a single-digit-ms operation.
    try:
        out, _ = await asyncio.wait_for(proc.communicate(),
                                         timeout=_TMUX_QUERY_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        log.warning("tmux display-message for %r timed out", name)
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(out.decode().strip())
    except ValueError:
        return None


async def claude_pid(name: str) -> int | None:
    """Best-effort: return the PID of the claude process running inside the
    tmux session. Falls back to the pane PID itself if no descendant matches
    (e.g. when tmux launched ``claude`` directly as the window command, so
    the pane process IS claude)."""
    pp = await _pane_pid(name)
    if pp is None:
        return None
    return await _find_claude_descendant(pp) or pp


async def session_cwd(name: str) -> str | None:
    """Best-effort: return the cwd of the claude (or fallback shell) process
    running inside the tmux session. Used to scope TTS to the session's
    Claude Code project transcript.
    """
    pid = await claude_pid(name)
    if pid is None:
        return None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


async def claude_session_id(name: str) -> str | None:
    """Return the claude *sessionId* for this tmux session.

    Claude Code writes ``~/.claude/sessions/<pid>.json`` per running
    process with its current sessionId (the same UUID it uses for the
    JSONL transcript filename). Reading this is the only reliable way
    to bind a tmux session to a specific claude transcript when several
    claude processes are running with overlapping cwds — otherwise the
    cwd-based filter in ws.py lets two claudes in the same project dir
    cross-talk on the TTS stream.

    Returns None if no claude process is found OR claude version is
    too old to write the session file. Callers should fall back to
    looser cwd-based filtering in that case.
    """
    pid = await claude_pid(name)
    if pid is None:
        return None
    path = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    try:
        data = json.loads(await asyncio.to_thread(path.read_text))
    except (OSError, ValueError):
        return None
    # Cross-check: the JSON should agree with the path it lives at,
    # otherwise it's a stale file for a recycled PID.
    if not isinstance(data, dict) or data.get("pid") != pid:
        return None
    sid = data.get("sessionId")
    return sid if isinstance(sid, str) and sid else None


async def _find_claude_descendant(root_pid: int) -> int | None:
    """Walk descendants of root_pid, return the first that is the running
    claude process.

    Hardening over the naive "cmdline contains 'claude'":
      - Match argv[0]'s **basename** (or basename of the first argv that
        looks like a script path) — refuses to claim arbitrary `claude*`
        processes.
      - Re-verify the parent in /proc/<pid>/stat to defend against PID
        reuse between reading children and reading cmdline.
    """
    visited: set[int] = set()
    stack: list[tuple[int, int]] = [(root_pid, root_pid)]  # (pid, expected_parent)
    while stack:
        pid, expected_parent = stack.pop()
        if pid in visited:
            continue
        visited.add(pid)
        try:
            children_raw = await asyncio.to_thread(_read_children, pid)
        except OSError:
            continue
        for child in children_raw:
            try:
                ppid = await asyncio.to_thread(_read_ppid, child)
            except OSError:
                continue
            if ppid != pid:
                continue  # PID reuse: this child is no longer parented to us
            try:
                argv = await asyncio.to_thread(_read_argv, child)
            except OSError:
                continue
            if _is_claude_argv(argv):
                return child
            stack.append((child, pid))
    return None


_CLAUDE_BASENAMES = {"claude", "claude-code"}


def _is_claude_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    # argv[0] is usually the interpreter or the launcher binary. The most
    # reliable signal is finding 'claude' or 'claude-code' as the basename
    # of any argv entry — covers both `/usr/local/bin/claude` and
    # `node .../claude-code/cli.js`.
    for arg in argv:
        if not arg:
            continue
        # Strip query/positional separators; we only care about the path part.
        base = os.path.basename(arg).split(".")[0]
        if base in _CLAUDE_BASENAMES:
            return True
    return False


def _read_children(pid: int) -> list[int]:
    path = f"/proc/{pid}/task/{pid}/children"
    try:
        with open(path) as f:
            return [int(x) for x in f.read().split()]
    except FileNotFoundError:
        return []


def _read_argv(pid: int) -> list[str]:
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        raw = f.read()
    if not raw:
        return []
    return raw.rstrip(b"\x00").decode(errors="replace").split("\x00")


def _read_ppid(pid: int) -> int:
    # /proc/<pid>/stat fields: pid (comm) state ppid ...
    # comm may contain spaces / parens, so split on the LAST ')' for safety.
    with open(f"/proc/{pid}/stat") as f:
        data = f.read()
    rparen = data.rfind(")")
    if rparen < 0:
        raise OSError("malformed /proc stat")
    after = data[rparen + 1 :].split()
    # after[0] = state, after[1] = ppid
    return int(after[1])


async def session_exists(name: str) -> bool:
    return await asyncio.to_thread(_sync_session_exists, name)


async def create_session(name: str, command: str = "claude",
                          cwd: str | None = None) -> None:
    """Create a tmux session and run *command* as its window. *cwd*
    becomes the session's starting working directory (falls back to
    $HOME, which is the legacy ws.py auto-create behaviour). For Claude
    Code we either pass plain ``claude`` or ``claude --resume <uuid>``."""
    await asyncio.to_thread(_sync_create_session, name, command, cwd)


async def kill_session(name: str) -> bool:
    return await asyncio.to_thread(_sync_kill_session, name)


async def rename_session(old: str, new: str) -> bool:
    return await asyncio.to_thread(_sync_rename_session, old, new)


def attach_argv(name: str) -> list[str]:
    """argv for spawning a tmux client attached to the given session.
    `--` terminates option parsing so a session name like ``-foo`` can't
    be reinterpreted as a flag (also rejected by ``safe_name``)."""
    return [TMUX_BIN, "attach-session", "-t", name, "--"]


def safe_name(name: str) -> str:
    """Validate tmux session names; reject anything with shell metacharacters,
    dots, slashes, or a leading dash (which would look like a tmux flag).

    Slashes in particular are forbidden because tmux historically built
    socket paths from session names — even though that doesn't apply to
    ccpipe today, it's cheap forward-looking hardening for any future
    code that interpolates a session name into a filesystem path."""
    if not name or name.startswith("-"):
        raise ValueError(f"invalid session name: {name!r}")
    if any(c in name for c in " \t\n.:/'\"\\$`;&|<>(){}[]*?#"):
        raise ValueError(f"invalid session name: {name!r}")
    return name
