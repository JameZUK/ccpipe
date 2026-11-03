"""The replay capture sent at WS attach (ws._capture_replay_history) and
the trim that keeps lines scrolled during the attach from landing twice
(ws._drop_last_rows).

The replay is the pane's HISTORY only ("-E -1"), followed by a blank
screenful: tmux's attach redraw paints the screen itself. tmux's history
size is captured in the same command list, so _send_history can drop the
rows that scrolled into history after it last looked (they also arrive
in the held output after the redraw).
"""
import asyncio
from unittest.mock import patch

from ccpipe.ws import _capture_replay_history, _drop_last_rows, _pane_rows


class _FakeStream:
    """Minimal asyncio.StreamReader: serves *data* then EOF."""
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        chunk, self._data = (self._data, b"") if n < 0 else (self._data[:n], self._data[n:])
        return chunk


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self.stdout = _FakeStream(stdout)
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


def run(coro):
    return asyncio.run(coro)


def test_history_with_its_size_and_width():
    fake = _FakeProc(b"line1\nline2\n\x1b[31mred\x1b[0m\n3 80\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec", return_value=fake):
        out = run(_capture_replay_history("work"))
    # CRLF between lines, none after the last; the size line is parsed off.
    assert out == (b"line1\r\nline2\r\n\x1b[31mred\x1b[0m", 3, 80)


def test_history_only_and_atomic_with_its_size():
    fake = _FakeProc(b"x\n1 80\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec", return_value=fake) as spawn:
        run(_capture_replay_history("work"))
    args = list(spawn.call_args[0])
    assert args[1] == "capture-pane" and "-p" in args and "-e" in args and "-J" in args
    s_value = args[args.index("-S") + 1]
    assert s_value.startswith("-") and int(s_value) < 0
    assert args[args.index("-E") + 1] == "-1"          # history, not the screen
    # The size comes from the same tmux invocation, after the capture.
    sep = args.index(";")
    assert args[sep + 1] == "display-message" and "#{history_size} #{pane_width}" in args[sep:]


def test_empty_history_returns_nothing():
    # tmux answers "-E -1" with the screen's first row when there's no history.
    fake = _FakeProc(b"bash-5.3$\n0 60\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec", return_value=fake):
        assert run(_capture_replay_history("work")) == (b"", 0, 60)


def test_failures_return_none():
    with patch("ccpipe.ws.asyncio.create_subprocess_exec", return_value=_FakeProc(b"x\n1 80\n", 1)):
        assert run(_capture_replay_history("work")) is None
    with patch("ccpipe.ws.asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        assert run(_capture_replay_history("work")) is None
    with patch("ccpipe.ws.asyncio.create_subprocess_exec", return_value=_FakeProc(b"no size line\n")):
        assert run(_capture_replay_history("work")) is None

    class _HangingStream:
        async def read(self, n: int = -1) -> bytes:
            await asyncio.sleep(10)
            return b"never"

    class _Hanging:
        returncode = None
        stdout = _HangingStream()
        def kill(self): pass
        async def wait(self): return 0

    with patch("ccpipe.ws.asyncio.create_subprocess_exec", return_value=_Hanging()):
        with patch("ccpipe.ws._HISTORY_CAPTURE_TIMEOUT_S", 0.05):
            assert run(_capture_replay_history("work")) is None


def test_crlf_normalised_idempotently():
    fake = _FakeProc(b"already\r\nhere\r\nfine\n3 80\n")
    with patch("ccpipe.ws.asyncio.create_subprocess_exec", return_value=fake):
        assert run(_capture_replay_history("work"))[0] == b"already\r\nhere\r\nfine"


# ── dropping the rows that scrolled during the attach ───────────────────

def test_drop_whole_rows():
    h = b"\r\n".join(b"line%d" % i for i in range(10))
    assert _drop_last_rows(h, 3, 80) == b"\r\n".join(b"line%d" % i for i in range(7))
    assert _drop_last_rows(h, 0, 80) == h
    assert _drop_last_rows(h, 50, 80) == b""


def test_wrapped_lines_count_their_rows():
    long = b"x" * 25                                   # 3 rows at width 10
    h = b"a\r\n" + long + b"\r\nb"
    assert _pane_rows(long, 10) == 3
    assert _drop_last_rows(h, 4, 10) == b"a"           # b (1) + long (3)


def test_a_line_cut_part_way_is_kept():
    # 2 rows to drop, but the last line before them spans 3: dropping it
    # would lose a row that was history before the attach, so keep it.
    long = b"y" * 25
    h = b"a\r\n" + long + b"\r\nb"
    assert _drop_last_rows(h, 2, 10) == b"a\r\n" + long


def test_escapes_and_wide_characters():
    red = b"\x1b[31m" + b"z" * 10 + b"\x1b[0m"          # 10 columns, 1 row at 10
    assert _pane_rows(red, 10) == 1
    wide = "字".encode() * 6                            # 12 columns: 2 rows, counted high
    assert _pane_rows(wide, 10) >= 2
