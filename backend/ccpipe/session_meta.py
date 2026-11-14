"""ccpipe's record of every session it has seen — running or stopped.

tmux forgets a session the moment it ends (a reboot, an idle stop, `exit`),
and its own times reset when a sticky session is recreated. This record
keeps what the session picker shows and what resuming needs:

    {"<session>": {
        "created": <unix>,   first created (survives recreation)
        "opened":  <unix>,   a main terminal last connected (not a tile)
        "active":  <unix>,   last pane activity seen (minute granularity)
        "cwd":     "/abs",   the session's directory — needed to resume
        "claude":  "<uuid>", the Claude conversation it was on — so a resume
                             picks THAT conversation (`claude --resume`),
                             not just the newest one in the directory
        "job":     "<id>",   it was showing a Claude background session
                             (`claude attach <id>`): resumes by attaching
                             to that again while Claude still knows it
        "shell":   true,     never seen running Claude (a plain shell session,
                             e.g. one started over ssh): resumes as a shell
        "priority": "background",  yields CPU and memory to the other
                             sessions (priority.py); absent = normal
        "stopped": <unix>,   set while it isn't running
        "stop_reason": "idle" | "stopped" | "gone" | "update" (being
                             restarted on a newer Claude Code: claude_update.py)
    }, ...}

Storage: ``$XDG_STATE_HOME/ccpipe/session_meta.json`` (override with
``CCPIPE_SESSION_META_FILE``), 0600, atomic replace — as sticky.py.

An entry with no cwd can't be resumed, so once its session is gone it is
dropped (unless sticky — sticky.py has the cwd). "Forget" removes an entry
outright. At most MAX_STOPPED stopped entries are kept (oldest dropped).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import statefile
from .paths import state_dir

log = logging.getLogger(__name__)

META_FILE_ENV = "CCPIPE_SESSION_META_FILE"
OPENED_WRITE_INTERVAL_S = 60
ACTIVE_WRITE_STEP_S = 60
MAX_STOPPED = 100
# A Claude Code conversation id (the transcript's file name). The one copy:
# lifecycle.py and routes/sessions.py validate ids with it too.
JOB_RE = re.compile(r"^[0-9a-f]{4,36}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_REASONS = ("idle", "stopped", "gone", "update")

_lock = threading.Lock()
_opened_written: dict[str, float] = {}

Meta = dict[str, dict[str, Any]]


def path() -> Path:
    override = os.environ.get(META_FILE_ENV)
    return Path(override) if override else state_dir() / "session_meta.json"


def _clean(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        return {}
    out: dict[str, Any] = {}
    for k in ("created", "opened", "active", "stopped"):
        v = info.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[k] = int(v)
    cwd = info.get("cwd")
    if isinstance(cwd, str) and cwd.startswith("/") and "\0" not in cwd:
        out["cwd"] = cwd
    cid = info.get("claude")
    if isinstance(cid, str) and UUID_RE.match(cid):
        out["claude"] = cid
    elif info.get("shell") is True:
        out["shell"] = True
    job = info.get("job")
    if isinstance(job, str) and JOB_RE.match(job) and "claude" in out:
        out["job"] = job
    if info.get("priority") == "background":
        out["priority"] = "background"
    reason = info.get("stop_reason")
    if reason in _REASONS and "stopped" in out:
        out["stop_reason"] = reason
    return out


def load() -> Meta:
    # A failed read blocks _save until one works (statefile.py), so a
    # passing error can't replace every remembered session with one entry.
    data = statefile.read_json(path())
    if not isinstance(data, dict):
        return {}
    out: Meta = {}
    for name, info in data.items():
        entry = _clean(info)
        if isinstance(name, str) and entry:
            out[name] = entry
    return out


def _save(data: Meta) -> None:
    statefile.write(path(), json.dumps(data, indent=2, sort_keys=True).encode() + b"\n")


def _try_save(data: Meta) -> None:
    try:
        _save(data)
    except OSError as exc:
        log.warning("could not save session meta: %s", exc)


def note_created(name: str, when: int | None = None, cwd: str | None = None) -> None:
    """A brand-new session: start its record afresh."""
    with _lock:
        data = load()
        entry: dict[str, Any] = {"created": int(when or time.time())}
        if cwd:
            entry["cwd"] = cwd
        data[name] = _clean(entry)
        _opened_written.pop(name, None)
        _save(data)


def note_opened(name: str, when: float | None = None) -> None:
    now = when or time.time()
    last = _opened_written.get(name)
    if last is not None and now - last < OPENED_WRITE_INTERVAL_S:
        return
    with _lock:
        _opened_written[name] = now
        data = load()
        data.setdefault(name, {})["opened"] = int(now)
        _try_save(data)


def note_details(name: str, cwd: str | None, claude: str | None,
                 has_claude: bool | None = None, job: str | None = None) -> None:
    """Record a running session's directory and Claude conversation.

    ``has_claude`` is whether a Claude process is in the session right now
    (None = not checked). A session never seen with Claude is marked
    ``shell`` so it resumes as a shell, not as `claude --continue`; once a
    conversation has been seen it stays a Claude session (Claude may just
    have exited to the shell for a while). ``job``: the Claude seen is a
    background session the pane is attached to; an ordinary Claude seen
    later clears it."""
    with _lock:
        data = load()
        entry = data.setdefault(name, {})
        new = dict(entry)
        if cwd and cwd.startswith("/"):
            new["cwd"] = cwd
        if claude and UUID_RE.match(claude):
            new["claude"] = claude
            new.pop("shell", None)
            if job and JOB_RE.match(job):
                new["job"] = job
            else:
                new.pop("job", None)
        elif has_claude:
            new.pop("shell", None)
        elif has_claude is False and "claude" not in new:
            new["shell"] = True
        if new != entry:
            data[name] = new
            _try_save(data)


def mark_stopped(name: str, reason: str, when: float | None = None) -> None:
    with _lock:
        data = load()
        entry = data.setdefault(name, {})
        entry["stopped"] = int(when or time.time())
        entry["stop_reason"] = reason if reason in _REASONS else "stopped"
        _try_save(data)


def mark_running(name: str) -> None:
    with _lock:
        data = load()
        entry = data.get(name)
        if entry and ("stopped" in entry or "stop_reason" in entry):
            entry.pop("stopped", None)
            entry.pop("stop_reason", None)
            _try_save(data)


def set_priority(name: str, level: str) -> None:
    """"background" or "normal" (priority.py). Raises OSError when it
    can't be saved: the caller is answering a request to change it."""
    with _lock:
        data = load()
        entry = data.setdefault(name, {})
        if level == "background":
            entry["priority"] = "background"
        else:
            entry.pop("priority", None)
        _save(data)


def rename(old: str, new: str) -> None:
    with _lock:
        data = load()
        if old in data:
            data[new] = data.pop(old)
            _save(data)
        if old in _opened_written:
            _opened_written[new] = _opened_written.pop(old)


def forget(name: str) -> None:
    with _lock:
        data = load()
        if data.pop(name, None) is not None:
            _save(data)
        _opened_written.pop(name, None)


def last_used(entry: dict[str, Any]) -> int:
    """When the session was last in use — its recency for boot ordering."""
    return max(entry.get("opened", 0), entry.get("active", 0), entry.get("created", 0))


def reconcile(live: dict[str, tuple[int, int]], keep: set[str],
              now: float | None = None) -> Meta:
    """Merge with the live sessions (name → (tmux created, tmux activity)).

    Running: backfill / keep the older ``created``, advance ``active``, and
    clear any stopped mark. Not running: mark it stopped ("gone", if nothing
    recorded why) — or drop it when it can't be resumed (no cwd, not in
    ``keep``, the sticky names). Writes only when something changed."""
    now = int(now or time.time())
    with _lock:
        data = load()
        before = json.dumps(data, sort_keys=True)
        for name, (tmux_created, activity) in live.items():
            entry = data.setdefault(name, {})
            if tmux_created > 0 and (entry.get("created") is None or tmux_created < entry["created"]):
                entry["created"] = tmux_created
            if activity - entry.get("active", 0) >= ACTIVE_WRITE_STEP_S:
                entry["active"] = activity
            entry.pop("stopped", None)
            entry.pop("stop_reason", None)
        for name in [n for n in data if n not in live]:
            entry = data[name]
            if not entry.get("cwd") and name not in keep:
                del data[name]
                continue
            if "stopped" not in entry:
                entry["stopped"] = now
                entry["stop_reason"] = "gone"
        stopped = sorted((n for n in data if n not in live and n not in keep),
                         key=lambda n: data[n].get("stopped", 0), reverse=True)
        for name in stopped[MAX_STOPPED:]:
            del data[name]
        if json.dumps(data, sort_keys=True) != before:
            _try_save(data)
        return data
