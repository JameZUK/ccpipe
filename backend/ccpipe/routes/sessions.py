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
import re
import shlex
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import tmux
from ..auth import AuthDep, CsrfDep
from ..tmux_control import CONTROL_SESSION_NAME

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


class CreateSessionBody(BaseModel):
    name: str
    # Optional initial working directory for the new tmux session.
    cwd: str | None = None
    # Optional Claude sessionId (UUID) to resume.
    resumeSessionId: str | None = None


class RenameSessionBody(BaseModel):
    newName: str


# ─── tmux session CRUD ────────────────────────────────────────────────────

@router.get("/api/sessions", response_model=list[SessionInfo], dependencies=[AuthDep])
async def list_sessions() -> list[SessionInfo]:
    sessions = await tmux.list_sessions()
    return [SessionInfo(**s.__dict__) for s in sessions]


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
        cwd = str(resolved)

    if body.resumeSessionId:
        if not _UUID_RE.match(body.resumeSessionId):
            raise HTTPException(status_code=400, detail="invalid resumeSessionId")
        # shlex.quote is belt-and-braces: the UUID regex already bounds
        # the value to [0-9a-fA-F-], but libtmux passes window_command
        # to a shell and we want zero ambiguity.
        command = f"claude --resume {shlex.quote(body.resumeSessionId)}"
    else:
        command = "claude"

    await tmux.create_session(name, command=command, cwd=cwd)
    for s in await tmux.list_sessions():
        if s.name == name:
            return SessionInfo(**s.__dict__)
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
    for s in await tmux.list_sessions():
        if s.name == new_name:
            return SessionInfo(**s.__dict__)
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
    return {"deleted": True}


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
    for f in sessions_dir.glob("*.json"):
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
        f = path.open("rb")
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


@router.get("/api/claude-sessions/{session_id}/export", dependencies=[AuthDep])
async def claude_session_export(session_id: str, cwd: str) -> StreamingResponse:
    """Stream a markdown rendering of a claude session's JSONL transcript."""
    if not _UUID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    if not cwd.startswith("/"):
        raise HTTPException(status_code=400, detail="cwd must be absolute")
    try:
        resolved = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="cwd does not exist")
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
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
    projects_dir = _projects_subdir_for_cwd(str(resolved))
    if not projects_dir.is_dir():
        return {"sessions": []}

    running = _running_claude_session_ids()
    out: list[dict[str, Any]] = []
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
        first_msg = await asyncio.to_thread(_read_first_real_user_message, jsonl)
        out.append({
            "id": sid,
            "mtime": int(stat.st_mtime),
            "size": stat.st_size,
            "firstUserMessage": first_msg,
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"sessions": out[:50]}
