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
import difflib
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import tmux
from ..auth import AuthDep, CsrfDep, SameOriginDep
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


async def _noop_capture() -> list[str] | None:
    """Awaitable that returns None immediately — used by the parallel
    capture path when one of the two screens doesn't need a capture
    so gather() still gets two awaitables and the destructuring lines
    up cleanly."""
    return None


async def _capture_pane_plain(
    session: str, lines: int, alternate: bool = False, join: bool = False,
    part: str = "all",
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
        "-t", tmux.pane_target(validated),
        "-p",                  # print to stdout
        "-S", f"-{n}",         # start N lines back from current
        # No -E: capture extends through visible bottom, so the
        # result is "the last N rendered lines tmux knows about".
    ]
    # part="history": scrollback only; part="screen": visible rows only.
    if part == "history":
        cmd += ["-E", "-1"]
    elif part == "screen":
        cmd[cmd.index("-S") + 1] = "0"

    if alternate:
        cmd.append("-a")
    if join:
        # Logical lines (schema-3 snapshots): join rows tmux wrapped, to
        # line up with the frontend's soft-wrap-joined dump.
        cmd.append("-J")
    async with _SNAPSHOT_SUBPROCESS_SEMAPHORE:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=tmux.tmux_env(),
            )
        except FileNotFoundError:
            return None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(),
                                             timeout=_DIFF_CAPTURE_TIMEOUT_S)
        except asyncio.TimeoutError:
            # kill() then BOUNDED wait — without the wait() asyncio
            # leaks the Process transport / child-watcher state, but
            # if the child is stuck in uninterruptible sleep
            # (D-state) a bare wait() can block forever while still
            # holding the snapshot-endpoint semaphore. Cap it.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(ProcessLookupError, asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            return None
    if proc.returncode != 0 or not out:
        return None
    # Split into lines. tmux ends the last line with LF too; drop the
    # empty string that leaves so the bottoms line up.
    text = out.decode("utf-8", errors="replace")
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


async def _pane_info(session: str) -> tuple[int, int]:
    """(status rows at the bottom of the client, pane history size).

    The frontend's xterm holds the whole client screen, status line
    included; capture-pane holds only the pane. Status rows are 0 when
    the status line is off, at the top, or can't be read.
    """
    try:
        validated = tmux.safe_name(session)
    except ValueError:
        return 0, 0
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux.TMUX_BIN, "display-message", "-p", "-t", tmux.pane_target(validated),
            "#{status}|#{status-position}|#{history_size}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=tmux.tmux_env(),
        )
    except FileNotFoundError:
        return 0, 0
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_DIFF_CAPTURE_TIMEOUT_S)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(ProcessLookupError, asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        return 0, 0
    status, position, history = (out.decode(errors="replace").strip().split("|") + ["", "", ""])[:3]
    rows = 0
    if position == "bottom":
        if status == "on":
            rows = 1
        elif status.isdigit() and 2 <= int(status) <= 5:
            rows = int(status)
    return rows, int(history) if history.isdigit() else 0


async def _capture_logical(session: str, lines: int, history_size: int) -> list[str] | None:
    """Schema-3 backend side: history as LOGICAL lines (-J), then the
    visible screen as rows. That's the shape xterm holds: the replay is
    -J lines xterm soft-wraps itself (joined again by the frontend dump),
    while tmux repaints the screen row by row, so screen rows are never
    soft-wrapped in xterm even where tmux wrapped them."""
    screen = await _capture_pane_plain(session, lines, part="screen")
    if screen is None:
        return None
    if history_size <= 0:          # -E -1 with no history returns a stray row
        return screen
    history = await _capture_pane_plain(session, lines, join=True, part="history")
    return (history or []) + screen


def _diff_buffers(
    frontend_tail: list[str], backend_tail: list[str],
) -> dict[str, Any]:
    """Match two line lists and report how well they agree.
    Trailing-space-tolerant (xterm pads cells with spaces; tmux
    capture-pane may pad differently).

    Lines are matched as sequences (difflib), not position by position
    from the bottom: a few extra or missing lines in one place (e.g.
    rows xterm wrapped itself just above tmux's repainted screen) then
    count as a few mismatches, instead of shifting every line above
    them out of step.

    Returns a dict with:
      lines_compared, matches, mismatches, mismatch_examples (first
      N divergent stretches, with the lines from each side).
    """
    n = min(len(frontend_tail), len(backend_tail))
    front = [(line or "").rstrip() for line in frontend_tail[-n:]] if n else []
    # Give the backend a little extra context above the window so lines
    # the frontend has near its top can still find their partners.
    back_n = min(len(backend_tail), n + 64)
    back = [(line or "").rstrip() for line in backend_tail[-back_n:]] if back_n else []
    sm = difflib.SequenceMatcher(None, front, back, autojunk=False)
    matches = sum(block.size for block in sm.get_matching_blocks())
    mismatch_examples: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or len(mismatch_examples) >= 5:
            continue
        if tag == "insert" and i1 == 0:
            continue            # backend context above the frontend's window
        mismatch_examples.append({
            "line_from_bottom": n - i1,
            "frontend": "\n".join(front[i1:i2]),
            "backend": "\n".join(back[j1:j2]),
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


@router.get("/api/debug/sessions", dependencies=[AuthDep, SameOriginDep])
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
    #
    # Two alignment details, without which nearly every line "differs":
    # xterm's active buffer ends with tmux's status row(s), which
    # capture-pane never includes, so those are dropped first; and
    # schema-3 dumps are logical lines, compared against capture-pane -J.
    diff: dict[str, Any] | None = None
    if body.session:
        payload = body.payload if isinstance(body.payload, dict) else {}
        schema = payload.get("schema")
        join = isinstance(schema, int) and schema >= 3
        status_rows, history_size = await _pane_info(body.session)

        def _drop_status(tail: Any) -> Any:
            if status_rows and isinstance(tail, list) and len(tail) > status_rows:
                return tail[:-status_rows]
            return tail

        buffers = payload.get("buffers")
        if isinstance(buffers, dict):
            active_type = str(buffers.get("activeType", ""))
            normal_tail = buffers.get("normal") or []
            alt_tail = buffers.get("alternate") or []
            if active_type == "alternate":
                alt_tail = _drop_status(alt_tail)
            else:
                normal_tail = _drop_status(normal_tail)
            # Capture both screens in parallel — sequential awaits
            # doubled the worst-case snapshot-endpoint latency to
            # ~4 s (2 × 2 s timeout). gather() collapses to a single
            # wall-clock wait. Skip the alt-screen capture entirely
            # when the user isn't on alt-screen — that's the common
            # case and saves one full subprocess spawn.
            want_alt = (active_type == "alternate" and
                        isinstance(alt_tail, list) and bool(alt_tail))
            want_normal = isinstance(normal_tail, list) and bool(normal_tail)
            tasks = []
            if want_normal and join:
                tasks.append(_capture_logical(body.session, len(normal_tail), history_size))
            elif want_normal:
                tasks.append(_capture_pane_plain(
                    body.session, len(normal_tail), alternate=False))
            else:
                tasks.append(_noop_capture())
            if want_alt:
                tasks.append(_capture_pane_plain(
                    body.session, len(alt_tail), alternate=True))
            else:
                tasks.append(_noop_capture())
            normal_back, alt_back = await asyncio.gather(*tasks)
            # Treat an all-blank tmux alt-screen capture as "no alt
            # state" — tmux's alternate buffer survives after a TUI
            # exits (vim's contents persist for minutes after :q),
            # producing a confusing "0/N match" panel for content
            # that nobody actually has on screen.
            if alt_back is not None and not any(
                (line or "").strip() for line in alt_back
            ):
                alt_back = None
            diff = {
                "active_type": active_type,
                "normal": (
                    _diff_buffers(list(normal_tail), normal_back)
                    if want_normal and normal_back is not None else None
                ),
                "alternate": (
                    _diff_buffers(list(alt_tail), alt_back)
                    if want_alt and alt_back is not None else None
                ),
            }
        else:
            # Schema-1 fallback: legacy snapshots have only ``buffer.tail``
            # which is the active-buffer dump. Compare against whichever
            # screen tmux is currently presenting (the default, no -a).
            front_tail = _drop_status((payload.get("buffer", {}) or {}).get("tail"))
            if isinstance(front_tail, list) and front_tail:
                back_tail = await _capture_pane_plain(body.session, len(front_tail))
                if back_tail is not None:
                    diff = {
                        "active_type": "unknown (schema-1)",
                        "normal": _diff_buffers(list(front_tail), back_tail),
                        "alternate": None,
                    }
    # Sanitise the session string for the log line — Pydantic doesn't
    # constrain it, so a value containing literal "\n" would otherwise
    # forge a second log entry. The note is logged with %r which
    # already escapes; session uses %s for readability.
    safe_session = (body.session or "?").replace("\r", "").replace("\n", " ")[:64]
    # Log shape metadata + diff SUMMARY only — not the raw payload or
    # mismatch_examples. Terminal contents in the buffer tail can
    # include secrets typed at a read prompt, tokens, or paths;
    # we don't want them in the operator's journal at INFO. The full
    # detail still rides in the HTTP response so the user can see it
    # in the snapshot modal.
    payload_meta = _payload_shape(body.payload) if isinstance(body.payload, dict) else {}
    diff_summary = _diff_summary(diff) if diff else None
    log.info("frontend snapshot: session=%s note=%r payload_meta=%s backend=%s diff=%s",
             safe_session, body.note, payload_meta, backend, diff_summary)
    return {
        "backend": backend,
        "active_sessions_for_name": len(matches),
        "content_diff": diff,
    }


def _payload_shape(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract non-sensitive shape info from a frontend snapshot
    payload — sizes, types, identifiers — leaving raw terminal
    contents out of the journal."""
    out: dict[str, Any] = {
        "schema": payload.get("schema"),
        "capturedAt": payload.get("capturedAt"),
    }
    term = payload.get("terminal")
    if isinstance(term, dict):
        out["terminal"] = {
            k: term.get(k) for k in
            ("cols", "rows", "bufferType", "bufferLength", "renderer")
            if k in term
        }
    socket = payload.get("socket")
    if isinstance(socket, dict):
        out["socket"] = {
            k: socket.get(k) for k in
            ("readyStateLabel", "framesReceived", "bytesReceived",
             "helloCount", "lastHistoryBytes", "receivingHistory")
            if k in socket
        }
    buf = payload.get("buffer")
    if isinstance(buf, dict):
        out["buffer_lines"] = buf.get("lineCount")
    bufs = payload.get("buffers")
    if isinstance(bufs, dict):
        out["buffers"] = {
            "activeType": bufs.get("activeType"),
            "normal_lines": len(bufs.get("normal", []) or []),
            "alternate_lines": len(bufs.get("alternate", []) or []),
        }
    return out


def _diff_summary(diff: dict[str, Any]) -> dict[str, Any]:
    """Compress a content_diff for journal logging — keep the
    match-percentage signal, drop the per-line mismatch_examples
    which contain raw terminal text."""
    out: dict[str, Any] = {"active_type": diff.get("active_type")}
    for label in ("normal", "alternate"):
        sub = diff.get(label)
        if sub is None:
            out[label] = None
        else:
            out[label] = {
                k: sub.get(k) for k in
                ("lines_compared", "matches", "mismatches",
                 "frontend_lines", "backend_lines")
            }
    return out
