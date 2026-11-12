"""What needs you, in every session: Claude asking you something, and Claude
having finished a turn that nobody was looking at.

Two levels:

  ask   Claude is waiting on you: a question (its AskUserQuestion dialog) or
        a permission prompt. Urgent. It lasts while Claude waits.
  done  Claude finished its turn (it was working, and now it's idle) while
        no page was showing the session. Quiet. It lasts until a page shows
        the session, Claude starts working again, or you clear it.

One watcher for the whole server (``run``, started by the app lifespan).
Every POLL_S it reads Claude Code's session files,
``~/.claude/sessions/<pid>.json``: their ``status`` is "busy", "shell" (idle
with a background shell task), "idle" or "waiting". An MCP tool Claude has
moved to the background doesn't show there: Claude says "idle" while it
runs (2.1.319, 2026-11-07: a 13-minute kicad autoroute in chirper), and
only its footer says "1 MCP task". So an idle Claude's footer is read now
and then, and ``claude_states`` reports it as "mcp". The question itself is read
off Claude's pane when the wait starts (claude_asks.py). It costs one small
file read per Claude each second, and a screen capture only while one waits.

Listeners (``subscribe``) get each change: ws.py (the session's own terminal
speaks the question; every other open page is told), notify.py (Web Push).
/api/attention and /api/sessions list ``items()``. lifecycle.py never stops
a session that is asking.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import claude_asks, tmux
from .paths import claude_project_dir
from .tmux_control import control_client

log = logging.getLogger(__name__)

POLL_S = 1.0
# While Claude waits, how often the screen is read again for a new dialog
# (two questions asked back to back keep it "waiting" throughout).
REREAD_S = claude_asks.REREAD_S
PANES_EVERY_S = 10.0      # how often the pane → session map is re-read
PANES_MISS_S = 2.0        # at most this often when a pane isn't in it
# Claude has to stay idle this long before its turn counts as finished.
SETTLE_S = 3.0
# A question whose dialog can't be read yet is announced once its text is
# in, or after this long without it.
PENDING_S = 4.0
# A Claude missing from one look (a file caught mid-write, tmux slow to
# answer) isn't gone: only after this many looks in a row.
GONE_AFTER = 3
MAX_BODY = 600
_TAIL_BYTES = 256 * 1024  # how much of a transcript is read for the last reply

# Several Claudes in one session (split panes): the one that needs you most.
_RANK = tmux._STATUS_RANK
# An idle Claude's footer is read this often, and more often while it shows
# a background MCP task (so the list shows it end soon after it does).
MCP_EVERY_S = 30.0
MCP_WHILE_S = 5.0
_MCP_RE = re.compile(r"\b(\d+) MCP tasks?\b")


@dataclass
class Item:
    id: int
    session: str
    level: str          # "ask" | "done"
    kind: str           # "question" | "permission" | "done"
    title: str
    body: str
    since: float        # wall clock, when it started
    key: str = field(default="", compare=False)
    spoken: str = field(default="", compare=False)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "session": self.session, "level": self.level,
                "kind": self.kind, "title": self.title, "body": self.body,
                "since": int(self.since)}


# Events a listener gets:
#   "new"     an item to tell you about (a new question, a finished turn)
#   "quiet"   an item that was already there (ccpipe just started): listed,
#             not announced
#   "update"  the same item, now with its text (it couldn't be read before)
#   "end"     it's gone (answered, seen, cleared, the session ended)
Listener = Callable[[str, Item], None]

_items: dict[tuple[str, str], Item] = {}
_listeners: list[Listener] = []
_seq = itertools.count(1)


def _nobody_watching(_session: str) -> bool:
    return False


# Is a page showing this session right now? Set by ws.py.
_watched: Callable[[str], bool] = _nobody_watching


def set_watched(fn: Callable[[str], bool]) -> None:
    global _watched
    _watched = fn


def subscribe(fn: Listener) -> Callable[[], None]:
    _listeners.append(fn)

    def cancel() -> None:
        if fn in _listeners:
            _listeners.remove(fn)
    return cancel


def items() -> list[Item]:
    """Everything that needs you now: asks first, then the oldest first."""
    return sorted(_items.values(), key=lambda i: (i.level != "ask", i.since))


def get(session: str, level: str) -> Item | None:
    return _items.get((session, level))


def asking(session: str) -> bool:
    return (session, "ask") in _items


def announcement(item: Item) -> str:
    """What's said after the chime when a session needs you (POST
    /api/attention/say, routes/notify.py)."""
    name = re.sub(r"[-_]+", " ", item.session)
    if item.level == "done":
        return f"{name} has finished."
    if item.kind == "permission":
        return f"{name} needs your permission."
    return f"{name} has a question."


# (device, item id) → when it was said: several pages on one device each
# ask, and it's said once.
_said: dict[tuple[str, int], float] = {}


def first_say(device: str, item_id: int) -> bool:
    """Whether this device has yet to hear the item (and now it has). A page
    with no device id always may."""
    now = time.monotonic()
    for k, t in list(_said.items()):
        if now - t > 600:
            del _said[k]
    if not device:
        return True
    key = (device, item_id)
    if key in _said:
        return False
    _said[key] = now
    return True


def _emit(event: str, item: Item) -> None:
    for fn in list(_listeners):
        try:
            fn(event, item)
        except Exception:
            log.exception("attention listener failed")


def _put(item: Item, event: str) -> None:
    _items[(item.session, item.level)] = item
    _emit(event, item)


def _drop(session: str, level: str) -> None:
    item = _items.pop((session, level), None)
    if item is not None:
        _emit("end", item)


def seen(session: str) -> None:
    """A page is showing the session: its finished turn has been seen."""
    _drop(session, "done")


def dismiss(session: str | None = None) -> None:
    """Clear finished turns: one session's, or all of them."""
    for s, level in list(_items):
        if level == "done" and (session is None or s == session):
            _drop(s, "done")


def _clip(text: str, n: int = MAX_BODY) -> str:
    return claude_asks._clip(text, n)


def _ask_item(session: str, ask: claude_asks.Ask) -> Item:
    return Item(next(_seq), session, "ask", ask.kind, ask.title, _clip(ask.body),
                time.time(), key=ask.key, spoken=ask.spoken)


def _generic(waiting_for: Any) -> claude_asks.Ask:
    """Claude waits, but the dialog couldn't be read off the screen."""
    return claude_asks.describe([], waiting_for if isinstance(waiting_for, str) else None)


# ─── The last reply, for a finished turn ──────────────────────────────────

def last_reply(state: dict[str, Any]) -> str:
    """The end of Claude's last reply in this conversation, as plain text,
    or "" if it can't be found. Public for tests."""
    from .tts import _extract_assistant_text, prepare_for_tts

    sid, cwd = state.get("sessionId"), state.get("cwd")
    if not (isinstance(sid, str) and isinstance(cwd, str)
            and re.fullmatch(r"[0-9a-fA-F-]{36}", sid)):
        return ""
    path = claude_project_dir(cwd) / f"{sid}.jsonl"
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read()
    except OSError:
        return ""
    for raw in reversed(tail.split(b"\n")):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        text = _extract_assistant_text(rec)
        if text:
            return _clip(prepare_for_tts(text, "full"), 300)
        if rec.get("type") == "user" and not rec.get("isSidechain") \
                and not rec.get("toolUseResult"):
            return ""        # your prompt came after Claude's last reply
    return ""


# ─── Reading Claude's session files ───────────────────────────────────────

_pane_map: dict[str, tuple[str, int]] = {}   # tmux pane id → (session name, pane pid)
_panes_at = float("-inf")
_file_cache: dict[Path, tuple[int, int, dict[str, Any]]] = {}
# Claudes (pid, start) that aren't in one of this tmux server's panes: in
# another tmux server (pane ids are only unique within one), or none.
_foreign: set[tuple[int, str]] = set()
# The latest look, for /api/sessions and the idle check (claude_states).
_snapshot: dict[str, tuple[str, int]] = {}
_snapshot_at = float("-inf")


async def _refresh_panes() -> None:
    global _panes_at
    _panes_at = time.monotonic()
    sep = tmux._FIELD_SEP
    out = await tmux._tmux_out("list-panes", "-a", "-F",
                               f"#{{pane_id}}{sep}#{{session_name}}{sep}#{{pane_pid}}")
    if out is None:
        return              # tmux didn't answer: keep the map we have
    panes: dict[str, tuple[str, int]] = {}
    for line in out.splitlines():
        parts = line.split(sep)
        if len(parts) != 3 or not parts[0] or not parts[1] or parts[1] == tmux.CONTROL_SESSION_NAME:
            continue
        try:
            panes[parts[0]] = (parts[1], int(parts[2]))
        except ValueError:
            continue
    _pane_map.clear()
    _pane_map.update(panes)


def _parent(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[-1].split()
        return int(fields[1])
    except (OSError, ValueError, IndexError):
        return None


def _descends(pid: int, ancestor: int) -> bool:
    """``pid`` is ``ancestor`` or runs under it."""
    for _ in range(32):
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
        nxt = _parent(pid)
        if nxt is None:
            return False
        pid = nxt
    return False


def _read_files() -> list[dict[str, Any]]:
    """Every live Claude's session file (a stale one, for a process that's
    gone or a pid that was reused, is skipped)."""
    d = Path.home() / ".claude" / "sessions"
    try:
        paths = list(d.glob("*.json"))
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    live: set[Path] = set()
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        live.add(p)
        hit = _file_cache.get(p)
        # A file changed in the last second is read again whatever its
        # stamp says: file times are coarse (a few ms), and "busy" → "idle"
        # is the same size.
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size \
                and time.time() - st.st_mtime > 1.0:
            data = hit[2]
        else:
            try:
                data = json.loads(p.read_text())
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                _file_cache[p] = (st.st_mtime_ns, st.st_size, data)
            except (OSError, ValueError):
                # Caught mid-write: what it said last time still stands.
                if hit is None:
                    continue
                data = hit[2]
        pid = data.get("pid")
        if not isinstance(pid, int) or not isinstance(data.get("status"), str):
            continue
        start = tmux._proc_start_time(pid)
        if start is None or str(data.get("procStart", "")) != start:
            continue
        out.append(data)
    for p in list(_file_cache):
        if p not in live:
            del _file_cache[p]
    return out


def _place(data: dict[str, Any]) -> str | None:
    """The session of this Claude, or None when its pane isn't known yet."""
    where = data.get("tmux")
    entry = _pane_map.get(where.rpartition(".")[2]) if isinstance(where, str) else None
    return entry[0] if entry else None


async def _claudes() -> dict[str, dict[str, Any]]:
    """session name → the session file of its Claude (the one that needs
    you most, when a session has several)."""
    files = await asyncio.to_thread(_read_files)
    if time.monotonic() - _panes_at >= PANES_EVERY_S:
        await _refresh_panes()
    ours: list[tuple[str, dict[str, Any]]] = []
    unplaced: list[dict[str, Any]] = []
    for data in files:
        key = (data["pid"], str(data.get("procStart", "")))
        if key in _foreign:
            continue
        name = _place(data)
        if name is None:
            unplaced.append(data)
        else:
            ours.append((name, data))
    # A Claude in a pane we don't know yet (a new session or split): read
    # the panes again, now rather than in ten seconds. Still not there: it
    # isn't in this tmux server.
    if unplaced and time.monotonic() - _panes_at >= PANES_MISS_S:
        await _refresh_panes()
        for data in unplaced:
            name = _place(data)
            if name is None:
                _foreign.add((data["pid"], str(data.get("procStart", ""))))
            else:
                ours.append((name, data))
    out: dict[str, dict[str, Any]] = {}
    for name, data in ours:
        # The same pane id in another tmux server: this Claude must run
        # under the pane's own process to be this session's.
        where = data.get("tmux", "")
        pane_pid = _pane_map.get(where.rpartition(".")[2], ("", 0))[1]
        if not _descends(data["pid"], pane_pid):
            _foreign.add((data["pid"], str(data.get("procStart", ""))))
            continue
        prev = out.get(name)
        if prev is None or _RANK.get(data["status"], 0) > _RANK.get(prev["status"], 0):
            out[name] = data
    live = {(d["pid"], str(d.get("procStart", ""))) for d in files}
    _foreign.intersection_update(live)
    return out


def _take_snapshot(claudes: dict[str, dict[str, Any]]) -> None:
    global _snapshot_at
    _snapshot.clear()
    for name, data in claudes.items():
        since = data.get("statusUpdatedAt")
        status = data["status"]
        tr = _tracks.get(name)
        if status == "idle" and tr is not None and tr.mcp:
            status = "mcp"
        _snapshot[name] = (status, int(since / 1000) if isinstance(since, (int, float)) else 0)
    _snapshot_at = time.monotonic()


async def _capture(target: str) -> list[str] | None:
    """A pane's screen, over the control connection (no tmux process to
    start), or tmux itself when that isn't up."""
    lines = await control_client.query(f"capture-pane -p -t {target}")
    if lines is None:
        out = await tmux._tmux_out("capture-pane", "-p", "-t", target)
        lines = None if out is None else out.split("\n")
    return lines


def footer_mcp_tasks(lines: list[str]) -> int:
    """How many background MCP tasks Claude's footer counts ("⏵⏵ auto mode
    on · 1 MCP task"): the lines under the prompt box, so the "1 MCP task
    still running" of a finished turn above it doesn't count. Public for
    tests."""
    lines = [ln.rstrip() for ln in lines]
    while lines and not lines[-1]:
        lines.pop()
    rule = max((i for i, ln in enumerate(lines) if claude_asks._SOLID_RULE_RE.match(ln)), default=-1)
    footer = lines[rule + 1:] if rule >= 0 else lines[-3:]
    m = _MCP_RE.search("\n".join(footer))
    return int(m.group(1)) if m else 0


async def _check_mcp(name: str, tr: "_Track", state: dict[str, Any]) -> None:
    now = time.monotonic()
    stamp = state.get("statusUpdatedAt")
    if stamp != tr.mcp_stamp:
        # Just went idle: look once the footer has had a moment to draw.
        if isinstance(stamp, (int, float)) and time.time() * 1000 - stamp < 1000:
            return
    elif now - tr.mcp_at < (MCP_WHILE_S if tr.mcp else MCP_EVERY_S):
        return
    tr.mcp_at, tr.mcp_stamp = now, stamp
    lines = await _capture(await claude_asks._pane_of(name, state))
    if lines is not None:
        tr.mcp = footer_mcp_tasks(lines)


async def claude_states() -> dict[str, tuple[str, int]]:
    """session name → (Claude's status, since when): the watcher's last
    look, a second old at most; read afresh when it isn't running. "mcp":
    idle, with a background MCP task running."""
    if time.monotonic() - _snapshot_at < 3 * POLL_S:
        return dict(_snapshot)
    return await tmux.claude_states()


# ─── The watcher ──────────────────────────────────────────────────────────

@dataclass
class _Track:
    status: str | None = None
    stamp: Any = None
    first: bool = True
    ask_key: str | None = None
    read_at: float = float("-inf")
    busy_since: float | None = None     # wall clock
    idle_at: float | None = None        # monotonic
    missing: int = 0                    # looks in a row without its Claude
    mcp: int = 0                        # background MCP tasks its footer shows
    mcp_at: float = float("-inf")       # monotonic
    mcp_stamp: Any = None               # the status stamp it was read for
    # A new question not read off the screen yet (announced once it is).
    pending: claude_asks.Ask | None = None
    pending_since: float | None = None  # monotonic


_tracks: dict[str, _Track] = {}


async def _step(name: str, tr: _Track, state: dict[str, Any] | None, quiet: bool) -> None:
    status = state.get("status") if state else None
    stamp = state.get("statusUpdatedAt") if state else None
    waiting = status == "waiting"
    first = tr.first
    # A new wait: from not waiting, or a fresh status stamp (a second prompt
    # right after the first, between two looks).
    fresh = waiting and not first and (tr.status != "waiting" or stamp != tr.stamp)
    tr.first = False
    tr.status, tr.stamp = status, stamp

    # ── asking ──
    if not waiting:
        tr.ask_key = None
        tr.pending = tr.pending_since = None
        _drop(name, "ask")
    elif state is not None:
        pid = state["pid"]
        ask: claude_asks.Ask | None = None
        if fresh:
            # None: answered before the screen could be read.
            ask = await claude_asks._read_dialog(name, pid, state)
            tr.read_at = time.monotonic()
        elif first or time.monotonic() - tr.read_at >= REREAD_S:
            ask = await claude_asks._read_now(name, state)
            tr.read_at = time.monotonic()
            if ask is None and get(name, "ask") is None:
                ask = _generic(state.get("waitingFor"))
        if ask is not None:
            cur = get(name, "ask")
            if cur is not None and not cur.body and ask.body and ask.kind == cur.kind:
                # The same wait, read properly at last: not a second alert.
                cur.title, cur.body, cur.key, cur.spoken = ask.title, _clip(ask.body), ask.key, ask.spoken
                tr.ask_key = ask.key
                _emit("update", cur)
            elif cur is None or ask.key != tr.ask_key:
                if not ask.body and not (first and quiet):
                    # Not on screen yet: say it once there's something to say.
                    tr.pending = ask
                    if tr.pending_since is None:
                        tr.pending_since = time.monotonic()
                else:
                    tr.ask_key = ask.key
                    tr.pending = tr.pending_since = None
                    item = _ask_item(name, ask)
                    log.info("attention: %s asks: %s", name, item.title)
                    _put(item, "quiet" if first and quiet else "new")
        if tr.pending is not None and tr.pending_since is not None \
                and time.monotonic() - tr.pending_since >= PENDING_S:
            # Still unreadable: announce it without the words.
            ask, tr.pending, tr.pending_since = tr.pending, None, None
            tr.ask_key = ask.key
            _put(_ask_item(name, ask), "new")

    # ── a background MCP task (only the footer says) ──
    if status == "idle" and state is not None:
        await _check_mcp(name, tr, state)
    else:
        tr.mcp, tr.mcp_stamp = 0, None

    # ── finished ──
    if status in ("busy", "waiting"):
        tr.idle_at = None
        if tr.busy_since is None:
            tr.busy_since = time.time()
        _drop(name, "done")
    elif status in ("idle", "shell") and tr.busy_since is not None:
        now = time.monotonic()
        if tr.idle_at is None:
            tr.idle_at = now
        elif now - tr.idle_at >= SETTLE_S:
            tr.busy_since = tr.idle_at = None
            if not _watched(name) and state is not None:
                body = await asyncio.to_thread(last_reply, state)
                item = Item(next(_seq), name, "done", "done", "Claude finished",
                            body, time.time(), key="done")
                log.info("attention: %s finished", name)
                _put(item, "new")


async def tick(quiet: bool = False) -> None:
    claudes = await _claudes()
    for name in set(_tracks) - set(claudes):
        tr = _tracks[name]
        tr.missing += 1
        if tr.missing < GONE_AFTER:
            continue
        # No Claude in it any more (it exited, the session ended).
        _tracks.pop(name, None)
        _drop(name, "ask")
        _drop(name, "done")
    for name in claudes:
        if name in _tracks:
            _tracks[name].missing = 0
    steps = [_step(n, _tracks.setdefault(n, _Track()), claudes[n], quiet) for n in claudes]
    results = await asyncio.gather(*steps, return_exceptions=True)
    for n, r in zip(claudes, results):
        if isinstance(r, Exception):
            _warn_step(n, r)
    _take_snapshot(claudes)


# A session whose check keeps failing is said so once a minute, not hidden
# at debug level and not once a second.
_step_warned: dict[str, float] = {}


def _warn_step(name: str, exc: BaseException) -> None:
    now = time.monotonic()
    if now - _step_warned.get(name, -1e9) < 60.0:
        return
    if len(_step_warned) > 256:
        _step_warned.clear()
    _step_warned[name] = now
    log.warning("attention: checking %s failed: %r", name, exc)


async def run() -> None:
    """Background loop (started by the app lifespan). What's waiting when it
    starts is listed but not announced: a restart of ccpipe mustn't ring
    every device again."""
    quiet = True
    failures = 0
    while True:
        try:
            await tick(quiet)
            quiet = False
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            # The first failure in full; while it keeps failing, wait
            # longer (up to a minute) and say so now and then, instead of
            # a traceback every second.
            if failures == 1:
                log.exception("attention tick failed")
            elif failures % 20 == 0:
                log.error("attention tick still failing (%d in a row)", failures)
        await asyncio.sleep(min(60.0, POLL_S * (2 ** min(failures, 6))) if failures else POLL_S)


def _reset_for_tests() -> None:
    """Forget every session (listeners stay: ws.py's is set at import)."""
    global _panes_at, _snapshot_at
    _items.clear()
    _tracks.clear()
    _pane_map.clear()
    _file_cache.clear()
    _foreign.clear()
    _snapshot.clear()
    _said.clear()
    _panes_at = _snapshot_at = float("-inf")
