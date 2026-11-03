"""Session lifecycle: every session is remembered (session_meta.py), a
stopped one resumes the same Claude conversation when opened, only the most
recently used few start at boot, and idle ones are stopped to free memory.

Settings (config.SessionsConfig, Settings → Sessions):
  boot_sticky / boot_other            how many of the most recently used
                                      sticky / other sessions start at boot
  idle_stop_sticky_min / _other_min   stop after this long completely idle
                                      (0 = never)

"Completely idle" is deliberately hard to reach. A session is BUSY — and
its idle clock restarts — whenever any of these holds (busy_reason):
  1. a terminal or tile is attached;
  2. there was output or input in the pane;
  3. Claude is working, or idle with a background shell task — Claude
     Code's own status ("busy" / "shell") in ~/.claude/sessions/<pid>.json
     (tmux.claude_states); older versions: a spinner in the pane title;
  4. a Claude tool command is running, foreground or background — Claude
     runs each as `bash -c 'source ~/.claude/shell-snapshots/…'`, so any
     process under Claude carrying that is a task (a CI-wait loop sleeping
     at 0 % CPU still counts);
  5. a pane's shell is running anything other than Claude — a server, a
     build, an editor, in any window or pane of the session;
  6. its processes used CPU (≥ CPU_BUSY_PCT of a core over the last tick);
  7. Claude is waiting on a question / permission prompt (checked from the
     screen just before a stop).
Helpers Claude starts by itself (MCP servers, the Playwright browser,
language servers) are NOT tasks: on 2026-11-02 19 of 23 idle sessions had
exactly those running and nothing else.

Only CLAUDE sessions are idle-stopped — a Claude process must be running
in the session — and only when the directory is known (so it can resume).
A plain shell (one started over ssh, say) costs next to nothing and is
never touched. Every auto-stop is logged with how long it had been idle.

Resuming starts `claude --resume <its conversation>` (or `--continue` when
the transcript is gone); a session never seen running Claude resumes as a
plain shell in its directory.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, NamedTuple

from . import config as app_config
from . import proctree, session_meta, sticky, tmux
from .paths import claude_project_dir

log = logging.getLogger(__name__)

TICK_S = 60
CPU_BUSY_PCT = 3.0                 # of one core, averaged over a tick
DETAILS_EVERY_S = 300
PROMPT_RE = re.compile(r"Do you want to|Would you like to|❯\s*1\.\s*Yes|\[y/n\]|\(y/n\)", re.I)

_started = time.time()                        # ≈ process start (imported at startup)
_busy_at: dict[str, float] = {}               # last moment a busy signal was seen
_cpu_prev: dict[str, tuple[float, float]] = {}  # (monotonic, cpu seconds) last tick
_details_at: dict[str, float] = {}
_status: dict[str, dict[str, Any]] = {}


class LifecycleError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ─── busy / idle ──────────────────────────────────────────────────────────

def _working(title: str | None) -> bool:
    first = (title or "").lstrip()[:1]
    return bool(first) and 0x2800 <= ord(first) <= 0x28FF


def busy_reason(s: tmux.TmuxSession, roots: list[int], table: proctree.Table,
                title: str | None, now: float, mono: float,
                claude: str | None = None) -> str | None:
    """Why the session is in use right now, or None if nothing says so."""
    if s.clients > 0:
        return "open in a terminal"
    if now - s.activity < TICK_S * 2:
        return "recent output"
    if claude == "busy" or _working(title):
        return "Claude is working"
    if claude == "shell":
        return "background task running"
    pids: list[int] = []
    for root in roots:
        rp = table.procs.get(root)
        if rp is None:
            continue
        pids.extend(table.subtree(root))
        claudes = [root] if rp.is_claude else []
        if not rp.is_claude:
            for c in table.kids.get(root, ()):
                cp = table.procs[c]
                if cp.is_claude:
                    claudes.append(c)
                else:
                    return f"running {cp.comm}"
        for cr in claudes:
            for p in table.subtree(cr)[1:]:
                if table.procs[p].is_claude_task:
                    return "running a command"
    cpu = table.cpu_of(pids)
    prev = _cpu_prev.get(s.name)
    _cpu_prev[s.name] = (mono, cpu)
    if prev and mono > prev[0]:
        pct = (cpu - prev[1]) / (mono - prev[0]) * 100.0
        if pct >= CPU_BUSY_PCT:
            return f"using CPU ({pct:.0f}% of a core)"
    return None


def has_claude(roots: list[int], table: proctree.Table, state: str | None) -> bool:
    """Is a Claude process running in the session (any pane)?"""
    if state is not None:
        return True
    return any(table.procs[p].is_claude for r in roots for p in table.subtree(r))


def status() -> dict[str, dict[str, Any]]:
    """name → {"busy": reason | None, "idle_since": unix, "stops_at": unix | None},
    as of the last tick."""
    return _status


async def tick() -> None:
    cfg = app_config.load().sessions
    sessions, complete = await tmux.list_sessions_checked()
    if not complete:
        return                     # tmux unreachable: decide nothing this tick
    live = {s.name for s in sessions}
    for gone in set(_status) - live:
        _status.pop(gone, None)
        _busy_at.pop(gone, None)
        _cpu_prev.pop(gone, None)
    if not sessions:
        return
    panes, details, claude = await asyncio.gather(
        tmux.all_panes(), tmux.active_panes(), tmux.claude_states())
    table = await asyncio.to_thread(proctree.snapshot)
    stick = sticky.load()
    meta = session_meta.load()
    now, mono = time.time(), time.monotonic()
    for s in sessions:
        cwd, title = details.get(s.name, (None, None))
        roots = panes.get(s.name, [])
        state = claude.get(s.name, (None, 0))[0]
        reason = busy_reason(s, roots, table, title, now, mono, state)
        if reason:
            _busy_at[s.name] = now
        m = meta.get(s.name, {})
        idle_since = max(_busy_at.get(s.name, _started), s.activity, s.created, m.get("opened", 0))
        limit = cfg.idle_stop_sticky_min if s.name in stick else cfg.idle_stop_other_min
        resumable = bool(m.get("cwd") or stick.get(s.name, {}).get("cwd") or cwd)
        claude_in = has_claude(roots, table, state)
        stoppable = bool(limit and resumable and claude_in and not reason)
        stops_at = idle_since + limit * 60 if stoppable else None
        _status[s.name] = {"busy": reason, "idle_since": int(idle_since),
                           "stops_at": int(stops_at) if stops_at else None}

        if now - _details_at.get(s.name, 0) >= DETAILS_EVERY_S:
            _details_at[s.name] = now
            try:
                sid, ccwd = await tmux.claude_sid_and_cwd(s.name)
                session_meta.note_details(s.name, ccwd or cwd, sid, claude_in)
            except Exception:
                log.debug("details for %s failed", s.name, exc_info=True)

        if stops_at is not None and now >= stops_at:
            screen = await tmux.capture_screen(s.name) or []
            if PROMPT_RE.search("\n".join(screen[-15:])):
                _busy_at[s.name] = now
                _status[s.name].update(busy="waiting for an answer", stops_at=None)
                continue
            log.info("idle stop: %s (%s) — idle %.1f h, limit %d min",
                     s.name, "sticky" if s.name in stick else "ephemeral",
                     (now - idle_since) / 3600, limit)
            try:
                await stop(s.name, reason="idle")
            except Exception:
                log.exception("idle stop of %s failed", s.name)


async def run() -> None:
    """Background loop (started by the app lifespan)."""
    while True:
        await asyncio.sleep(TICK_S)
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lifecycle tick failed")


# ─── resume / stop / forget ───────────────────────────────────────────────

def _transcript_exists(cwd: str, claude_id: str) -> bool:
    return (claude_project_dir(cwd) / f"{claude_id}.jsonl").is_file()


def resume_command(cwd: str, claude_id: str | None, shell: bool = False) -> str:
    """The conversation the session was on when known (and its transcript is
    still there); otherwise the directory's most recent one. A session that
    never ran Claude gets its shell back."""
    if shell:
        return f"exec {shlex.quote(os.environ.get('SHELL') or '/bin/bash')} -i"
    if claude_id and session_meta.UUID_RE.match(claude_id) and _transcript_exists(cwd, claude_id):
        return tmux.wrap_in_shell(f"claude --resume {shlex.quote(claude_id)}")
    return tmux.wrap_in_shell("claude --continue")


class Resumable(NamedTuple):
    cwd: str
    claude: str | None
    shell: bool


def resumable(name: str) -> Resumable | None:
    m = session_meta.load().get(name, {})
    cwd = m.get("cwd") or sticky.load().get(name, {}).get("cwd")
    return Resumable(cwd, m.get("claude"), bool(m.get("shell"))) if cwd else None


def ended_note(name: str) -> str:
    """Why a session that was open is no longer running, for the terminal."""
    m = session_meta.load().get(name)
    if m is None:
        return f"{name} was removed"
    return {"stopped": f"{name} was stopped",
            "idle": f"{name} was stopped after being idle"}.get(m.get("stop_reason", ""),
                                                                f"{name} has ended")


async def resume(name: str, *, capped: bool = True) -> None:
    """Start a remembered session again in its directory. No-op if running."""
    if await tmux.session_exists(name):
        return
    target = resumable(name)
    if target is None:
        raise LifecycleError(404, "nothing to resume: its directory isn't known")
    cwd, claude_id, shell = target
    try:
        resolved = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        raise LifecycleError(409, f"its directory no longer exists: {cwd}")
    if not resolved.is_dir():
        raise LifecycleError(409, f"not a directory: {cwd}")
    # Same jail as creating a session (the recorded cwd came from the pane,
    # which a shell can have cd'd anywhere).
    from fastapi import HTTPException
    from .routes.fs import _enforce_fs_jail
    try:
        _enforce_fs_jail(resolved)
    except HTTPException:
        raise LifecycleError(403, f"directory is outside the allowed root: {cwd}")
    if capped and await tmux.at_session_cap():
        raise LifecycleError(429, f"session limit reached ({tmux.MAX_SESSIONS})")
    await tmux.create_session(name, command=resume_command(str(resolved), claude_id, shell),
                              cwd=str(resolved))
    session_meta.mark_running(name)
    _busy_at[name] = time.time()
    log.info("resumed session %r in %s (%s)", name, resolved,
             "shell" if shell else f"conversation {claude_id}" if claude_id else "latest conversation")


async def stop(name: str, reason: str = "stopped") -> None:
    """Stop the session's processes, keeping it listed to resume later."""
    if not await tmux.session_exists(name):
        return
    try:
        sid, cwd = await tmux.claude_sid_and_cwd(name)
        details, panes, states = await asyncio.gather(
            tmux.active_panes(), tmux.all_panes(), tmux.claude_states())
        table = await asyncio.to_thread(proctree.snapshot)
        claude_in = has_claude(panes.get(name, []), table, states.get(name, (None, 0))[0])
        session_meta.note_details(name, cwd or details.get(name, (None, None))[0], sid, claude_in)
    except Exception:
        log.debug("details before stop failed", exc_info=True)
    if not await tmux.kill_session(name):
        raise LifecycleError(500, "stop failed")
    session_meta.mark_stopped(name, reason)
    _status.pop(name, None)


async def forget(name: str) -> None:
    """Stop it if running and remove it from the list for good (sticky too,
    or it would come back after a reboot)."""
    if await tmux.session_exists(name):
        await tmux.kill_session(name)
    session_meta.forget(name)
    sticky.clear(name)
    _status.pop(name, None)


# ─── boot ─────────────────────────────────────────────────────────────────

async def boot_restore() -> None:
    """At boot — a tmux server that started with ccpipe, so every session
    died since ccpipe last ran — start the most recently used
    ``boot_sticky`` sticky and ``boot_other`` other sessions.

    A backend restart (deploy) finds the tmux server already running and
    starts nothing, even when no user session is running: sessions the
    user stopped stay stopped. The server's age is the test, not the
    session count — ccpipe's hidden anchor session keeps the server alive
    with zero user sessions, and the lifespan creates that anchor before
    this runs."""
    if await tmux.list_sessions():
        return
    started = await tmux.server_started_at()
    if started is None or started < int(_started) - 1:
        log.info("boot: tmux server predates ccpipe (a restart, not a boot) — starting nothing")
        return
    cfg = app_config.load().sessions
    meta = session_meta.load()
    stick = sticky.load()
    names = {n for n, e in meta.items() if e.get("cwd")} | {n for n, e in stick.items() if e.get("cwd")}

    def recency(n: str) -> int:
        return session_meta.last_used(meta.get(n, {}))

    picks = (sorted((n for n in names if n in stick), key=recency, reverse=True)[:cfg.boot_sticky]
             + sorted((n for n in names if n not in stick), key=recency, reverse=True)[:cfg.boot_other])
    log.info("boot: starting %d of %d remembered sessions (%d sticky, %d other max): %s",
             len(picks), len(names), cfg.boot_sticky, cfg.boot_other, ", ".join(picks) or "none")

    async def one(n: str) -> None:
        try:
            await resume(n, capped=False)
        except Exception as exc:
            log.warning("boot: could not start %r: %s", n, exc)

    await asyncio.gather(*(one(n) for n in picks))
