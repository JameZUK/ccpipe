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

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import AuthDep, CsrfDep
from ..ws import _active_counters

# Sanity caps for the frontend snapshot body. The schema is free-form
# so a misbehaving page (e.g. an XSS in a content modal) couldn't pipe
# arbitrary-sized blobs into the operator's journal; this gates that.
_SNAPSHOT_MAX_PAYLOAD_BYTES = 256 * 1024     # ~quarter MiB serialized
_SNAPSHOT_MAX_NOTE_BYTES = 1024

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
    log.info("frontend snapshot: session=%s note=%r frontend=%s backend=%s",
             body.session or "?", body.note, body.payload, backend)
    return {"backend": backend, "active_sessions_for_name": len(matches)}
