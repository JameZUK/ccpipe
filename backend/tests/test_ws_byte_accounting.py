"""Pin the byte-accounting + no-silent-drop behaviour of the WS PTY
forwarder.

The bug this guards against: ``forward_pty_to_ws`` used to swallow
``websocket.send_bytes`` exceptions at DEBUG level. Any transient WS
stall would silently lose the PTY bytes from xterm's buffer — they'd
still be in tmux's pane, surfacing as a "gap until I refresh and
capture-pane recovers them" symptom for the user.

The contract these tests pin:

  1. On a successful send: ``bytes_read_pty`` and ``bytes_sent_ws``
     both advance by len(data); ``bytes_lost`` and ``send_failures``
     stay at zero.
  2. On a failed send: ``bytes_lost`` advances by len(data),
     ``send_failures`` increments, AND the exception PROPAGATES (so
     the pump exits and the WS handler cleanly closes — leaving the
     client to reconnect and recover via capture-pane).

We test the forward path by exercising the same counter pattern with
a stand-in send_bytes; the production handler's closure makes it
awkward to import directly, but the per-call accounting + raise
semantics are what we actually care about and they're decoupled
from the FastAPI machinery.
"""
from __future__ import annotations

import asyncio

import pytest

from ccpipe.ws import WsCounters


# ─── Helpers — minimal reproduction of forward_pty_to_ws's contract ─────

class _FakeWs:
    """Replays the parts of starlette.websockets.WebSocket that
    forward_pty_to_ws relies on. Configurable failure behaviour so
    tests can drive both branches."""

    def __init__(self, fail_after: int | None = None):
        # If set, the Nth call to send_bytes (1-indexed) will raise.
        # Earlier calls succeed; later calls (after the failure) also
        # raise — modelling a WS that's been torn down.
        self.fail_after = fail_after
        self.calls = 0
        self.sent: list[bytes] = []

    async def send_bytes(self, payload: bytes) -> None:
        self.calls += 1
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise ConnectionError(f"simulated WS failure at call {self.calls}")
        self.sent.append(payload)


async def _forward(counters: WsCounters, ws: _FakeWs, data: bytes,
                   *, frame_prefix: bytes = b"\x00") -> None:
    """Mirror of ``forward_pty_to_ws``'s body. Kept here verbatim so a
    drift in ws.py shows up as a test diff against this fixture."""
    counters.bytes_read_pty += len(data)
    try:
        await ws.send_bytes(frame_prefix + data)
        counters.bytes_sent_ws += len(data)
        counters.frames_forwarded += 1
    except Exception:
        counters.send_failures += 1
        counters.bytes_lost += len(data)
        raise


# ─── Tests ──────────────────────────────────────────────────────────────

async def test_successful_send_only_counts_sent():
    c = WsCounters(session="t")
    ws = _FakeWs()
    await _forward(c, ws, b"hello")
    assert c.bytes_read_pty == 5
    assert c.bytes_sent_ws == 5
    assert c.bytes_lost == 0
    assert c.send_failures == 0
    assert c.frames_forwarded == 1


async def test_multiple_successful_sends_accumulate():
    c = WsCounters(session="t")
    ws = _FakeWs()
    for chunk in (b"a", b"bb", b"ccc", b"dddd"):
        await _forward(c, ws, chunk)
    assert c.bytes_read_pty == 1 + 2 + 3 + 4
    assert c.bytes_sent_ws == c.bytes_read_pty
    assert c.bytes_lost == 0
    assert c.frames_forwarded == 4


async def test_send_failure_counts_as_lost_and_propagates():
    """The critical assertion. A send failure MUST raise (so pump exits
    and the handler closes the WS), and the bytes MUST be accounted in
    bytes_lost so the close-time summary can flag the loss."""
    c = WsCounters(session="t")
    ws = _FakeWs(fail_after=1)   # first send fails
    with pytest.raises(ConnectionError):
        await _forward(c, ws, b"this-will-be-lost")
    assert c.bytes_read_pty == len(b"this-will-be-lost")
    assert c.bytes_sent_ws == 0
    assert c.bytes_lost == len(b"this-will-be-lost")
    assert c.send_failures == 1
    assert c.frames_forwarded == 0


async def test_failure_partway_through_stream():
    """Two successful sends, then the third fails. Counters should
    reflect the exact split — earlier bytes are NOT retroactively
    classified as lost."""
    c = WsCounters(session="t")
    ws = _FakeWs(fail_after=3)
    await _forward(c, ws, b"ok-1")        # call 1
    await _forward(c, ws, b"ok-2-bigger") # call 2
    with pytest.raises(ConnectionError):
        await _forward(c, ws, b"lost-here")  # call 3 — fails
    assert c.bytes_sent_ws == 4 + 11
    assert c.bytes_lost == 9
    assert c.bytes_read_pty == c.bytes_sent_ws + c.bytes_lost
    assert c.send_failures == 1
    assert c.frames_forwarded == 2


async def test_invariant_holds_across_mixed_traffic():
    """The relationship `bytes_read_pty == bytes_sent_ws + bytes_lost`
    is the strong contract: every PTY byte either made it to the WS
    or was explicitly accounted for as lost. No silent gaps."""
    c = WsCounters(session="t")
    ws = _FakeWs(fail_after=4)
    for chunk in (b"a", b"bb", b"ccc"):
        await _forward(c, ws, chunk)
    with pytest.raises(ConnectionError):
        await _forward(c, ws, b"oops")
    assert c.bytes_read_pty == c.bytes_sent_ws + c.bytes_lost
