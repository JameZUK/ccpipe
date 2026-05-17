"""Tests for tmux scrollback capture sent at WS attach."""
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from ccpipe.ws import _capture_session_history


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return (self._stdout, b"")

    def kill(self):
        pass


async def test_capture_returns_bytes_with_crlf_and_padding():
    fake = _FakeProc(b"line1\nline2\n\x1b[31mred\x1b[0m\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=fake) as spawn:
        out = await _capture_session_history("work", viewport_rows=4)
    # Captured content normalised to CRLF + 4 blank-line scrolls so the
    # history is pushed into xterm's scrollback before tmux's redraw.
    assert out == (
        b"line1\r\nline2\r\n\x1b[31mred\x1b[0m\r\n"
        b"\r\n\r\n\r\n\r\n"
    )
    # Argv contains -p -e plus the right -S/-E flags. Last-N-lines cap
    # uses a negative integer (e.g. -10000).
    args = spawn.call_args[0]
    assert "capture-pane" in args
    assert "-p" in args and "-e" in args
    assert "-E" in args and "-1" in args
    assert "-S" in args
    s_value = args[args.index("-S") + 1]
    assert s_value.startswith("-") and int(s_value) < 0


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
    fake = _FakeProc(b"already\r\nhere\r\nfine\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=fake):
        out = await _capture_session_history("work", viewport_rows=2)
    # Content stays as CRLF (no doubled \r\r), padded with viewport_rows blanks.
    assert out.startswith(b"already\r\nhere\r\nfine\r\n")
    assert out.endswith(b"\r\n\r\n")
    assert b"\r\r" not in out


async def test_capture_pads_with_viewport_rows_blanks():
    fake = _FakeProc(b"only-line\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec",
               return_value=fake):
        out = await _capture_session_history("work", viewport_rows=12)
    # Exactly viewport_rows blank lines appended after the content.
    suffix = out[len(b"only-line\r\n"):]
    assert suffix == b"\r\n" * 12
