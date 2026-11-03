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
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import libtmux
from libtmux.exc import LibTmuxException

from . import proctree

log = logging.getLogger(__name__)

# Hidden tmux session ccpipe keeps alive for its control-mode client (and
# as the anchor that keeps the server's -g options alive). Filtered from
# /api/sessions so users don't see it in the picker. Defined HERE, the
# lower-level module, and re-exported by tmux_control: tmux_control imports
# tmux at load time, so importing it back from tmux was an import cycle
# (importing tmux_control first found tmux half-initialised).
CONTROL_SESSION_NAME = "__ccpipe_ctrl"

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
    # Unix timestamp of the most recent pane output in any window of
    # this session — tmux's #{session_activity}. Drives the picker's
    # "last use" ordering: a session that just had output / input
    # bubbles to the top of its group automatically. Zero when tmux
    # doesn't expose it (older tmux, or libtmux fallback).
    activity: int = 0
    # Number of tmux clients attached (each ccpipe terminal or tile is one).
    clients: int = 0
    # tmux's #{session_last_attached}; 0 if never attached since the tmux
    # server started. session_meta keeps the record that survives reboots.
    last_attached: int = 0


def tmux_env() -> dict[str, str]:
    """``os.environ`` minus ccpipe's own ``CCPIPE_*`` settings, for every
    tmux spawn. A tmux server copies the environment of the client that
    starts it into its global environment, and every pane inherits that —
    so without this each claude/shell would see ccpipe's config (and
    ``CCPIPE_AUTH_PASSWORD`` whenever a drop-in sets it)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("CCPIPE_")}


def _server() -> libtmux.Server:
    # libtmux.Server is cheap to construct; no persistent state.
    return libtmux.Server()


def _sync_list_sessions() -> tuple[list[TmuxSession], bool]:
    """(user sessions, complete). ``complete`` is False when the listing
    can't be trusted to be the whole truth: the query failed, or tmux
    reported no sessions at all — while ccpipe runs its hidden anchor
    session always exists, so an empty answer means the server was
    unreachable (libtmux returns [] rather than raising for that)."""
    try:
        sessions = _server().sessions
    except LibTmuxException:
        return [], False
    result: list[TmuxSession] = []
    for s in sessions:
        if s.session_name == CONTROL_SESSION_NAME:
            continue  # hidden ccpipe control session — see tmux_control.py
        result.append(TmuxSession(
            name=s.session_name or "",
            windows=int(s.session_windows or 0),
            attached=int(s.session_attached or 0) > 0,
            created=int(s.session_created or 0),
            activity=int(getattr(s, "session_activity", 0) or 0),
            clients=int(s.session_attached or 0),
            last_attached=int(getattr(s, "session_last_attached", 0) or 0),
        ))
    return result, bool(sessions)


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


# Small cache around list_sessions() to coalesce bursts. tmux's
# `list-sessions` spawns a fresh subprocess every call (~5 ms);
# without coalescing, the picker reload-on-mutation pattern stacks
# multiple calls in rapid succession (the picker calls refresh()
# after every create / rename / kill / sticky-toggle). 250 ms is well
# below human-perceivable staleness for the session list.
_LIST_SESSIONS_CACHE_TTL_S = 0.25
_list_sessions_cache: tuple[float, list[TmuxSession], bool] | None = None
_list_sessions_lock = asyncio.Lock()


async def list_sessions_checked() -> tuple[list[TmuxSession], bool]:
    """The user sessions, and whether that list is complete (see
    _sync_list_sessions). Anything that concludes a session has ENDED from
    its absence must check the flag: a failed query looks like "every
    session is gone"."""
    global _list_sessions_cache
    now = asyncio.get_event_loop().time()
    cached = _list_sessions_cache
    if cached is not None and now - cached[0] < _LIST_SESSIONS_CACHE_TTL_S:
        return cached[1], cached[2]
    async with _list_sessions_lock:
        # Re-check inside the lock: a concurrent caller may have just
        # populated the cache while we were queued.
        cached = _list_sessions_cache
        now = asyncio.get_event_loop().time()
        if cached is not None and now - cached[0] < _LIST_SESSIONS_CACHE_TTL_S:
            return cached[1], cached[2]
        fresh, complete = await asyncio.to_thread(_sync_list_sessions)
        _list_sessions_cache = (now, fresh, complete)
        return fresh, complete


async def list_sessions() -> list[TmuxSession]:
    return (await list_sessions_checked())[0]


async def server_started_at() -> int | None:
    """When the tmux server started (unix), or None if it isn't reachable."""
    out = await _tmux_out("display-message", "-p", "#{start_time}")
    try:
        return int(out.strip()) if out else None
    except ValueError:
        return None


def invalidate_list_sessions_cache() -> None:
    """Drop any cached session list so the very next list_sessions()
    call returns fresh. Tests and the lifespan teardown call this to
    avoid carrying state across reinitialisation."""
    global _list_sessions_cache
    _list_sessions_cache = None


_TMUX_QUERY_TIMEOUT_S = 5.0


async def _pane_pid(name: str) -> int | None:
    """Active-pane PID for a tmux session, or None if the lookup failed (a
    hung tmux server times out rather than pinning the WS attach)."""
    out = await _tmux_out("display-message", "-t", pane_target(name), "-p", "#{pane_pid}")
    try:
        return int(out.strip()) if out else None
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


# The /history view polls every 3.5 s per open viewer, and resolving the
# claude pid costs a tmux subprocess plus a /proc walk each time. Cache the
# pid per session; a hit is revalidated with one /proc read (the process
# start time guards against pid reuse) and expires after a short TTL so a
# claude relaunched in the pane is picked up. create/kill/rename drop the
# entry immediately. The sessionId is still re-read every time (claude can
# switch transcripts without a new process).
_CLAUDE_PID_CACHE: dict[str, tuple[int, str, float]] = {}   # name → (pid, start, at)
_CLAUDE_PID_TTL_S = 15.0


def _proc_start_time(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    # Field 22 (starttime); split after the ")" that closes comm, which may
    # itself contain spaces or parentheses.
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


async def _cached_claude_pid(name: str) -> int | None:
    hit = _CLAUDE_PID_CACHE.get(name)
    now = time.monotonic()
    if hit and now - hit[2] < _CLAUDE_PID_TTL_S and _proc_start_time(hit[0]) == hit[1]:
        return hit[0]
    pid = await claude_pid(name)
    start = _proc_start_time(pid) if pid is not None else None
    if pid is not None and start is not None:
        _CLAUDE_PID_CACHE[name] = (pid, start, now)
        if len(_CLAUDE_PID_CACHE) > 128:        # stale names from renames/kills
            _CLAUDE_PID_CACHE.pop(next(iter(_CLAUDE_PID_CACHE)))
    else:
        _CLAUDE_PID_CACHE.pop(name, None)
    return pid


async def cached_claude_pid(name: str) -> int | None:
    """The pid of the claude in session *name*, cached as for /history."""
    return await _cached_claude_pid(name)


async def claude_sid_and_cwd(name: str) -> tuple[str | None, str | None]:
    """Resolve the claude (sessionId, cwd) for a tmux session with a SINGLE
    pid lookup. ``claude_session_id`` and ``session_cwd`` each resolve the pid
    independently (a tmux subprocess + /proc walk); the /history poll hits this
    on its hot path, so do the work once — and reuse it across polls."""
    pid = await _cached_claude_pid(name)
    if pid is None:
        return None, None
    try:
        cwd: str | None = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = None
    sid: str | None = None
    spath = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    try:
        data = json.loads(await asyncio.to_thread(spath.read_text))
        if isinstance(data, dict) and data.get("pid") == pid:
            v = data.get("sessionId")
            if isinstance(v, str) and v:
                sid = v
    except (OSError, ValueError):
        pass
    return sid, cwd


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


def wrap_in_shell(claude_cmd: str) -> str:
    """Wrap a claude invocation so the tmux session survives claude
    exiting. When claude exits, ``exec $SHELL -i`` replaces the shell
    process with an interactive shell in the same working directory,
    so the pane lands at a prompt instead of dying. Without this the
    only-pane closes → only-window closes → session is destroyed.
    """
    shell = os.environ.get("SHELL") or "/bin/bash"
    return f"{claude_cmd}; exec {shlex.quote(shell)} -i"


# Upper bound on user sessions (the control session doesn't count). Each is
# a live claude — hundreds of MB — so a runaway client (a reconnect loop
# with a fresh name each time, a script) could otherwise spawn them until
# this memory-tight host OOMs. Sticky restore at startup isn't capped: those
# sessions are ones the operator already had.
MAX_SESSIONS = 32


async def at_session_cap() -> bool:
    user = [s for s in await list_sessions() if s.name != CONTROL_SESSION_NAME]
    return len(user) >= MAX_SESSIONS


async def create_session(name: str, command: str = "claude",
                          cwd: str | None = None) -> None:
    """Create a tmux session and run *command* as its window. *cwd*
    becomes the session's starting working directory (falls back to
    $HOME, which is the legacy ws.py auto-create behaviour). For Claude
    Code we either pass plain ``claude`` or ``claude --resume <uuid>``."""
    _CLAUDE_PID_CACHE.pop(name, None)
    try:
        await asyncio.to_thread(_sync_create_session, name, command, cwd)
    finally:
        # Our own mutation must be visible to the very next list_sessions()
        # — e.g. POST /api/sessions looks the new session up right after
        # creating it, and the cap check just before primed the cache.
        invalidate_list_sessions_cache()


async def kill_session(name: str) -> bool:
    _CLAUDE_PID_CACHE.pop(name, None)
    try:
        return await asyncio.to_thread(_sync_kill_session, name)
    finally:
        invalidate_list_sessions_cache()


async def rename_session(old: str, new: str) -> bool:
    _CLAUDE_PID_CACHE.pop(old, None)
    _CLAUDE_PID_CACHE.pop(new, None)
    try:
        return await asyncio.to_thread(_sync_rename_session, old, new)
    finally:
        invalidate_list_sessions_cache()


def attach_argv(name: str, watch: bool = False) -> list[str]:
    """argv for spawning a tmux client attached to the given session.
    `--` terminates option parsing so a session name like ``-foo`` can't
    be reinterpreted as a flag (also rejected by ``safe_name``).

    ``watch`` attaches a read-only client that tmux leaves out of the
    window-size calculation (``ignore-size``): a small monitor tile then
    never shrinks the session on every other device."""
    argv = [TMUX_BIN, "attach-session"]
    if watch:
        argv += ["-f", "read-only,ignore-size"]
    return argv + ["-t", session_target(name), "--"]


# tmux resolves a bare `-t name` by prefix / fnmatch, and treats `%3` / `@1`
# as pane / window ids — so `-t fo` lands in session "foobar". A leading
# "=" demands an exact session-name match. Commands that take a
# target-SESSION accept "=name"; commands that take a target-PANE (or
# window) need "=name:" — with plain "=name" they match nothing and fail
# silently (verified against tmux 3.7c).
def session_target(name: str) -> str:
    return f"={name}"


def pane_target(name: str) -> str:
    """The active pane of session *name*, matched exactly."""
    return f"={name}:"


_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def safe_name(name: str) -> str:
    """Validate tmux session names: letters, digits, "_" and "-" only, no
    leading dash (it would look like a tmux flag).

    An allowlist rather than a list of bad characters, so tmux target
    syntax (`%3`, `@1`, `=x`, `!`, `~`), control characters and NUL can't
    slip through, and nothing path- or shell-like ever reaches tmux argv."""
    if not name or name.startswith("-"):
        raise ValueError(f"invalid session name: {name!r}")
    # Defense-in-depth length cap: a session name is a short human label;
    # anything past 128 chars is abuse, not a real name, and bounds the
    # size of every tmux argv / log line / display-message it flows into.
    if len(name) > 128:
        raise ValueError(f"invalid session name (too long): {name[:32]!r}…")
    if not _SAFE_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid session name: {name!r}")
    return name


# ─── Picker details: active pane's cwd + title, screen previews ──────────

_FIELD_SEP = "\x1f"
# Control characters (C0 + DEL + C1) — never wanted in a title shown in the UI.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


async def _tmux_out(*args: str) -> str | None:
    """stdout of one tmux command, or None on failure / timeout."""
    return await proctree.run_argv([TMUX_BIN, *args], _TMUX_QUERY_TIMEOUT_S,
                                   env=tmux_env(), check=True)


async def active_panes() -> dict[str, tuple[str, str]]:
    """session name → (cwd, title) of the active pane of its active window,
    for every session, in one tmux call. The title is what the program in
    the pane set (Claude Code puts its task name there)."""
    fmt = _FIELD_SEP.join([
        "#{session_name}", "#{window_active}#{pane_active}",
        "#{pane_current_path}", "#{pane_title}",
    ])
    out = await _tmux_out("list-panes", "-a", "-F", fmt)
    result: dict[str, tuple[str, str]] = {}
    for line in (out or "").splitlines():
        parts = line.split(_FIELD_SEP)
        if len(parts) != 4 or parts[1] != "11" or parts[0] == CONTROL_SESSION_NAME:
            continue
        result[parts[0]] = (parts[2], _CTRL_RE.sub("", parts[3])[:200])
    return result


async def window_layout(name: str) -> tuple[int, int, int] | None:
    """(width, height, status-line rows) of the session's current window.
    The height excludes the status line, which takes that many rows of each
    client's terminal (0 when the status line is off)."""
    out = await _tmux_out("display-message", "-p", "-t", pane_target(name),
                          f"#{{window_width}}{_FIELD_SEP}#{{window_height}}{_FIELD_SEP}#{{status}}")
    parts = (out or "").strip().split(_FIELD_SEP)
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    status = parts[2].strip()
    rows = 0 if status in ("off", "0", "") else (int(status) if status.isdigit() else 1)
    return int(parts[0]), int(parts[1]), rows


PANE_STATE_FORMAT = (f"#{{window_width}}{_FIELD_SEP}#{{window_height}}{_FIELD_SEP}#{{status}}"
                     f"{_FIELD_SEP}#{{history_size}}")


async def pane_state(name: str) -> tuple[int, int, int, int] | None:
    """window_layout plus the active pane's history size (rows scrolled
    into tmux's scrollback): (width, height, status rows, history rows)."""
    out = await _tmux_out("display-message", "-p", "-t", pane_target(name), PANE_STATE_FORMAT)
    return parse_pane_state(out)


def parse_pane_state(out: str | None) -> tuple[int, int, int, int] | None:
    """PANE_STATE_FORMAT's output as pane_state's tuple."""
    parts = (out or "").strip().split(_FIELD_SEP)
    if len(parts) != 4 or not all(p.isdigit() for p in (parts[0], parts[1], parts[3])):
        return None
    status = parts[2].strip()
    rows = 0 if status in ("off", "0", "") else (int(status) if status.isdigit() else 1)
    return int(parts[0]), int(parts[1]), rows, int(parts[3])


async def capture_screen(name: str) -> list[str] | None:
    """The visible screen of the session's active pane as plain text (no
    escapes), trailing spaces and trailing blank rows removed."""
    out = await _tmux_out("capture-pane", "-p", "-t", pane_target(name))
    if out is None:
        return None
    lines = [line.rstrip() for line in out.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


async def all_panes() -> dict[str, list[int]]:
    """session name → pane pids of every pane in every window."""
    out = await _tmux_out("list-panes", "-a", "-F", f"#{{session_name}}{_FIELD_SEP}#{{pane_pid}}")
    result: dict[str, list[int]] = {}
    for line in (out or "").splitlines():
        name, _, pid = line.partition(_FIELD_SEP)
        if name and name != CONTROL_SESSION_NAME and pid.isdigit():
            result.setdefault(name, []).append(int(pid))
    return result


# Claude Code (2.1.x) keeps ~/.claude/sessions/<pid>.json per running
# process, including what it is doing — "status": "busy" (working on a
# turn), "shell" (idle at the prompt with a background shell task running)
# or "idle" — and the tmux pane it runs in ("tmux": "name:@4.%4"). This is
# the reliable "is Claude working" signal: the pane title shows ✳ even mid
# turn, and a long tool call produces no pane output for minutes.
_STATUS_RANK = {"busy": 3, "shell": 2, "idle": 1}


def _read_claude_states(pane_sessions: dict[str, str]) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    d = Path.home() / ".claude" / "sessions"
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return result
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        pid, status, where = data.get("pid"), data.get("status"), data.get("tmux")
        if not (isinstance(pid, int) and isinstance(status, str) and isinstance(where, str)):
            continue
        # Stale files outlive their process (and pids get reused): the file
        # records the process start time, which must match /proc.
        start = _proc_start_time(pid)
        if start is None or str(data.get("procStart", "")) != start:
            continue
        name = pane_sessions.get(where.rpartition(".")[2])
        if not name:
            continue
        since = data.get("statusUpdatedAt")
        since_s = int(since / 1000) if isinstance(since, (int, float)) else 0
        prev = result.get(name)
        # Several Claudes in one session (split panes): the busiest wins.
        if prev is None or _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(prev[0], 0):
            result[name] = (status, since_s)
    return result


async def claude_states() -> dict[str, tuple[str, int]]:
    """session name → (Claude's status, since when) for every session with a
    live Claude in it."""
    out = await _tmux_out("list-panes", "-a", "-F", f"#{{pane_id}}{_FIELD_SEP}#{{session_name}}")
    panes: dict[str, str] = {}
    for line in (out or "").splitlines():
        pane, _, name = line.partition(_FIELD_SEP)
        if pane and name and name != CONTROL_SESSION_NAME:
            panes[pane] = name
    return await asyncio.to_thread(_read_claude_states, panes)
