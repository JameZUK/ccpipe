"""Tests for PtyProcess: spawn, resize, terminate hardening."""
import asyncio
import os
import signal

import pytest

from ccpipe.pty_relay import PtyProcess


async def _spawn(*argv: str, cols: int = 80, rows: int = 24) -> PtyProcess:
    p = PtyProcess(list(argv), cols=cols, rows=rows)
    await p.start()
    return p


async def test_spawn_and_read_output():
    p = await _spawn("/bin/echo", "hello")
    try:
        data = b""
        for _ in range(8):
            chunk = await p.read()
            if not chunk:
                break
            data += chunk
        assert b"hello" in data
    finally:
        await p.terminate()


async def test_terminate_is_idempotent():
    p = await _spawn("/bin/cat")
    await p.terminate()
    # Second call must not raise.
    await p.terminate()


async def test_terminate_kills_long_running_process():
    p = await _spawn("/bin/sleep", "30")
    pid = p._pid
    assert pid is not None
    assert os.kill(pid, 0) is None  # exists
    await p.terminate()
    # Give the kernel a tick to reap.
    await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_resize_does_not_crash_post_terminate():
    p = await _spawn("/bin/cat")
    await p.terminate()
    # Should be a no-op, definitely not raise.
    p.resize(120, 40)


async def test_terminate_sigkills_unresponsive_child():
    # Spawn a process that ignores SIGTERM so we exercise the SIGKILL path.
    # 'trap "" TERM' makes the shell ignore SIGTERM; then it sleeps.
    p = await _spawn("/bin/sh", "-c", "trap '' TERM; sleep 30")
    pid = p._pid
    await p.terminate()
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_write_after_terminate_is_silent_noop():
    p = await _spawn("/bin/cat")
    await p.terminate()
    # Late frames arriving on a closing WS must not raise; write is a
    # no-op once the master FD has been closed.
    p.write(b"x")  # should not raise
