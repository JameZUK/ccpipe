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
import re
import sys
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from . import claude_asks, drafts, plan_usage, session_meta, tmux, tmux_ui, voice_ptt
from . import config as app_config
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
# Until when (monotonic) Claude's REC footer is ccpipe's own doing: a start
# press (mic_start) or a stop that's still draining. No "Claude is
# listening" prompt then (see _TerminalConnection._watch_for_listening).
_voice_quiet_until = 0.0
_VOICE_QUIET_S = 4.0

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
    msg = json.loads(text)
    # Every client frame is a JSON object; anything else (a bare number,
    # a list, a string) is rejected here rather than crashing the caller's
    # msg.get() and tearing down the socket.
    if not isinstance(msg, dict):
        raise ValueError("frame is not a JSON object")
    return msg


# Warnings about malformed client frames are rate-limited: a buggy or
# hostile client can send thousands a second, and each used to log a line.
_WARN_INTERVAL_S = 10.0
_warn_state: dict[str, tuple[float, int]] = {}   # key → (last logged, suppressed)


def _warn_limited(key: str, fmt: str, *args: object) -> None:
    now = time.monotonic()
    last, suppressed = _warn_state.get(key, (0.0, 0))
    if now - last < _WARN_INTERVAL_S:
        _warn_state[key] = (last, suppressed + 1)
        return
    if suppressed:
        fmt += " (+%d similar suppressed)"
        args = (*args, suppressed)
    log.warning(fmt, *args)
    _warn_state[key] = (now, 0)


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
    """Close every live WebSocket whose session is no longer valid: its
    ``cred_version`` no longer matches the current credential, or its login
    has been revoked (logout). Returns the count of sockets actually
    closed.

    Called from the credential-mutation routes (auth_change_credentials,
    totp_confirm_endpoint, totp_disable_endpoint) immediately after the
    version bump lands on disk. Safe to call from the asyncio loop;
    each close goes through Starlette's WebSocket.close which is
    idempotent and handles "already closed" cleanly.
    """
    # Snapshot the set before iterating — close() schedules a discard
    # via the WS handler's finally block, which may mutate _live_ws.
    closed = 0
    for ws in list(_live_ws):
        session = ws.scope.get("session") or {}
        if not is_session_authed(session):
            try:
                await ws.close(code=1008, reason=reason)
                closed += 1
            except Exception as exc:
                # Already-closed or transport-level error — log and continue.
                log.debug("close_stale_ws_sockets: close failed: %s", exc)
    if closed:
        log.info("closed %d stale ws (reason=%r)", closed, reason)
    return closed


_build_cache: tuple[float, str | None] = (-1.0, None)


def _current_build() -> str | None:
    """The commit of the frontend being served (dist/version.json, cached
    by mtime), or None if it can't be read."""
    global _build_cache
    from .routes.static import _frontend_dist
    path = _frontend_dist() / "version.json"
    try:
        mtime = path.stat().st_mtime
        if mtime != _build_cache[0]:
            build = json.loads(path.read_text()).get("build")
            commit = build.split()[0] if isinstance(build, str) and build.split() else None
            _build_cache = (mtime, commit)
    except (OSError, ValueError, AttributeError):
        return None
    return _build_cache[1]


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
    claude_recording: Callable[[], Awaitable[bool]] | None = None,
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
        # Press only while Claude is recording: a press to an idle Claude
        # would start a recording nobody is talking into (voice_ptt.py).
        if claude_recording is not None and not await claude_recording():
            log.info("ptt-release skipped: Claude isn't recording")
            return
        # \x1b k = ESC k = meta+k = the binding ccpipe installs into
        # ~/.claude/keybindings.json for the voice:pushToTalk action.
        pty_proc.write(b"\x1bk")
        voice_ptt.pressed("recording")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("scheduled release-PTT after %.2fs failed", delay_seconds)


class _NoTts:
    """Stand-in TTS subscription for tiles, which never speak."""
    muted = True

    def cancel(self) -> None:
        pass


# How often a watch tile re-checks its session's size.
_WATCH_SIZE_POLL_S = 1.0


async def _watch_client_size(session: str) -> tuple[int, int]:
    """Client size that shows *session*'s window exactly: the window plus
    the status line(s) tmux draws on every client."""
    size = await tmux_ui.window_size(session) or (_FALLBACK_COLS, _FALLBACK_ROWS)
    status = await tmux_ui.display(session, "#{status}") or "on"
    lines = 0 if status == "off" else int(status) if status.isdigit() else 1
    return _clamp_dim(size[0]), _clamp_dim(size[1] + lines)


async def handle_terminal_ws(websocket: WebSocket, session: str,
                             watch: bool = False, tile: bool = False,
                             reattach: bool = False) -> None:
    try:
        await _TerminalConnection(websocket, session, watch=watch, tile=tile,
                                  reattach=reattach).run()
    except WebSocketDisconnect:
        # The client went away mid-handshake (e.g. a phone's network
        # handoff, or the browser replacing a dial it had just started).
        # Routine, and nothing to clean up — registrations happen after
        # the handshake sends — so don't log it as an ASGI traceback.
        log.debug("ws for session=%s closed during handshake", session)


# Live connections per tmux session, for fanning out composer-draft edits:
# typing on one device updates the prompt box on every other device viewing
# the same session (last write wins — the server just relays the newest).
_draft_peers: dict[str, set["_TerminalConnection"]] = {}
# Held so fire-and-forget relay sends aren't garbage-collected mid-flight.
_draft_sends: set[asyncio.Task[bool]] = set()


def _is_draft_frame(text: str) -> bool:
    # The client serialises {"type":"draft","text":…} with "type" first, so
    # a prefix check routes it without parsing every keystroke. Both the
    # compact and the ": " spellings are accepted, as for control frames.
    return text.startswith(('{"type":"draft"', '{"type": "draft"'))


class _TerminalConnection:
    """One browser terminal connection to a tmux session.

    ``run()`` walks the lifecycle in order: handshake (auto-create, initial
    resize, history capture, hello) → registrations → commit zone (spawn the
    tmux-attach relay) → receive loop → teardown. The per-connection state
    that used to live in one 570-line function's closures lives here, and
    each phase is its own method."""

    def __init__(self, websocket: WebSocket, session: str,
                 watch: bool = False, tile: bool = False, reattach: bool = False) -> None:
        self.ws = websocket
        self.session = session
        # A reconnect of a terminal that was already attached: it never
        # creates or resumes the session. If the session is gone it ended
        # while this terminal had it open — stopped or forgotten from the
        # picker, idle-stopped, `exit`ed — and bringing it back would undo
        # exactly that (the old reconnect resurrected a session the moment
        # it was stopped, and a forgotten one as a fresh Claude in $HOME).
        self.reattach = reattach
        # A secondary tile beside the main terminal (tile=True): no TTS, no
        # mic, and it never creates a session. A watch tile additionally
        # attaches read-only + ignore-size, so watching never resizes the
        # session, and its input is dropped.
        self.watch = watch
        self.tile = tile or watch
        self.watch_size: tuple[int, int] | None = None
        self.watch_task: asyncio.Task[None] | None = None
        # Watches for Claude stopping to ask something (main terminal only).
        self.ask_task: asyncio.Task[None] | None = None
        # The page's build ("5478df8_2026-11-03T2120Z" → "5478df8").
        params = getattr(websocket, "query_params", None) or {}
        self.build = (params.get("build") or "").split("_")[0][:40]
        # Budget for the page's tts_log reports (see _receive_loop).
        self.tts_log_window = 0.0
        self.tts_log_count = 0
        # Track WS-send so we can serialize sends from multiple tasks safely.
        self.send_lock = asyncio.Lock()
        # Per-WS byte accounting. Registered in the global active list for
        # live diagnostics; a summary is logged in teardown so every WS close
        # leaves a "ws closed: …" line in the journal that tells us how much
        # PTY data flowed and whether any was lost.
        self.counters = WsCounters(session=session, started_at=time.monotonic())
        # Captured from the client's disconnect frame so the close reason
        # shows in the "ws closed" line — diagnoses periodic reconnects (1000
        # client close / 1001 going-away-backgrounded / 1006 abnormal-drop).
        self.disconnect_code: int | None = None
        # The first failed TTS send proves the WS is no longer reachable; we
        # set this flag so subsequent callbacks short-circuit instead of
        # forcing asyncio + httpx to keep streaming Kokoro chunks into a dead
        # socket. tts_sub.cancel() is also called, which removes this fan-out
        # target from the next utterance entirely.
        self.alive = True
        self.tmux_sub: Any = None
        self.tts_sub: Any = None
        self.pty_proc: PtyProcess | None = None
        self.pty_task: asyncio.Task[None] | None = None
        self.mic_limiter = _MicRateLimiter()
        # Per-WS opaque token used to claim the mic singleton on first use.
        self.mic_token: object = object()
        # In-flight PTT-release tasks scheduled by mic_stop. Tracked so we
        # can cancel them in teardown: without this, a fast disconnect right
        # after mic_stop leaves the sleeping release task as the last
        # reference to its closure; when it fires (~drain_pad_ms later) it
        # writes Esc k into pty_proc, which on a re-attached session is a
        # different mic-token's pty (same name, same pty because tmux
        # sessions persist) and aborts the new voice interaction.
        self.pending_releases: set[asyncio.Task[None]] = set()
        # The mic_start in flight (voice_ptt.arm): waits for Claude to be
        # ready, presses /voice's key and confirms it took.
        self.arm_task: asyncio.Task[None] | None = None
        # "Claude is listening" prompt: Claude started recording without a
        # ccpipe mic (its key pressed in the terminal) — see
        # _watch_for_listening.
        self.listen_tail = b""
        self.listen_task: asyncio.Task[None] | None = None
        # Relay output held back until the replay has been sent (run()).
        self.hold_pty = False
        self.held_pty: list[bytes] = []
        # Set by the first held output with text in it: tmux's drawing of
        # this client's screen (_send_history).
        self.pty_redraw = asyncio.Event()

    # ── lifecycle ──────────────────────────────────────────────────────
    async def run(self) -> None:
        await self.ws.accept()
        if self.tile or self.reattach:
            # A tile shows an existing session; it must never quietly
            # (re)create one — a tile left over from a killed session would
            # otherwise resurrect it. Likewise a reconnect (see __init__).
            if not await tmux.session_exists(self.session):
                from . import lifecycle
                await self._session_gone(lifecycle.ended_note(self.session))
                return
        if not self.tile:
            if not await self._ensure_session():
                return
            # "Last opened" in the session picker (tiles don't count).
            with contextlib.suppress(Exception):
                await asyncio.to_thread(session_meta.note_opened, self.session)

        if self.watch:
            # The tmux client is sized to the session, not the browser: the
            # tile scales the result to fit.
            self.watch_size = await _watch_client_size(self.session)
            cols, rows = self.watch_size
            leftover: list[str] = []
        else:
            # Wait briefly for the client's initial 'resize' message so we
            # spawn the PTY (and thus the tmux client) at the correct
            # dimensions. With window-size=latest this means the attached
            # window resizes correctly on first attach instead of briefly
            # rendering at the fallback 120x40.
            cols, rows, leftover = await _wait_for_initial_resize(self.ws, self.session)

        # Replay tmux's full pane (history + visible) on EVERY connect, not
        # just the first one. The frontend `term.reset()`s on `hello` so the
        # bytes we send replace the xterm buffer rather than appending to it.
        # This is what closes the "new output isn't in scrollback after a
        # reconnect" hole — during a network blip, lines that scrolled into
        # tmux's history were previously never delivered to xterm because we
        # skipped this capture on reconnects.
        #
        # Only for a single-pane window: the capture is the ACTIVE pane's
        # history, which in a split window would be drawn across the whole
        # screen. With splits, tmux's attach redraw alone paints the panes.
        #
        # The capture is taken AFTER our tmux client has attached (and tmux
        # has resized the window to it), not before: see _send_history.
        replay = await tmux_ui.window_panes(self.session) <= 1

        # NOTE: the tmux-attach relay (PtyProcess) is spawned LATER, in the
        # no-await "commit zone" (_start_pty) just before the receive loop's
        # try/finally — NOT here. Spawning it before the fallible handshake
        # below (hello / history / stream_ready sends, every one of which
        # raises if the client has already vanished) used to leak the `tmux
        # attach-session` child: the guaranteeing finally hadn't been entered
        # yet, so a mid-handshake disconnect left the relay attached forever.
        # Under tmux window-size=latest those orphans pinned the shared pane
        # to their fallback width (120x40), so a later mobile client saw wide
        # content wrapped into blank-gap scrollback — the "scrambled output /
        # massive spaces and gaps on mobile" report. Deferring start() past
        # the last fallible await closes the leak.
        await self._send_hello()

        # Fallible (spawns a tmux query) — so it runs BEFORE anything below
        # is registered in module-level state; a raise here leaks nothing.
        tts_filter = await _build_tts_filter(self.session)
        self._register(tts_filter)

        # Hold the relay's output until stream_ready is on the wire: the
        # client takes everything between hello and stream_ready as the
        # replay (and drops PTY output there when it has no replay), so
        # tmux's attach redraw must land after it. Always, not only with a
        # replay: a split window has none, and its redraw was being dropped
        # whenever it beat stream_ready (code review, 2026-11-03).
        self.hold_pty = True
        await self._start_pty(cols, rows)
        self.pty_task = asyncio.create_task(self._pty_lifecycle())
        if self.watch:
            self.watch_task = asyncio.create_task(self._follow_window_size())
        if not self.tile:
            self.ask_task = asyncio.create_task(
                claude_asks.watch(self.session, self._on_claude_ask))
        try:
            if replay:
                await self._send_history(cols, rows)
            # Tell the client we're past the slow part of setup. Used as the
            # signal to fire the first latency-measuring ping — pinging any
            # earlier (e.g. at hello, which is sent BEFORE the history-bytes
            # blob) means the ping queues server-side behind the history send
            # and the round-trip reflects setup time, not network RTT. By the
            # time stream_ready lands the server is one statement away from
            # the main receive() loop and a ping pongs back at network speed.
            await self.send_json({"type": "stream_ready"})
            await self._release_pty()
            # Any non-resize messages we drained while waiting for the
            # initial resize are applied now that the relay exists (inside
            # the try so a malformed one can't skip the teardown in finally).
            for msg in leftover:
                _handle_client_text(msg, self.pty_proc, self.session)
            await self._receive_loop()
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("ws handler error")
        finally:
            await self._teardown()

    async def _ensure_session(self) -> bool:
        """Auto-create the session if it doesn't exist yet. A REMEMBERED one
        (stopped, not started at boot, or a sticky one that vanished) is
        resumed in its recorded directory on its Claude conversation —
        otherwise we'd silently recreate it in $HOME, which was the "after
        a reboot ccpipe lands in /home" bug. Unknown names get a fresh
        claude in $HOME. Returns False if the socket was refused."""
        if await tmux.session_exists(self.session):
            return True
        # A remembered session (stopped when idle, not started at boot, or a
        # sticky one that vanished): resume it in its directory, on the same
        # Claude conversation — see lifecycle.py. If that fails (its
        # directory is gone or outside the root) say so: falling through to
        # a fresh Claude in $HOME under the same name would also overwrite
        # the record of where — and on which conversation — it lived.
        from . import lifecycle
        if lifecycle.resumable(self.session):
            try:
                await lifecycle.resume(self.session)
                return True
            except lifecycle.LifecycleError as exc:
                log.warning("resume of %r on connect failed: %s", self.session, exc.detail)
                if exc.status == 429:
                    await self.ws.close(code=1013, reason="session limit reached")
                else:
                    await self._session_gone(f"couldn't resume {self.session}: {exc.detail}")
                return False
        if await tmux.at_session_cap():
            await self.ws.close(code=1013, reason="session limit reached")
            return False
        else:
            # Same $SHELL -i wrapper as POST /api/sessions, so the session
            # survives claude exiting instead of vanishing with it.
            await tmux.create_session(self.session,
                                       command=tmux.wrap_in_shell(plan_usage.claude_command()))
        return True

    async def _session_gone(self, note: str) -> None:
        """Tell the client the session isn't there (and won't be made), then
        close. The client goes back to the session list and shows *note*."""
        with contextlib.suppress(Exception):
            await self.ws.send_json({"type": "session_gone", "session": self.session, "note": note})
            await self.ws.close(code=1000, reason="no such session")

    async def _send_hello(self) -> None:
        # Best-effort: try to open the mic pipe now so the hello message can
        # advertise voice capability accurately.
        voice_available = _mic_writer.write(b"")  # zero-length write probes the FD
        # Resolve the tmux session's working directory so the client can
        # default file/directory-browse dialogs to the project root the user
        # is actually working in, rather than the fs jail root (typically
        # $HOME). Best-effort: session_cwd may return None if tmux's pane
        # query failed; client falls back to the fs config root in that case.
        session_cwd_value = await tmux.session_cwd(self.session)
        await self.ws.send_json({
            "type": "hello",
            "session": self.session,
            "cwd": session_cwd_value,
            "tts": tts_service.enabled,
            "voice": voice_available,
            # The session's composer draft, restored into the prompt box.
            "draft": drafts.get(self.session),
            # Watch tiles: the size the tmux client was attached at.
            **({"cols": self.watch_size[0], "rows": self.watch_size[1]}
               if self.watch_size else {}),
        })

    def _register(self, tts_filter: Any) -> None:
        """Hook this connection into module-level state. No awaits: nothing
        between here and _start_pty's failure branch / the teardown in run()
        can leave these registered."""
        _active_counters.append(self.counters)
        # Register for credential-rotation / logout kicks (M2).
        _live_ws.add(self.ws)
        _draft_peers.setdefault(self.session, set()).add(self)
        # Subscribe to control-mode events; forward to this WS as JSON.
        self.tmux_sub = control_client.subscribe(self._on_tmux_event)
        # Only the main terminal speaks: a tile subscribing too would play
        # every reply of its session a second time.
        self.tts_sub = _NoTts() if self.tile else tts_service.subscribe(
            on_start=self._on_tts_start, on_chunk=self._on_tts_chunk,
            on_end=self._on_tts_end,
            content_filter=lambda r: not self.outdated() and tts_filter(r),
        )

    def outdated(self) -> bool:
        """The page runs an older build than the one being served. A visible
        one reloads itself within minutes; a hidden one may never: on the
        iPad, pages from earlier builds lived on after their reload and even
        after the app was quit, and played every reply too — and there only
        one page can play, so they aborted the live one (2026-11-03). Such
        pages get no speech and no question announcements."""
        current = _current_build()
        return bool(current and self.build and self.build != current)

    def _unregister(self) -> None:
        with contextlib.suppress(ValueError):
            _active_counters.remove(self.counters)
        _live_ws.discard(self.ws)
        peers = _draft_peers.get(self.session)
        if peers is not None:
            peers.discard(self)
            if not peers:
                _draft_peers.pop(self.session, None)
        self.tmux_sub.cancel()
        self.tts_sub.cancel()

    async def _send_history(self, cols: int, rows: int) -> None:
        """Send the replay: the pane's scrollback as it was when tmux drew
        this client's screen, then a blank screenful. tmux's attach redraw
        (held until this is sent) erases the client's screen and paints the
        pane itself, so the replay carries history only:

        - Captured once tmux has resized the window to this client: history
          reflows on a resize (2026-11-03: captured before, 9 lines were lost
          attaching a phone to a 60x24 window).
        - Lines that scroll into history between the redraw and the capture
          would land twice: in the captured history and again from the held
          output that follows the redraw (measured: 2-3 lines per attach at
          200 lines/s). tmux's history size is read just after the redraw
          (over the control connection, well under a millisecond) and again
          atomically with the capture; the rows added in between are dropped
          from the capture's end. Only whole lines are dropped, and the
          reading can only come after the redraw, so at worst a duplicate
          remains, never a lost row.
        - The blank screenful is the client's height (pane plus status
          line), so all of the history is above the screen when tmux's erase
          comes, and none of it is erased.
        """
        # Wait for tmux's redraw of this client, not just its first bytes:
        # tmux first sends terminal setup (modes, queries, a clear), then
        # draws the screen 0.3-1 ms later (measured). A history size read
        # before the drawing is too small, and dropping by it lost lines.
        # The drawing is the first output with any text in it; a blank,
        # idle pane has none, and then the timeout costs nothing.
        try:
            await asyncio.wait_for(self.pty_redraw.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
        t_redraw = time.monotonic()
        t_seen = 0.0
        seen_history: int | None = None
        deadline = time.monotonic() + 1.0
        while True:
            state = await _pane_state_now(self.session)
            if state is None:
                break
            width, height, status_rows, seen_history = state
            t_seen = time.monotonic()
            if self.watch or (width, height) == (cols, max(1, rows - status_rows)):
                break
            if time.monotonic() >= deadline:
                break                    # another client sets the size: capture as is
            await asyncio.sleep(0.03)
        captured = await _capture_replay_history(self.session)
        if captured is None:
            return
        history, history_rows, pane_width = captured
        dropped = 0
        if seen_history is not None and 0 < history_rows - seen_history <= _SEAM_MAX_ROWS:
            before = history.count(b"\r\n")
            history = _drop_last_rows(history, history_rows - seen_history, pane_width)
            dropped = before - history.count(b"\r\n")
        log.debug("replay %s: history %s→%d rows, %d lines dropped; size read %.1f ms after the "
                  "redraw, capture %.1f ms after", self.session, seen_history, history_rows, dropped,
                  (t_seen - t_redraw) * 1000, (time.monotonic() - t_redraw) * 1000)
        blob = await asyncio.to_thread(_trim_wrap_padding, history, cols)
        if not blob:
            return
        async with self.send_lock:
            await self.ws.send_bytes(bytes([FRAME_PTY_OUTPUT]) + blob + b"\r\n" * rows)

    async def _release_pty(self) -> None:
        """Forward the relay output held during the replay, in order, then
        stop holding."""
        while self.held_pty:
            await self._send_pty(self.held_pty.pop(0))
        self.hold_pty = False

    async def _start_pty(self, cols: int, rows: int) -> None:
        # ── Commit zone ──────────────────────────────────────────────────
        # Spawn the relay HERE, after every fallible handshake await. From
        # this point to the receive loop's `try` in run() there is no await
        # that can raise-and-escape, so once the `tmux attach-session` child
        # exists it is guaranteed to be reaped by run()'s finally
        # (pty_proc.terminate()). See the NOTE in run().
        self.pty_proc = PtyProcess(tmux.attach_argv(self.session, watch=self.watch),
                                   env=tmux.tmux_env(), cols=cols, rows=rows)
        try:
            await self.pty_proc.start()
        except BaseException:
            # start() itself failed (fork/exec — likely under memory
            # pressure) — reap any half-spawned child and undo the
            # registrations before propagating, since the teardown in run()
            # is not yet active. Without this each failure leaked its
            # counters, its _live_ws entry and a tmux subscription that keeps
            # re-checking session existence on every sessions-changed event.
            with contextlib.suppress(Exception):
                await self.pty_proc.terminate()
            self._unregister()
            raise

    async def _pty_lifecycle(self) -> None:
        """Run the PTY pump; on EOF, surface the exit to the client and close
        the WS so the receive loop unblocks.

        Without this the receive loop would keep awaiting messages and
        pty_proc.write() would silently no-op, leaving the WS as a zombie
        until the client disconnects.

        Post-pump sends are guarded with suppress(CancelledError) so the
        client still learns the PTY exited even if teardown is cancelling us
        concurrently (e.g. server shutdown racing PTY EOF). Without that, the
        client would see a silent WS close and have to infer the exit from
        reconnect failure.
        """
        try:
            await pump(self.pty_proc, self.forward_pty_to_ws)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pty pump failed")
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await self.send_json({"type": "pty_exited"})
            except Exception:
                pass
            try:
                await self.ws.close(code=1000, reason="pty exited")
            except Exception:
                pass

    async def _teardown(self) -> None:
        global _mic_owner
        counters, pty_proc = self.counters, self.pty_proc
        # Always emit byte-flow counters on close — this is the diagnostic
        # anchor for "did we lose any PTY bytes on this connection".
        # Lost-byte / send-failure counts being > 0 means forward_pty_to_ws
        # raised at least once (typically a transient WS stall) and the
        # client should have reconnected to recover via capture-pane replay.
        # Search the journal for "ws closed:". Fold in any chunks the relay
        # had to drop (it now applies backpressure instead, so this should
        # stay 0): they count toward bytes_read_pty as well as bytes_lost —
        # otherwise the documented invariant `bytes_read_pty == bytes_sent_ws
        # + bytes_lost` (debug.py + test_ws_byte_accounting) would be false
        # precisely in the loss case it exists to detect.
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
            counters.bytes_lost, counters.send_failures, self.disconnect_code,
        )
        # Release the mic if this WS was the owner so the next connection
        # can claim it.
        if _mic_owner is self.mic_token:
            _mic_owner = None
        # Cancel any in-flight PTT-release tasks. Without this they hold a
        # ref to pty_proc and fire after the WS is gone, writing Esc k into
        # the pty — which on a tmux session that's been re-attached aborts
        # the new client's /voice interaction (see pending_releases).
        for rel_task in list(self.pending_releases):
            rel_task.cancel()
        if self.arm_task is not None:
            self.arm_task.cancel()
        if self.listen_task is not None:
            self.listen_task.cancel()
        self._unregister()
        if self.watch_task is not None:
            self.watch_task.cancel()
        if self.ask_task is not None:
            self.ask_task.cancel()
        self.pty_task.cancel()
        # Await pump cancellation BEFORE tearing down the PTY so a pending
        # send_text/send_bytes doesn't race with the socket close.
        try:
            await asyncio.wait_for(self.pty_task, timeout=2.0)
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
            await self.ws.close()
        except Exception:
            pass

    # ── receive side ───────────────────────────────────────────────────
    async def _receive_loop(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg.get("type") == "websocket.disconnect":
                self.disconnect_code = msg.get("code")
                return
            if (text := msg.get("text")) is not None:
                if not await self._on_text(text):
                    return
            elif (data := msg.get("bytes")) is not None:
                # Same revoked-credential gate as for text frames — a session
                # that's been revoked or whose cred_version has bumped should
                # stop being able to push mic PCM into the FIFO too.
                if not await self._still_authed("binary"):
                    return
                if self.tile:
                    continue                 # tiles have no mic
                _handle_client_binary(data, self.pty_proc, self.mic_limiter, self.mic_token)

    async def _still_authed(self, what: str) -> bool:
        """Re-check the session on incoming frames. authorize_websocket only
        fires at connect, so without this an open WS would survive a logout,
        a credential bump (password change, TOTP toggle) or "sign out
        everywhere" — an attacker holding an authenticated WS could keep
        typing into the PTY. The check is an in-memory lookup; trivial.
        Closing with 1008 tells the frontend it must re-authenticate."""
        if _is_session_still_authed(self.ws):
            return True
        log.info("ws closed mid-stream: session no longer authorized (%s)", what)
        await self.ws.close(code=1008, reason="session revoked")
        return False

    async def _on_text(self, text: str) -> bool:
        """Handle one text frame. Returns False when the loop must stop."""
        # Per-frame size cap. Frontend chunks input at 4 KiB and control
        # messages are tiny, so 64 KiB is well above legitimate traffic. A
        # hijacked page streaming unbounded JSON would otherwise hit the
        # substring sniffs + full json.loads on every keystroke-sized frame.
        # The downstream pty_relay write buffer is the real backpressure for
        # sustained streaming — we don't also impose a sliding-window byte
        # budget because that silently truncates large pastes.
        tlen = len(text.encode("utf-8"))
        if tlen > _TEXT_FRAME_MAX_BYTES:
            _warn_limited("oversized", "oversized text frame (%d > %d bytes); dropping",
                          tlen, _TEXT_FRAME_MAX_BYTES)
            return True
        # Draft frames first: they carry arbitrary typed text, so they must
        # never reach the substring sniffs below (ping / tts_mute / mic_stop).
        if _is_draft_frame(text):
            if not await self._still_authed("draft"):
                return False
            self._on_draft(text)
            return True
        # Intercept "ping" first so we can reply pong from here. The pong
        # lets the frontend detect dead-but-not-yet-closed sockets after
        # Android tab-freeze: it expects a pong (or any server message)
        # within ~45s of its keepalive ping; absence forces a reconnect.
        if _is_ping(text):
            if not await self._still_authed("ping"):
                return False
            # DIAG: measure how long the server spends between observing the
            # ping and getting the pong onto the WS. If this is consistently
            # <1ms but clients report 20ms RTT, the delay is on the network /
            # radio side. If this matches the client-reported delta, it's
            # send_lock contention with the live PTY pump (and the fix is to
            # bypass / prioritise pong sends).
            t0 = time.monotonic()
            await self.send_pong_unlocked()
            log.debug("ping→pong: %.1fms (session=%s frames=%d)",
                      (time.monotonic() - t0) * 1000, self.session,
                      self.counters.frames_forwarded)
            return True
        # Mute state mirror: the client tells us when the user toggles TTS,
        # so we can skip the Kokoro round-trip while nobody's listening.
        # Cheap dispatch — no JSON parse on the hot input path.
        if _is_control_frame(text, '"type":"tts_mute"', '"type": "tts_mute"'):
            try:
                payload = _safe_json_loads(text)
                self.tts_sub.muted = bool(payload.get("value"))
            except Exception:
                pass
            return True
        if _is_control_frame(text, '"type":"mic_stop"', '"type": "mic_stop"'):
            self._on_mic_stop()
            return True
        if _is_control_frame(text, '"type":"mic_error"', '"type": "mic_error"'):
            # The browser couldn't start its mic: log why (cleaned, bounded).
            with contextlib.suppress(Exception):
                msg = _safe_json_loads(text)
                clean = lambda v, n: re.sub(r"[\x00-\x1f]", " ", str(v))[:n]  # noqa: E731
                log.info("mic_error from client: %s: %s (session=%s)",
                         clean(msg.get("name", "?"), 40), clean(msg.get("message", ""), 200), self.session)
            return True
        if _is_control_frame(text, '"type":"tts_log"', '"type": "tts_log"'):
            # What the page's audio player did (played, was refused, heard
            # silence): the only view of playback on a device we can't see.
            # Bounded: 40 lines a minute per connection.
            now = time.monotonic()
            if now - self.tts_log_window >= 60.0:
                self.tts_log_window, self.tts_log_count = now, 0
            self.tts_log_count += 1
            if self.tts_log_count <= 40:
                with contextlib.suppress(Exception):
                    msg = _safe_json_loads(text)
                    log.info("tts client (session=%s build=%s conn=%x): %s", self.session,
                             self.build or "?", id(self) & 0xffff,
                             re.sub(r"[\x00-\x1f]", " ", str(msg.get("msg", "")))[:300])
            return True
        if _is_control_frame(text, '"type":"mic_start"', '"type": "mic_start"'):
            if not await self._still_authed("mic"):
                return False
            if not self.watch:
                self._on_mic_start()
            return True
        if not await self._still_authed("input"):
            return False
        if self.watch:
            return True                      # read-only: no input, no resize
        _handle_client_text(text, self.pty_proc, self.session)
        return True

    def _on_draft(self, text: str) -> None:
        """The prompt box changed on this device: store it as the session's
        draft and relay it to every OTHER connection on the session."""
        try:
            msg = _safe_json_loads(text)
        except ValueError:
            return
        draft = msg.get("text")
        if not isinstance(draft, str):
            return
        stored = drafts.put(self.session, draft)
        for peer in list(_draft_peers.get(self.session, ())):
            if peer is self:
                continue
            task = asyncio.create_task(peer.send_json({"type": "draft", "text": stored}))
            _draft_sends.add(task)
            task.add_done_callback(_draft_sends.discard)

    def _on_mic_start(self) -> None:
        """The client wants to dictate. Rather than the client pressing
        /voice's key blind, the server gets Claude recording and confirms
        it on screen (voice_ptt.arm), then answers mic_armed — the client
        streams audio only after that — or mic_failed with the reason."""
        global _mic_owner
        if self.arm_task is not None and not self.arm_task.done():
            # A new start replaces one still in flight (e.g. stop then start
            # again quickly). No double press: if the old one already got
            # Claude recording, arm() sees that and doesn't press again.
            self.arm_task.cancel()
        if _mic_owner is not None and _mic_owner is not self.mic_token:
            task = asyncio.create_task(self.send_json(
                {"type": "mic_failed", "reason": "Dictation is in use on another device"}))
            _draft_sends.add(task)
            task.add_done_callback(_draft_sends.discard)
            return
        self.arm_task = asyncio.create_task(self._arm_voice())

    async def _arm_voice(self) -> None:
        global _mic_owner
        # A stop still draining must press its key before we press ours.
        pending = [t for t in self.pending_releases if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)
        global _voice_quiet_until
        _voice_quiet_until = time.monotonic() + _VOICE_QUIET_S
        # Claim the mic now, so a stop that arrives mid-arm still schedules
        # a (screen-checked) release.
        _mic_owner = self.mic_token
        _mic_writer.reset()
        if sys.platform == "darwin":
            ok, why = True, "local transcription"     # no /voice to press
        else:
            pty = self.pty_proc
            ok, why = await voice_ptt.arm(
                lambda: tmux.capture_screen(self.session),
                lambda: pty.write(b"\x1bk") if pty is not None else None,
            )
        _voice_quiet_until = time.monotonic() + _VOICE_QUIET_S
        log.info("mic_start: %s (session=%s)", why, self.session)
        if not ok and _mic_owner is self.mic_token:
            _mic_owner = None
        await self.send_json({"type": "mic_armed"} if ok else {"type": "mic_failed", "reason": why})

    def _on_mic_stop(self) -> None:
        """The client has torn down its mic and wants claude's /voice
        push-to-talk released. We CAN'T just forward the release keystroke
        immediately because audio captured in the last ~few-hundred-ms is
        still in flight through the pipe → Pulse → claude STT, and claude's
        STT itself needs another ~1-2s to finalise transcription. So we
        estimate the pipeline drain based on bytes-written stats from
        _mic_writer, add the configured pad, and write the release keystroke
        to the PTY ourselves after waiting that long. The client is no
        longer involved in the release timing."""
        global _mic_owner, _voice_quiet_until
        if self.arm_task is not None and not self.arm_task.done():
            self.arm_task.cancel()       # stopped while waiting for Claude
        if _mic_owner is not self.mic_token:
            return
        if sys.platform == "darwin":
            # macOS: no Pulse pipeline to drain. The MicTranscriber holds the
            # entire utterance in memory; hand it off to whisper-cpp
            # asynchronously and let it type the result into the PTY when
            # transcription finishes. Fire-and-forget so the WS receive loop
            # stays responsive; whisper is ~real-time on Apple Silicon (1-2 s
            # for short utterances on base.en).
            log.info("mic_stop: bytes=%d drops=%d → local transcribe",
                     _mic_writer.bytes_written, _mic_writer.drops)
            asyncio.create_task(_mic_writer.finalize(self.pty_proc))  # type: ignore[attr-defined]
            _mic_owner = None
            return
        # Linux: drain Pulse, wait the configured pad for claude's STT to
        # finalise, then write the release keystroke. Use a local module
        # reference so reload-during-dev (importlib.reload) picks up edits.
        from . import config as _app_config
        cfg = _app_config.load().mic
        drain_s = _mic_writer.estimate_drain_seconds()
        pad_s = cfg.drain_pad_ms / 1000.0
        total = drain_s + pad_s
        _voice_quiet_until = time.monotonic() + total + _VOICE_QUIET_S
        log.info("mic_stop: bytes=%d drops=%d drain=%.2fs pad=%.2fs total=%.2fs %s",
                 _mic_writer.bytes_written, _mic_writer.drops, drain_s, pad_s, total,
                 _mic_writer.levels.summary())
        _mic_writer.reset()
        _mic_owner = None
        # M3: cancel any prior pending release from this same handler before
        # scheduling a new one. Without this, a rapid mic_start → mic_stop →
        # mic_start → mic_stop sequence leaves the first release task in
        # flight — its Esc k fires mid-second-recording's drain and toggles
        # claude /voice at the wrong time. At most one release per WS is ever
        # meaningful; older ones are stale.
        for old in list(self.pending_releases):
            old.cancel()
        self.pending_releases.clear()
        # Track the release task so teardown can cancel it on disconnect,
        # and only write Esc k if no other handler has claimed the mic in the
        # meantime. The lambda's default arg binds mic_token at scheduling
        # time — gating on `_mic_owner is None or _mic_owner is mt` lets a
        # re-record from the SAME handler still get its drain-end release
        # fired, while a swap to a different owner correctly suppresses it.
        rel_task = asyncio.create_task(_release_ptt_after(
            self.pty_proc, total,
            still_authoritative=(lambda mt=self.mic_token: _mic_owner is None or _mic_owner is mt),
            claude_recording=lambda: voice_ptt.should_release(lambda: tmux.capture_screen(self.session)),
        ))
        self.pending_releases.add(rel_task)
        rel_task.add_done_callback(self.pending_releases.discard)

    async def _follow_window_size(self) -> None:
        """Watch tiles: keep the read-only client exactly the session's size
        (it's left out of tmux's sizing, so it must follow along) and tell
        the browser, which rescales the tile."""
        while True:
            await asyncio.sleep(_WATCH_SIZE_POLL_S)
            try:
                size = await _watch_client_size(self.session)
            except Exception:
                continue
            if size == self.watch_size or self.pty_proc is None:
                continue
            self.watch_size = size
            self.pty_proc.resize(*size)
            await self.send_json({"type": "size", "cols": size[0], "rows": size[1]})

    # ── send side ──────────────────────────────────────────────────────
    async def send_json(self, msg: dict) -> bool:
        async with self.send_lock:
            try:
                await self.ws.send_json(msg)
                return True
            except Exception as exc:
                log.debug("send_json failed: %s", exc)
                return False

    async def send_pong_unlocked(self) -> bool:
        """Send a pong WITHOUT acquiring send_lock.

        Pongs are 14 bytes and the WS frame is atomic at the protocol layer
        (no fragmentation), so they don't need to serialise against PTY /
        TTS sends. Without this bypass a slow chunk send holding send_lock
        can hold the pong past the client's 45s stale-check, forcing a
        spurious reconnect of an otherwise-healthy socket.
        """
        try:
            await self.ws.send_json({"type": "pong"})
            return True
        except Exception as exc:
            log.debug("send_pong failed: %s", exc)
            return False

    async def send_binary(self, prefix: int, payload: bytes) -> bool:
        async with self.send_lock:
            try:
                await self.ws.send_bytes(bytes([prefix]) + payload)
                return True
            except Exception as exc:
                log.debug("send_bytes failed: %s", exc)
                return False

    async def forward_pty_to_ws(self, data: bytes) -> None:
        # Send PTY bytes as a WS binary frame so xterm receives raw UTF-8
        # without a decode/encode roundtrip. Crucially this also avoids
        # corrupting multi-byte codepoints split across 64 KiB read
        # boundaries (which `bytes.decode(errors="replace")` would mangle).
        # Prefixed with FRAME_PTY_OUTPUT so a PTY chunk that happens to
        # start with FRAME_TTS_AUDIO (0x02 = Ctrl-B in normal terminal
        # output) doesn't get misclassified as an audio chunk on the client.
        #
        # If send_bytes raises (WS disconnected, transport stall, etc.) we
        # re-raise so pump() exits and _pty_lifecycle cleanly closes the WS.
        # Swallowing it silently leaked PTY bytes from xterm whenever the WS
        # hiccupped — they'd never reach the client's buffer but would still
        # be in tmux's pane, producing the "gap until I refresh and
        # capture-pane recovers them" symptom. Re-raising trades a warning +
        # a reconnect for guaranteed eventual consistency.
        self._watch_for_listening(data)
        if self.hold_pty:
            self.held_pty.append(data)
            if not self.pty_redraw.is_set() and _has_visible_text(data):
                self.pty_redraw.set()
            return
        await self._send_pty(data)

    def _watch_for_listening(self, data: bytes) -> None:
        """Claude can start recording without ccpipe: its /voice key pressed
        in the terminal (Alt+K). Its microphone is ccpipe's pipe, though, and
        browsers only open a mic from a tap, so tell the client, which offers
        "Claude is listening · tap to talk". Cheap byte check on the output
        (tmux sends Claude's footer text when it changes), then a screen
        check to confirm."""
        if self.watch or (self.listen_task is not None and not self.listen_task.done()):
            return
        seam = self.listen_tail + data[:16]
        # The last 16 bytes seen, even when they arrived in several small
        # chunks (one chunk's tail alone missed a phrase split three ways).
        self.listen_tail = data[-16:] if len(data) >= 16 else (self.listen_tail + data)[-16:]
        if b"tap to send" not in data and b"tap to send" not in seam:
            return
        if _mic_owner is not None or time.monotonic() < _voice_quiet_until:
            return
        self.listen_task = asyncio.create_task(self._listening_prompt())

    async def _listening_prompt(self) -> None:
        capture = lambda: tmux.capture_screen(self.session)  # noqa: E731
        if voice_ptt.voice_state(await capture()) != "recording" or _mic_owner is not None:
            return
        log.info("voice: Claude is listening without a ccpipe mic (session=%s)", self.session)
        await self.send_json({"type": "voice_listening", "on": True})
        try:
            while True:
                await asyncio.sleep(1.0)
                if _mic_owner is not None or time.monotonic() < _voice_quiet_until:
                    break                # a device took over dictation
                if voice_ptt.voice_state(await capture()) != "recording":
                    break
        finally:
            with contextlib.suppress(Exception):
                await self.send_json({"type": "voice_listening", "on": False})

    async def _send_pty(self, data: bytes) -> None:
        counters = self.counters
        counters.bytes_read_pty += len(data)
        async with self.send_lock:
            try:
                await self.ws.send_bytes(bytes([FRAME_PTY_OUTPUT]) + data)
                counters.bytes_sent_ws += len(data)
                counters.frames_forwarded += 1
            except Exception as exc:
                counters.send_failures += 1
                counters.bytes_lost += len(data)
                log.warning("send_bytes(pty) failed (%d bytes lost from this "
                            "ws; client should reconnect and re-capture pane): %s",
                            len(data), exc)
                raise

    # ── fan-in from tmux control mode and TTS ─────────────────────────
    async def _on_tmux_event(self, event: TmuxEvent) -> None:
        if event.name == "sessions-changed":
            # list_sessions() is cached briefly behind a lock, so every
            # connected tab checking at once shares ONE tmux call instead of
            # spawning a has-session subprocess each.
            if self.session not in {s.name for s in await tmux.list_sessions()}:
                await self.send_json({"type": "session_gone", "session": self.session})
                return
        await self.send_json({
            "type": "session_event",
            "event": event.name,
            "args": event.args,
        })

    async def _on_tts_start(self, text: str, utterance: str = "", uid: int = 0) -> None:
        if not self.alive:
            return
        # `text` is the sentence starting now; `utterance` the whole spoken
        # reply it's part of and `id` its number, so the "replay" pill
        # replays all of it and the page notifies once per reply. Up to
        # 4000 chars, /api/tts/speak's limit.
        if not await self.send_json({"type": "tts_start", "text": text[:4000],
                                     "utterance": (utterance or text)[:4000],
                                     "id": uid}):
            self._tts_dead()

    async def _on_tts_chunk(self, chunk: bytes) -> None:
        if not self.alive:
            return
        if not await self.send_binary(FRAME_TTS_AUDIO, chunk):
            self._tts_dead()

    async def _on_tts_end(self) -> None:
        if not self.alive:
            return
        if not await self.send_json({"type": "tts_end"}):
            self._tts_dead()

    async def _on_claude_ask(self, ask: claude_asks.Ask) -> None:
        """Claude is waiting on a question or a permission prompt: tell the
        page (for a notification) and say it, unless voice is muted or set
        to read nothing. The id is the spoken copy's, so the page doesn't
        notify a second time when it finishes playing."""
        if not self.alive or self.outdated():
            return
        uid = None
        if app_config.load().tts.scope != "off" and not isinstance(self.tts_sub, _NoTts):
            uid = tts_service.speak_to(ask.spoken, self.tts_sub)
        await self.send_json({"type": "claude_ask", "kind": ask.kind, "title": ask.title,
                              "text": ask.body[:4000] or ask.spoken, "id": uid})

    def _tts_dead(self) -> None:
        self.alive = False
        self.tts_sub.cancel()


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

async def _capture_replay_history(session: str) -> tuple[bytes, int, int] | None:
    """The pane's scrollback only (not the visible screen), for the
    replay, with tmux's history size and the pane's width at the same
    instant: one command list, which tmux runs without the pane's program
    output being processed in between. None if the capture failed."""
    target = tmux.pane_target(session)
    out = await _tmux_capture([
        "capture-pane", "-t", target, "-p", "-e", "-J",
        "-S", f"-{_HISTORY_MAX_LINES}", "-E", "-1",
        # Last, so a capture cut at the front still has it.
        ";", "display-message", "-p", "-t", target, "#{history_size} #{pane_width}",
    ])
    if out is None:
        return None
    body, _, last = out.rstrip(b"\n").rpartition(b"\n")
    parts = last.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    history_rows, width = int(parts[0]), int(parts[1])
    if history_rows == 0:
        # With no history, "-E -1" returns the screen's first row instead.
        return b"", 0, width
    return _replay_bytes(body), history_rows, width


async def _tmux_capture(args: list[str]) -> bytes | None:
    """Run tmux with *args* and return its stdout, keeping at most the
    last _HISTORY_MAX_BYTES (+ slack) of it, starting at a line. None on
    any failure or empty output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux.TMUX_BIN, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=tmux.tmux_env(),
        )
    except FileNotFoundError:
        return None
    assert proc.stdout is not None
    try:
        out, cut = await asyncio.wait_for(
            _read_tail(proc.stdout, _HISTORY_MAX_BYTES + _HISTORY_READ_SLACK),
            timeout=_HISTORY_CAPTURE_TIMEOUT_S)
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        # kill() then BOUNDED wait — bare proc.wait() can block
        # forever if the child is stuck in uninterruptible sleep,
        # and history capture happens during WS attach so a stuck
        # subprocess would stall every reconnect.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(ProcessLookupError, asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        return None
    if proc.returncode != 0 or not out:
        return None
    if cut:
        # The tail read started mid-line (possibly mid escape sequence);
        # drop everything up to the first line break.
        nl = out.find(b"\n")
        out = out[nl + 1:] if nl != -1 else b""
    return out or None


def _replay_bytes(out: bytes) -> bytes:
    """capture-pane output as xterm-ready replay bytes."""
    # tmux capture-pane joins lines with LF. xterm.js wants CRLF to start
    # a new line at column 0; otherwise lines stack on the right of the
    # previous one. Normalise (idempotent if already CRLF).
    normalised = out.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    # No trailing CRLF: the caller decides what follows the last line
    # (_send_history: a blank screenful).
    if normalised.endswith(b"\r\n"):
        normalised = normalised[:-2]
    # Cap the blob size, truncating the OLDEST content from the front so
    # the newest lines always survive. Trim to the next CRLF boundary so we
    # never emit a half escape sequence / partial line.
    if len(normalised) > _HISTORY_MAX_BYTES:
        cut = len(normalised) - _HISTORY_MAX_BYTES
        nl = normalised.find(b"\r\n", cut)
        normalised = normalised[nl + 2:] if nl != -1 else normalised[cut:]
    return normalised


# Escape sequences: CSI, OSC / DCS / APC / PM strings, and two-byte ESC x.
_ESCAPES = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[\]P_^][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[ -~]")
_VISIBLE = re.compile(rb"[!-~\x80-\xff]")


def _has_visible_text(data: bytes) -> bool:
    """Any printable character outside escape sequences: tmux's terminal
    setup has none, its drawing of the screen does."""
    return _VISIBLE.search(_ESCAPES.sub(b"", data)) is not None


async def _pane_state_now(session: str) -> tuple[int, int, int, int] | None:
    """tmux.pane_state, over the control connection when it's up. Its
    reply is in well under a millisecond; spawning tmux takes ~5 ms, and
    lines that scroll into history in that time after the attach redraw
    are the replay's leftover duplicates (measured: 1-2 lines per attach
    at 200 lines/s)."""
    try:
        tmux.safe_name(session)      # it goes into a tmux command line
    except ValueError:
        return await tmux.pane_state(session)
    lines = await control_client.query(
        f"display-message -p -t '{tmux.pane_target(session)}' '{tmux.PANE_STATE_FORMAT}'")
    state = tmux.parse_pane_state(lines[0]) if lines else None
    return state if state is not None else await tmux.pane_state(session)


# Rows added between the size check and the capture that _send_history
# will drop; more than this means something else happened (a clear, the
# history limit's trim) and nothing is dropped.
_SEAM_MAX_ROWS = 10_000


def _pane_rows(line: bytes, width: int) -> int:
    """How many pane rows a captured (-J joined) line took, at *width*,
    counted high where unsure: a wide character can leave a column empty
    at a row's end, so each one counts an extra column."""
    if len(line) <= width:
        return 1
    text = _ANSI.sub(b"", line).decode("utf-8", errors="replace")
    cols = _visible_width(line)
    wide = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))
    return max(1, -(-(cols + wide) // width))


def _drop_last_rows(history: bytes, rows: int, width: int) -> bytes:
    """Drop the last *rows* pane rows from a captured history, whole lines
    only. A line that would be cut part-way is kept: the cost is a
    duplicate, where cutting into it would lose rows."""
    if rows <= 0 or not history or width <= 0:
        return history
    lines = history.split(b"\r\n")
    while lines and rows > 0:
        r = _pane_rows(lines[-1], width)
        if r > rows:
            break
        lines.pop()
        rows -= r
    return b"\r\n".join(lines)


# Trailing padding in the replay. capture-pane -J keeps each line's
# trailing spaces, and Claude Code pads its lines to the width it drew at
# — so history written while the desktop had the window at ~190 columns
# arrived on a 51-column phone as one line of text plus three or four
# rows of blank wrap (measured 2026-11-02: 2,287 of 6,103 replayed rows
# blank; tmux itself held 2,043 lines). Only lines wider than the client
# are trimmed, so same-width history keeps its padding (and background).
_TRAILING_PAD = re.compile(rb"(?:[ ]|\x1b\[[0-9;:]*m)+$")
_ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _visible_width(line: bytes) -> int:
    text = _ANSI.sub(b"", line).decode("utf-8", errors="replace")
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or ch == "\u200d":
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _trim_wrap_padding(data: bytes, cols: int) -> bytes:
    """Drop the trailing spaces (keeping any SGR codes among them) from
    replay lines wider than *cols*, so padding doesn't wrap into blank
    rows on a narrower client. (The replay is history only, so every line
    can be trimmed: tmux's redraw paints the screen itself.)
    """
    if not data or cols <= 0:
        return data
    lines = data.split(b"\r\n")
    for i, line in enumerate(lines):
        if len(line) <= cols:
            continue               # bytes >= visible width: can't wrap
        m = _TRAILING_PAD.search(line)
        if m is None or b" " not in m.group(0):
            continue
        if _visible_width(line) <= cols:
            continue
        lines[i] = line[:m.start()] + m.group(0).replace(b" ", b"")
    return b"\r\n".join(lines)


# Raw capture bytes kept beyond _HISTORY_MAX_BYTES while reading: CRLF
# normalisation grows the blob, and the front is trimmed to a line boundary
# afterwards, so keep some headroom rather than exactly the cap.
_HISTORY_READ_SLACK = 1 << 20


async def _read_tail(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    """Read *stream* to EOF keeping only the last *cap* bytes (the newest
    lines — what history replay wants), so a huge capture never sits in
    memory whole. Returns (tail, whether anything was cut from the front)."""
    buf = bytearray()
    cut = False
    while chunk := await stream.read(65536):
        buf += chunk
        if len(buf) > 2 * cap:
            del buf[:len(buf) - cap]
            cut = True
    if len(buf) > cap:
        del buf[:len(buf) - cap]
        cut = True
    return bytes(buf), cut


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
            except (KeyError, ValueError, TypeError, OverflowError):
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
        _warn_limited("non-json", "non-JSON text frame: %r", text[:200])
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
            except (TypeError, ValueError, OverflowError):
                return  # malformed resize (incl. 1e999 → inf), ignore
            pty_proc.resize(cols, rows)
            # Keep the per-session size cache fresh so a later reconnect
            # seeds from the current (possibly rotated) width, not a stale
            # one. See _last_client_size / _FALLBACK_COLS.
            if session is not None:
                _remember_client_size(session, cols, rows)
        case "ping" | "tts_mute" | "tts_log" | "mic_stop" | "mic_start" | "mic_error":
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
            _warn_limited("unknown-type", "unknown text message type: %r",
                          str(msg.get("type"))[:40])


def _handle_client_binary(data: bytes, pty_proc: PtyProcess,
                           limiter: "_SlidingByteLimiter",
                           mic_token: object) -> None:
    if not data:
        return
    frame_type = data[0]
    if frame_type == FRAME_MIC_PCM:
        payload = data[1:]
        if len(payload) > _MIC_MAX_FRAME_BYTES:
            _warn_limited("mic-big", "mic frame too large (%d bytes); dropping", len(payload))
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
