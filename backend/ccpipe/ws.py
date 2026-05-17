"""WebSocket handler: bridges a browser xterm.js to a tmux session via PTY.

Also subscribes to tmux control-mode events so the browser learns about
session lifecycle changes (created/renamed/closed) in real time.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect

from . import tmux
from .auth import is_session_authed
from .mic import MicWriter
from .pty_relay import PtyProcess, pump
from .tmux_control import TmuxEvent, control_client
from .tts import tts_service

log = logging.getLogger(__name__)

# Binary frame type prefixes (1 byte). The first byte is the channel
# tag; the rest is the payload. Both directions:
#   client → server : FRAME_MIC_PCM
#   server → client : FRAME_PTY_OUTPUT, FRAME_TTS_AUDIO
# History bytes share the PTY channel (they're just pre-recorded
# terminal output). An unknown leading byte is dropped at both ends.
FRAME_MIC_PCM    = 0x01    # client → server, Int16 PCM mic audio
FRAME_TTS_AUDIO  = 0x02    # server → client, encoded TTS audio chunk
FRAME_PTY_OUTPUT = 0x00    # server → client, raw PTY bytes — see comment

# Process-wide singleton; mic plumbing is shared across WS clients because
# Pulse's pipe-source is a single FIFO on disk.
_mic_writer = MicWriter()

# Tracks which WS connection (if any) currently owns the mic. Two
# simultaneous WS clients writing into the same FIFO interleave Int16
# frames, which sounds like garbled static on Claude's /voice. The first
# WS to send PCM after a quiet period claims ownership; concurrent
# others have their frames silently dropped until the owner disconnects.
_mic_owner: object | None = None

# Mic ingress rate caps. A hijacked WS could otherwise flood the pipe with
# garbage; defense in depth.  The numbers leave generous headroom for
# legitimate 16 kHz mono Int16 audio (~32 KB/s).
_MIC_MAX_FRAME_BYTES = 32 * 1024            # one WS frame
_MIC_BUDGET_BYTES = 1 * 1024 * 1024         # sustained 1s window
_MIC_BUDGET_WINDOW_S = 1.0


async def _build_tts_filter(tmux_session: str):
    """Return a content filter that gates TTS to *this* claude conversation
    and to records appended after the WS attach time.

    Preferred matcher: claude's own sessionId (UUID) read from
    ``~/.claude/sessions/<pid>.json``. This is the only way to fully
    isolate two concurrent claude processes that happen to share a cwd
    or sit in a parent/child dir relationship — without it, the TTS
    fan-out cross-talks between sibling tmux sessions.

    Fallback matcher (older claude builds that don't write the session
    file): the cwd-based filter, looser but better than silence.
    """
    sid = await tmux.claude_session_id(tmux_session)
    cutoff_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if sid:
        log.info("tts: scoping session %s to claude sessionId=%s after=%s",
                 tmux_session, sid, cutoff_iso)

        def _accept_sid(record: dict) -> bool:
            if record.get("sessionId") != sid:
                return False
            ts = record.get("timestamp")
            if isinstance(ts, str) and ts < cutoff_iso:
                return False
            return True

        return _accept_sid

    cwd = await tmux.session_cwd(tmux_session)
    if not cwd:
        log.info("tts: no sessionId/cwd for session %s; this WS will receive no audio",
                 tmux_session)
        return lambda _r: False

    expected = os.path.realpath(cwd)
    log.warning(
        "tts: claude session metadata missing for tmux %s; falling back to "
        "cwd=%s (audio can leak between concurrent claudes in this dir)",
        tmux_session, expected,
    )

    def _accept_cwd(record: dict) -> bool:
        record_cwd = record.get("cwd")
        if not isinstance(record_cwd, str) or not record_cwd:
            return False
        record_real = os.path.realpath(record_cwd)
        # Claude Code's "cwd" field tracks the effective cwd (it changes
        # as the user `cd`s inside the session). Our reference is the OS
        # cwd of the claude process — the dir it was launched from. They
        # differ when the user navigates into a subdirectory of the
        # project. Accept any record whose cwd is the expected dir OR
        # any subdirectory of it — same project, same conversation.
        if record_real != expected and not record_real.startswith(expected + os.sep):
            return False
        ts = record.get("timestamp")
        if isinstance(ts, str) and ts < cutoff_iso:
            return False
        return True

    return _accept_cwd


async def handle_terminal_ws(websocket: WebSocket, session: str) -> None:
    await websocket.accept()

    # Auto-create the session if it doesn't exist yet.
    if not await tmux.session_exists(session):
        await tmux.create_session(session)

    # Wait briefly for the client's initial 'resize' message so we spawn the
    # PTY (and thus the tmux client) at the correct dimensions. With
    # window-size=latest this means the attached window resizes correctly on
    # first attach instead of briefly rendering at the fallback 120x40.
    initial_cols, initial_rows, leftover = await _wait_for_initial_resize(websocket)

    # Capture tmux's full pane (history + visible) on EVERY connect, not
    # just the first one. The frontend `term.reset()`s on `hello` so the
    # bytes we send below replace the xterm buffer rather than appending
    # to it. This is what closes the "new output isn't in scrollback after
    # a reconnect" hole — during a network blip, lines that scrolled into
    # tmux's history were previously never delivered to xterm because we
    # skipped this capture on reconnects.
    history_bytes = await _capture_session_history(session, initial_rows)

    pty_proc = PtyProcess(tmux.attach_argv(session),
                          cols=initial_cols, rows=initial_rows)
    await pty_proc.start()
    # Any non-resize messages we drained while waiting are applied now.
    for msg in leftover:
        _handle_client_text(msg, pty_proc)

    # Best-effort: try to open the mic pipe now so the hello message can
    # advertise voice capability accurately.
    voice_available = _mic_writer.write(b"")  # zero-length write probes the FD
    await websocket.send_json({
        "type": "hello",
        "session": session,
        "tts": tts_service.enabled,
        "voice": voice_available,
    })

    # Track WS-send so we can serialize sends from multiple tasks safely.
    send_lock = asyncio.Lock()

    async def send_json(msg: dict) -> bool:
        async with send_lock:
            try:
                await websocket.send_json(msg)
                return True
            except Exception as exc:
                log.debug("send_json failed: %s", exc)
                return False

    async def send_text(text: str) -> bool:
        async with send_lock:
            try:
                await websocket.send_text(text)
                return True
            except Exception as exc:
                log.debug("send_text failed: %s", exc)
                return False

    async def forward_pty_to_ws(data: bytes) -> None:
        # Send PTY bytes as a WS binary frame so xterm receives raw UTF-8
        # without a decode/encode roundtrip. Crucially this also avoids
        # corrupting multi-byte codepoints split across 64 KiB read
        # boundaries (which `bytes.decode(errors="replace")` would mangle).
        # Prefixed with FRAME_PTY_OUTPUT so a PTY chunk that happens to
        # start with FRAME_TTS_AUDIO (0x02 = Ctrl-B in normal terminal
        # output) doesn't get misclassified as an audio chunk on the
        # client side.
        async with send_lock:
            try:
                await websocket.send_bytes(bytes([FRAME_PTY_OUTPUT]) + data)
            except Exception as exc:
                log.debug("send_bytes(pty) failed: %s", exc)

    # Subscribe to control-mode events; forward to this WS as JSON.
    async def on_tmux_event(event: TmuxEvent) -> None:
        if event.name == "sessions-changed":
            if not await tmux.session_exists(session):
                await send_json({"type": "session_gone", "session": session})
                return
        await send_json({
            "type": "session_event",
            "event": event.name,
            "args": event.args,
        })

    async def send_binary(prefix: int, payload: bytes) -> bool:
        async with send_lock:
            try:
                await websocket.send_bytes(bytes([prefix]) + payload)
                return True
            except Exception as exc:
                log.debug("send_bytes failed: %s", exc)
                return False

    # The first failed send proves the WS is no longer reachable; we set
    # this flag so subsequent callbacks short-circuit instead of forcing
    # asyncio + httpx to keep streaming Kokoro chunks into a dead socket.
    # tts_sub.cancel() (below) is also called, which removes this fan-out
    # target from the next utterance entirely.
    ws_alive = True

    async def on_tts_start(text: str) -> None:
        nonlocal ws_alive
        if not ws_alive:
            return
        # Send up to 4000 chars so the frontend has enough text to send
        # to /api/tts/speak for the "replay last response" pill. Longer
        # utterances get truncated, replay won't capture the full thing
        # in that case — Kokoro's own input limit is around the same.
        if not await send_json({"type": "tts_start", "text": text[:4000]}):
            ws_alive = False
            tts_sub.cancel()

    async def on_tts_chunk(chunk: bytes) -> None:
        nonlocal ws_alive
        if not ws_alive:
            return
        if not await send_binary(FRAME_TTS_AUDIO, chunk):
            ws_alive = False
            tts_sub.cancel()

    async def on_tts_end() -> None:
        nonlocal ws_alive
        if not ws_alive:
            return
        if not await send_json({"type": "tts_end"}):
            ws_alive = False
            tts_sub.cancel()

    tmux_sub = control_client.subscribe(on_tmux_event)
    tts_sub = tts_service.subscribe(
        on_start=on_tts_start, on_chunk=on_tts_chunk, on_end=on_tts_end,
        content_filter=await _build_tts_filter(session),
    )

    # Send any captured history before the live pump starts. xterm.js
    # writes these bytes into its scrollback; tmux attach's incoming
    # redraw will then paint the current visible pane on top. Prefixed
    # with FRAME_PTY_OUTPUT so the client dispatches it through the
    # same PTY pipeline as live output.
    if history_bytes:
        async with send_lock:
            try:
                await websocket.send_bytes(bytes([FRAME_PTY_OUTPUT]) + history_bytes)
            except Exception as exc:
                log.debug("history send failed: %s", exc)

    async def _pty_lifecycle() -> None:
        """Run the PTY pump; on EOF, surface the exit to the client and
        close the WS so the receive loop below unblocks.

        Without this the receive loop would keep awaiting messages and
        pty_proc.write() would silently no-op, leaving the WS as a
        zombie until the client disconnects.

        Post-pump sends are guarded with suppress(CancelledError) so the
        client still learns the PTY exited even if the outer handler's
        finally is cancelling us concurrently (e.g. server shutdown
        racing PTY EOF). Without that, the client would see a silent WS
        close and have to infer the exit from reconnect failure.
        """
        try:
            await pump(pty_proc, forward_pty_to_ws)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pty pump failed")
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await send_json({"type": "pty_exited"})
            except Exception:
                pass
            try:
                await websocket.close(code=1000, reason="pty exited")
            except Exception:
                pass

    pty_task = asyncio.create_task(_pty_lifecycle())
    mic_limiter = _MicRateLimiter()
    # Per-WS opaque token used to claim the mic singleton on first use.
    mic_token: object = object()

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (text := msg.get("text")) is not None:
                # Intercept "ping" first so we can reply pong from this
                # scope where send_json is available. The pong lets the
                # frontend detect dead-but-not-yet-closed sockets after
                # Android tab-freeze: it expects a pong (or any server
                # message) within ~45s of its keepalive ping; absence
                # forces a reconnect.
                if _is_ping(text):
                    # Re-check the session on every ping. authorize_websocket
                    # only fires at connect, so without this an open WS would
                    # survive a credential bump (password change, TOTP
                    # disable, "sign out everywhere"). Closing with 1008
                    # tells the frontend it must re-authenticate.
                    if not _is_session_still_authed(websocket):
                        log.info("ws closed mid-stream: session no longer authorized")
                        await websocket.close(code=1008, reason="session revoked")
                        break
                    await send_json({"type": "pong"})
                    continue
                # Mute state mirror: the client tells us when the user
                # toggles TTS, so we can skip the Kokoro round-trip
                # while nobody's listening. Cheap dispatch — no JSON
                # parse on the hot input path.
                if '"type":"tts_mute"' in text or '"type": "tts_mute"' in text:
                    try:
                        payload = json.loads(text)
                        tts_sub.muted = bool(payload.get("value"))
                    except Exception:
                        pass
                    continue
                _handle_client_text(text, pty_proc)
            elif (data := msg.get("bytes")) is not None:
                _handle_client_binary(data, pty_proc, mic_limiter, mic_token)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws handler error")
    finally:
        # Release the mic if this WS was the owner so the next connection
        # can claim it.
        global _mic_owner
        if _mic_owner is mic_token:
            _mic_owner = None
        tmux_sub.cancel()
        tts_sub.cancel()
        pty_task.cancel()
        # Await pump cancellation BEFORE tearing down the PTY so a
        # pending send_text/send_bytes doesn't race with the socket close.
        try:
            await asyncio.wait_for(pty_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        try:
            await pty_proc.terminate()
        except Exception:
            log.exception("pty terminate failed")
        try:
            await websocket.close()
        except Exception:
            pass


# Time budget for the client to send its initial 'resize' message before we
# give up and spawn the PTY at the fallback size. Frontend sends it
# immediately on ws.onopen, so this should resolve in a few tens of ms.
_INITIAL_RESIZE_TIMEOUT_S = 0.8
_FALLBACK_COLS = 120
_FALLBACK_ROWS = 40
# Upper bound on resize dimensions. struct.pack("HHHH", ...) in pty_relay
# rejects values >65535 with struct.error, which would tear down the WS
# every time a (possibly compromised) client sent a giant resize. Real
# terminals never need anywhere near this; 1000 leaves plenty of room.
_RESIZE_MAX = 1000


def _clamp_dim(v: int) -> int:
    return max(1, min(_RESIZE_MAX, v))

_HISTORY_CAPTURE_TIMEOUT_S = 2.0
# Match the frontend's xterm scrollback setting (terminal.ts). Lines older
# than this would fall out of the browser's buffer anyway.
_HISTORY_MAX_LINES = 10_000


async def _capture_session_history(session: str, viewport_rows: int) -> bytes:
    """Return the full tmux pane content (scrollback + visible) as
    xterm-ready bytes, preserving ANSI escape sequences.

    The previous implementation captured *only* scrollback (``-E -1``)
    and padded with ``viewport_rows`` blank LFs so the captured tail
    would scroll past xterm's visible region before tmux's attach
    redraw could clobber it. That seam was fragile: the padding count
    had to equal the rows tmux would paint, but the two diverge as
    soon as anything shifts the pane height (status bar, multi-pane
    layouts, tmux config drift). When they disagreed the result was
    silently-missing lines at the seam — exactly the
    "older-overwriting-newer" symptom reported by the user.

    The new approach is seam-free: capture the *entire* pane (history
    + visible), send it as-is, and let tmux's attach redraw paint
    over the visible region with the same bytes we just placed there.
    The visible portion of our capture and tmux's redraw show
    identical content (they're snapshots of the same pane microseconds
    apart) so the overwrite is a no-op — no data loss, no alignment
    to maintain. ``viewport_rows`` is kept on the API for callers
    that still pass it; it's unused.

    Returns empty bytes on any failure or when the pane is empty.
    """
    del viewport_rows  # accepted for backwards compat; intentionally unused
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux.TMUX_BIN, "capture-pane",
            "-t", session,
            "-p",                              # print to stdout
            "-e",                              # include escape sequences
            "-S", f"-{_HISTORY_MAX_LINES}",    # start: N lines into history
            # No -E flag: capture extends through the visible pane to
            # the bottom, so the resulting bytes describe the WHOLE
            # pane state at this instant.
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return b""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(),
                                         timeout=_HISTORY_CAPTURE_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return b""
    if proc.returncode != 0 or not out:
        return b""
    # tmux capture-pane joins lines with LF. xterm.js wants CRLF to start
    # a new line at column 0; otherwise lines stack on the right of the
    # previous one. Normalise (idempotent if already CRLF).
    normalised = out.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if not normalised.endswith(b"\r\n"):
        normalised += b"\r\n"
    return normalised


async def _wait_for_initial_resize(websocket: WebSocket
                                    ) -> tuple[int, int, list[str]]:
    """Drain WS messages until we see a resize or hit the timeout.

    Returns (cols, rows, leftover_text_messages). Any non-resize messages
    received while waiting are returned so the caller can apply them once
    the PTY exists.
    """
    cols, rows = _FALLBACK_COLS, _FALLBACK_ROWS
    leftover: list[str] = []
    deadline = asyncio.get_event_loop().time() + _INITIAL_RESIZE_TIMEOUT_S
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return cols, rows, leftover
        try:
            msg = await asyncio.wait_for(websocket.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            return cols, rows, leftover
        if msg.get("type") == "websocket.disconnect":
            return cols, rows, leftover
        text = msg.get("text")
        if text is None:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if parsed.get("type") == "resize":
            try:
                cols = _clamp_dim(int(parsed["cols"]))
                rows = _clamp_dim(int(parsed["rows"]))
            except (KeyError, ValueError, TypeError):
                pass
            return cols, rows, leftover
        leftover.append(text)


def _is_session_still_authed(websocket: WebSocket) -> bool:
    """Re-check the session cookie at heartbeat time. Cheap (in-memory
    only) and catches a credential bump that happened after the WS
    upgrade completed — without this, a password change would not
    revoke active sockets."""
    session = websocket.scope.get("session") or {}
    return is_session_authed(session)


def _is_ping(text: str) -> bool:
    """Cheap pre-parse check so the receive loop can dispatch pongs
    without doing a full json.loads on every keystroke. The substring
    check is tighter than ``"ping"`` alone so paste content containing
    the word doesn't waste a parse — it has to look like our literal
    ``{"type":"ping"}`` shape (modulo whitespace) to even be tested.
    """
    if '"type":"ping"' not in text and '"type": "ping"' not in text:
        return False
    try:
        return json.loads(text).get("type") == "ping"
    except (json.JSONDecodeError, AttributeError):
        return False


def _handle_client_text(text: str, pty_proc: PtyProcess) -> None:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        log.warning("non-JSON text frame: %r", text[:200])
        return
    match msg.get("type"):
        case "input":
            data = msg.get("data", "")
            if isinstance(data, str):
                pty_proc.write(data.encode("utf-8"))
        case "resize":
            try:
                cols = _clamp_dim(int(msg.get("cols", 120)))
                rows = _clamp_dim(int(msg.get("rows", 40)))
            except (TypeError, ValueError):
                return  # malformed resize, ignore
            pty_proc.resize(cols, rows)
        case "ping":
            # Pong is sent from the receive loop where send_json is in
            # scope — see _ping_needs_pong() check there.
            pass
        case _:
            log.warning("unknown text message type: %r", msg.get("type"))


def _handle_client_binary(data: bytes, pty_proc: PtyProcess,
                           limiter: "_MicRateLimiter",
                           mic_token: object) -> None:
    if not data:
        return
    frame_type = data[0]
    if frame_type == FRAME_MIC_PCM:
        payload = data[1:]
        if len(payload) > _MIC_MAX_FRAME_BYTES:
            log.warning("mic frame too large (%d bytes); dropping", len(payload))
            return
        if not limiter.allow(len(payload)):
            return  # over rate budget; silently drop
        # First-write-wins ownership of the singleton FIFO. A second
        # WS that starts sending PCM while another is active sees its
        # frames silently dropped here, so we don't interleave Int16
        # samples into the pipe and produce garbled audio downstream.
        global _mic_owner
        if _mic_owner is None:
            _mic_owner = mic_token
        if _mic_owner is not mic_token:
            return
        _mic_writer.write(payload)
        return
    log.warning("unknown binary frame type: 0x%02x", frame_type)


class _MicRateLimiter:
    """Sliding-window byte budget. One instance per WS connection.

    Uses a deque + maintained running total so each frame is O(1)
    amortised: popleft() is O(1) and we update the total incrementally
    rather than re-summing the window. Matters at ~50 frames/s of
    16kHz mono Int16 audio."""

    def __init__(self) -> None:
        self._window: deque[tuple[float, int]] = deque()  # (ts, bytes)
        self._total: int = 0

    def allow(self, n_bytes: int) -> bool:
        # time.monotonic is the right primitive for sync code paths:
        # asyncio.get_event_loop().time() triggers a deprecation when
        # called outside a running-loop context, and `allow` is invoked
        # synchronously from _handle_client_binary.
        now = time.monotonic()
        cutoff = now - _MIC_BUDGET_WINDOW_S
        while self._window and self._window[0][0] < cutoff:
            _, expired = self._window.popleft()
            self._total -= expired
        if self._total + n_bytes > _MIC_BUDGET_BYTES:
            log.warning("mic rate budget exceeded (%d > %d in %.1fs); dropping",
                        self._total + n_bytes, _MIC_BUDGET_BYTES,
                        _MIC_BUDGET_WINDOW_S)
            return False
        self._window.append((now, n_bytes))
        self._total += n_bytes
        return True
