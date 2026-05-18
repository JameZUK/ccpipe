"""Read-only diagnostics endpoints.

These exist so the operator can introspect a running ccpipe without
restarting it or grepping the journal. The first useful surface is
live per-WS byte counters: each open ``handle_terminal_ws`` registers
its ``WsCounters`` in ``ws._active_counters``, and this route flattens
that list to JSON for human inspection.

Auth-gated like everything else under /api/*. Read-only — no
state-changing operations live here.
"""
from __future__ import annotations

import time

from fastapi import APIRouter

from ..auth import AuthDep
from ..ws import _active_counters

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
