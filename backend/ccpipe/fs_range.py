"""HTTP byte ranges for files served from an already-open descriptor.

A range request asks for part of a file (``Range: bytes=1000-1999``) and
the server answers ``206 Partial Content``. Two things need it here:

  - **downloads on a bad connection**: a browser can resume a download
    that broke off, and ccpipe's own downloader fetches big files in
    pieces and retries only the piece that failed;
  - **audio and video**: players seek by asking for the bytes at the new
    position, and Safari won't play media from a server without ranges.

Only a single range is served; a multi-range request gets the whole file
(allowed by RFC 9110 §14.2). ``If-Range`` is honoured, so a resume never
splices pieces of two different versions of a file together: if the file
changed since the first piece, the client is sent the whole new file.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterator
from email.utils import formatdate

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

_CHUNK = 64 * 1024
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)


class _FdBody:
    """The response body: *length* bytes of *fd* from *start*. Closes the
    descriptor once sent, or when the response is dropped unsent (a client
    that hung up before the first byte), exactly once either way."""

    def __init__(self, fd: int, start: int, length: int) -> None:
        self.fd: int | None = fd
        self.start, self.length = start, length

    def __iter__(self) -> Iterator[bytes]:
        try:
            pos, left = self.start, self.length
            while left > 0 and self.fd is not None:
                chunk = os.pread(self.fd, min(_CHUNK, left), pos)
                if not chunk:
                    return               # the file shrank under us
                pos += len(chunk)
                left -= len(chunk)
                yield chunk
        except OSError:
            return
        finally:
            self.close()

    def close(self) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __del__(self) -> None:
        self.close()


def etag(st: os.stat_result) -> str:
    """A validator that changes whenever the file's content can have."""
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}-{st.st_ino:x}"'


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """The byte range [start, end] (inclusive) asked for, None for "send
    the whole file", or "unsatisfiable" when the range starts past the
    end."""
    if not header:
        return None
    m = _RANGE_RE.match(header)
    if not m:
        return None                          # malformed or multi-range: whole file
    first, last = m.group(1), m.group(2)
    if first == "" and last == "":
        return None
    if first == "":                          # suffix: the last N bytes
        n = int(last)
        if n == 0:
            return "unsatisfiable"
        return (max(0, size - n), size - 1) if size else "unsatisfiable"
    start = int(first)
    end = int(last) if last != "" else size - 1
    if start >= size or end < start:
        return "unsatisfiable"
    return start, min(end, size - 1)


def _if_range_ok(request: Request, st: os.stat_result, tag: str, last_modified: str) -> bool:
    v = request.headers.get("if-range")
    if v is None:
        return True
    v = v.strip()
    return v == tag if v.startswith(('"', "W/")) else v == last_modified


def ranged_response(request: Request, fd: int, st: os.stat_result,
                    media_type: str, headers: dict[str, str]) -> Response:
    """Serve the open file *fd* (stat *st*), honouring Range. Takes
    ownership of *fd*: it's closed when the body has been sent, or now
    if there is no body."""
    size = st.st_size
    tag = etag(st)
    last_modified = formatdate(st.st_mtime, usegmt=True)
    base = {**headers, "Accept-Ranges": "bytes", "ETag": tag, "Last-Modified": last_modified}

    want = parse_range(request.headers.get("range"), size)
    if want is not None and not _if_range_ok(request, st, tag, last_modified):
        want = None
    if want == "unsatisfiable":
        os.close(fd)
        return Response(status_code=416, headers={**base, "Content-Range": f"bytes */{size}"})
    if want is None:
        start, end, status = 0, size - 1, 200
    else:
        assert isinstance(want, tuple)
        start, end = want
        status = 206
        base["Content-Range"] = f"bytes {start}-{end}/{size}"
    length = max(0, end - start + 1)
    base["Content-Length"] = str(length)

    body = _FdBody(fd, start, length)
    return StreamingResponse(body, status_code=status, media_type=media_type, headers=base)
