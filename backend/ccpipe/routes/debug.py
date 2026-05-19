"""Diagnostics endpoints.

These exist so the operator can introspect a running ccpipe without
restarting it or grepping the journal. The first useful surface is
live per-WS byte counters: each open ``handle_terminal_ws`` registers
its ``WsCounters`` in ``ws._active_counters``, and this route flattens
that list to JSON for human inspection.

Auth-gated like everything else under /api/*. The snapshot endpoint
accepts a frontend report so we can correlate client-side state with
the backend's live counters in a single log line.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import tmux
from ..auth import AuthDep, CsrfDep
from ..ws import _active_counters

# Sanity caps for the frontend snapshot body. The schema is free-form
# so a misbehaving page (e.g. an XSS in a content modal) couldn't pipe
# arbitrary-sized blobs into the operator's journal; this gates that.
_SNAPSHOT_MAX_PAYLOAD_BYTES = 256 * 1024     # ~quarter MiB serialized
_SNAPSHOT_MAX_NOTE_BYTES = 1024

# Tmux capture-pane caps for the diagnostic content-diff. Match the
# frontend dumpBuffer() default (500) and the xterm scrollback cap
# (10_000) so we never request more than xterm could surface.
_DIFF_CAPTURE_TIMEOUT_S = 2.0
_DIFF_MAX_LINES = 10_000


async def _capture_pane_plain(session: str, lines: int) -> list[str] | None:
    """Run `tmux capture-pane -p -t <session> -S -<lines>` and return
    the plain-text lines. No -e flag: we want the *rendered* text so
    it lines up with xterm's translateToString(true) output for a
    line-by-line diff.

    Returns None on any failure or empty output (caller can render
    "backend capture failed" rather than a confusing empty diff).
    """
    if lines <= 0:
        return None
    n = min(max(1, int(lines)), _DIFF_MAX_LINES)
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux.TMUX_BIN, "capture-pane",
            "-t", session,
            "-p",                  # print to stdout
            "-S", f"-{n}",         # start N lines back from current
            # No -E: capture extends through visible bottom, so the
            # result is "the last N rendered lines tmux knows about".
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(),
                                         timeout=_DIFF_CAPTURE_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    if proc.returncode != 0 or not out:
        return None
    # Split into lines; tmux uses LF, no terminator on last line.
    return out.decode("utf-8", errors="replace").split("\n")


def _diff_buffers(
    frontend_tail: list[str], backend_tail: list[str],
) -> dict[str, Any]:
    """Align two line lists from the bottom and report how well they
    agree. Trailing-space-tolerant (xterm pads cells with spaces; tmux
    capture-pane may pad differently).

    Returns a dict with:
      lines_compared, matches, mismatches, mismatch_examples (first
      N mismatching indices with the divergent lines from each side).
    """
    front = [(line or "").rstrip() for line in frontend_tail]
    back = [(line or "").rstrip() for line in backend_tail]
    n = min(len(front), len(back))
    # Align from the bottom: front[-1] vs back[-1], etc. The TOP of
    # the comparison window is "how far back both can see".
    front_tail = front[-n:] if n else []
    back_tail = back[-n:] if n else []
    matches = 0
    mismatch_examples: list[dict[str, Any]] = []
    for i in range(n):
        if front_tail[i] == back_tail[i]:
            matches += 1
        elif len(mismatch_examples) < 5:
            mismatch_examples.append({
                "line_from_bottom": n - i,
                "frontend": front_tail[i],
                "backend": back_tail[i],
            })
    return {
        "lines_compared": n,
        "frontend_lines": len(front),
        "backend_lines": len(back),
        "matches": matches,
        "mismatches": n - matches,
        "mismatch_examples": mismatch_examples,
    }

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/debug/sessions", dependencies=[AuthDep])
async def list_active_ws_sessions() -> dict[str, object]:
    """Live snapshot of every open WS handler's byte-flow counters.

    Use this to verify in real time whether any session is dropping
    PTY bytes:

      - ``bytes_read_pty - bytes_sent_ws - bytes_lost == 0``  is the
        invariant; any drift means an in-flight chunk that the WS
        hasn't either sent or counted as lost yet (rare race during a
        single send, never lasts more than one chunk).
      - ``bytes_lost > 0`` is the unambiguous "this connection had to
        drop content" signal — typically a transient WS stall. The
        client should have auto-reconnected and re-captured the pane
        via ``capture-pane`` so the bytes are recoverable upstream.

    The close-time summary (``"ws closed: …"`` in the journal) emits
    the same fields once the connection ends, so this endpoint is the
    in-flight view of the same data.
    """
    now = time.monotonic()
    return {
        "sessions": [
            {
                "session": c.session,
                "duration_s": round(now - c.started_at, 1),
                "frames_forwarded": c.frames_forwarded,
                "bytes_read_pty": c.bytes_read_pty,
                "bytes_sent_ws": c.bytes_sent_ws,
                "bytes_lost": c.bytes_lost,
                "send_failures": c.send_failures,
            }
            for c in _active_counters
        ],
    }


class FrontendSnapshot(BaseModel):
    """Frontend-captured diagnostic snapshot.

    Posted from the browser when the user triggers the debug snapshot
    affordance (keyboard shortcut or Settings button) so we can
    correlate xterm-side state and counters with the backend's view of
    the same session at the same instant. Free-form ``payload`` field
    so we can extend the snapshot shape without rev-locking the
    server — the server's job here is just to bracket the report with
    the live ``WsCounters`` and log the pair atomically.
    """
    session: str = Field(default="", description="tmux session name")
    note: str = Field(default="", description="optional user-supplied label")
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/debug/snapshot", dependencies=[AuthDep, CsrfDep])
async def post_frontend_snapshot(body: FrontendSnapshot) -> dict[str, object]:
    """Log a frontend-captured diagnostic snapshot alongside the
    server's live counters for the same session.

    The combined record is emitted at INFO level so it survives the
    default journal retention. Returns the merged record so the
    client can render "what the server saw" in the same modal that
    captured the report.
    """
    # Cap payload + note sizes before doing anything else with them.
    # The note is reflected into the log line; the payload would be
    # too if uncapped.
    if len(body.note) > _SNAPSHOT_MAX_NOTE_BYTES:
        raise HTTPException(status_code=413, detail="note too large")
    try:
        payload_bytes = len(json.dumps(body.payload, default=str))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="payload not serialisable")
    if payload_bytes > _SNAPSHOT_MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    now = time.monotonic()
    matches = [c for c in _active_counters if c.session == body.session]
    backend: dict[str, object] = {}
    if matches:
        # If multiple WS handlers are open for the same session (rare
        # but possible during a fast reconnect), pick the most recently
        # started one — that's the connection the frontend is talking
        # through right now.
        c = max(matches, key=lambda c: c.started_at)
        backend = {
            "duration_s": round(now - c.started_at, 1),
            "frames_forwarded": c.frames_forwarded,
            "bytes_read_pty": c.bytes_read_pty,
            "bytes_sent_ws": c.bytes_sent_ws,
            "bytes_lost": c.bytes_lost,
            "send_failures": c.send_failures,
        }
    # Content diff: compare the frontend's xterm buffer tail against
    # what tmux's capture-pane reports for the same session at the
    # same instant. This catches a class of bugs the byte-counter
    # check misses — corrupted escape sequences, scrollback eviction
    # off-by-one, multi-byte codepoints split across frames — where
    # the wire was fine but the rendered content diverged.
    diff: dict[str, Any] | None = None
    try:
        front_tail = body.payload.get("buffer", {}).get("tail")
    except AttributeError:
        front_tail = None
    if body.session and isinstance(front_tail, list) and front_tail:
        backend_tail = await _capture_pane_plain(body.session, len(front_tail))
        if backend_tail is not None:
            diff = _diff_buffers([str(x) for x in front_tail], backend_tail)
    log.info("frontend snapshot: session=%s note=%r frontend=%s backend=%s diff=%s",
             body.session or "?", body.note, body.payload, backend, diff)
    return {
        "backend": backend,
        "active_sessions_for_name": len(matches),
        "content_diff": diff,
    }
