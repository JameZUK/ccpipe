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
import contextlib
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import tmux
from ..auth import AuthDep, CsrfDep
from ..tmux_control import CONTROL_SESSION_NAME
from ..ws import _active_counters

# Cap how many capture-pane subprocesses can run concurrently across
# all in-flight /api/debug/snapshot requests. Each capture is bounded
# at 2 s by the per-call timeout, so 2 concurrent + a 2 s wait is the
# worst the operator can pile up by mashing the snapshot button.
_SNAPSHOT_SUBPROCESS_SEMAPHORE = asyncio.Semaphore(2)

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


async def _capture_pane_plain(
    session: str, lines: int, alternate: bool = False,
) -> list[str] | None:
    """Run `tmux capture-pane -p -t <session> -S -<lines>` and return
    the plain-text lines. No -e flag: we want the *rendered* text so
    it lines up with xterm's translateToString(true) output for a
    line-by-line diff.

    ``alternate=True`` adds the `-a` flag so tmux returns the
    alternate screen instead of the main pane. Alt-screen TUIs
    (claude code, vim, less) keep their live UI here while the
    main screen accumulates scrollback; comparing the wrong screen
    against xterm's wrong buffer produces 90%+ false-positive
    mismatches.

    Returns None on any failure or empty output (caller can render
    "backend capture failed" rather than a confusing empty diff).
    """
    if lines <= 0:
        return None
    # Validate the session name before passing to tmux. argv-form
    # subprocess invocation already prevents shell injection, but a
    # bare `-flag` value would confuse tmux's argv parser and an
    # operator-readable session name (e.g. the internal control
    # session) shouldn't be accessible via this diagnostic surface.
    try:
        validated = tmux.safe_name(session)
    except ValueError:
        return None
    if validated == CONTROL_SESSION_NAME:
        return None
    n = min(max(1, int(lines)), _DIFF_MAX_LINES)
    cmd = [
        tmux.TMUX_BIN, "capture-pane",
        "-t", validated,
        "-p",                  # print to stdout
        "-S", f"-{n}",         # start N lines back from current
        # No -E: capture extends through visible bottom, so the
        # result is "the last N rendered lines tmux knows about".
    ]
    if alternate:
        cmd.append("-a")
    async with _SNAPSHOT_SUBPROCESS_SEMAPHORE:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(),
                                             timeout=_DIFF_CAPTURE_TIMEOUT_S)
        except asyncio.TimeoutError:
            # kill() AND wait() — without the wait() asyncio leaks the
            # Process transport / child-watcher state, which piles up
            # under repeat snapshot taps.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                await proc.wait()
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
    n = min(len(frontend_tail), len(backend_tail))
    # Slice + rstrip only the compared window — previously we rstripped
    # the entire input on both sides (up to 10K lines × 2) before
    # discarding all but the last n. On large buffers that's pure waste.
    front_tail = [(line or "").rstrip() for line in frontend_tail[-n:]] if n else []
    back_tail = [(line or "").rstrip() for line in backend_tail[-n:]] if n else []
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
        "frontend_lines": len(frontend_tail),
        "backend_lines": len(backend_tail),
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
    #
    # Schema-2 snapshots include both xterm buffers (normal + alt).
    # Compare like-to-like against tmux's two screens — alt-screen
    # TUIs (claude code, vim) switch between buffers constantly and
    # comparing across them just produces noise.
    diff: dict[str, Any] | None = None
    if body.session:
        buffers = body.payload.get("buffers") if isinstance(body.payload, dict) else None
        if isinstance(buffers, dict):
            active_type = str(buffers.get("activeType", ""))
            normal_tail = buffers.get("normal") or []
            alt_tail = buffers.get("alternate") or []
            normal_back = (
                await _capture_pane_plain(body.session, len(normal_tail) or 1, alternate=False)
                if isinstance(normal_tail, list) and normal_tail else None
            )
            alt_back = (
                await _capture_pane_plain(body.session, len(alt_tail) or 1, alternate=True)
                if isinstance(alt_tail, list) and alt_tail else None
            )
            diff = {
                "active_type": active_type,
                "normal": (
                    _diff_buffers([str(x) for x in normal_tail], normal_back)
                    if isinstance(normal_tail, list) and normal_back is not None else None
                ),
                "alternate": (
                    _diff_buffers([str(x) for x in alt_tail], alt_back)
                    if isinstance(alt_tail, list) and alt_back is not None else None
                ),
            }
        else:
            # Schema-1 fallback: legacy snapshots have only ``buffer.tail``
            # which is the active-buffer dump. Compare against whichever
            # screen tmux is currently presenting (the default, no -a).
            front_tail = (body.payload.get("buffer", {}) or {}).get("tail") \
                if isinstance(body.payload, dict) else None
            if isinstance(front_tail, list) and front_tail:
                back_tail = await _capture_pane_plain(body.session, len(front_tail))
                if back_tail is not None:
                    diff = {
                        "active_type": "unknown (schema-1)",
                        "normal": _diff_buffers([str(x) for x in front_tail], back_tail),
                        "alternate": None,
                    }
    # Sanitise the session string for the log line — Pydantic doesn't
    # constrain it, so a value containing literal "\n" would otherwise
    # forge a second log entry. The note is logged with %r which
    # already escapes; session uses %s for readability.
    safe_session = (body.session or "?").replace("\r", "").replace("\n", " ")[:64]
    log.info("frontend snapshot: session=%s note=%r frontend=%s backend=%s diff=%s",
             safe_session, body.note, body.payload, backend, diff)
    return {
        "backend": backend,
        "active_sessions_for_name": len(matches),
        "content_diff": diff,
    }
