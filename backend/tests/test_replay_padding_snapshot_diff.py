"""History-replay padding trim + snapshot-diff alignment.

The replay (capture-pane -e -J) keeps each line's trailing padding, so a
line Claude Code padded to a 190-column desktop wrapped into blank rows on
a 51-column phone. And the snapshot diff compared xterm rows against tmux
rows bottom-aligned, which fell out of step wherever the two wrapped
differently and with tmux's status row in xterm's buffer.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time

import pytest

from ccpipe.ws import _trim_wrap_padding

SGR_BG = b"\x1b[48;5;22m"
RESET = b"\x1b[39m\x1b[49m"


def test_trim_drops_padding_that_would_wrap():
    line = b"hello" + b" " * 186                  # padded to 191 columns
    assert _trim_wrap_padding(line, 51) == b"hello"


def test_trim_keeps_sgr_codes_among_the_padding():
    line = SGR_BG + b"+ added" + b" " * 150 + RESET
    assert _trim_wrap_padding(line, 51) == SGR_BG + b"+ added" + RESET


def test_trim_leaves_lines_that_fit():
    fits = b"short" + b" " * 40                   # 45 columns: no wrap
    same_width = b"x" * 10 + b" " * 41            # exactly 51
    data = fits + b"\r\n" + same_width
    assert _trim_wrap_padding(data, 51) == data


def test_trim_counts_wide_characters_and_ignores_escapes():
    # 20 CJK chars = 40 columns + 8 spaces = 48 columns: fits in 51 even
    # though it is far more than 51 BYTES.
    wide = ("漢" * 20).encode() + b" " * 8
    assert _trim_wrap_padding(wide, 51) == wide
    # Escape bytes don't count as width either.
    coloured = b"\x1b[38;5;231m" * 10 + b"ok" + b" " * 40
    assert _trim_wrap_padding(coloured, 51) == coloured


def test_trim_keeps_text_and_inner_spaces_of_long_lines():
    line = b"a   b" + b"c" * 80 + b"   "
    assert _trim_wrap_padding(line, 51) == b"a   b" + b"c" * 80


def test_trim_multiline_and_empty():
    data = b"one" + b" " * 100 + b"\r\n\r\ntwo" + b" " * 100
    assert _trim_wrap_padding(data, 51) == b"one\r\n\r\ntwo"
    assert _trim_wrap_padding(b"", 51) == b""
    assert _trim_wrap_padding(b"x" + b" " * 100, 0) == b"x" + b" " * 100


@pytest.fixture
def tmux_server(monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    d = tempfile.mkdtemp(prefix="cct", dir="/tmp")      # short: socket path limit
    monkeypatch.setenv("TMUX_TMPDIR", d)
    monkeypatch.delenv("TMUX", raising=False)
    env = {**os.environ, "TMUX_TMPDIR": d}
    env.pop("TMUX", None)
    # A wide window, a padded line and a long one, then the window
    # narrows — the desktop-then-phone shape.
    script = "printf 'top\\n%-150s|\\n%s\\nend\\n' padded " + "x" * 120 + "; seq 1 40; sleep 600"
    subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "s1",
                    "-x", "160", "-y", "20", script], check=True, env=env)
    time.sleep(0.3)
    subprocess.run(["tmux", "resize-window", "-t", "s1", "-x", "40", "-y", "20"],
                   check=True, env=env)
    yield env
    subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def test_snapshot_diff_lines_up_logical_lines_and_status_row(tmux_server):
    from ccpipe.routes import debug

    async def go():
        status_rows, history = await debug._pane_info("s1")
        assert status_rows == 1
        back = await debug._capture_logical("s1", 50, history)
        assert back is not None
        # What a schema-3 frontend sends: the pane's logical lines plus
        # tmux's status row as the last line of the client screen.
        front = [line.rstrip() for line in back] + ["[s1] 0:sleep*   12:00"]
        res = await debug.post_frontend_snapshot(debug.FrontendSnapshot(
            session="s1", note="", payload={
                "schema": 3,
                "buffers": {"activeType": "normal", "normal": front, "alternate": []},
            }))
        normal = res["content_diff"]["normal"]
        assert normal["mismatches"] == 0, normal["mismatch_examples"]
        assert normal["lines_compared"] == len(back)
        # The padded and long lines came back joined, not as 40-col rows.
        assert any(line.startswith("padded") and line.rstrip().endswith("|") for line in back)
        assert "x" * 120 in back

    asyncio.run(go())
