"""tmux session management + Claude Code resume/export endpoints.

These are co-located because they share a half-dozen helpers
(_UUID_RE, _projects_subdir_for_cwd, _running_claude_session_ids,
JSONL streaming) and the URLs are conceptually related (one names
*live* tmux sessions, the other names *historic* claude JSONL
transcripts that can be resumed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import socket
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import config as app_config
from .. import attention, drafts, lifecycle, plan_usage, session_meta, sticky, sysstats, tmux, tmux_ui
from . import layouts
from ..auth import AuthDep, CsrfDep, SameOriginDep
from ..paths import claude_project_dir
from ..tmux_control import CONTROL_SESSION_NAME
from .fs import _enforce_fs_jail, content_disposition_attachment

log = logging.getLogger(__name__)
router = APIRouter()


def _reject_control_session(name: str) -> None:
    """Refuse user-facing CRUD on the hidden tmux session that backs
    control-mode notifications. Without this guard, a misbehaving client
    could rename / delete / attach to it and break the entire session
    list. 404 (not 400) keeps the response shape identical to a
    legitimate missing-session response."""
    if name == CONTROL_SESSION_NAME:
        raise HTTPException(status_code=404, detail="session not found")


# Used to validate resumeSessionId and to confirm a /api/claude-sessions
# JSONL filename matches the claude session UUID format.
_UUID_RE = session_meta.UUID_RE


# ─── Pydantic bodies ───────────────────────────────────────────────────────

class SessionInfo(BaseModel):
    name: str
    windows: int
    attached: bool
    created: int
    # True when the session is in the persisted sticky map — i.e. it
    # will be auto-recreated by the lifespan hook after a backend
    # restart. Surfaced so the UI can render a pin indicator and flip
    # the kebab menu label between "make sticky" / "make ephemeral".
    sticky: bool = False
    # Unix timestamp of the most recent pane output in this session
    # (tmux's #{session_activity}). Drives the picker's "last use"
    # ordering so a session that's actively producing output bubbles
    # to the top of its sticky / non-sticky group.
    activity: int = 0
    # When ccpipe first created the session (survives the reboot recreation
    # of a sticky session, unlike tmux's own created time — see
    # session_meta.py) and when a terminal last opened it (0 = not known).
    opened: int = 0
    # Terminals / tiles attached right now.
    clients: int = 0
    # The active pane's working directory and title (Claude Code sets the
    # title to its current task, with a spinner glyph while it works).
    cwd: str | None = None
    title: str | None = None
    # Resource use (sysstats.py): CPU as % of one core like top (200 = two
    # cores busy, averaged over the last ~15 s), memory as
    # bytes (PSS of the session's processes), disk as bytes used by its
    # directory. None until measured.
    cpu: float | None = None
    mem: int | None = None
    disk: int | None = None
    # Idle tracking (lifecycle.py): why it counts as in use, when it went
    # idle, and when it will be stopped if it stays idle (None = it won't).
    busy: str | None = None
    idle_since: int | None = None
    stops_at: int | None = None
    # Claude Code's own status: "busy" (working on a turn), "waiting" (a
    # question or permission prompt), "mcp" (idle with a background MCP
    # task, from its footer: attention.py), "shell" (idle
    # with a background shell task) or "idle"; None = no Claude running.
    claude: str | None = None
    claude_since: int | None = None
    # What needs you here (attention.py): Claude's question, or its
    # finished turn that no one has looked at. None = nothing.
    attention: dict[str, Any] | None = None


class StoppedSessionInfo(BaseModel):
    """A remembered session that isn't running; opening it resumes it."""
    name: str
    sticky: bool = False
    created: int = 0
    opened: int = 0
    stopped: int = 0
    # "idle" (auto-stopped), "stopped" (by the user) or "gone" (rebooted,
    # exited, not started at boot).
    stop_reason: str = "gone"
    cwd: str
    # The Claude conversation it resumes, when known.
    conversation: bool = False
    # Never seen running Claude: it resumes as a plain shell.
    shell: bool = False
    disk: int | None = None


class SessionsConfigBody(BaseModel):
    boot_sticky: int | None = Field(default=None, ge=0, le=app_config.BOOT_MAX)
    boot_other: int | None = Field(default=None, ge=0, le=app_config.BOOT_MAX)
    # 0 = never; otherwise 15 min .. 30 days (clamped).
    idle_stop_sticky_min: int | None = Field(default=None, ge=0, le=app_config.IDLE_MAX_MIN)
    idle_stop_other_min: int | None = Field(default=None, ge=0, le=app_config.IDLE_MAX_MIN)


class CreateSessionBody(BaseModel):
    name: str
    # Optional initial working directory for the new tmux session.
    cwd: str | None = None
    # Optional Claude sessionId (UUID) to resume.
    resumeSessionId: str | None = None


class RenameSessionBody(BaseModel):
    newName: str


class StickyBody(BaseModel):
    sticky: bool


_HOSTNAME = socket.gethostname()


def _attention_of(name: str) -> dict[str, Any] | None:
    item = attention.get(name, "ask") or attention.get(name, "done")
    return item.public() if item else None


def _to_session_info(s: tmux.TmuxSession, sticky_names: set[str],
                     meta: session_meta.Meta | None = None,
                     panes: dict[str, tuple[str, str]] | None = None,
                     claude: dict[str, tuple[str, int]] | None = None) -> SessionInfo:
    m = (meta or {}).get(s.name, {})
    cwd, title = (panes or {}).get(s.name, (None, None))
    if title in (_HOSTNAME, _HOSTNAME.split(".")[0]):
        title = None                  # tmux's default title: says nothing
    use = sysstats.session_stats().get(s.name, {})
    cstate, csince = (claude or {}).get(s.name, (None, None))
    idle = lifecycle.status().get(s.name, {})
    return SessionInfo(
        name=s.name,
        windows=s.windows,
        attached=s.attached,
        created=m.get("created") or s.created,
        sticky=s.name in sticky_names,
        activity=s.activity,
        opened=m.get("opened") or s.last_attached,
        clients=s.clients,
        cwd=cwd or None,
        title=title or None,
        cpu=use.get("cpu"),
        mem=use.get("mem"),
        disk=sysstats.project_size(cwd),
        busy=idle.get("busy"),
        idle_since=idle.get("idle_since"),
        stops_at=idle.get("stops_at"),
        claude=cstate,
        claude_since=csince or None,
        attention=_attention_of(s.name),
    )


async def _session_infos() -> list[SessionInfo]:
    sysstats.touch()
    (sessions, complete), panes, claude = await asyncio.gather(
        tmux.list_sessions_checked(), tmux.active_panes(), attention.claude_states())
    sticky_names = sticky.sticky_names()
    if complete:
        meta = await asyncio.to_thread(
            session_meta.reconcile, {s.name: (s.created, s.activity) for s in sessions}, sticky_names)
    else:
        # tmux didn't answer: an empty list here would mark every session
        # stopped ("gone") and drop the ones that can't be resumed.
        meta = session_meta.load()
    return [_to_session_info(s, sticky_names, meta, panes, claude) for s in sessions]


async def _session_info(name: str) -> SessionInfo | None:
    return next((i for i in await _session_infos() if i.name == name), None)


# ─── tmux session CRUD ────────────────────────────────────────────────────

@router.get("/api/sessions", response_model=list[SessionInfo], dependencies=[AuthDep, SameOriginDep])
async def list_sessions() -> list[SessionInfo]:
    return await _session_infos()


_PREVIEW_MAX_SESSIONS = 64
_PREVIEW_MAX_LINES = 60
_preview_slots = asyncio.Semaphore(8)


@router.get("/api/sessions/previews", dependencies=[AuthDep, SameOriginDep])
async def session_previews(names: str = "", lines: int = 16) -> dict[str, Any]:
    """The bottom of each named session's screen as plain text, for the
    picker's grid view: ``{"previews": {name: [line, ...]}}``. Names are
    comma-separated; unknown or invalid ones are left out."""
    lines = max(1, min(lines, _PREVIEW_MAX_LINES))
    wanted: list[str] = []
    for raw in names.split(",")[:_PREVIEW_MAX_SESSIONS]:
        try:
            n = tmux.safe_name(raw.strip())
        except ValueError:
            continue
        if n != CONTROL_SESSION_NAME and n not in wanted:
            wanted.append(n)
    live = {s.name for s in await tmux.list_sessions()}

    async def one(n: str) -> tuple[str, list[str] | None]:
        async with _preview_slots:
            screen = await tmux.capture_screen(n)
        return n, screen[-lines:] if screen is not None else None

    results = await asyncio.gather(*(one(n) for n in wanted if n in live))
    return {"previews": {n: p for n, p in results if p is not None}}


@router.post("/api/sessions", response_model=SessionInfo,
              dependencies=[AuthDep, CsrfDep])
async def create_session(body: CreateSessionBody) -> SessionInfo:
    try:
        name = tmux.safe_name(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    if await tmux.session_exists(name):
        raise HTTPException(status_code=409, detail="session already exists")
    if await tmux.at_session_cap():
        raise HTTPException(status_code=429,
                            detail=f"session limit reached ({tmux.MAX_SESSIONS})")

    cwd: str | None = None
    if body.cwd:
        if not body.cwd.startswith("/"):
            raise HTTPException(status_code=400, detail="cwd must be absolute")
        try:
            resolved = Path(body.cwd).resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(status_code=400, detail="cwd does not exist")
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail="cwd is not a directory")
        # The cwd is operator-supplied and used to spawn claude — without
        # the jail check we'd happily start claude in /etc, /var/log,
        # anywhere the uvicorn UID can chdir. Claude then indexes that
        # tree under ~/.claude/projects/<encoded>, exposing contents to
        # subsequent /api/claude-sessions calls. Jail the cwd to the
        # /api/fs root so this surface matches the rest of the app.
        _enforce_fs_jail(resolved)
        cwd = str(resolved)

    if body.resumeSessionId:
        if not _UUID_RE.match(body.resumeSessionId):
            raise HTTPException(status_code=400, detail="invalid resumeSessionId")
        # shlex.quote is belt-and-braces: the UUID regex already bounds
        # the value to [0-9a-fA-F-], but libtmux passes window_command
        # to a shell and we want zero ambiguity.
        claude_cmd = plan_usage.claude_command(f"--resume {shlex.quote(body.resumeSessionId)}")
    else:
        claude_cmd = plan_usage.claude_command()

    # Wrap in $SHELL -i so the tmux session survives claude exit and
    # drops to a prompt in the original cwd. Without this the only-pane
    # closes when claude exits → session is destroyed → reconnect path
    # auto-creates a fresh session in $HOME, losing the cwd.
    command = tmux.wrap_in_shell(claude_cmd)

    await tmux.create_session(name, command=command, cwd=cwd)
    await asyncio.to_thread(session_meta.note_created, name, None, cwd)
    info = await _session_info(name)
    if info is None:
        raise HTTPException(status_code=500, detail="session created but not found in list")
    return info


@router.patch("/api/sessions/{name}", response_model=SessionInfo,
               dependencies=[AuthDep, CsrfDep])
async def rename_session_endpoint(name: str, body: RenameSessionBody) -> SessionInfo:
    try:
        name = tmux.safe_name(name)
        new_name = tmux.safe_name(body.newName)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    _reject_control_session(new_name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="session not found")
    if name != new_name:
        if await tmux.session_exists(new_name):
            raise HTTPException(status_code=409, detail="target name already in use")
        if not await tmux.rename_session(name, new_name):
            raise HTTPException(status_code=500, detail="rename failed")
        # Preserve sticky flag across rename — without this a sticky
        # session would silently lose its persisted entry on rename.
        sticky.rename(name, new_name)
        # The composer draft follows the session too.
        drafts.rename(name, new_name)
        # …and so do saved screen layouts (its own and any tile showing it).
        layouts.rename(name, new_name)
        # …and its created / last-opened record.
        session_meta.rename(name, new_name)
    info = await _session_info(new_name)
    if info is None:
        raise HTTPException(status_code=500, detail="renamed but session vanished")
    return info


@router.delete("/api/sessions/{name}", dependencies=[AuthDep, CsrfDep])
async def delete_session_endpoint(name: str) -> dict[str, bool]:
    try:
        name = tmux.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="session not found")
    # "Kill" stops the session but keeps it remembered (and sticky, with its
    # cwd): it stays in the picker and resumes — same directory, same Claude
    # conversation — when opened. POST …/forget removes it for good.
    # lifecycle.stop also drops the cached scrollback, so a session reusing
    # the name can't inherit it.
    try:
        await lifecycle.stop(name, reason="stopped")
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return {"deleted": True}


def _lifecycle_name(name: str) -> str:
    try:
        name = tmux.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    return name


@router.get("/api/sessions/remembered", response_model=list[StoppedSessionInfo],
            dependencies=[AuthDep, SameOriginDep])
async def remembered_sessions() -> list[StoppedSessionInfo]:
    """Sessions ccpipe remembers that aren't running, most recently used first."""
    sysstats.touch()
    sessions, complete = await tmux.list_sessions_checked()
    if not complete:
        return []                     # can't tell running from stopped
    live = {s.name for s in sessions}
    meta = session_meta.load()
    stick = sticky.load()
    out: list[StoppedSessionInfo] = []
    for name in set(meta) | set(stick):
        if name in live or name == CONTROL_SESSION_NAME:
            continue
        m = meta.get(name, {})
        cwd = m.get("cwd") or stick.get(name, {}).get("cwd")
        if not cwd:
            continue
        out.append(StoppedSessionInfo(
            name=name, sticky=name in stick, created=m.get("created", 0),
            opened=m.get("opened", 0), stopped=m.get("stopped", 0),
            stop_reason=m.get("stop_reason", "gone"), cwd=cwd,
            conversation=bool(m.get("claude")), shell=bool(m.get("shell")),
            disk=sysstats.project_size(cwd)))
    out.sort(key=lambda i: max(i.opened, i.stopped, i.created), reverse=True)
    return out


@router.post("/api/sessions/{name}/resume", response_model=SessionInfo,
             dependencies=[AuthDep, CsrfDep])
async def resume_session(name: str) -> SessionInfo:
    name = _lifecycle_name(name)
    try:
        await lifecycle.resume(name)
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    info = await _session_info(name)
    if info is None:
        raise HTTPException(status_code=500, detail="resumed but not found")
    return info


@router.post("/api/sessions/{name}/stop", dependencies=[AuthDep, CsrfDep])
async def stop_session(name: str) -> dict[str, bool]:
    name = _lifecycle_name(name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="session not running")
    try:
        await lifecycle.stop(name, reason="stopped")
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return {"stopped": True}


@router.post("/api/sessions/{name}/forget", dependencies=[AuthDep, CsrfDep])
async def forget_session(name: str) -> dict[str, bool]:
    """Stop it if running and drop it from the list (and the sticky set)."""
    name = _lifecycle_name(name)
    await lifecycle.forget(name)
    return {"forgotten": True}


@router.get("/api/session-config", dependencies=[AuthDep, SameOriginDep])
async def session_config_get() -> dict[str, Any]:
    return dict(app_config.load().to_dict()["sessions"])


@router.post("/api/session-config", dependencies=[AuthDep, CsrfDep])
async def session_config_set(body: SessionsConfigBody) -> dict[str, Any]:
    cfg = app_config.load_for_edit()
    merged = {**cfg.to_dict()["sessions"],
              **{k: v for k, v in body.model_dump().items() if v is not None}}
    cfg.sessions = app_config.SessionsConfig.from_dict(merged)
    try:
        app_config.save(cfg)
    except OSError as exc:
        log.warning("could not save session config: %s", exc)
        raise HTTPException(status_code=500, detail="could not save the settings")
    return dict(cfg.to_dict()["sessions"])


@router.post("/api/sessions/{name}/sticky", response_model=SessionInfo,
              dependencies=[AuthDep, CsrfDep])
async def set_sticky_endpoint(name: str, body: StickyBody) -> SessionInfo:
    """Toggle the sticky flag for an existing session. Sticky sessions
    are auto-recreated by the lifespan hook on backend restart, with
    ``claude --continue`` so the most recent conversation for the cwd
    resumes automatically."""
    try:
        name = tmux.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="session not found")
    if body.sticky:
        cwd = await tmux.session_cwd(name)
        if not cwd:
            raise HTTPException(
                status_code=500,
                detail="could not resolve session cwd; cannot make sticky",
            )
        sticky.set_sticky(name, cwd)
    else:
        sticky.clear(name)
    info = await _session_info(name)
    if info is None:
        raise HTTPException(status_code=500, detail="session vanished")
    return info


# ─── Claude Code session listing + export ─────────────────────────────────

def _projects_subdir_for_cwd(cwd: str) -> Path:
    """``~/.claude/projects/<encoded>/`` for a cwd — see paths.claude_project_dir."""
    return claude_project_dir(cwd)


def _read_first_real_user_message(path: Path) -> str | None:
    """First genuine user prompt (skipping framework caveats / command
    stdout), trimmed to 120 chars, for the picker preview. Shares the single
    transcript parser + caveat predicate so "what counts as a real prompt"
    can't drift from the export / history readers. Bounded scan."""
    for n, (rtype, content, _ts, _off) in enumerate(_iter_transcript_records(path)):
        if n >= 400:
            break
        if rtype != "user":
            continue
        text = _stringify_content(content).strip()
        if text and not _is_framework_caveat(text):
            return text[:120]
    return None


def _running_claude_session_ids() -> set[str]:
    """Currently-running Claude Code sessionIds, sourced from
    ``~/.claude/sessions/<pid>.json`` (one file per live claude process).
    Used to filter the resume picker — we don't want to tempt the user
    into a second `claude --resume` of a conversation already running."""
    out: set[str] = set()
    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return out
    # Real claude session files are under ~1 KiB; a 16 KiB cap is far
    # above legitimate values. Without the cap an authenticated client
    # who can write into ~/.claude/sessions/ could plant a multi-GB
    # file and read_text() would OOM the worker on every picker open.
    _MAX_SESSION_FILE_BYTES = 16 * 1024
    for f in sessions_dir.glob("*.json"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_size > _MAX_SESSION_FILE_BYTES:
            log.warning("skipping oversized claude session file %s (%d bytes)",
                        f, st.st_size)
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        if isinstance(sid, str):
            out.add(sid)
    return out


def _stringify_content(content: Any) -> str:
    """Flatten claude-code JSONL ``message.content`` into plain text.
    Accepts a string or a list of typed blocks; only ``text`` blocks
    survive."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    return ""


def _is_framework_caveat(text: str) -> bool:
    """A user 'message' that is actually framework plumbing (command-output
    caveats, local-command wrappers) rather than a typed prompt — these wrap
    their content in XML-ish tags. Shared by every transcript reader so the
    'what counts as a real prompt' rule can't drift between them."""
    return not text or text.lstrip().startswith("<")


def _iter_transcript_records(path: Path, start_offset: int = 0):
    """Single parse scaffold shared by the export renderer, the picker
    preview, and the /history block builder. Yields
    ``(rtype, content, ts, resume_offset)`` for each user/assistant record,
    starting at byte ``start_offset``.

    ``resume_offset`` is the byte position after the last fully
    newline-terminated line consumed — a safe point to resume an incremental
    re-parse from, so a partially-written trailing line is re-read next time
    rather than skipped. Opened ``O_NOFOLLOW`` because a same-UID actor could
    race the leaf into a symlink between path resolution and open (matches the
    fs_read / fs_download hardening)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return
    with os.fdopen(fd, "rb") as f:
        if start_offset:
            f.seek(start_offset)
        resume = start_offset
        while True:
            line = f.readline()        # readline (not `for line in f`) so
            if not line:               # f.tell() stays accurate per line
                break
            if not line.endswith(b"\n"):
                break                  # partial trailing line (caught mid-write):
                                       # never process it and don't advance
                                       # `resume` past it — re-read it, complete,
                                       # on the next incremental pass.
            resume = f.tell()          # advance past EVERY complete line (incl.
            stripped = line.strip()    # blanks / skipped record types) so growth
            if not stripped:           # polls don't re-scan a trailing run of them
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            rtype = obj.get("type")
            if rtype not in ("user", "assistant"):
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
            yield rtype, msg.get("content"), ts, resume


def _iter_jsonl_as_markdown(path: Path):
    """Yield UTF-8 markdown chunks for a transcript, streaming (no full
    materialisation). Skips framework caveats + non-text content. Used by the
    export endpoint's response body."""
    first = True
    for rtype, content, _ts, _off in _iter_transcript_records(path):
        text = _stringify_content(content)
        if rtype == "user":
            if _is_framework_caveat(text):
                continue
            header = "## User"
        else:
            if not text:
                continue
            header = "## Claude"
        sep = "" if first else "\n"
        first = False
        yield f"{sep}{header}\n\n{text.strip()}\n".encode("utf-8")


def _tool_summary(block: dict[str, Any]) -> str:
    """Render a tool_use block to a console-style one-liner (plus key detail)
    so the /history view shows the commands run and edits made, not just
    prose. Bounded so a giant Write/diff can't blow up a single block."""
    name = block.get("name") or "tool"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}

    def cap(s: object, n: int) -> str:
        t = str(s)
        return t if len(t) <= n else t[:n] + " …"

    if name == "Bash":
        cmd = cap(inp.get("command", ""), 2000)
        desc = inp.get("description")
        return f"$ {cmd}" + (f"\n  ({desc})" if isinstance(desc, str) and desc else "")
    if name in ("Edit", "MultiEdit"):
        fp = inp.get("file_path", "")
        old, new = cap(inp.get("old_string", ""), 600), cap(inp.get("new_string", ""), 600)
        diff = "\n".join(["- " + ln for ln in old.splitlines()]
                         + ["+ " + ln for ln in new.splitlines()])
        return f"✎ Edit {fp}\n{diff}" if diff else f"✎ Edit {fp}"
    if name == "Write":
        fp = inp.get("file_path", "")
        return f"✎ Write {fp}\n{cap(inp.get('content', ''), 1500)}"
    if name in ("Read", "Grep", "Glob"):
        target = inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
        return f"▸ {name} {target}".rstrip()
    # Generic fallback: tool name + its first couple of inputs.
    parts = ", ".join(f"{k}={cap(v, 80)}" for k, v in list(inp.items())[:3])
    return f"▸ {name}" + (f" {parts}" if parts else "")


def _record_to_blocks(blocks: list[dict[str, Any]], rtype: str,
                      content: Any, ts: str | None) -> None:
    """Append the render block(s) for one transcript record to ``blocks`` in
    place, each with a stable index: user prose, assistant prose, and one
    block per assistant tool_use (so commands/edits show inline)."""
    def emit(role: str, text: str) -> None:
        text = (text or "").strip()
        if text:
            blocks.append({"i": len(blocks), "role": role, "text": text, "ts": ts})

    if rtype == "user":
        text = _stringify_content(content)
        if not _is_framework_caveat(text):
            emit("user", text)
        return
    if isinstance(content, str):
        emit("assistant", content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and isinstance(b.get("text"), str):
                emit("assistant", b["text"])
            elif t == "tool_use":
                emit("tool", _tool_summary(b))


# ── parsed-block cache (H1) ───────────────────────────────────────────────
# Re-parsing a large transcript on every 3.5 s /history poll burned ~1.5 s of
# CPU each time, almost always to find nothing new, and concurrent parses
# contend in the thread pool. Cache parsed blocks keyed on (st_mtime_ns,
# st_size): an unchanged poll becomes a single stat(); a grown file is parsed
# incrementally from the last complete-line offset (the stable-index invariant
# makes appending sound); anything else (shrink / mtime regression / rotation)
# falls back to a full re-parse. Bounded to a few sessions so the held block
# lists can't balloon memory on this host.
class _CachedBlocks:
    __slots__ = ("mtime", "size", "offset", "blocks")

    def __init__(self, mtime: int, size: int, offset: int,
                 blocks: list[dict[str, Any]]):
        self.mtime, self.size, self.offset, self.blocks = mtime, size, offset, blocks


_BLOCKS_CACHE: "OrderedDict[str, _CachedBlocks]" = OrderedDict()
_BLOCKS_CACHE_MAX = 6
# _BLOCKS_LOCK guards only the cache dict; the parse itself runs under a
# per-transcript lock so one big (re)parse doesn't stall every other
# session's cheap "unchanged" poll.
_BLOCKS_LOCK = threading.Lock()
# (Key locks are never evicted: dropping one while a parse holds it would let
# a second parser start on the same transcript. One small Lock per transcript
# path seen is negligible.)
_BLOCKS_KEY_LOCKS: dict[str, threading.Lock] = {}


def _transcript_blocks(path: Path) -> list[dict[str, Any]]:
    """Parsed render blocks for a transcript ``{i, role, text, ts}`` (role
    ``user`` | ``assistant`` | ``tool``), newest last, with ``i`` a stable
    paging/live-tail cursor. Cached + incrementally updated (see above).

    Returns the cached list instance — callers MUST treat it read-only (slice,
    never mutate)."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = str(path)
    with _BLOCKS_LOCK:
        key_lock = _BLOCKS_KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        with _BLOCKS_LOCK:
            cached = _BLOCKS_CACHE.get(key)
            if cached and cached.mtime == st.st_mtime_ns and cached.size == st.st_size:
                _BLOCKS_CACHE.move_to_end(key)
                return cached.blocks
        # Incremental only when the file strictly grew with a newer mtime
        # (an append); otherwise re-parse from scratch.
        if cached and st.st_size > cached.size and st.st_mtime_ns >= cached.mtime:
            blocks, offset = cached.blocks, cached.offset
        else:
            blocks, offset = [], 0
        # Incremental appends mutate the live cached list in place; if the parse
        # raises mid-way, roll those appends back and leave the old cache entry
        # intact (unchanged size/offset) so the next poll cleanly retries —
        # otherwise the half-applied blocks compound on every future poll.
        base = len(blocks)
        resume = offset
        try:
            for rtype, content, ts, off in _iter_transcript_records(path, offset):
                resume = off
                _record_to_blocks(blocks, rtype, content, ts)
        except Exception:
            del blocks[base:]
            raise
        with _BLOCKS_LOCK:
            _BLOCKS_CACHE[key] = _CachedBlocks(st.st_mtime_ns, st.st_size, resume, blocks)
            _BLOCKS_CACHE.move_to_end(key)
            while len(_BLOCKS_CACHE) > _BLOCKS_CACHE_MAX:
                _BLOCKS_CACHE.popitem(last=False)
        return blocks


@router.get("/api/sessions/{name}/history", dependencies=[AuthDep, SameOriginDep])
async def session_history(name: str, before: int | None = None,
                          after: int | None = None,
                          limit: int = 40) -> dict[str, Any]:
    """Paged conversation history for the session's bound claude transcript,
    newest last. The /history view renders these as console-style text.

    - no cursor          → the most recent ``limit`` blocks (the tail).
    - ``before=<cursor>`` → the ``limit`` blocks immediately older than it.
    - ``after=<cursor>``  → blocks NEWER than it (live tail; the view polls
      this to stream in newly-run commands and replies without a refresh).

    ``oldestCursor`` is what to pass as ``before`` for the next older page;
    ``newestCursor`` (the last block's index, or the request's ``after`` when
    nothing new) is what to poll back as ``after``. ``gen`` is a generation
    token (the claude sessionId) — when it changes (claude restarted into a
    new transcript) the client re-loads from the tail. This is the conversation
    REVIEW surface; tmux console scrollback is separate and unaffected."""
    try:
        safe = tmux.safe_name(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad session")
    limit = max(1, min(limit, 500))
    sid, cwd = await tmux.claude_sid_and_cwd(safe)   # one pid resolution (L4)
    if not sid or not cwd:
        raise HTTPException(status_code=404, detail="no claude session bound to this tmux session")
    # Containment parity with the export endpoint: sid must be the UUID it
    # claims to be, and the resolved path must stay inside the projects dir
    # (O_NOFOLLOW alone doesn't stop `..` or an intermediate symlinked dir).
    if not _UUID_RE.match(sid):
        raise HTTPException(status_code=404, detail="session not found")
    projects_dir = _projects_subdir_for_cwd(cwd)
    path = projects_dir / f"{sid}.jsonl"
    try:
        if path.exists():
            path.resolve(strict=True).relative_to(projects_dir.resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="session not found")

    blocks = await asyncio.to_thread(_transcript_blocks, path)
    total = len(blocks)

    if after is not None:
        # Live tail: everything newer than `after` (capped). Nothing older.
        start = max(0, min(after + 1, total))
        page = blocks[start:start + limit]
        newest = page[-1]["i"] if page else after
        return {
            "gen": sid,
            "total": total,
            "blocks": page,
            "newestCursor": newest,
            "oldestCursor": start,
            "hasOlder": start > 0,
            "hasNewer": (start + len(page)) < total,
        }

    end = total if before is None else max(0, min(before, total))
    start = max(0, end - limit)
    page = blocks[start:end]
    return {
        "gen": sid,
        "total": total,
        "blocks": page,
        "newestCursor": page[-1]["i"] if page else -1,
        "oldestCursor": start,
        "hasOlder": start > 0,
        "hasNewer": end < total,
    }


@router.get("/api/claude-sessions/{session_id}/export", dependencies=[AuthDep, SameOriginDep])
async def claude_session_export(session_id: str, cwd: str) -> StreamingResponse:
    """Stream a markdown rendering of a claude session's JSONL transcript.

    Same-origin gate matches the fs GETs: an authenticated browser
    session would otherwise let a top-level navigation drop the
    transcript into the operator's Downloads via a malicious link."""
    if not _UUID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    if not cwd.startswith("/"):
        raise HTTPException(status_code=400, detail="cwd must be absolute")
    try:
        resolved = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="cwd does not exist")
    # Jail the cwd: without this, an authenticated client can enumerate
    # & export the markdown for any claude transcript whose project dir
    # exists, regardless of whether the cwd is reachable through the
    # /api/fs root. Stay consistent with the rest of the app's surface.
    _enforce_fs_jail(resolved)
    projects_dir = _projects_subdir_for_cwd(str(resolved))
    jsonl = projects_dir / f"{session_id}.jsonl"
    # Confirm the file is still inside the projects dir after symlink
    # expansion — path-traversal hygiene.
    try:
        target = jsonl.resolve(strict=True)
        target.relative_to(projects_dir.resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="session not found")
    # Probe by reading the first chunk so we can return 404 cleanly
    # rather than serving an empty 200 for an unreadable transcript.
    def _probe() -> bytes | None:
        for chunk in _iter_jsonl_as_markdown(target):
            return chunk
        return None
    first_chunk = await asyncio.to_thread(_probe)
    if not first_chunk:
        raise HTTPException(status_code=404, detail="empty or unreadable transcript")

    # Streaming body — re-iterates the file from the top. Cheap given
    # the probe was just one record; avoids leaking the probe's fd
    # into the response generator.
    filename = f"ccpipe-{session_id[:8]}.md"
    return StreamingResponse(
        _iter_jsonl_as_markdown(target),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


@router.get("/api/claude-sessions", dependencies=[AuthDep, SameOriginDep])
async def claude_sessions(cwd: str) -> dict[str, Any]:
    """List Claude Code sessions persisted under the project dir
    corresponding to *cwd*, with their first user message preview so
    the user can identify the right one to resume.

    Excludes sessionIds for any claude process currently running on
    this machine — resuming a live session would create a conflicting
    second claude with the same sessionId."""
    if not cwd.startswith("/"):
        raise HTTPException(status_code=400, detail="cwd must be absolute")
    try:
        resolved = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="cwd does not exist")
    # Jail the cwd — see claude_session_export for rationale.
    _enforce_fs_jail(resolved)
    projects_dir = _projects_subdir_for_cwd(str(resolved))
    if not projects_dir.is_dir():
        return {"sessions": []}

    # L4: glob + stat + read_text per session file — blocking I/O.
    # Tens of ms on a busy box (with many concurrent claude sessions),
    # noticeable jitter for PTY pumps and WS pings during the wait.
    running = await asyncio.to_thread(_running_claude_session_ids)
    # Collect viable JSONL paths first.
    viable: list[tuple[str, Path, int, int]] = []
    for jsonl in projects_dir.glob("*.jsonl"):
        sid = jsonl.stem
        if not _UUID_RE.match(sid):
            continue
        if sid in running:
            continue
        try:
            stat = jsonl.stat()
        except OSError:
            continue
        viable.append((sid, jsonl, int(stat.st_mtime), stat.st_size))

    # Sort + truncate to the top 50 BEFORE the per-file header reads.
    # The endpoint caps the response at 50 anyway, so reading the
    # first real user message for every JSONL on disk (potentially
    # hundreds on a power-user dir) and then discarding 95% of the
    # results was pure executor traffic. Truncating first cuts the
    # to_thread fan-out to the bounded set we actually return.
    viable.sort(key=lambda v: v[2], reverse=True)
    viable = viable[:50]

    first_msgs = await asyncio.gather(*(
        asyncio.to_thread(_read_first_real_user_message, path)
        for (_, path, _, _) in viable
    ))

    out: list[dict[str, Any]] = [
        {"id": sid, "mtime": mtime, "size": size, "firstUserMessage": first}
        for (sid, _, mtime, size), first in zip(viable, first_msgs)
    ]
    return {"sessions": out}


# ─── tmux toolbar (windows / panes / copy-mode) ───────────────────────────

class TmuxActionBody(BaseModel):
    action: str
    arg: int | str | None = None


async def _existing_session(name: str) -> str:
    try:
        name = tmux.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="session not found")
    return name


@router.get("/api/sessions/{name}/windows", dependencies=[AuthDep, SameOriginDep])
async def session_windows(name: str) -> list[dict[str, Any]]:
    return await tmux_ui.list_windows(await _existing_session(name))


@router.post("/api/sessions/{name}/tmux", dependencies=[AuthDep, CsrfDep])
async def session_tmux_action(name: str, body: TmuxActionBody) -> list[dict[str, Any]]:
    """Run one toolbar action; returns the session's windows afterwards."""
    name = await _existing_session(name)
    try:
        await tmux_ui.perform(name, body.action, body.arg)
    except tmux_ui.ActionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return await tmux_ui.list_windows(name)
