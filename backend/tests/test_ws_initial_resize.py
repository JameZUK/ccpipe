"""Tests for the initial-resize wait that fixes the first-attach-wrong-size race."""
import json
import asyncio

import pytest

from ccpipe.ws import (
    _wait_for_initial_resize,
    _INITIAL_RESIZE_TIMEOUT_S,
    _FALLBACK_COLS,
    _FALLBACK_ROWS,
    _last_client_size,
)


@pytest.fixture(autouse=True)
def _clear_size_cache():
    # The per-session last-size cache is module-global; clear it so one
    # test's remembered size can't seed another's fallback.
    _last_client_size.clear()
    yield
    _last_client_size.clear()


class _FakeWS:
    def __init__(self, messages: list[dict]):
        self._messages = list(messages)

    async def receive(self):
        if not self._messages:
            # Simulate carrier idle: hang for longer than the deadline.
            await asyncio.sleep(_INITIAL_RESIZE_TIMEOUT_S * 2)
            return {"type": "websocket.disconnect"}
        return self._messages.pop(0)


async def test_returns_resize_when_first_message():
    ws = _FakeWS([
        {"type": "websocket.receive",
         "text": json.dumps({"type": "resize", "cols": 100, "rows": 30})},
    ])
    cols, rows, leftover = await _wait_for_initial_resize(ws, "s")
    assert cols == 100 and rows == 30
    assert leftover == []


async def test_buffers_non_resize_until_resize_arrives():
    ws = _FakeWS([
        {"type": "websocket.receive",
         "text": json.dumps({"type": "input", "data": "x"})},
        {"type": "websocket.receive",
         "text": json.dumps({"type": "ping"})},
        {"type": "websocket.receive",
         "text": json.dumps({"type": "resize", "cols": 90, "rows": 24})},
    ])
    cols, rows, leftover = await _wait_for_initial_resize(ws, "s")
    assert cols == 90 and rows == 24
    assert len(leftover) == 2
    assert json.loads(leftover[0])["type"] == "input"
    assert json.loads(leftover[1])["type"] == "ping"


async def test_falls_back_to_narrow_default_on_timeout():
    # An unseen session with no resize falls back to the NARROW default,
    # never the old wide 120 — a wide guess strands gappy frames in
    # mobile scrollback once the real (narrower) size arrives.
    ws = _FakeWS([])  # no messages → blocks past deadline
    cols, rows, leftover = await _wait_for_initial_resize(ws, "fresh")
    assert (cols, rows) == (_FALLBACK_COLS, _FALLBACK_ROWS)
    assert (cols, rows) == (80, 24)
    assert leftover == []


async def test_reconnect_seeds_from_last_known_size():
    # First connect learns the client's width…
    ws1 = _FakeWS([
        {"type": "websocket.receive",
         "text": json.dumps({"type": "resize", "cols": 58, "rows": 44})},
    ])
    cols, rows, _ = await _wait_for_initial_resize(ws1, "phone")
    assert (cols, rows) == (58, 44)
    # …so a reconnect that times out before its resize arrives re-attaches
    # at that same width instead of the wide/narrow fallback. This is what
    # stops a mobile reconnect storm from drawing a wrong-width frame.
    ws2 = _FakeWS([])
    cols, rows, _ = await _wait_for_initial_resize(ws2, "phone")
    assert (cols, rows) == (58, 44)


async def test_timed_out_fallback_does_not_poison_cache():
    # A fallback (no real resize) must NOT be written to the size cache,
    # otherwise a single bad attach would pin the session's seed width.
    ws = _FakeWS([])
    await _wait_for_initial_resize(ws, "ghost")
    assert "ghost" not in _last_client_size


async def test_clamps_invalid_dimensions_to_one():
    ws = _FakeWS([
        {"type": "websocket.receive",
         "text": json.dumps({"type": "resize", "cols": 0, "rows": -5})},
    ])
    cols, rows, _ = await _wait_for_initial_resize(ws, "s")
    assert cols == 1 and rows == 1


async def test_ignores_disconnect_message():
    ws = _FakeWS([{"type": "websocket.disconnect"}])
    cols, rows, leftover = await _wait_for_initial_resize(ws, "fresh")
    assert (cols, rows) == (_FALLBACK_COLS, _FALLBACK_ROWS)
    assert leftover == []
