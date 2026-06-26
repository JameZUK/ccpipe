"""Regression test for the leaked `tmux attach-session` relay.

The bug (diagnosed from 12 orphaned `tmux attach-session` processes
accumulated over a server's 4-day uptime, 8 of them on a single
session): `handle_terminal_ws` used to spawn the PTY relay
(`PtyProcess.start()`) BEFORE the fallible attach handshake — the
`hello` / history / `stream_ready` sends. Each of those raises if the
client has already disconnected. Because the relay was spawned before
the receive loop's `try/finally`, a mid-handshake disconnect left the
`tmux attach-session` child attached forever.

Why it mattered: under tmux `window-size=latest`, an orphaned relay
pins the shared pane to its own (fallback 120x40) width. A later
mobile client attaching at ~50 cols then receives 120-col content that
wraps into mostly-blank rows — the user-reported "scrambled output /
massive spaces and gaps on mobile", which was session-specific exactly
because only sessions carrying an orphan were affected.

The fix moves `PtyProcess.start()` into a no-await "commit zone" after
the last fallible handshake await, so the relay is spawned only once
the reaping `finally` is guaranteed to run.

These tests pin the invariant: **a relay is spawned only when it will
be reaped.** The leak test fails on the pre-fix ordering (start() would
run before the failing hello, with no matching terminate()).
"""
from __future__ import annotations

import asyncio

import pytest

from fastapi import WebSocketDisconnect

import ccpipe.ws as wsmod


class _FakePty:
    """Stand-in for PtyProcess that records start()/terminate()."""

    def __init__(self, *args, **kwargs):
        self.start_calls = 0
        self.terminate_calls = 0
        _FakePty.instances.append(self)

    async def start(self):
        self.start_calls += 1

    async def terminate(self):
        self.terminate_calls += 1

    def write(self, data):  # pragma: no cover - not exercised here
        pass

    def resize(self, cols, rows):  # pragma: no cover
        pass

    def bytes_dropped(self):
        return 0


_FakePty.instances: list["_FakePty"] = []


class _FakeSub:
    def cancel(self):
        pass


class _FakeWS:
    """Minimal WebSocket: drives accept/receive/send for the handler.

    `fail_hello` makes the FIRST send_json raise — the handler sends the
    `hello` frame via a direct `websocket.send_json(...)` (no swallowing
    try/except), so this simulates the client vanishing mid-handshake.
    """

    def __init__(self, *, fail_hello: bool = False):
        self.fail_hello = fail_hello
        self._sends = 0

    async def accept(self):
        pass

    async def receive(self):
        # Main loop: report an immediate clean disconnect.
        return {"type": "websocket.disconnect", "code": 1001}

    async def send_json(self, msg):
        self._sends += 1
        if self.fail_hello and self._sends == 1:
            raise WebSocketDisconnect(code=1001)

    async def send_bytes(self, data):
        pass

    async def send_text(self, text):
        pass

    async def close(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def _patch_deps(monkeypatch):
    _FakePty.instances.clear()

    async def _true(*a, **k):
        return True

    async def _cwd(*a, **k):
        return "/home/tester"

    async def _no_history(*a, **k):
        return b""

    async def _resize(_ws, _session):
        return (50, 40, [])

    async def _tts_filter(_session):
        return None

    async def _pump_forever(_pty, _on_output):
        # Block until the handler cancels our task in its finally.
        await asyncio.Event().wait()

    monkeypatch.setattr(wsmod.tmux, "session_exists", _true)
    monkeypatch.setattr(wsmod.tmux, "attach_argv", lambda name: ["tmux", "attach", "-t", name])
    monkeypatch.setattr(wsmod.tmux, "session_cwd", _cwd)
    monkeypatch.setattr(wsmod, "_capture_session_history", _no_history)
    monkeypatch.setattr(wsmod, "_wait_for_initial_resize", _resize)
    monkeypatch.setattr(wsmod, "_build_tts_filter", _tts_filter)
    monkeypatch.setattr(wsmod, "PtyProcess", _FakePty)
    monkeypatch.setattr(wsmod, "pump", _pump_forever)
    monkeypatch.setattr(wsmod.control_client, "subscribe", lambda *a, **k: _FakeSub())
    monkeypatch.setattr(wsmod.tts_service, "subscribe", lambda *a, **k: _FakeSub())
    monkeypatch.setattr(wsmod._mic_writer, "write", lambda data: False)
    monkeypatch.setattr(wsmod, "_is_session_still_authed", lambda _ws: True)


async def test_relay_reaped_on_normal_disconnect():
    """Happy path: relay is spawned, and reaped when the client leaves."""
    ws = _FakeWS()
    await wsmod.handle_terminal_ws(ws, "sess")
    assert len(_FakePty.instances) == 1
    pty = _FakePty.instances[0]
    assert pty.start_calls == 1
    assert pty.terminate_calls == 1, "relay must be terminated on disconnect"


async def test_no_relay_leak_when_handshake_send_fails():
    """The leak invariant: if the client disconnects during the attach
    handshake (hello send raises), the relay must NEVER be left orphaned.

    With the fix, start() runs only after the handshake, so it is never
    reached here — start_calls == 0. On the pre-fix ordering start() ran
    before the failing hello and terminate() was never called, so this
    asserts the exact regression away."""
    ws = _FakeWS(fail_hello=True)
    with pytest.raises(WebSocketDisconnect):
        await wsmod.handle_terminal_ws(ws, "sess")
    # Either no relay was spawned, or if one was it was also reaped —
    # never a spawn without a matching terminate.
    for pty in _FakePty.instances:
        assert pty.start_calls == pty.terminate_calls, (
            "relay spawned during a failed handshake was left orphaned"
        )
    spawned_unreaped = sum(
        p.start_calls - p.terminate_calls for p in _FakePty.instances
    )
    assert spawned_unreaped == 0
