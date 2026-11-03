"""Viewing files in the browser, and transfers that survive a bad connection.

``GET /api/fs/inline`` serves a file for the file viewer to show: images,
SVG, PDF, audio and video, by an explicit extension allowlist and with
byte ranges (so media can seek). Everything is sandboxed except PDF:

  - images, SVG, audio and video carry ``Content-Security-Policy: sandbox``,
    so opened on their own (an SVG in a new tab, say) they get an opaque
    origin and no script runs. The viewer shows SVG through ``<img>``,
    which never runs SVG script anyway;
  - PDF can't be sandboxed (Chrome and Firefox refuse to render a
    sandboxed PDF), but a PDF renders in the browser's own viewer, not
    as a page of this origin. It may be framed by this origin only, for
    the viewer's in-page preview.

Uploads go in pieces: ``POST /api/fs/uploads`` starts one, ``PUT
/api/fs/uploads/{id}?offset=N`` sends a piece, ``GET`` asks how much has
arrived, ``POST …/finish`` puts the file in place, ``DELETE`` abandons it.
The part-file sits next to the target (a hidden ``.ccpipe-….tmp``, as the
one-shot upload uses), so finishing is one atomic rename, and its length
on disk *is* the received count, which keeps a resume right even across a
ccpipe restart. Pieces are written at their offset (pwrite), so a retried
piece whose first attempt half-arrived just rewrites the same bytes.
Abandoned uploads are removed after a day: the registry of part-files is
kept in the state directory and swept at startup, hourly (sweep_loop, run
from main.py's lifespan) and whenever an upload starts.

The upload routes are all ``async`` on purpose: the registry is then only
ever touched from the event loop, one request at a time, so it needs no
lock. The one slow step, writing a piece to disk, goes to a thread in
1 MiB batches.
"""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import secrets
import stat as stat_mod
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from .. import config as app_config
from ..auth import AuthDep, CsrfDep, SameOriginDep
from ..fs_range import ranged_response
from ..paths import state_dir
from ..safe_write import NEW_FILE_MODE, atomic_write_text, existing_mode, open_temp, temp_name
from .fs import _resolve_fs_path, _resolve_write_target, _walk_parent_nofollow

log = logging.getLogger(__name__)
router = APIRouter()

# ─── Inline viewing ────────────────────────────────────────────────────────

# By extension, not by sniffing or the system's mime.types: what's served
# inline is a fixed, reviewed list.
INLINE_TYPES: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".bmp": "image/bmp", ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".weba": "audio/webm",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".ogv": "video/ogg", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
}

_SANDBOX_CSP = ("sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; "
                "style-src 'unsafe-inline'; frame-ancestors 'none'")
_PDF_CSP = "frame-ancestors 'self'"


def open_readonly(resolved: Path) -> tuple[int, os.stat_result]:
    """Open the regular file *resolved* for reading without following a
    symlink at any component (as /api/fs/read does). Returns (fd, stat)."""
    parent_fd, leaf = _walk_parent_nofollow(str(resolved))
    try:
        try:
            # O_NONBLOCK: opening a FIFO for reading would otherwise wait for
            # a writer, holding a worker thread for good (the type check
            # below rejects it). It changes nothing for a regular file.
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise HTTPException(status_code=403, detail="symlink at target")
            if exc.errno == errno.ENOENT:
                raise HTTPException(status_code=404, detail="path not found")
            if exc.errno == errno.EACCES:
                raise HTTPException(status_code=403, detail="permission denied")
            raise HTTPException(status_code=500, detail=f"open failed: {exc}")
    finally:
        os.close(parent_fd)
    st = os.fstat(fd)
    if not stat_mod.S_ISREG(st.st_mode):
        os.close(fd)
        raise HTTPException(status_code=400, detail="not a regular file")
    return fd, st


@router.get("/api/fs/inline", dependencies=[AuthDep, SameOriginDep])
def fs_inline(request: Request, path: str) -> Response:
    resolved = _resolve_fs_path(path)
    ctype = INLINE_TYPES.get(resolved.suffix.lower())
    if ctype is None:
        raise HTTPException(status_code=415, detail="no inline view for this file type")
    fd, st = open_readonly(resolved)
    pdf = ctype == "application/pdf"
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(resolved.name, safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _PDF_CSP if pdf else _SANDBOX_CSP,
        "X-Frame-Options": "SAMEORIGIN" if pdf else "DENY",
        "Cache-Control": "private, no-cache",
    }
    return ranged_response(request, fd, st, ctype, headers)


# ─── Resumable uploads ─────────────────────────────────────────────────────

CHUNK_MAX = 16 * 1024 * 1024          # one PUT's body (main.py caps it too)
# A new file's mode, as open() would give it (safe_write.NEW_FILE_MODE less
# the umask). The part-file itself stays 0600 until it's finished, so it can
# be reopened for writing even when the file it replaces is read-only.
def _read_umask() -> int:
    """The process umask, read without changing it (os.umask can only be
    read by setting it, which would briefly loosen it for every thread)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    return 0o022


_UMASK = _read_umask()
_UPLOAD_TTL_S = 24 * 3600
_WRITE_BATCH = 1024 * 1024
_MAX_UPLOADS = 64


@dataclass
class _Upload:
    id: str
    path: str        # as asked for: re-validated at every step
    final: str       # where it lands (a symlink resolved to its target)
    tmp: str         # the part-file's name, in final's directory
    size: int
    created: float


_uploads: dict[str, _Upload] = {}
_loaded = False


def _registry() -> Path:
    return state_dir() / "uploads.json"


def _save() -> None:
    try:
        atomic_write_text(_registry(), json.dumps([asdict(u) for u in _uploads.values()]))
    except OSError as exc:
        log.warning("uploads registry not saved: %s", exc)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        for d in json.loads(_registry().read_text()):
            u = _Upload(**d)
            _uploads[u.id] = u
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        log.warning("uploads registry unreadable, ignored: %s", exc)
    _sweep()


def _part_open(u: _Upload, flags: int) -> tuple[int, int]:
    """Open *u*'s part-file. Returns (fd, parent_fd); the caller closes
    both. 410 if it's gone."""
    parent_fd, _ = _walk_parent_nofollow(u.final)
    try:
        fd = os.open(u.tmp, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        if exc.errno == errno.ENOENT:
            raise HTTPException(status_code=410, detail="upload expired; start again")
        raise HTTPException(status_code=500, detail=f"part-file: {exc}")
    return fd, parent_fd


def _discard(u: _Upload) -> None:
    _uploads.pop(u.id, None)
    try:
        parent_fd, _ = _walk_parent_nofollow(u.final)
        try:
            os.unlink(u.tmp, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except (OSError, HTTPException):
        pass
    _save()


def _sweep() -> None:
    """Drop uploads untouched for a day (the part-file's mtime is the last
    piece received)."""
    now = time.time()
    for u in list(_uploads.values()):
        try:
            fd, parent_fd = _part_open(u, os.O_RDONLY)
            try:
                touched = os.fstat(fd).st_mtime
            finally:
                os.close(fd)
                os.close(parent_fd)
        except HTTPException:
            touched = 0.0
        if now - touched > _UPLOAD_TTL_S:
            log.info("upload %s to %s abandoned; removed", u.id, u.final)
            _discard(u)


_SWEEP_EVERY_S = 3600


async def sweep_loop() -> None:
    """Remove abandoned uploads at startup and then hourly (main.py)."""
    while True:
        try:
            _load()                 # the first call loads and sweeps
            _sweep()
        except Exception:           # never let the loop die
            log.exception("upload sweep failed")
        await asyncio.sleep(_SWEEP_EVERY_S)


def _get(upload_id: str) -> _Upload:
    _load()
    u = _uploads.get(upload_id)
    if u is None:
        raise HTTPException(status_code=404, detail="no such upload; start again")
    return u


def _received(u: _Upload) -> int:
    fd, parent_fd = _part_open(u, os.O_RDONLY)
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)
        os.close(parent_fd)


class UploadStart(BaseModel):
    path: str
    size: int = Field(ge=0)


@router.post("/api/fs/uploads", dependencies=[AuthDep, CsrfDep])
async def upload_start(body: UploadStart) -> dict[str, Any]:
    _load()
    _sweep()
    final = _resolve_write_target(body.path)
    cap = app_config.load().fs.upload_limit_mb * 1024 * 1024
    if body.size > cap:
        raise HTTPException(status_code=413, detail=f"file exceeds upload limit ({cap} bytes)")
    if len(_uploads) >= _MAX_UPLOADS:
        raise HTTPException(status_code=429, detail="too many unfinished uploads")
    parent_fd, _ = _walk_parent_nofollow(str(final))
    try:
        tmp = temp_name()
        try:
            fd = open_temp(tmp, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.EACCES:
                raise HTTPException(status_code=403, detail="permission denied")
            raise HTTPException(status_code=500, detail=f"upload failed: {exc}")
        os.close(fd)
    finally:
        os.close(parent_fd)
    u = _Upload(id=secrets.token_urlsafe(16), path=body.path, final=str(final),
                tmp=tmp, size=body.size, created=time.time())
    _uploads[u.id] = u
    _save()
    return {"id": u.id, "received": 0, "size": u.size, "chunk_max": CHUNK_MAX}


@router.get("/api/fs/uploads/{upload_id}", dependencies=[AuthDep, SameOriginDep])
async def upload_status(upload_id: str) -> dict[str, Any]:
    u = _get(upload_id)
    return {"id": u.id, "received": _received(u), "size": u.size}


@router.put("/api/fs/uploads/{upload_id}", dependencies=[AuthDep, CsrfDep])
async def upload_chunk(request: Request, upload_id: str, offset: int) -> Response:
    u = _get(upload_id)
    fd, parent_fd = _part_open(u, os.O_WRONLY)
    try:
        received = os.fstat(fd).st_size
        if offset < 0 or offset > received:
            return JSONResponse(status_code=409, content={
                "detail": "offset is past what has arrived", "received": received})
        pos = offset              # where the next batch goes
        buf = bytearray()

        async def flush() -> None:
            nonlocal pos
            if buf:
                data = bytes(buf)
                buf.clear()
                await asyncio.to_thread(os.pwrite, fd, data, pos)
                pos += len(data)

        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                end = pos + len(buf) + len(chunk)
                if end > u.size or end - offset > CHUNK_MAX:
                    raise HTTPException(status_code=413, detail="piece runs past the file's size")
                buf += chunk
                if len(buf) >= _WRITE_BATCH:
                    await flush()
        except ClientDisconnect:
            pass             # what arrived is kept; the client resumes from it
        finally:
            await flush()
        received = os.fstat(fd).st_size
    finally:
        os.close(fd)
        os.close(parent_fd)
    return JSONResponse({"id": u.id, "received": received, "size": u.size})


@router.post("/api/fs/uploads/{upload_id}/finish", dependencies=[AuthDep, CsrfDep])
async def upload_finish(upload_id: str) -> dict[str, Any]:
    u = _get(upload_id)
    # The target is validated again: the jail and deny-list hold at the
    # moment the file appears, not only when the upload began.
    final = _resolve_write_target(u.path)
    if str(final) != u.final:
        _discard(u)
        raise HTTPException(status_code=409, detail="the destination changed; upload again")
    fd, parent_fd = _part_open(u, os.O_RDONLY)
    try:
        got = os.fstat(fd).st_size
        if got != u.size:
            return JSONResponse(status_code=409, content={
                "detail": "not all of the file has arrived", "received": got})
        await asyncio.to_thread(os.fsync, fd)
        # Keep the mode of the file being replaced (an executable stays
        # executable, a private file private), as the editor's save does.
        mode = existing_mode(final.name, dir_fd=parent_fd)
        os.fchmod(fd, mode if mode is not None else NEW_FILE_MODE & ~_UMASK)
        try:
            os.replace(u.tmp, final.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.EACCES:
                raise HTTPException(status_code=403, detail="permission denied")
            raise HTTPException(status_code=500, detail=f"upload failed: {exc}")
    finally:
        os.close(fd)
        os.close(parent_fd)
    _uploads.pop(u.id, None)
    _save()
    return {"path": u.final, "size": u.size}


@router.delete("/api/fs/uploads/{upload_id}", dependencies=[AuthDep, CsrfDep])
async def upload_cancel(upload_id: str) -> dict[str, Any]:
    _discard(_get(upload_id))
    return {"ok": True}
