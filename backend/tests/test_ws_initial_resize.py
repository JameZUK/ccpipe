"""Tests for the initial-resize wait that fixes the first-attach-wrong-size race."""
import json
import asyncio
from unittest.mock import AsyncMock

import pytest

from ccpipe.ws import _wait_for_initial_resize, _INITIAL_RESIZE_TIMEOUT_S


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
    cols, rows, leftover = await _wait_for_initial_resize(ws)
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
    cols, rows, leftover = await _wait_for_initial_resize(ws)
    assert cols == 90 and rows == 24
    assert len(leftover) == 2
    assert json.loads(leftover[0])["type"] == "input"
    assert json.loads(leftover[1])["type"] == "ping"


async def test_falls_back_on_timeout():
    ws = _FakeWS([])  # no messages → blocks past deadline
    cols, rows, leftover = await _wait_for_initial_resize(ws)
    assert (cols, rows) == (120, 40)
    assert leftover == []


async def test_clamps_invalid_dimensions_to_one():
    ws = _FakeWS([
        {"type": "websocket.receive",
         "text": json.dumps({"type": "resize", "cols": 0, "rows": -5})},
    ])
    cols, rows, _ = await _wait_for_initial_resize(ws)
    assert cols == 1 and rows == 1


async def test_ignores_disconnect_message():
    ws = _FakeWS([{"type": "websocket.disconnect"}])
    cols, rows, leftover = await _wait_for_initial_resize(ws)
    assert (cols, rows) == (120, 40)
    assert leftover == []
