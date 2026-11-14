"""Keeping the running Claudes on the installed Claude Code version.

A package update replaces the ``claude`` binary; the Claudes already
running carry on with the old one until they are restarted (seen
2026-11-14: three of six sessions two and three releases behind). Each
running Claude records its version in ``~/.claude/sessions/<pid>.json``;
the installed one is what ``claude --version`` prints (a local command:
no network, no account).

A session whose Claude is older is marked in the picker, and can be
restarted on the same conversation: by hand from its menu, or by itself
with Settings → Sessions → "keep Claude Code up to date".

**A restart must not cost anything**, so it only happens when all of this
holds (``blocked`` says which doesn't):

* the session has one pane and one Claude, and that Claude says it is
  idle: not working, not waiting for an answer, no background shell or
  MCP task;
* nothing else in the session is running or using CPU, and no terminal
  or tile has it open (lifecycle.busy_reason);
* it has been like that for SETTLE_S (by itself only; a restart you ask
  for skips the wait and doesn't mind an open terminal);
* its conversation is known and its transcript is there, so the restart
  resumes that conversation and not the directory's newest;
* for a pane attached to a Claude background session: Claude's own
  record of it shows nothing in flight.

What it can't see: something that lives only inside an idle Claude and
would have woken it later (a scheduled wake-up, a /loop). That is why it
is off by default.

How: an ordinary session is stopped and resumed (lifecycle.py, as an
idle stop and a tap would). A background session is restarted by Claude
itself, ``claude respawn <job>``, and the pane attached again.

One session per pass, and a session is tried once per installed version:
if a restart didn't bring it up to date it isn't restarted again.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time
from typing import Any

from . import attention, config as app_config, lifecycle, proctree, session_meta, tmux

log = logging.getLogger(__name__)

CHECK_S = 60
VERSION_EVERY_S = 600
SETTLE_S = 600
_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")
# Reasons from lifecycle.busy_reason that a restart you asked for ignores.
_MANUAL_OK = ("open in a terminal", "recent output")
_STATUS_WHY = {"busy": "Claude is working", "waiting": "Claude is waiting for an answer",
               "shell": "a background task is running", "mcp": "an MCP task is running"}

_installed: tuple[str | None, float] = (None, -1e9)
_status: dict[str, dict[str, Any]] = {}
_tried: dict[str, str] = {}          # session → installed version it was restarted for


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


async def installed_version(fresh: bool = False) -> str | None:
    """The installed Claude Code version ("2.1.322"), checked at most
    every VERSION_EVERY_S; None when it can't be told."""
    global _installed
    now = time.monotonic()
    if not fresh and now - _installed[1] < VERSION_EVERY_S:
        return _installed[0]
    exe = shutil.which("claude")
    out = await proctree.run_argv([exe, "--version"], 20.0, env=tmux.tmux_env(), check=True) if exe else None
    v = parse_version(out)
    _installed = (".".join(map(str, v)) if v else None, now)
    return _installed[0]


def blocked_reason(s: tmux.TmuxSession, procs: list[tmux.ClaudeProc], panes: int,
                   life: dict[str, Any] | None, meta: dict[str, Any], now: float,
                   manual: bool = False) -> str | None:
    """Why this session can't be restarted right now; None = it can."""
    if len(procs) != 1:
        return "more than one Claude is running in it"
    if s.windows > 1 or panes > 1:
        return "it has more than one pane"
    proc = procs[0]
    if proc.status != "idle":
        return _STATUS_WHY.get(proc.status, f"Claude is {proc.status}")
    if attention.asking(s.name):
        return _STATUS_WHY["waiting"]
    if life is None:
        return "not checked yet"
    busy = life.get("busy")
    if busy and not (manual and busy in _MANUAL_OK):
        return busy
    if not manual and now - life.get("idle_since", now) < SETTLE_S:
        return f"idle for less than {SETTLE_S // 60} minutes"
    if proc.job:
        idle = tmux.job_idle(proc.job)
        if idle is None:
            return "its background session can't be checked"
        if not idle:
            return _STATUS_WHY["shell"]
        return None
    cwd = meta.get("cwd")
    if not (cwd and proc.sid and session_meta.UUID_RE.match(proc.sid)
            and lifecycle._transcript_exists(cwd, proc.sid)):
        return "its conversation can't be resumed exactly"
    return None


async def survey(manual: str | None = None) -> dict[str, dict[str, Any]]:
    """session → {"from", "to", "blocked"} for every session with a Claude
    older than the installed one. ``manual``: judge that session as for a
    restart the user asked for."""
    target = await installed_version()
    want = parse_version(target)
    if want is None:
        return {}
    sessions, complete = await tmux.list_sessions_checked()
    if not complete:
        return {}
    procs, panes = await asyncio.gather(tmux.claude_procs(), tmux.all_panes())
    meta = await asyncio.to_thread(session_meta.load)
    life = lifecycle.status()
    now = time.time()
    out: dict[str, dict[str, Any]] = {}
    for s in sessions:
        mine = procs.get(s.name, [])
        old = [p for p in mine if (parse_version(p.version) or want) < want]
        if not old:
            continue
        out[s.name] = {
            "from": old[0].version, "to": target,
            "blocked": blocked_reason(s, mine, len(panes.get(s.name, [])), life.get(s.name),
                                      meta.get(s.name, {}), now, manual == s.name),
        }
    return out


def status(name: str) -> dict[str, Any] | None:
    """As of the last pass (a minute old at most)."""
    return _status.get(name)


async def restart(name: str, manual: bool = False) -> None:
    """Restart session *name* on the installed version, if that is safe now."""
    global _status
    st = (await survey(name if manual else None)).get(name)
    if st is None:
        raise lifecycle.LifecycleError(409, "it is already on the installed version")
    if st["blocked"]:
        raise lifecycle.LifecycleError(409, f"not now: {st['blocked']}")
    _tried[name] = st["to"]
    log.info("restarting %r for Claude Code %s → %s (%s)", name, st["from"], st["to"],
             "asked" if manual else "idle")
    await lifecycle.restart_for_update(name)
    _status = {k: v for k, v in _status.items() if k != name}


async def _pass() -> None:
    global _status
    _status = await survey()
    for gone in set(_tried) - set(_status):
        _tried.pop(gone, None)
    if not app_config.load().sessions.auto_update:
        return
    for name, st in _status.items():
        if st["blocked"] or _tried.get(name) == st["to"]:
            continue
        try:
            await restart(name)
        except lifecycle.LifecycleError as exc:
            log.warning("could not restart %r for the update: %s", name, exc.detail)
        return                                  # one session per pass


async def run() -> None:
    """Background loop (started by the app lifespan)."""
    await asyncio.sleep(CHECK_S)                # after lifecycle's first tick
    while True:
        try:
            await _pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("update pass failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=CHECK_S)
