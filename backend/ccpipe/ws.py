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
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect

from . import tmux
from .auth import is_session_authed
from .mic import MicWriter
from .pty_relay import PtyProcess, pump
from .tmux_control import TmuxEvent, control_client
from .tts import tts_service

# macOS uses a different mic backend (in-memory PCM buffer +
# whisper-cpp local transcription) because claude's /voice keybinding
# handler is upstream-broken — see transcriber_macos.py for the full
# story. The Linux import path doesn't pull the transcriber module in.
if sys.platform == "darwin":
    from .transcriber_macos import MicTranscriber
else:
    MicTranscriber = None  # type: ignore[misc,assignment]

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

# Process-wide singleton; the mic backend is shared across WS clients
# because the underlying resource is shared too (Pulse pipe-source on
# Linux, the local transcriber on macOS — only one whisper
# subprocess can usefully run at a time anyway). The macOS branch
# returns a MicTranscriber that exposes the same write/reset/stats
# surface as MicWriter, so the rest of this file is platform-agnostic.
_mic_writer: MicWriter = (
    MicTranscriber() if sys.platform == "darwin" and MicTranscriber is not None
    else MicWriter()
)

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

# Per-text-frame size cap. The frontend never sends a single text
# frame larger than a few KB even for big pastes — terminal.ts chunks
# input at 4 KiB and resize/mic_stop/tts_mute/ping are tiny — so 64
# KiB is well above legitimate traffic. An authenticated bad page
# that tries to stream unbounded text into pty_proc.write is dropped
# here before the JSON parse on the hot path can happen.
#
# We DON'T also impose a sliding-window byte-rate cap on top: a large
# paste (e.g. a multi-megabyte log) chunks into many 4 KiB frames in
# rapid succession, which a 4 MiB/s budget would silently truncate
# mid-paste — exactly the "paste broke" symptom the C3 fix produced.
# Defense-in-depth against sustained streaming is handled downstream
# by pty_relay's _WRITE_BUFFER_LIMIT (4 MiB) which backpressures the
# PTY master's kernel buffer.
_TEXT_FRAME_MAX_BYTES = 64 * 1024

# Bracket-count ceiling for WS JSON parse. stdlib json.loads is
# recursive, so a deeply-nested payload like ``[[[…]]]]`` can blow the
# Python recursion limit and tear down this handler with a logged
# exception. Our control messages have shape `{type, …}` with at most
# one nested array (resize cols/rows are scalars), so a count of <=64
# open brackets is well above legitimate traffic and well below the
# default 1000-deep recursion limit. Conservative — counts every `{`
# and `[` including siblings — but the false-positive cost is zero on
# real ccpipe frames.
_JSON_BRACKET_CAP = 64


def _safe_json_loads(text: str):
    """``json.loads`` with a cheap pre-screen for nesting depth.

    Refuses input whose total open-bracket count exceeds
    ``_JSON_BRACKET_CAP`` BEFORE handing the bytes to stdlib json,
    which would otherwise recurse and trip ``RecursionError``.
    """
    if text.count("{") + text.count("[") > _JSON_BRACKET_CAP:
        raise ValueError("json too deeply nested")
    return json.loads(text)


@dataclass
class WsCounters:
    """Per-WS byte accounting so we can prove (in the journal, after the
    fact) whether the live PTY stream lost any content while the
    connection was open. Every WS handler instantiates one and logs a
    summary on close — grep ``"ws closed:"`` to see flow stats for any
    session.

    Loss-class fields (``bytes_lost`` / ``send_failures``) being non-zero
    on close is the unambiguous signal that ``forward_pty_to_ws`` had to
    drop bytes — typically a transient WS stall that the new behaviour
    (raise + reconnect + capture-pane replay) recovers, but worth
    catching when it happens so the failure mode is visible instead of
    silent.
    """
    session: str = ""
    started_at: float = 0.0
    bytes_read_pty: int = 0
    bytes_sent_ws: int = 0
    bytes_lost: int = 0
    send_failures: int = 0
    # PTY-output frames forwarded (count, not size). Useful for
    # distinguishing a few huge frames from many small ones at debug time.
    frames_forwarded: int = 0


# Registry of currently-open WS handlers' counters. Each handler
# inserts itself on entry, removes itself on exit. An admin diagnostic
# endpoint can iterate this list for a live snapshot. Concurrent
# access is single-threaded by asyncio so no lock is needed.
_active_counters: list[WsCounters] = []

# Registry of live WebSocket objects so credential rotation can
# proactively close stale sockets without waiting for the next inbound
# frame (M2). The session-version pong re-check at ws.py:531/628/637
# only fires on inbound traffic — a passive-receive WS (claude is
# speaking, attacker isn't typing) would survive "sign out everywhere"
# indefinitely otherwise. Asyncio single-threaded, no lock needed.
_live_ws: set[WebSocket] = set()


async def close_stale_ws_sockets(reason: str = "credentials changed") -> int:
    """Close every live WebSocket whose session ``cred_version`` no
    longer matches the current credential. Returns the count of sockets
    actually closed.

    Called from the credential-mutation routes (auth_change_credentials,
    totp_confirm_endpoint, totp_disable_endpoint) immediately after the
    version bump lands on disk. Safe to call from the asyncio loop;
    each close goes through Starlette's WebSocket.close which is
    idempotent and handles "already closed" cleanly.
    """
    from .auth import get_credential
    current_version = get_credential().version
    # Snapshot the set before iterating — close() schedules a discard
    # via the WS handler's finally block, which may mutate _live_ws.
    closed = 0
    for ws in list(_live_ws):
        session = ws.scope.get("session") or {}
        stored = session.get("cred_version")
        if not isinstance(stored, int) or stored != current_version:
            try:
                await ws.close(code=1008, reason=reason)
                closed += 1
            except Exception as exc:
                # Already-closed or transport-level error — log and continue.
                log.debug("close_stale_ws_sockets: close failed: %s", exc)
    if closed:
        log.info("credential rotation closed %d stale ws (reason=%r)", closed, reason)
    return closed


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


async def _release_ptt_after(
    pty_proc: PtyProcess,
    delay_seconds: float,
    still_authoritative: Callable[[], bool] | None = None,
) -> None:
    """Sleep for `delay_seconds`, then write the meta+k keystroke into
    `pty_proc` so claude's /voice exits push-to-talk and submits the
    captured utterance.

    ``still_authoritative`` is checked AFTER the sleep, before writing:
    if it returns False (the WS that scheduled this release has gone
    away and another mic-claim happened in the meantime) we skip the
    write. Without this guard a stale release from session A's earlier
    /voice interaction can land in session A's reattached client and
    abort its current /voice — same tmux session, same pty_proc,
    different mic_token.
    """
    try:
        await asyncio.sleep(max(0.0, delay_seconds))
        if still_authoritative is not None and not still_authoritative():
            log.debug("ptt-release suppressed: handler no longer authoritative")
            return
        # \x1b k = ESC k = meta+k = the binding ccpipe installs into
        # ~/.claude/keybindings.json for the voice:pushToTalk action.
        pty_proc.write(b"\x1bk")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("scheduled release-PTT after %.2fs failed", delay_seconds)


async def handle_terminal_ws(websocket: WebSocket, session: str) -> None:
    # Two scopes in this function (mic_stop dispatch + finally cleanup)
    # both assign to _mic_owner. Python requires `global` to come before
    # any read of the name in the same function, so hoist it here once
    # rather than scattering per-block declarations.
    global _mic_owner
    await websocket.accept()

    # Auto-create the session if it doesn't exist yet.
    if not await tmux.session_exists(session):
        await tmux.create_session(session)

    # Wait briefly for the client's initial 'resize' message so we spawn the
    # PTY (and thus the tmux client) at the correct dimensions. With
    # window-size=latest this means the attached window resizes correctly on
    # first attach instead of briefly rendering at the fallback 120x40.
    initial_cols, initial_rows, leftover = await _wait_for_initial_resize(websocket, session)

    # Capture tmux's full pane (history + visible) on EVERY connect, not
    # just the first one. The frontend `term.reset()`s on `hello` so the
    # bytes we send below replace the xterm buffer rather than appending
    # to it. This is what closes the "new output isn't in scrollback after
    # a reconnect" hole — during a network blip, lines that scrolled into
    # tmux's history were previously never delivered to xterm because we
    # skipped this capture on reconnects.
    history_bytes = await _capture_session_history(session, initial_rows)

    # NOTE: the tmux-attach relay (PtyProcess) is spawned LATER, in the
    # no-await "commit zone" just before the receive loop's try/finally —
    # NOT here. Spawning it before the fallible handshake below (hello /
    # history / stream_ready sends, every one of which raises if the
    # client has already vanished) used to leak the `tmux attach-session`
    # child: the guaranteeing finally hadn't been entered yet, so a
    # mid-handshake disconnect left the relay attached forever. Under
    # tmux window-size=latest those orphans pinned the shared pane to
    # their fallback width (120x40), so a later mobile client saw wide
    # content wrapped into blank-gap scrollback — the "scrambled output /
    # massive spaces and gaps on mobile" report. Deferring start() past
    # the last fallible await closes the leak.

    # Best-effort: try to open the mic pipe now so the hello message can
    # advertise voice capability accurately.
    voice_available = _mic_writer.write(b"")  # zero-length write probes the FD
    # Resolve the tmux session's working directory so the client can
    # default file/directory-browse dialogs to the project root the
    # user is actually working in, rather than the fs jail root
    # (typically $HOME). Best-effort: session_cwd may return None if
    # tmux's pane query failed; client falls back to the fs config
    # root in that case.
    session_cwd_value = await tmux.session_cwd(session)
    await websocket.send_json({
        "type": "hello",
        "session": session,
        "cwd": session_cwd_value,
        "tts": tts_service.enabled,
        "voice": voice_available,
    })

    # Track WS-send so we can serialize sends from multiple tasks safely.
    send_lock = asyncio.Lock()

    # Per-WS byte accounting. Registered in the global active list for
    # live diagnostics; a summary is logged in the `finally` block so
    # every WS close leaves a "ws closed: …" line in the journal that
    # tells us how much PTY data flowed and whether any was lost.
    counters = WsCounters(session=session, started_at=time.monotonic())
    _active_counters.append(counters)
    # Captured from the client's disconnect frame so the close reason shows
    # in the "ws closed" line — diagnoses periodic reconnects (1000 client
    # close / 1001 going-away-backgrounded / 1006 abnormal-drop).
    disconnect_code: int | None = None
    # Register for credential-rotation kicks (M2). Deregistration is in
    # the WS handler's finally block alongside _active_counters.remove.
    _live_ws.add(websocket)

    async def send_json(msg: dict) -> bool:
        async with send_lock:
            try:
                await websocket.send_json(msg)
                return True
            except Exception as exc:
                log.debug("send_json failed: %s", exc)
                return False

    async def send_pong_unlocked() -> bool:
        """Send a pong WITHOUT acquiring send_lock.

        Pongs are 14 bytes and the WS frame is atomic at the protocol
        layer (no fragmentation), so they don't need to serialise
        against PTY / TTS sends. Without this bypass a slow chunk send
        holding send_lock can hold the pong past the client's 45s
        stale-check, forcing a spurious reconnect of an otherwise-
        healthy socket.
        """
        try:
            await websocket.send_json({"type": "pong"})
            return True
        except Exception as exc:
            log.debug("send_pong failed: %s", exc)
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
        #
        # If send_bytes raises (WS disconnected, transport stall, etc.)
        # we re-raise so pump() exits and _pty_lifecycle cleanly closes
        # the WS. Without re-raising, the previous DEBUG-level swallow
        # silently leaked PTY bytes from xterm whenever the WS hiccupped
        # — they'd never make it to the client's buffer but would still
        # be in tmux's pane, producing the "gap until I refresh and
        # capture-pane recovers them" symptom. Re-raising trades a
        # warning + a reconnect for guaranteed eventual consistency.
        counters.bytes_read_pty += len(data)
        async with send_lock:
            try:
                await websocket.send_bytes(bytes([FRAME_PTY_OUTPUT]) + data)
                counters.bytes_sent_ws += len(data)
                counters.frames_forwarded += 1
            except Exception as exc:
                counters.send_failures += 1
                counters.bytes_lost += len(data)
                log.warning("send_bytes(pty) failed (%d bytes lost from this "
                            "ws; client should reconnect and re-capture pane): %s",
                            len(data), exc)
                raise

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

    # Tell the client we're past the slow part of setup. Used as the
    # signal to fire the first latency-measuring ping — pinging any
    # earlier (e.g. at hello, which is sent BEFORE the history-bytes
    # blob) means the ping queues server-side behind the history send
    # and the round-trip reflects setup time, not network RTT. By the
    # time stream_ready lands the server is one statement away from
    # the main receive() loop and a ping pongs back at network speed.
    await send_json({"type": "stream_ready"})

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

    # ── Commit zone ──────────────────────────────────────────────────
    # Spawn the relay HERE, after every fallible handshake await above.
    # From this point to the receive loop's `try` below there is no await
    # that can raise-and-escape, so once the `tmux attach-session` child
    # exists it is guaranteed to be reaped by that loop's finally
    # (pty_proc.terminate()). See the NOTE near _capture_session_history.
    pty_proc = PtyProcess(tmux.attach_argv(session),
                          cols=initial_cols, rows=initial_rows)
    try:
        await pty_proc.start()
    except BaseException:
        # start() itself failed (fork/exec) — reap any half-spawned child
        # before propagating, since the finally below is not yet active.
        with contextlib.suppress(Exception):
            await pty_proc.terminate()
        raise
    # Any non-resize messages we drained while waiting for the initial
    # resize are applied now that the relay exists.
    for msg in leftover:
        _handle_client_text(msg, pty_proc, session)

    pty_task = asyncio.create_task(_pty_lifecycle())
    mic_limiter = _MicRateLimiter()
    # Per-WS opaque token used to claim the mic singleton on first use.
    mic_token: object = object()
    # In-flight PTT-release tasks scheduled by mic_stop. Tracked so we
    # can cancel them in `finally`: without this, a fast disconnect
    # right after mic_stop leaves the sleeping release task as the
    # last reference to its closure; when it fires (~drain_pad_ms
    # later) it writes Esc k into pty_proc, which on a re-attached
    # session is a different mic-token's pty (same name, same pty
    # because tmux sessions persist) and aborts the new voice
    # interaction.
    pending_releases: set[asyncio.Task[None]] = set()

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                disconnect_code = msg.get("code")
                break
            if (text := msg.get("text")) is not None:
                # Per-frame size cap. Frontend chunks input at 4 KiB
                # and control messages are tiny, so 64 KiB is well
                # above legitimate traffic. A hijacked page streaming
                # unbounded JSON would otherwise hit the substring
                # sniffs + full json.loads on every keystroke-sized
                # frame. The downstream pty_relay write buffer is
                # the real backpressure for sustained streaming —
                # we don't also impose a sliding-window byte budget
                # because that silently truncates large pastes.
                tlen = len(text.encode("utf-8"))
                if tlen > _TEXT_FRAME_MAX_BYTES:
                    log.warning("oversized text frame (%d > %d bytes); dropping",
                                tlen, _TEXT_FRAME_MAX_BYTES)
                    continue
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
                    # DIAG: measure how long the server spends between
                    # observing the ping and getting the pong onto the
                    # WS. If this is consistently <1ms but clients report
                    # 20ms RTT, the delay is on the network / radio side.
                    # If this matches the client-reported delta, it's
                    # send_lock contention with the live PTY pump (and
                    # the fix is to bypass / prioritise pong sends).
                    _ping_t0 = time.monotonic()
                    await send_pong_unlocked()
                    _ping_dt_ms = (time.monotonic() - _ping_t0) * 1000
                    log.debug("ping→pong: %.1fms (session=%s frames=%d)",
                              _ping_dt_ms, session, counters.frames_forwarded)
                    continue
                # Mute state mirror: the client tells us when the user
                # toggles TTS, so we can skip the Kokoro round-trip
                # while nobody's listening. Cheap dispatch — no JSON
                # parse on the hot input path.
                if _is_control_frame(text, '"type":"tts_mute"', '"type": "tts_mute"'):
                    try:
                        payload = _safe_json_loads(text)
                        tts_sub.muted = bool(payload.get("value"))
                    except Exception:
                        pass
                    continue
                # mic_stop: the client has torn down its mic and wants
                # claude's /voice push-to-talk released. We CAN'T just
                # forward the release keystroke immediately because
                # audio captured in the last ~few-hundred-ms is still
                # in flight through the pipe → Pulse → claude STT, and
                # claude's STT itself needs another ~1-2s to finalise
                # transcription. So we estimate the pipeline drain
                # based on bytes-written stats from _mic_writer, add
                # the configured pad, and write the release keystroke
                # to the PTY ourselves after waiting that long. The
                # client is no longer involved in the release timing.
                if _is_control_frame(text, '"type":"mic_stop"', '"type": "mic_stop"'):
                    if _mic_owner is mic_token:
                        if sys.platform == "darwin":
                            # macOS: no Pulse pipeline to drain. The
                            # MicTranscriber holds the entire utterance
                            # in memory; hand it off to whisper-cpp
                            # asynchronously and let it type the result
                            # into the PTY when transcription finishes.
                            # Fire-and-forget so the WS receive loop
                            # stays responsive; whisper is ~real-time
                            # on Apple Silicon (1-2 s for short
                            # utterances on base.en).
                            log.info(
                                "mic_stop: bytes=%d drops=%d → local transcribe",
                                _mic_writer.bytes_written, _mic_writer.drops,
                            )
                            asyncio.create_task(_mic_writer.finalize(pty_proc))  # type: ignore[attr-defined]
                            _mic_owner = None
                        else:
                            # Linux: drain Pulse, wait the configured
                            # pad for claude's STT to finalise, then
                            # write the release keystroke. Use a local
                            # module reference so reload-during-dev
                            # (importlib.reload) picks up edits.
                            from . import config as _app_config
                            cfg = _app_config.load().mic
                            drain_s = _mic_writer.estimate_drain_seconds()
                            pad_s = cfg.drain_pad_ms / 1000.0
                            total = drain_s + pad_s
                            log.info(
                                "mic_stop: bytes=%d drops=%d drain=%.2fs pad=%.2fs total=%.2fs",
                                _mic_writer.bytes_written,
                                _mic_writer.drops, drain_s, pad_s, total,
                            )
                            _mic_writer.reset()
                            _mic_owner = None
                            # M3: cancel any prior pending release from
                            # this same handler before scheduling a new
                            # one. Without this, a rapid mic_start →
                            # mic_stop → mic_start → mic_stop sequence
                            # leaves the first release task in flight
                            # — its Esc k fires mid-second-recording's
                            # drain and toggles claude /voice at the
                            # wrong time. At most one release per WS is
                            # ever meaningful; older ones are stale.
                            for old in list(pending_releases):
                                old.cancel()
                            pending_releases.clear()
                            # Track the release task so the `finally`
                            # block can cancel it on disconnect, and
                            # only write Esc k if no other handler has
                            # claimed the mic in the meantime. We
                            # capture mic_token in the lambda's default
                            # arg so the closure binds it at scheduling
                            # time — gating on `_mic_owner is None or
                            # _mic_owner is mt` lets a re-record from
                            # the SAME handler still get its drain-end
                            # release fired, while a swap to a different
                            # owner correctly suppresses the write.
                            rel_task = asyncio.create_task(
                                _release_ptt_after(
                                    pty_proc, total,
                                    still_authoritative=(
                                        lambda mt=mic_token: (
                                            _mic_owner is None or _mic_owner is mt
                                        )
                                    ),
                                )
                            )
                            pending_releases.add(rel_task)
                            rel_task.add_done_callback(pending_releases.discard)
                    continue
                # Re-check the session on every non-intercepted text
                # frame (typically `input`). Without this, a credential
                # bump (password change, TOTP toggle) only takes effect
                # on the next 30 s keepalive ping — meaning an attacker
                # holding an authenticated WS could keep typing into
                # the PTY for up to one keepalive cycle after the
                # operator clicked "sign out everywhere". The check is
                # an in-memory dict lookup + int compare; trivial.
                if not _is_session_still_authed(websocket):
                    log.info("ws closed mid-stream: session no longer authorized (input)")
                    await websocket.close(code=1008, reason="session revoked")
                    break
                _handle_client_text(text, pty_proc, session)
            elif (data := msg.get("bytes")) is not None:
                # Same revoked-credential gate as for text frames — a
                # session whose cred_version has bumped should stop
                # being able to push mic PCM into the FIFO too.
                if not _is_session_still_authed(websocket):
                    log.info("ws closed mid-stream: session no longer authorized (binary)")
                    await websocket.close(code=1008, reason="session revoked")
                    break
                _handle_client_binary(data, pty_proc, mic_limiter, mic_token)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws handler error")
    finally:
        # Always emit byte-flow counters on close — this is the
        # diagnostic anchor for "did we lose any PTY bytes on this
        # connection". Lost-byte / send-failure counts being > 0 means
        # forward_pty_to_ws raised at least once (typically a transient
        # WS stall) and the client should have reconnected to recover
        # via capture-pane replay. Search the journal for "ws closed:".
        # Fold in any chunks that pty_relay's bounded read queue had
        # to drop (saturated by a stalled WS pump). These bytes WERE
        # read off the master fd, so they count toward bytes_read_pty
        # as well as bytes_lost — otherwise the documented invariant
        # `bytes_read_pty == bytes_sent_ws + bytes_lost` (debug.py +
        # test_ws_byte_accounting) is false precisely in the loss case
        # it exists to detect.
        try:
            dropped = pty_proc.bytes_dropped()
            counters.bytes_lost += dropped
            counters.bytes_read_pty += dropped
        except Exception:
            pass
        duration = time.monotonic() - counters.started_at
        log.info(
            "ws closed: session=%s duration=%.1fs frames=%d "
            "bytes_read_pty=%d bytes_sent_ws=%d bytes_lost=%d send_failures=%d "
            "close_code=%s",
            counters.session, duration, counters.frames_forwarded,
            counters.bytes_read_pty, counters.bytes_sent_ws,
            counters.bytes_lost, counters.send_failures, disconnect_code,
        )
        try:
            _active_counters.remove(counters)
        except ValueError:
            pass
        _live_ws.discard(websocket)
        # Release the mic if this WS was the owner so the next connection
        # can claim it.
        if _mic_owner is mic_token:
            _mic_owner = None
        # Cancel any in-flight PTT-release tasks. Without this they hold
        # a ref to pty_proc through their closure and fire after the
        # WS is gone, writing Esc k into the pty — which on a tmux
        # session that's been re-attached aborts the new client's
        # /voice interaction (see C2 comment above pending_releases).
        for rel_task in list(pending_releases):
            rel_task.cancel()
        tmux_sub.cancel()
        tts_sub.cancel()
        pty_task.cancel()
        # Await pump cancellation BEFORE tearing down the PTY so a
        # pending send_text/send_bytes doesn't race with the socket close.
        try:
            await asyncio.wait_for(pty_task, timeout=2.0)
        except asyncio.TimeoutError:
            # pump didn't honour cancellation within 2s — likely stuck in a
            # send on a wedged transport. terminate()'s fd-identity guards
            # make the subsequent teardown safe, but surface it so a
            # non-cancelling pump is visible rather than silent.
            log.warning("pty pump did not exit within 2s of cancel "
                        "(session=%s); tearing down anyway", counters.session)
        except (asyncio.CancelledError, Exception):
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
# give up and spawn the PTY at the fallback size. The frontend sends it
# immediately on ws.onopen, so on a healthy link this resolves in tens of
# ms and the timeout is never approached. It's bumped well above that
# (was 0.8s) so a slow mobile reconnect still delivers the real size
# BEFORE we attach — attaching at a guessed width and only then resizing
# is what strands a wide frame in xterm's scrollback (see _FALLBACK_COLS).
_INITIAL_RESIZE_TIMEOUT_S = 2.5
# Fallback size used ONLY when a brand-new session's client never sends an
# initial resize within the timeout. Deliberately NARROW: a fallback WIDER
# than the real client makes claude draw full-width frames that, once the
# real (narrower) resize lands and the pane shrinks, are stranded in
# xterm's scrollback as blank-gap wrapped rows — the "massive spaces /
# gaps on mobile" symptom. Narrow content never gaps on a wider display,
# so an under-guess is always safe; the old 120-wide over-guess was not.
# Reconnects do better still: they seed from the session's last known size
# (see _last_client_size), so a phone reconnect storm re-attaches at the
# width it last used (~60) instead of any fallback.
_FALLBACK_COLS = 80
_FALLBACK_ROWS = 24
# Last client size observed per session. Seeds the initial attach size on
# a RECONNECT before the client's resize arrives, so a mobile reconnect
# storm re-attaches at the width it last used rather than the fallback.
# Updated on every resize (initial + live); a timed-out fallback never
# writes here. Bounded by the number of distinct session names (tiny for
# a personal tool).
_last_client_size: dict[str, tuple[int, int]] = {}


def _remember_client_size(session: str, cols: int, rows: int) -> None:
    _last_client_size[session] = (cols, rows)
# Upper bound on resize dimensions. struct.pack("HHHH", ...) in pty_relay
# rejects values >65535 with struct.error, which would tear down the WS
# every time a (possibly compromised) client sent a giant resize. Real
# terminals never need anywhere near this; 1000 leaves plenty of room.
_RESIZE_MAX = 1000


def _clamp_dim(v: int) -> int:
    return max(1, min(_RESIZE_MAX, v))

_HISTORY_CAPTURE_TIMEOUT_S = 2.0
# Capture at most this many lines of scrollback on attach. This is
# deliberately matched to the frontend's xterm scrollback cap
# (terminal.ts maxScrollback) — lines older than what xterm retains
# can't be displayed anyway, so capturing them just wastes bytes.
# NOTE: tmux history-limit is set to 50_000 (tmux_setup.py) for safety
# margin / future use, but only the most recent _HISTORY_MAX_LINES are
# delivered to the browser. The other ~40k lines are intentionally
# undeliverable via reconnect replay; raise BOTH this and xterm's
# maxScrollback together if deeper reachable history is ever wanted.
_HISTORY_MAX_LINES = 10_000
# Hard ceiling on the captured/cached blob size. With -e, each line
# carries its full escape-sequence run, so a wide, densely-coloured pane
# could otherwise produce a multi-MB transient (held in the subprocess
# buffer + the cache + the outgoing frame simultaneously). Truncate from
# the FRONT (oldest) so the newest content — what the user is looking at
# — always survives.
_HISTORY_MAX_BYTES = 4 * 1024 * 1024

# Tiny per-session cache for the most recent capture-pane output, so
# that a mobile reconnect storm (Wi-Fi → cellular handoff fires several
# online/visibility events in quick succession) doesn't pay one full
# `tmux capture-pane` for each WS attempt. The window is kept SHORT:
# a cached blob is a point-in-time snapshot, and the longer it's reused
# the more a busy streaming session can mutate between the snapshot and
# the attach redraw — content that scrolls out of the visible region in
# that gap lands in neither the blob nor tmux's redraw, a dropped/
# duplicated band at the seam. 250 ms still coalesces a handoff storm
# (those events fire within tens of ms) while bounding staleness an
# order of magnitude tighter than the old 1 s. Keyed by session name.
_HISTORY_CACHE_TTL_S = 0.25
_history_cache: dict[str, tuple[float, bytes]] = {}


def _clear_history_cache() -> None:
    """Drop any cached capture-pane output. Called by the tests
    between runs so a stale entry from one test case doesn't bypass
    the subprocess mock in the next one."""
    _history_cache.clear()


def invalidate_history_cache(session: str) -> None:
    """Drop the cached capture for *session*. Called when a session is
    renamed or killed so a recreated session reusing the same name (or
    the rename's new name) can't be served a stale blob from the prior
    occupant within the TTL window."""
    _history_cache.pop(session, None)


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
    # Coalesce repeated captures within the TTL window. A mobile
    # reconnect storm can fire several attaches in quick succession;
    # without this each one pays a tmux subprocess + several MB of
    # capture output. Mutation is on the asyncio loop thread so no
    # lock is needed.
    now = time.monotonic()
    cached = _history_cache.get(session)
    if cached is not None:
        if now - cached[0] < _HISTORY_CACHE_TTL_S:
            return cached[1]
        # L2: stale on read — drop so the dict doesn't hold this entry
        # until the 64-key threshold sweep runs. A session that was
        # attached once then renamed/killed would otherwise sit with
        # its ~MB of capture bytes until 63 other unique session names
        # appear over the process lifetime.
        _history_cache.pop(session, None)
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux.TMUX_BIN, "capture-pane",
            "-t", session,
            "-p",                              # print to stdout
            "-e",                              # include escape sequences
            "-J",                              # join wrapped lines (+ trailing
                                               # spaces) so xterm can re-wrap a
                                               # logically-long line to the
                                               # client viewport instead of
                                               # rendering it hard-broken at the
                                               # capture-time pane width
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
        # kill() then BOUNDED wait — bare proc.wait() can block
        # forever if the child is stuck in uninterruptible sleep,
        # and history capture happens during WS attach so a stuck
        # subprocess would stall every reconnect.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(ProcessLookupError, asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        return b""
    if proc.returncode != 0 or not out:
        return b""
    # tmux capture-pane joins lines with LF. xterm.js wants CRLF to start
    # a new line at column 0; otherwise lines stack on the right of the
    # previous one. Normalise (idempotent if already CRLF).
    normalised = out.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    # IMPORTANT: do NOT end the blob with a trailing CRLF. On a full
    # reconnect the frontend replays this blob and tmux's attach redraw
    # then repaints the visible screen IN PLACE. A trailing CRLF first
    # scrolls the grid up by one row — so the top visible line is pushed
    # into scrollback AND immediately repainted on screen by the redraw,
    # a duplicate line per reconnect that piles up under mobile reconnect
    # churn ("duplicate data in scrollback"). Ending exactly at the last
    # line's content lets the redraw overwrite with no scroll, no dup.
    # The next bytes after this blob are always a cursor-positioning
    # redraw (\x1b[H…), so nothing concatenates onto that last line.
    # Validated headless in frontend/test/dup-seam.mjs.
    if normalised.endswith(b"\r\n"):
        normalised = normalised[:-2]
    # Cap the blob size, truncating the OLDEST content from the front so
    # the newest (visible) lines always survive. Trim to the next CRLF
    # boundary so we never emit a half escape sequence / partial line.
    if len(normalised) > _HISTORY_MAX_BYTES:
        cut = len(normalised) - _HISTORY_MAX_BYTES
        nl = normalised.find(b"\r\n", cut)
        normalised = normalised[nl + 2:] if nl != -1 else normalised[cut:]
    # Cache for the coalesce window. Periodic eviction of stale entries
    # keeps the dict bounded — a session whose name is renamed/killed
    # ages out within TTL; until then we just have one stale ~MB entry.
    _history_cache[session] = (now, normalised)
    # Opportunistic GC: when the dict gets non-trivial, drop entries
    # older than 10× the TTL. Avoids unbounded growth in long-running
    # processes with many distinct session names over the lifetime.
    if len(_history_cache) > 64:
        cutoff = now - _HISTORY_CACHE_TTL_S * 10
        for k in [k for k, (t, _) in _history_cache.items() if t < cutoff]:
            _history_cache.pop(k, None)
    return normalised


async def _wait_for_initial_resize(websocket: WebSocket, session: str
                                    ) -> tuple[int, int, list[str]]:
    """Drain WS messages until we see a resize or hit the timeout.

    Returns (cols, rows, leftover_text_messages). Any non-resize messages
    received while waiting are returned so the caller can apply them once
    the PTY exists.

    On timeout we fall back to this session's LAST KNOWN client size if we
    have one (a reconnect reuses the width it last attached at), else the
    narrow default — never the old wide guess that produced gappy
    scrollback on mobile.
    """
    cols, rows = _last_client_size.get(session, (_FALLBACK_COLS, _FALLBACK_ROWS))
    leftover: list[str] = []
    # Defense in depth: cap how much pre-resize chatter we'll buffer
    # before bailing. Legitimate clients send at most one or two pre-
    # resize messages (a TTS mute mirror, an immediate input frame);
    # a flooder could otherwise pre-fill the leftover list with up to
    # 64 KiB × _INITIAL_RESIZE_TIMEOUT_S worth of text before the PTY
    # is even spawned.
    _LEFTOVER_MAX_ENTRIES = 16
    _LEFTOVER_MAX_BYTES = 64 * 1024
    leftover_bytes = 0
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
            parsed = _safe_json_loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if parsed.get("type") == "resize":
            try:
                cols = _clamp_dim(int(parsed["cols"]))
                rows = _clamp_dim(int(parsed["rows"]))
                _remember_client_size(session, cols, rows)
            except (KeyError, ValueError, TypeError):
                pass
            return cols, rows, leftover
        if len(leftover) >= _LEFTOVER_MAX_ENTRIES:
            continue
        tlen = len(text.encode("utf-8"))
        if leftover_bytes + tlen > _LEFTOVER_MAX_BYTES:
            continue
        leftover.append(text)
        leftover_bytes += tlen


def _is_session_still_authed(websocket: WebSocket) -> bool:
    """Re-check the session cookie at heartbeat time. Cheap (in-memory
    only) and catches a credential bump that happened after the WS
    upgrade completed — without this, a password change would not
    revoke active sockets."""
    session = websocket.scope.get("session") or {}
    return is_session_authed(session)


# L3: substring fast-paths for control frames only apply to small text
# frames. Real control envelopes are well under 100 bytes; anything
# larger is a paste, which shouldn't match (and shouldn't pay the
# json.loads cost on the full payload).
_CONTROL_FRAME_MAX = 256


def _is_control_frame(text: str, needle1: str, needle2: str) -> bool:
    """Returns True when *text* is short enough to be a control envelope
    AND contains one of the needles. Used by the receive loop's cheap
    pre-dispatch for tts_mute / mic_stop without parsing JSON on every
    keystroke. See L3."""
    return len(text) <= _CONTROL_FRAME_MAX and (needle1 in text or needle2 in text)


def _is_ping(text: str) -> bool:
    """Cheap pre-parse check so the receive loop can dispatch pongs
    without doing a full json.loads on every keystroke. The substring
    check is tighter than ``"ping"`` alone so paste content containing
    the word doesn't waste a parse — it has to look like our literal
    ``{"type":"ping"}`` shape (modulo whitespace) to even be tested.
    Plus the L3 size cap: a paste >256 bytes can't be a ping anyway.
    """
    if not _is_control_frame(text, '"type":"ping"', '"type": "ping"'):
        return False
    try:
        return _safe_json_loads(text).get("type") == "ping"
    except (json.JSONDecodeError, ValueError, AttributeError):
        return False


def _handle_client_text(text: str, pty_proc: PtyProcess,
                        session: str | None = None) -> None:
    try:
        msg = _safe_json_loads(text)
    except (json.JSONDecodeError, ValueError):
        log.warning("non-JSON text frame: %r", text[:200])
        return
    match msg.get("type"):
        case "input":
            data = msg.get("data", "")
            if isinstance(data, str):
                # macOS: swallow the /voice trigger keystroke (ESC k,
                # i.e. meta+k) at both edges. The frontend still sends
                # it to arm/disarm tap mode on Linux, but the macOS
                # backend transcribes locally and types the result
                # into the PTY itself — claude's own /voice never
                # gets involved. Forwarding meta+k would either be a
                # no-op (today, since /voice is upstream-broken) or
                # worse, double-trigger if Anthropic ships a fix
                # mid-deploy.
                if sys.platform == "darwin" and data == "\x1bk":
                    return
                pty_proc.write(data.encode("utf-8"))
        case "resize":
            try:
                cols = _clamp_dim(int(msg.get("cols", 120)))
                rows = _clamp_dim(int(msg.get("rows", 40)))
            except (TypeError, ValueError):
                return  # malformed resize, ignore
            pty_proc.resize(cols, rows)
            # Keep the per-session size cache fresh so a later reconnect
            # seeds from the current (possibly rotated) width, not a stale
            # one. See _last_client_size / _FALLBACK_COLS.
            if session is not None:
                _remember_client_size(session, cols, rows)
        case "ping" | "tts_mute" | "mic_stop":
            # These are intercepted by the main WS receive loop where
            # the closures over send_json / tts_sub / _mic_writer live.
            # We still hit this code path when one of them lands in
            # the pre-resize buffer (drained by _wait_for_initial_resize
            # and replayed here) — at which point the side-effect
            # state doesn't exist yet, so silently dropping is the
            # right move. The frontend re-sends tts_mute on every
            # state change so the loss is transient; mic_stop arriving
            # pre-resize is implausible (mic lifecycle starts long
            # after connect); ping just won't get a pong this once.
            pass
        case _:
            log.warning("unknown text message type: %r", msg.get("type"))


def _handle_client_binary(data: bytes, pty_proc: PtyProcess,
                           limiter: "_SlidingByteLimiter",
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
            # New owner — zero the stats so estimate_drain_seconds()
            # below reflects only this recording. Without this, a prior
            # recording's bytes_written/first_write_ts would leak into
            # the next mic_stop's drain calculation if the owning WS
            # disconnected before sending mic_stop.
            _mic_writer.reset()
            _mic_owner = mic_token
        if _mic_owner is not mic_token:
            return
        _mic_writer.write(payload)
        return
    log.warning("unknown binary frame type: 0x%02x", frame_type)


class _SlidingByteLimiter:
    """Sliding-window byte budget. One instance per WS connection per
    direction. Uses a deque + maintained running total so each frame
    is O(1) amortised."""

    def __init__(self, budget_bytes: int, window_s: float, label: str) -> None:
        self._window: deque[tuple[float, int]] = deque()  # (ts, bytes)
        self._total: int = 0
        self._budget = budget_bytes
        self._window_s = window_s
        self._label = label

    def allow(self, n_bytes: int) -> bool:
        # time.monotonic is the right primitive for sync code paths:
        # asyncio.get_event_loop().time() triggers a deprecation when
        # called outside a running-loop context.
        now = time.monotonic()
        cutoff = now - self._window_s
        while self._window and self._window[0][0] < cutoff:
            _, expired = self._window.popleft()
            self._total -= expired
        if self._total + n_bytes > self._budget:
            log.warning("%s rate budget exceeded (%d > %d in %.1fs); dropping",
                        self._label, self._total + n_bytes, self._budget,
                        self._window_s)
            return False
        self._window.append((now, n_bytes))
        self._total += n_bytes
        return True


def _MicRateLimiter() -> _SlidingByteLimiter:
    """Factory for the mic-PCM byte-rate gate. Kept as a callable so
    existing call sites keep working with no signature change."""
    return _SlidingByteLimiter(_MIC_BUDGET_BYTES, _MIC_BUDGET_WINDOW_S, "mic")
