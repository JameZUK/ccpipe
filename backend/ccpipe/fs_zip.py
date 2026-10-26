"""Download a directory as a .zip, streamed (no temp file).

Used by /api/fs/dir-summary and /api/fs/download-zip (routes/fs.py), which
resolve + jail the requested directory first. The walk itself then:

  - opens the directory without following symlinks at any component, and
    walks it by file descriptor (os.fwalk with dir_fd), so a symlink planted
    inside the tree — or an intermediate directory swapped for one mid-walk —
    can never pull in anything outside it;
  - skips symlinks, sockets, FIFOs and devices: only regular files and
    directories go in the archive;
  - skips anything under a deny-listed path (``is_denied``), so zipping
    $HOME can't hand out ~/.claude or ccpipe's own credentials;
  - leaves hidden files/dirs out unless asked for (.git, .venv, …).
"""
from __future__ import annotations

import errno
import logging
import os
import stat as stat_mod
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
# The summary is a quick pre-flight ("1,234 files, 45 MB") — bounded so a
# huge tree can't pin a worker; past the cap it reports a lower bound.
SUMMARY_MAX_ENTRIES = 200_000
SUMMARY_MAX_SECONDS = 4.0
# Already-compressed formats are stored, not deflated again (saves CPU, and
# deflate wouldn't shrink them anyway).
_STORED_EXT = frozenset("""
    zip gz tgz bz2 xz zst 7z rar jar war apk whl
    png jpg jpeg gif webp avif heic ico
    mp3 m4a aac ogg opus flac mp4 m4v mkv mov webm avi
    pdf woff woff2 docx xlsx pptx odt ods
""".split())


def open_dir_nofollow(parent_fd: int, leaf: str) -> int:
    """Open directory *leaf* under *parent_fd* without following a symlink."""
    return os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=parent_fd)


def _walk(top_fd: int, top: Path, include_hidden: bool,
          is_denied: Callable[[Path], bool]
          ) -> Iterator[tuple[str, list[str], list[tuple[str, os.stat_result]], int, bool]]:
    """Yield (relative dir, subdir names, [(file name, lstat)], dir fd,
    hidden) for every directory under *top_fd*, pruning hidden / denied /
    symlinked entries. ``hidden`` is True when the directory itself is (or
    is under) a hidden one — only possible with include_hidden."""
    for dirpath, dirnames, filenames, dfd in os.fwalk(".", dir_fd=top_fd, follow_symlinks=False):
        rel = os.path.normpath(dirpath)
        rel = "" if rel == "." else rel
        hidden_here = any(p.startswith(".") for p in Path(rel).parts)
        keep_dirs = []
        for d in dirnames:
            if (not include_hidden and d.startswith(".")) or is_denied(top / rel / d):
                continue
            try:
                st = os.stat(d, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            if stat_mod.S_ISDIR(st.st_mode):        # not a symlink to one
                keep_dirs.append(d)
        dirnames[:] = keep_dirs                     # prune the walk in place
        files = []
        for f in filenames:
            if (not include_hidden and f.startswith(".")) or is_denied(top / rel / f):
                continue
            try:
                st = os.stat(f, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            if stat_mod.S_ISREG(st.st_mode):
                files.append((f, st))
        yield rel, keep_dirs, files, dfd, hidden_here


def summarize(top_fd: int, top: Path, is_denied: Callable[[Path], bool]) -> dict[str, Any]:
    """Counts for the download dialog, hidden files reported separately."""
    out = {"files": 0, "bytes": 0, "hidden_files": 0, "hidden_bytes": 0, "truncated": False}
    deadline = time.monotonic() + SUMMARY_MAX_SECONDS
    seen = 0
    for rel, _dirs, files, _fd, hidden_dir in _walk(top_fd, top, True, is_denied):
        for name, st in files:
            hidden = hidden_dir or name.startswith(".")
            out["hidden_files" if hidden else "files"] += 1
            out["hidden_bytes" if hidden else "bytes"] += st.st_size
        seen += len(files) + 1
        if seen > SUMMARY_MAX_ENTRIES or time.monotonic() > deadline:
            out["truncated"] = True
            break
    return out


class _Sink:
    """Write-only file object that collects zipfile's output for streaming.
    No tell()/seek(): zipfile then writes data descriptors instead of
    seeking back to patch headers."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def write(self, b: bytes) -> int:
        self.parts.append(bytes(b))
        return len(b)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = b"".join(self.parts)
        self.parts.clear()
        return data


def _date_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    t = time.localtime(max(mtime, 315532800))       # zip can't go before 1980
    return (max(t.tm_year, 1980), t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def stream_zip(top_fd: int, top: Path, root_name: str, include_hidden: bool,
               is_denied: Callable[[Path], bool]) -> Iterator[bytes]:
    """Yield a zip of the tree under *top_fd*; entries live under *root_name*/.
    Owns *top_fd* and closes it when done (or abandoned)."""
    sink = _Sink()
    try:
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as zf:
            for rel, dirs, files, dfd, _hidden in _walk(top_fd, top, include_hidden, is_denied):
                base = f"{root_name}/{rel}/" if rel else f"{root_name}/"
                if not dirs and not files:
                    zf.writestr(zipfile.ZipInfo(base), b"")   # keep empty dirs
                for name, st in files:
                    try:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                                     | os.O_CLOEXEC, dir_fd=dfd)
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.ENOENT, errno.ELOOP, errno.EPERM):
                            log.info("zip: skipping %s/%s: %s", rel, name, exc)
                        continue
                    with os.fdopen(fd, "rb") as f:
                        fst = os.fstat(f.fileno())
                        if not stat_mod.S_ISREG(fst.st_mode):
                            continue
                        info = zipfile.ZipInfo(base + name, _date_time(fst.st_mtime))
                        info.external_attr = (fst.st_mode & 0o7777 | stat_mod.S_IFREG) << 16
                        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                        info.compress_type = (zipfile.ZIP_STORED if ext in _STORED_EXT
                                              else zipfile.ZIP_DEFLATED)
                        info.file_size = fst.st_size
                        try:
                            with zf.open(info, "w", force_zip64=fst.st_size > (1 << 30)) as dest:
                                while chunk := f.read(CHUNK):
                                    dest.write(chunk)
                                    if sink.parts:
                                        yield sink.take()
                        except (OSError, RuntimeError) as exc:
                            # Unreadable mid-way, or grew past what the entry
                            # header allowed. The archive is already
                            # partially written, so stop cleanly here.
                            log.warning("zip: aborted at %s/%s: %s", rel, name, exc)
                            return
                    if sink.parts:
                        yield sink.take()
        tail = sink.take()                          # central directory
        if tail:
            yield tail
    finally:
        os.close(top_fd)
