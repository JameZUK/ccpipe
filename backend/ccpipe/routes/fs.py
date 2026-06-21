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

import errno
import logging
import mimetypes
import os
import stat as stat_mod
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config as app_config
from ..auth import AuthDep, CsrfDep

log = logging.getLogger(__name__)
router = APIRouter()

FS_ROOT_ENV = "CCPIPE_FS_ROOT"


def _require_same_origin(request: Request) -> None:
    """Belt-and-braces gate against cross-origin credentialed GETs.

    CORS already blocks cross-origin reads of JSON / octet-stream
    responses and SameSite=Lax keeps the session cookie off cross-site
    subresource fetches, so today's main risk is a top-level
    navigation (``<a target=_top href='…/api/fs/download?path=…'>``)
    silently dropping a file into the operator's Downloads folder.
    Sec-Fetch-Site is set by every modern browser; rejecting anything
    that isn't ``same-origin`` closes that vector without affecting
    legitimate in-app calls (which always carry the header value
    ``same-origin``). Matches the gate on ``/api/tts/preview``."""
    sfs = request.headers.get("sec-fetch-site", "").lower()
    if sfs and sfs != "same-origin":
        raise HTTPException(status_code=403, detail="cross-origin blocked")


SameOriginDep = Depends(_require_same_origin)

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
    # ~/.claude is where Claude Code stores its transcripts
    # (~/.claude/projects/<encoded>/<uuid>.jsonl), live-session
    # bookkeeping (~/.claude/sessions/<pid>.json), keybindings,
    # and settings. Letting an authenticated client browse/write
    # there would let them rewrite the voice:pushToTalk binding
    # we depend on for /voice release (mic_stop writes \x1b k),
    # or plant a JSONL the TTS watcher will read and stream
    # through Kokoro as if claude said it. The TTS service uses
    # this directory directly (not through /api/fs/*) so denying
    # the fs route doesn't affect normal operation.
    ".claude",
    # ~/.claude.json is Claude Code's GLOBAL config — a sibling FILE of
    # the ~/.claude dir, not under it, so the prefix match above does
    # NOT cover it. It holds the `oauthAccount` identity block and ~50
    # config keys; letting an authenticated session read it leaks
    # account identity, and letting it write tampers with Claude Code's
    # config. The live OAuth token lives in ~/.claude/.credentials.json
    # (already covered by the `.claude` entry above).
    ".claude.json",
)

def content_disposition_attachment(name: str) -> str:
    """Build a safe ``Content-Disposition: attachment`` value for *name*.

    Filenames on disk can contain quotes, CR/LF, and non-ASCII codepoints.
    Interpolating them straight into ``filename="..."`` would produce a
    malformed header (best case) or allow header injection on a non-
    validating ASGI stack (worst case). We emit both:

      - a sanitised ASCII fallback in ``filename=``, with control chars
        + ``"\\`` mapped to ``_``,
      - the original name as ``filename*=UTF-8''<percent-encoded>`` per
        RFC 5987 so modern clients still see the real name.
    """
    safe_ascii = "".join(
        c if (0x20 <= ord(c) < 0x7F and c not in '"\\') else "_"
        for c in name
    ) or "download"
    return (f'attachment; filename="{safe_ascii}"; '
            f"filename*=UTF-8''{quote(name, safe='')}")


# Inline editor cap. Files larger than this won't load into the editor;
# the panel offers them for download/delete only.
_FS_EDITOR_LIMIT = 1 * 1024 * 1024     # 1 MiB
# A simple binary heuristic — first 1 KiB containing any NUL byte is
# assumed to be a binary blob (matches what `git diff` decides for
# binary files in practice). UTF-8 decode failures are also rejected.
_FS_BINARY_SNIFF = 1024

# Markdown index (the toolbar "Docs" dropdown). A bounded walk of the
# project root for *.md / *.markdown so the list can't blow up on a huge
# tree, and the walk stays cheap by pruning VCS / build / dependency dirs
# in place. Hidden dirs are pruned too, which also keeps the walk clear of
# the deny-listed state dirs (.claude, .local/state/ccpipe, …).
_FS_MD_INDEX_MAX_ENTRIES = 500
_FS_MD_INDEX_MAX_DEPTH = 8
# Cap on inline-served images (/api/fs/raw). Unlike /api/fs/download (which
# is operator-initiated and uncapped), raw is auto-fetched by the viewer for
# every ![](img) reference in a document, so an oversized image in a crafted
# doc shouldn't force a huge transfer. Inline preview images don't need to be
# large.
_FS_RAW_MAX_BYTES = 25 * 1024 * 1024     # 25 MiB
_FS_MD_SUFFIXES = (".md", ".markdown")
_FS_MD_INDEX_PRUNE = frozenset({
    "node_modules", "venv", "__pycache__", "dist", "build", "target",
    "vendor", ".cache", ".next", ".svelte-kit", ".mypy_cache",
    ".pytest_cache", ".tox", ".git",
})


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
    # Enforce the jail/denylist on the FINAL target too, not just the
    # parent: otherwise a denied *leaf* name (e.g. creating ~/.claude.json
    # or ~/.claude when it doesn't yet exist) slips through because its
    # parent (~) is allowed. The deny model must cover the thing being
    # created/written, not only where it lives.
    _enforce_fs_jail(final)
    return parent, final


def _walk_parent_nofollow(absolute_path: str) -> tuple[int, str]:
    """Open every intermediate component of *absolute_path* with
    ``O_NOFOLLOW`` and return ``(parent_fd, leaf_name)``.

    Closes the M1 TOCTOU window left open by ``resolve()`` +
    ``open(..., O_NOFOLLOW)``: plain ``O_NOFOLLOW`` only refuses to
    follow a symlink at the **final** component, so if a concurrent
    writer with access inside the jail (notably ``claude`` itself via
    prompt injection) swaps an **intermediate** directory for a symlink
    pointing outside the jail between canonicalisation and open, the
    kernel re-walks the path on ``open()`` and reads/writes the wrong
    file. Walking component-by-component with ``openat(...,
    O_NOFOLLOW)`` makes any such swap fail with ``ELOOP``.

    Portability: uses only POSIX flags (no Linux-only ``O_PATH``), so
    it works identically on Linux and macOS. ``os.O_NOFOLLOW`` and
    ``os.O_DIRECTORY`` resolve to the platform-correct numeric values
    at runtime; ``os.open(..., dir_fd=)`` maps to ``openat(2)`` on both.

    The caller is responsible for ``os.close(parent_fd)`` once the
    leaf open has been performed (or skipped due to error). On error
    the fd is closed before the exception propagates.
    """
    parts = Path(absolute_path).parts
    if not parts or parts[0] != "/":
        # Defensive: callers go through _resolve_fs_path / _resolve_fs_parent_for_new,
        # both of which already enforce absolute paths. This is belt-and-braces.
        raise HTTPException(status_code=400, detail="path must be absolute")
    if len(parts) == 1:
        raise HTTPException(status_code=400, detail="cannot operate on /")

    cur_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[1:-1]:
            try:
                # NOTE: O_DIRECTORY is intentionally NOT set here. On
                # Linux, `O_NOFOLLOW | O_DIRECTORY` on a symlink-to-dir
                # returns ENOTDIR (the kernel evaluates O_DIRECTORY
                # against the unfollowed symlink), masking the attack
                # signal we care about. macOS may differ. Without
                # O_DIRECTORY, a symlink intermediate returns the clean
                # ELOOP we map to 403; a regular-file intermediate (a
                # bogus path like /etc/passwd/foo) returns ENOTDIR on
                # the *next* iteration's openat against a file fd, so
                # safety is preserved either way.
                nxt = os.open(
                    part,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=cur_fd,
                )
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise HTTPException(status_code=403, detail="symlink in path")
                if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                    raise HTTPException(status_code=404, detail="path not found")
                if exc.errno == errno.EACCES:
                    raise HTTPException(status_code=403, detail="permission denied")
                raise HTTPException(status_code=500, detail=f"path walk failed: {exc}")
            os.close(cur_fd)
            cur_fd = nxt
        return cur_fd, parts[-1]
    except BaseException:
        os.close(cur_fd)
        raise


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
                # Dir-only mode (the picker): use scandir's d_type via
                # e.is_dir(follow_symlinks=False) which on ext4/btrfs/
                # tmpfs is a NO-syscall check — and skip non-dirs
                # without ever stat()ing them. For a directory full of
                # source files this halves the syscall load again.
                if not include_files:
                    try:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                # One stat call per entry, with the type derived from
                # st_mode — previously is_dir(...) + stat(...) made
                # 2 syscalls per file. On a 2000-entry directory that's
                # 4000 syscalls cut to 2000.
                try:
                    st = e.stat(follow_symlinks=True)
                except OSError:
                    continue
                if stat_mod.S_ISDIR(st.st_mode):
                    entries.append({"name": e.name, "type": "dir"})
                    continue
                if not include_files:
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

@router.get("/api/fs/list", dependencies=[AuthDep, SameOriginDep])
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


@router.get("/api/fs/markdown-index", dependencies=[AuthDep, SameOriginDep])
async def fs_markdown_index(root: str) -> dict[str, Any]:
    """Return every Markdown file under *root* (the session's project
    directory) as ``{name, path, rel}`` sorted by relative path — the
    data source for the toolbar "Docs" dropdown. The walk is bounded in
    depth and entry count, does not follow directory symlinks, and prunes
    hidden + known-heavy dirs in place so it stays cheap on a large tree.
    The deny-list is enforced explicitly on every returned path (not left
    to the incidental hidden-dir prune), and symlinked files are skipped,
    so the index can never surface a path the rest of /api/fs/* refuses."""
    resolved = _resolve_fs_path(root)
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="root is not a directory")
    root_str = str(resolved)
    out: list[dict[str, Any]] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        depth = dirpath[len(root_str):].count(os.sep)
        if depth >= _FS_MD_INDEX_MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in _FS_MD_INDEX_PRUNE]
        for fn in filenames:
            if not fn.lower().endswith(_FS_MD_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            # Don't surface symlinked "docs": their path is inside the jail
            # but the target may not be — /api/fs/read would 403 on open, so
            # listing them just yields broken entries (and leaks the link's
            # existence). followlinks=False already excludes symlinked dirs.
            try:
                if stat_mod.S_ISLNK(os.lstat(full).st_mode):
                    continue
            except OSError:
                continue
            # Enforce the jail + deny-list on the leaf itself rather than
            # relying on the hidden-dir prune to coincide with the deny-list.
            try:
                _enforce_fs_jail(Path(full))
            except HTTPException:
                continue
            out.append({
                "name": fn,
                "path": full,
                "rel": os.path.relpath(full, root_str),
            })
            if len(out) >= _FS_MD_INDEX_MAX_ENTRIES:
                truncated = True
                break
        if truncated:
            break
    out.sort(key=lambda e: e["rel"].lower())
    return {"root": root_str, "entries": out, "truncated": truncated}


@router.get("/api/fs/read", dependencies=[AuthDep, SameOriginDep])
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
    # O_NOFOLLOW at the leaf plus a per-component O_NOFOLLOW walk of the
    # intermediates (via _walk_parent_nofollow). Together these close
    # the M1 TOCTOU window: an attacker who can write inside the jail
    # cannot swap any segment of the canonical path for a symlink
    # between _resolve_fs_path and this open(). ELOOP from either layer
    # surfaces as 403.
    parent_fd, leaf = _walk_parent_nofollow(str(resolved))
    try:
        try:
            fd = os.open(
                leaf,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
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
    try:
        with os.fdopen(fd, "rb") as f:
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


@router.get("/api/fs/stat", dependencies=[AuthDep, SameOriginDep])
async def fs_stat(path: str) -> dict[str, Any]:
    """Cheap metadata probe (size + float mtime) for one file. The
    Markdown viewer polls this to detect on-disk edits (from the editor,
    from ``claude``, from anything) without re-fetching the whole file
    each tick. ``mtime`` is a float so two saves in the same wall-second
    still register. Path safety matches /api/fs/read."""
    resolved = _resolve_fs_path(path)
    try:
        st = resolved.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="path not found")
    if not stat_mod.S_ISREG(st.st_mode):
        raise HTTPException(status_code=400, detail="not a regular file")
    return {"path": str(resolved), "size": st.st_size, "mtime": st.st_mtime}


@router.post("/api/fs/write", dependencies=[AuthDep, CsrfDep])
async def fs_write(body: FsWriteBody) -> dict[str, Any]:
    """Atomic write: temp file in the target dir, fsync, rename. The
    text payload is capped at the editor limit so a misbehaving client
    can't dump arbitrary data through this endpoint."""
    if len(body.content.encode("utf-8")) > _FS_EDITOR_LIMIT:
        raise HTTPException(status_code=413, detail="content too large")
    _, final = _resolve_fs_parent_for_new(body.path)
    tmp_name = final.name + ".ccpipe.tmp"
    # Walk the parent with per-component O_NOFOLLOW (M1 fix) so an
    # intermediate-directory swap to a symlink between resolve and
    # open is caught. The tmp + replace dance then runs entirely
    # via *dir_fd*, so the kernel never re-walks the path string
    # again after the canonical walk completed.
    parent_fd, _leaf = _walk_parent_nofollow(str(final))
    try:
        try:
            # O_NOFOLLOW at the leaf: refuse to follow a symlink at *tmp*.
            # Without this, an attacker who can place a file in the parent
            # dir could pre-create `<basename>.ccpipe.tmp` as a symlink to
            # ~/.bashrc and have our O_CREAT|O_TRUNC follow it and clobber
            # the target.
            fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o644,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise HTTPException(status_code=403, detail="symlink at target")
            if exc.errno == errno.EACCES:
                raise HTTPException(status_code=403, detail="permission denied")
            raise HTTPException(status_code=500, detail=f"write failed: {exc}")
        try:
            try:
                os.write(fd, body.content.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            # renameat: the rename happens inside the validated parent fd,
            # not by re-walking the path string.
            os.replace(tmp_name, final.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except PermissionError:
            try: os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError: pass
            raise HTTPException(status_code=403, detail="permission denied")
        except OSError as exc:
            try: os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError: pass
            raise HTTPException(status_code=500, detail=f"write failed: {exc}")
    finally:
        os.close(parent_fd)
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
    cap_bytes = app_config.load().fs.upload_limit_mb * 1024 * 1024
    tmp_name = final.name + ".ccpipe.tmp"
    received = 0
    success = False
    # NB: keep the fd open across the chunk loop and close it in finally
    # so a mid-stream client disconnect doesn't leak the descriptor.
    fd: int | None = None
    # Walk the parent with per-component O_NOFOLLOW (M1 fix). All
    # subsequent file ops use the resulting dir_fd, so the kernel
    # never re-walks the path string after we validated it.
    parent_fd, _leaf = _walk_parent_nofollow(str(final))
    try:
        try:
            # O_NOFOLLOW at the leaf: see fs_write — refuse to follow a
            # pre-placed symlink at the tmp path.
            fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o644,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise HTTPException(status_code=403, detail="symlink at target")
            if exc.errno == errno.EACCES:
                raise HTTPException(status_code=403, detail="permission denied")
            raise HTTPException(status_code=500, detail=f"upload failed: {exc}")
        try:
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
            os.replace(tmp_name, final.name,
                       src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            success = True
        except PermissionError:
            raise HTTPException(status_code=403, detail="permission denied")
    finally:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        if not success:
            try: os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError: pass
        os.close(parent_fd)
    return {"path": str(final), "size": received}


@router.get("/api/fs/download", dependencies=[AuthDep, SameOriginDep])
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

    # M1 fix: walk + open BEFORE returning the StreamingResponse, so an
    # ELOOP at any intermediate component (or the leaf) surfaces as a
    # 403 status — not as a silent empty body inside the generator. The
    # generator below owns the fd and closes it via os.fdopen's context.
    parent_fd, leaf = _walk_parent_nofollow(str(resolved))
    try:
        try:
            fd = os.open(
                leaf,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise HTTPException(status_code=403, detail="symlink at target")
            if exc.errno == errno.ENOENT:
                raise HTTPException(status_code=404, detail="path not found")
            if exc.errno == errno.EACCES:
                raise HTTPException(status_code=403, detail="permission denied")
            raise HTTPException(status_code=500, detail=f"download failed: {exc}")
    finally:
        os.close(parent_fd)

    def _gen():
        try:
            with os.fdopen(fd, "rb") as f:
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
            "Content-Disposition": content_disposition_attachment(resolved.name),
        },
    )


@router.get("/api/fs/raw", dependencies=[AuthDep, SameOriginDep])
async def fs_raw(path: str) -> StreamingResponse:
    """Serve a file **inline** with its sniffed Content-Type, restricted
    to ``image/*``. This backs relative ``![](./img.png)`` references in
    the Markdown viewer. Capping to images means this can never serve an
    HTML/JS/SVG file inline (an XSS vector that ``download``'s
    ``attachment`` disposition otherwise neutralises); anything else gets
    415. Path safety mirrors ``/api/fs/download`` exactly."""
    resolved = _resolve_fs_path(path)
    try:
        st = resolved.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="path not found")
    if not stat_mod.S_ISREG(st.st_mode):
        raise HTTPException(status_code=400, detail="not a regular file")
    if st.st_size > _FS_RAW_MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"image too large for inline render "
                                   f"({st.st_size} > {_FS_RAW_MAX_BYTES})")
    # SVG is an image MIME but can carry inline script, so exclude it —
    # raster images only. The viewer's CSP forbids script execution
    # anyway, but defence in depth is cheap here.
    ctype, _enc = mimetypes.guess_type(resolved.name)
    if not ctype or not ctype.startswith("image/") or ctype == "image/svg+xml":
        raise HTTPException(status_code=415, detail="not a raster image")

    parent_fd, leaf = _walk_parent_nofollow(str(resolved))
    try:
        try:
            fd = os.open(
                leaf,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
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

    def _gen():
        try:
            with os.fdopen(fd, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        return
                    yield chunk
        except OSError:
            return

    return StreamingResponse(
        _gen(),
        media_type=ctype,
        headers={
            "Content-Length": str(st.st_size),
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            # Short private cache so the viewer's live re-renders reuse the
            # already-fetched image (no flicker) instead of re-downloading
            # it every time the document text changes. A changed image is
            # at most this stale.
            "Cache-Control": "private, max-age=30",
        },
    )


@router.post("/api/fs/rename", dependencies=[AuthDep, CsrfDep])
async def fs_rename(body: FsRenameBody) -> dict[str, Any]:
    src = _resolve_fs_path(body.src)
    _, final = _resolve_fs_parent_for_new(body.dst)
    # Close the intermediate-symlink TOCTOU the same way read/write/
    # upload/download do (M1 fix): walk both parents with per-component
    # O_NOFOLLOW and perform the rename via *dir_fd* so the kernel never
    # re-walks the path strings after canonicalisation. Without this a
    # same-UID concurrent writer (notably `claude` under prompt
    # injection) could swap an intermediate dir for an out-of-jail
    # symlink between resolve() and os.rename().
    src_parent_fd, src_leaf = _walk_parent_nofollow(str(src))
    try:
        dst_parent_fd, dst_leaf = _walk_parent_nofollow(str(final))
        try:
            # lstat (follow_symlinks=False) so we don't follow a symlink
            # whose target happens to be missing — we'd otherwise quietly
            # overwrite the symlink target on rename, not the link itself.
            try:
                os.stat(dst_leaf, dir_fd=dst_parent_fd, follow_symlinks=False)
                raise HTTPException(status_code=409, detail="dst already exists")
            except FileNotFoundError:
                pass
            try:
                os.rename(src_leaf, dst_leaf,
                          src_dir_fd=src_parent_fd, dst_dir_fd=dst_parent_fd)
            except PermissionError:
                raise HTTPException(status_code=403, detail="permission denied")
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"rename failed: {exc}")
        finally:
            os.close(dst_parent_fd)
    finally:
        os.close(src_parent_fd)
    return {"path": str(final)}


@router.post("/api/fs/delete", dependencies=[AuthDep, CsrfDep])
async def fs_delete(body: FsPathBody) -> dict[str, bool]:
    """Delete one path. Refuses non-empty directories (the panel
    walks a confirm UX for those; we don't recursively rm to keep
    a missed click from nuking a tree)."""
    target = _resolve_fs_path(body.path)
    # Same M1 nofollow walk + dir_fd-relative syscall as rename/write so
    # an intermediate-directory symlink swap between resolve() and the
    # unlink/rmdir can't redirect the delete outside the jail.
    parent_fd, leaf = _walk_parent_nofollow(str(target))
    try:
        try:
            st = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if stat_mod.S_ISDIR(st.st_mode):
                os.rmdir(leaf, dir_fd=parent_fd)
            else:
                os.unlink(leaf, dir_fd=parent_fd)
        except OSError as exc:
            raise HTTPException(status_code=400,
                                detail=f"delete failed: {exc}")
    finally:
        os.close(parent_fd)
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


@router.get("/api/fs/config", dependencies=[AuthDep, SameOriginDep])
async def fs_config_get() -> dict[str, Any]:
    """Surfacing the upload cap + resolved fs root to the UI.

    The root lets the file panel default to a path inside the jail
    rather than hardcoding ``/home`` (which is the parent of the
    default root and so gets rejected with 403 every time the user
    opens the panel)."""
    return {
        "upload_limit_mb": app_config.load().fs.upload_limit_mb,
        "root": str(_fs_root()),
    }
