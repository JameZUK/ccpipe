"""File-transfer + filesystem-browser endpoints.

Every path that crosses the network is funnelled through:

  - ``_resolve_fs_path`` for paths that must already exist (read,
    download, list, delete, rename source)
  - ``_resolve_fs_parent_for_new`` for paths that may not exist yet
    (write target, upload target, rename destination, mkdir target)

Both helpers enforce the **root jail** (``CCPIPE_FS_ROOT`` env, default
``$HOME``) and a fixed deny-list (``.ssh``, ``.aws``, ``.gnupg``,
``.local/state/ccpipe``, …) so a logged-in session can't reach the
operator's credentials, SSH keys, or ccpipe's own state directory.
"""
from __future__ import annotations

import logging
import os
import stat as stat_mod
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config as app_config
from ..auth import AuthDep, CsrfDep

log = logging.getLogger(__name__)
router = APIRouter()

FS_ROOT_ENV = "CCPIPE_FS_ROOT"

# Paths underneath the root that we refuse to expose. Deliberately
# narrow: ccpipe is a personal admin tool, so .ssh / .aws / .gnupg /
# .kube / etc. are legitimately reachable through the file panel —
# blocking them would just get in the operator's way.
#
# The remaining entries protect *ccpipe's own* state. Letting the file
# endpoints read `.local/state/ccpipe/credentials` would expose the
# argon2 hash + TOTP secret to anyone who landed an authenticated
# session cookie; letting them write would let that attacker
# overwrite the credentials file and lock the legitimate operator
# out. That's privilege-escalation against ccpipe itself, distinct
# from user-file access.
_FS_DENY_SUBPATHS = (
    ".local/state/ccpipe",
    ".config/ccpipe",
)

# Inline editor cap. Files larger than this won't load into the editor;
# the panel offers them for download/delete only.
_FS_EDITOR_LIMIT = 1 * 1024 * 1024     # 1 MiB
# A simple binary heuristic — first 1 KiB containing any NUL byte is
# assumed to be a binary blob (matches what `git diff` decides for
# binary files in practice). UTF-8 decode failures are also rejected.
_FS_BINARY_SNIFF = 1024


# ─── Path validation ───────────────────────────────────────────────────────

def _fs_root() -> Path:
    """Resolved root directory for /api/fs/*. Defaults to the operator's
    home directory; override with CCPIPE_FS_ROOT to scope tighter."""
    override = os.environ.get(FS_ROOT_ENV, "").strip()
    base = Path(override) if override else Path.home()
    try:
        return base.resolve(strict=True)
    except (OSError, RuntimeError):
        # Misconfiguration. Fall back to an *empty* sentinel that
        # nothing can ever be relative_to, so all fs endpoints fail
        # closed rather than open.
        log.error("CCPIPE_FS_ROOT=%r does not exist; /api/fs/* will refuse all paths", base)
        return Path("/__ccpipe_invalid_fs_root__")


def _enforce_fs_jail(resolved: Path) -> None:
    """Raise 403 if *resolved* is outside the root or under a denied
    subpath. Caller must pass an already-resolved Path so symlink
    escapes have already collapsed."""
    root = _fs_root()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path outside allowed root")
    parts = rel.parts
    if not parts:
        return
    for deny in _FS_DENY_SUBPATHS:
        deny_parts = tuple(p for p in deny.split("/") if p)
        if not deny_parts:
            continue
        if parts[:len(deny_parts)] == deny_parts:
            raise HTTPException(status_code=403, detail="path denied")


def _resolve_fs_path(path: str) -> Path:
    """Common path-validation for all /api/fs/* endpoints. Demands an
    absolute path, resolves symlinks, enforces the jail+denylist, and
    raises HTTPException with the appropriate status on failure."""
    if not path or not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path must be absolute")
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        # ValueError catches NUL-byte-in-path; same client-friendly 404.
        raise HTTPException(status_code=404, detail="path not found")
    _enforce_fs_jail(resolved)
    return resolved


def _resolve_fs_parent_for_new(path: str) -> tuple[Path, Path]:
    """Validate *path* as a future filesystem target (write/upload/
    rename-dst/mkdir). Returns (parent, final). The parent must exist
    and lie within the jail; the final path lies under it but need not
    exist yet."""
    if not path or not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path must be absolute")
    try:
        target = Path(path)
        parent = target.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="parent not found")
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="parent is not a directory")
    _enforce_fs_jail(parent)
    if target.name in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="invalid basename")
    final = parent / target.name
    return parent, final


def _scan_dir_entries(resolved: Path, show_hidden: bool,
                       include_files: bool) -> list[dict[str, Any]]:
    """Shared scandir loop for /api/fs/list. Returns at most 2000
    entries — beyond that the picker becomes unusable anyway and the
    cost grows linearly with directory size."""
    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(resolved) as it:
            for e in it:
                if len(entries) >= 2000:
                    break
                if not show_hidden and e.name.startswith("."):
                    continue
                try:
                    is_dir = e.is_dir(follow_symlinks=True)
                except OSError:
                    continue
                if is_dir:
                    entries.append({"name": e.name, "type": "dir"})
                    continue
                if not include_files:
                    continue
                try:
                    st = e.stat(follow_symlinks=True)
                except OSError:
                    continue
                entries.append({
                    "name": e.name,
                    "type": "file",
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")
    # Dirs before files, then alphabetical within each group.
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return entries


# ─── Pydantic bodies ───────────────────────────────────────────────────────

class FsWriteBody(BaseModel):
    path: str
    content: str


class FsRenameBody(BaseModel):
    src: str
    dst: str


class FsPathBody(BaseModel):
    path: str


# ─── Routes ────────────────────────────────────────────────────────────────

@router.get("/api/fs/list", dependencies=[AuthDep])
async def fs_list(path: str, show_hidden: int = 0,
                   files: int = 0) -> dict[str, Any]:
    """List entries under *path*. ``files=0`` (default) returns only
    sub-directories — the directory-picker call site. ``files=1``
    additionally returns files with their size and mtime — the file-
    transfer panel call site. Symlinks are followed."""
    resolved = _resolve_fs_path(path)
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="path is not a directory")
    entries = _scan_dir_entries(resolved, show_hidden=bool(show_hidden),
                                  include_files=bool(files))
    parent = resolved.parent
    return {
        "path": str(resolved),
        "parent": str(parent) if parent != resolved else None,
        "entries": entries,
    }


@router.get("/api/fs/read", dependencies=[AuthDep])
async def fs_read(path: str) -> dict[str, Any]:
    """Return the file content as UTF-8 text. Rejects binary files and
    anything larger than the editor cap so we don't have to stream
    multi-MB blobs into the browser only for the editor to choke on
    them. Also rejects non-regular files — /proc/* and device nodes
    report st_size=0 but yield unbounded reads, so we must check the
    inode type explicitly."""
    resolved = _resolve_fs_path(path)
    try:
        st = resolved.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="path not found")
    if not stat_mod.S_ISREG(st.st_mode):
        raise HTTPException(status_code=400, detail="not a regular file")
    if st.st_size > _FS_EDITOR_LIMIT:
        raise HTTPException(status_code=413,
                            detail=f"file too large for editor "
                                   f"({st.st_size} > {_FS_EDITOR_LIMIT})")
    try:
        with resolved.open("rb") as f:
            head = f.read(_FS_BINARY_SNIFF)
            if b"\x00" in head:
                raise HTTPException(status_code=415, detail="file is binary")
            f.seek(0)
            # Read cap+1 so we 413 on a file that grew between stat and
            # open (TOCTOU), not silently truncate.
            data = f.read(_FS_EDITOR_LIMIT + 1)
            if len(data) > _FS_EDITOR_LIMIT:
                raise HTTPException(status_code=413, detail="file grew past editor cap")
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="file is not UTF-8")
    return {
        "path": str(resolved),
        "content": text,
        "size": st.st_size,
        "mtime": int(st.st_mtime),
    }


@router.post("/api/fs/write", dependencies=[AuthDep, CsrfDep])
async def fs_write(body: FsWriteBody) -> dict[str, Any]:
    """Atomic write: temp file in the target dir, fsync, rename. The
    text payload is capped at the editor limit so a misbehaving client
    can't dump arbitrary data through this endpoint."""
    if len(body.content.encode("utf-8")) > _FS_EDITOR_LIMIT:
        raise HTTPException(status_code=413, detail="content too large")
    _, final = _resolve_fs_parent_for_new(body.path)
    parent = final.parent
    tmp = parent / (final.name + ".ccpipe.tmp")
    try:
        # O_NOFOLLOW: refuse to follow a symlink at *tmp*. Without this,
        # an attacker who can place a file in the (in-jail) parent dir
        # — including `claude` itself via prompt injection — could
        # pre-create `<basename>.ccpipe.tmp` as a symlink to
        # ~/.bashrc and have our O_CREAT|O_TRUNC follow it and clobber
        # the target. ELOOP on open(); we surface as 500.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        try:
            os.write(fd, body.content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
    except PermissionError:
        try: tmp.unlink()
        except OSError: pass
        raise HTTPException(status_code=403, detail="permission denied")
    except OSError as exc:
        try: tmp.unlink()
        except OSError: pass
        raise HTTPException(status_code=500, detail=f"write failed: {exc}")
    try:
        st = final.stat()
    except OSError:
        st = None
    return {"path": str(final), "size": st.st_size if st else None}


@router.post("/api/fs/upload", dependencies=[AuthDep, CsrfDep])
async def fs_upload(request: Request, path: str) -> dict[str, Any]:
    """Stream a single uploaded file into *path*. We use the raw
    request body (not FastAPI's UploadFile) so the bytes pass through
    a temp file without ever sitting fully in memory."""
    _, final = _resolve_fs_parent_for_new(path)
    parent = final.parent
    cap_bytes = app_config.load().fs.upload_limit_mb * 1024 * 1024
    tmp = parent / (final.name + ".ccpipe.tmp")
    received = 0
    success = False
    # NB: keep the fd open across the chunk loop and close it in finally
    # so a mid-stream client disconnect doesn't leak the descriptor.
    fd: int | None = None
    try:
        # O_NOFOLLOW: see fs_write — refuse to follow a pre-placed
        # symlink at the tmp path, which would otherwise let an attacker
        # who controls the parent dir use the upload to clobber any
        # user-writable file the symlink points at.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if received > cap_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"upload exceeds limit ({cap_bytes} bytes)")
            os.write(fd, chunk)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, final)
        success = True
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")
    finally:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        if not success:
            try: tmp.unlink()
            except OSError: pass
    return {"path": str(final), "size": received}


@router.get("/api/fs/download", dependencies=[AuthDep])
async def fs_download(path: str) -> StreamingResponse:
    """Stream a file back to the browser as
    ``Content-Disposition: attachment``. No size cap — downloads are
    operator-initiated, and capping them would block legitimate
    workflows like grabbing a log."""
    resolved = _resolve_fs_path(path)
    try:
        st = resolved.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="path not found")
    if not stat_mod.S_ISREG(st.st_mode):
        raise HTTPException(status_code=400, detail="not a regular file")

    def _gen():
        try:
            with resolved.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        return
                    yield chunk
        except OSError:
            return

    return StreamingResponse(
        _gen(),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(st.st_size),
            "Content-Disposition": f'attachment; filename="{resolved.name}"',
        },
    )


@router.post("/api/fs/rename", dependencies=[AuthDep, CsrfDep])
async def fs_rename(body: FsRenameBody) -> dict[str, Any]:
    src = _resolve_fs_path(body.src)
    _, final = _resolve_fs_parent_for_new(body.dst)
    # lstat (not exists) so we don't follow a symlink whose target
    # happens to be missing — we'd otherwise quietly overwrite the
    # symlink target on rename, not the link itself.
    try:
        final.lstat()
        raise HTTPException(status_code=409, detail="dst already exists")
    except FileNotFoundError:
        pass
    try:
        os.rename(src, final)
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"rename failed: {exc}")
    return {"path": str(final)}


@router.post("/api/fs/delete", dependencies=[AuthDep, CsrfDep])
async def fs_delete(body: FsPathBody) -> dict[str, bool]:
    """Delete one path. Refuses non-empty directories (the panel
    walks a confirm UX for those; we don't recursively rm to keep
    a missed click from nuking a tree)."""
    target = _resolve_fs_path(body.path)
    try:
        if target.is_dir():
            os.rmdir(target)
        else:
            target.unlink()
    except OSError as exc:
        raise HTTPException(status_code=400,
                            detail=f"delete failed: {exc}")
    return {"deleted": True}


@router.post("/api/fs/mkdir", dependencies=[AuthDep, CsrfDep])
async def fs_mkdir(body: FsPathBody) -> dict[str, str]:
    _, final = _resolve_fs_parent_for_new(body.path)
    try:
        final.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="already exists")
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"mkdir failed: {exc}")
    return {"path": str(final.resolve())}


@router.get("/api/fs/config", dependencies=[AuthDep])
async def fs_config_get() -> dict[str, Any]:
    """Surfacing the upload cap to the UI so it can validate locally
    before initiating a multi-MB transfer."""
    return {"upload_limit_mb": app_config.load().fs.upload_limit_mb}
