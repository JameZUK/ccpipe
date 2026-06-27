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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import sticky, tmux, ws
from ..auth import AuthDep, CsrfDep
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
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


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


def _wrap_in_shell(claude_cmd: str) -> str:
    """Wrap a claude invocation so the tmux session survives claude
    exiting. When claude exits, ``exec $SHELL -i`` replaces the shell
    process with an interactive shell in the same working directory,
    so the pane lands at a prompt instead of dying. Without this the
    only-pane closes → only-window closes → session is destroyed.
    """
    shell = os.environ.get("SHELL") or "/bin/bash"
    return f"{claude_cmd}; exec {shlex.quote(shell)} -i"


def _to_session_info(s: tmux.TmuxSession, sticky_names: set[str]) -> SessionInfo:
    return SessionInfo(
        name=s.name,
        windows=s.windows,
        attached=s.attached,
        created=s.created,
        sticky=s.name in sticky_names,
        activity=s.activity,
    )


# ─── tmux session CRUD ────────────────────────────────────────────────────

@router.get("/api/sessions", response_model=list[SessionInfo], dependencies=[AuthDep])
async def list_sessions() -> list[SessionInfo]:
    sessions = await tmux.list_sessions()
    sticky_names = sticky.sticky_names()
    return [_to_session_info(s, sticky_names) for s in sessions]


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
        claude_cmd = f"claude --resume {shlex.quote(body.resumeSessionId)}"
    else:
        claude_cmd = "claude"

    # Wrap in $SHELL -i so the tmux session survives claude exit and
    # drops to a prompt in the original cwd. Without this the only-pane
    # closes when claude exits → session is destroyed → reconnect path
    # auto-creates a fresh session in $HOME, losing the cwd.
    command = _wrap_in_shell(claude_cmd)

    await tmux.create_session(name, command=command, cwd=cwd)
    sticky_names = sticky.sticky_names()
    for s in await tmux.list_sessions():
        if s.name == name:
            return _to_session_info(s, sticky_names)
    raise HTTPException(status_code=500, detail="session created but not found in list")


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
        # Drop any cached capture-pane blob under BOTH names so a
        # reconnect can't be served the prior occupant's history.
        ws.invalidate_history_cache(name)
        ws.invalidate_history_cache(new_name)
    sticky_names = sticky.sticky_names()
    for s in await tmux.list_sessions():
        if s.name == new_name:
            return _to_session_info(s, sticky_names)
    raise HTTPException(status_code=500, detail="renamed but session vanished")


@router.delete("/api/sessions/{name}", dependencies=[AuthDep, CsrfDep])
async def delete_session_endpoint(name: str) -> dict[str, bool]:
    try:
        name = tmux.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="session not found")
    if not await tmux.kill_session(name):
        raise HTTPException(status_code=500, detail="kill failed")
    # Auto-unflip sticky: kill implies the user no longer wants this
    # session, so don't quietly resurrect it on the next backend start.
    sticky.clear(name)
    # A new session reusing this name must not inherit the dead one's
    # cached scrollback.
    ws.invalidate_history_cache(name)
    return {"deleted": True}


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
    sticky_names = sticky.sticky_names()
    for s in await tmux.list_sessions():
        if s.name == name:
            return _to_session_info(s, sticky_names)
    raise HTTPException(status_code=500, detail="session vanished")


# ─── Claude Code session listing + export ─────────────────────────────────

def _projects_subdir_for_cwd(cwd: str) -> Path:
    """Return ``~/.claude/projects/<encoded>/`` for a given cwd. Claude
    encodes the cwd by replacing each '/' with '-', so
    ``/home/you/Projects/foo`` becomes ``-home-you-Projects-foo``."""
    encoded = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def _read_first_real_user_message(path: Path) -> str | None:
    """Scan up to 200 lines for the first user message that isn't a
    framework caveat / command stdout (those wrap their content in
    XML-ish tags). Returns up to 120 chars trimmed; ``None`` if no
    plain user prompt is found in the first 200 records."""
    try:
        with path.open("rb") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                content = msg.get("content")
                if isinstance(content, str):
                    if content.lstrip().startswith("<"):
                        continue
                    text = content.strip()
                    return text[:120] or None
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "text":
                            continue
                        text = (block.get("text") or "").strip()
                        if not text or text.startswith("<"):
                            continue
                        return text[:120]
    except OSError:
        return None
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


def _iter_jsonl_as_markdown(path: Path):
    """Yield UTF-8 markdown chunks for a claude-code JSONL transcript
    without materialising the full document in memory. Skips framework
    caveats, tool-use / tool-result records, and any non-text content
    blocks. Used by the export endpoint as a true streaming response
    body."""
    try:
        # O_NOFOLLOW: caller already resolved the path and confirmed
        # it sits inside the projects dir, but between that check and
        # this open() a same-UID actor could race the leaf into a
        # symlink. Refuse to follow at open time. Matches the
        # hardening on fs_read / fs_download in routes/fs.py.
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        f = os.fdopen(fd, "rb")
    except OSError:
        return
    try:
        first = True
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = obj.get("type")
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = msg.get("content")
            if rtype == "user":
                text = _stringify_content(content)
                if not text or text.lstrip().startswith("<"):
                    continue
                header = "## User"
            elif rtype == "assistant":
                text = _stringify_content(content)
                if not text:
                    continue
                header = "## Claude"
            else:
                continue
            sep = "" if first else "\n"
            first = False
            yield f"{sep}{header}\n\n{text.strip()}\n".encode("utf-8")
    finally:
        try: f.close()
        except OSError: pass


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


def _transcript_blocks(path: Path) -> list[dict[str, Any]]:
    """Parse a claude JSONL transcript into ordered render blocks
    ``{i, role, text, ts}`` where role is ``user`` | ``assistant`` | ``tool``.

    One assistant record can yield several blocks (its text plus one per
    tool_use) so the view shows the commands run and files changed inline.
    ``i`` is the block's position in this list — stable across appends (the
    file only grows at the end), so the client uses it as a paging / live-tail
    cursor."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return []
    blocks: list[dict[str, Any]] = []

    def emit(role: str, text: str, ts: str | None) -> None:
        text = (text or "").strip()
        if text:
            blocks.append({"i": len(blocks), "role": role, "text": text, "ts": ts})

    with os.fdopen(fd, "rb") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = obj.get("type")
            if rtype not in ("user", "assistant"):
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = msg.get("content")
            ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
            if rtype == "user":
                text = _stringify_content(content)
                if text and not text.lstrip().startswith("<"):
                    emit("user", text, ts)
                continue
            # assistant: split into text + tool_use blocks in order.
            if isinstance(content, str):
                emit("assistant", content, ts)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    t = b.get("type")
                    if t == "text" and isinstance(b.get("text"), str):
                        emit("assistant", b["text"], ts)
                    elif t == "tool_use":
                        emit("tool", _tool_summary(b), ts)
    return blocks


@router.get("/api/sessions/{name}/history", dependencies=[AuthDep])
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
    nothing new) is what to poll back as ``after``. This is the conversation
    REVIEW surface; tmux console scrollback is separate and unaffected."""
    try:
        safe = tmux.safe_name(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad session")
    limit = max(1, min(limit, 500))
    sid = await tmux.claude_session_id(safe)
    cwd = await tmux.session_cwd(safe)
    if not sid or not cwd:
        raise HTTPException(status_code=404, detail="no claude session bound to this tmux session")
    path = _projects_subdir_for_cwd(cwd) / f"{sid}.jsonl"
    blocks = await asyncio.to_thread(_transcript_blocks, path)
    total = len(blocks)

    if after is not None:
        # Live tail: everything newer than `after` (capped). Nothing older.
        start = max(0, min(after + 1, total))
        page = blocks[start:start + limit]
        newest = page[-1]["i"] if page else after
        return {
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
        "total": total,
        "blocks": page,
        "newestCursor": page[-1]["i"] if page else -1,
        "oldestCursor": start,
        "hasOlder": start > 0,
    }


@router.get("/api/claude-sessions/{session_id}/export", dependencies=[AuthDep])
async def claude_session_export(session_id: str, cwd: str,
                                  request: Request) -> StreamingResponse:
    """Stream a markdown rendering of a claude session's JSONL transcript.

    Same-origin gate matches the fs GETs: an authenticated browser
    session would otherwise let a top-level navigation drop the
    transcript into the operator's Downloads via a malicious link."""
    sfs = request.headers.get("sec-fetch-site", "").lower()
    if sfs and sfs != "same-origin":
        raise HTTPException(status_code=403, detail="cross-origin blocked")
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


@router.get("/api/claude-sessions", dependencies=[AuthDep])
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
