"""Tests for tmux scrollback capture sent at WS attach.

The old implementation captured *only* scrollback (``-E -1``) and
appended ``viewport_rows`` blank LFs as padding, so the captured tail
would scroll past xterm's visible region before tmux's attach redraw
overwrote it. That seam was fragile (off-by-N when status bar / multi-
pane layouts shifted the actual pane height); the
``scripts/scrollback-doctor.py`` harness demonstrated lines silently
disappearing at the seam.

The new implementation captures the *whole* pane (no ``-E`` filter)
and adds no padding. Tmux's attach redraw re-paints the visible
region with the same bytes that already sit there from our capture,
so the overwrite is a no-op and there's no seam to misalign.

These tests pin that new behaviour.
"""
import asyncio
from unittest.mock import patch

import pytest

from ccpipe.ws import _capture_session_history, _clear_history_cache


@pytest.fixture(autouse=True)
def _reset_capture_cache():
    """Drop any cached capture-pane output between tests so each one
    starts from a clean slate. Without this the 1 s coalesce window
    that benefits reconnect storms in production causes one test's
    cached output to bypass the next test's subprocess mock."""
    _clear_history_cache()
    yield
    _clear_history_cache()


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return (self._stdout, b"")

    def kill(self):
        pass


async def test_capture_returns_full_pane_no_padding():
    """tmux's output is normalised to CRLF; no padding is appended."""
    fake = _FakeProc(b"line1\nline2\n\x1b[31mred\x1b[0m\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=fake) as spawn:
        out = await _capture_session_history("work", viewport_rows=4)
    # CRLF-normalised content, no trailing padding.
    assert out == b"line1\r\nline2\r\n\x1b[31mred\x1b[0m\r\n"


async def test_capture_uses_no_end_filter():
    """The fix: capture-pane must NOT pass ``-E -1`` so the visible
    pane is included in the output (otherwise the seam between
    history and tmux's attach redraw silently drops lines)."""
    fake = _FakeProc(b"x\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=fake) as spawn:
        await _capture_session_history("work", viewport_rows=4)
    args = spawn.call_args[0]
    assert "capture-pane" in args
    assert "-p" in args and "-e" in args
    # The -S start-line cap is still present (and is a NEGATIVE int).
    assert "-S" in args
    s_value = args[args.index("-S") + 1]
    assert s_value.startswith("-") and int(s_value) < 0
    # Critical regression guard: -E must NOT appear. If it does, we've
    # reintroduced the seam bug.
    assert "-E" not in args, (
        "tmux capture-pane was invoked with -E, which limits the "
        "capture to scrollback only and reintroduces the scrollback "
        "seam bug demonstrated by scripts/scrollback-doctor.py"
    )


async def test_viewport_rows_is_no_op():
    """viewport_rows is accepted for API stability but must NOT affect
    the byte output (no padding, no other knobs key off it)."""
    fake_a = _FakeProc(b"alpha\n")
    fake_b = _FakeProc(b"alpha\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=fake_a):
        out_24 = await _capture_session_history("work", viewport_rows=24)
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=fake_b):
        out_80 = await _capture_session_history("work", viewport_rows=80)
    assert out_24 == out_80 == b"alpha\r\n"


async def test_capture_returns_empty_on_nonzero_exit():
    fake = _FakeProc(b"whatever", returncode=1)
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=fake):
        out = await _capture_session_history("work", viewport_rows=30)
    assert out == b""


async def test_capture_returns_empty_on_timeout():
    class _Hanging:
        returncode = None
        async def communicate(self):
            await asyncio.sleep(10)
            return (b"never", b"")
        def kill(self): pass
        async def wait(self): return 0
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=_Hanging()):
        with patch("ccpipe.ws._HISTORY_CAPTURE_TIMEOUT_S", 0.05):
            out = await _capture_session_history("work", viewport_rows=30)
    assert out == b""


async def test_capture_returns_empty_when_tmux_missing():
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               side_effect=FileNotFoundError):
        out = await _capture_session_history("work", viewport_rows=30)
    assert out == b""


async def test_capture_normalises_crlf_idempotently():
    """Mixed-line-ending input is normalised to CRLF; no \\r\\r results."""
    fake = _FakeProc(b"already\r\nhere\r\nfine\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=fake):
        out = await _capture_session_history("work", viewport_rows=2)
    assert out == b"already\r\nhere\r\nfine\r\n"
    assert b"\r\r" not in out
