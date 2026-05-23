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


# ── L7: child reap must not park a default-executor thread ──────────────


async def test_await_child_exit_uses_pidfd_when_available(monkeypatch):
    """The pidfd path should detect exit by calling os.pidfd_open + a
    loop reader, not by submitting a blocking waitpid to any executor."""
    from ccpipe import pty_relay

    if not pty_relay._supports_pidfd():
        pytest.skip("pidfd_open not available; nothing to verify on this kernel")

    pidfd_calls: list[int] = []
    real_pidfd_open = os.pidfd_open  # type: ignore[attr-defined]
    def tracking_open(pid: int, flags: int = 0) -> int:
        pidfd_calls.append(pid)
        return real_pidfd_open(pid, flags)
    monkeypatch.setattr(os, "pidfd_open", tracking_open, raising=False)

    # Trip a tripwire if the fallback executor is touched.
    def _explode(*_a, **_k):
        raise AssertionError("dedicated reap executor must not be used on pidfd path")
    monkeypatch.setattr(pty_relay._reap_executor, "submit", _explode)

    loop = asyncio.get_running_loop()
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    await asyncio.wait_for(pty_relay._await_child_exit(loop, pid), timeout=5.0)
    assert pidfd_calls == [pid], f"expected pidfd_open({pid}), got {pidfd_calls}"


async def test_await_child_exit_fallback_uses_dedicated_executor(monkeypatch):
    """Simulate pidfd_open returning ENOSYS (macOS / seccomp). The
    fallback must reap via the *dedicated* reap pool — we trip a
    tripwire on the default executor to catch any regression that
    would let PTY reaps starve argon2/list_sessions/etc. again."""
    import errno
    from ccpipe import pty_relay

    def _no_pidfd(_pid, flags=0):
        raise OSError(errno.ENOSYS, "simulated")
    monkeypatch.setattr(os, "pidfd_open", _no_pidfd, raising=False)

    reap_submits: list[int] = []
    real_submit = pty_relay._reap_executor.submit
    def tracking_submit(fn, *args, **kwargs):
        reap_submits.append(args[0] if args else -1)
        return real_submit(fn, *args, **kwargs)
    monkeypatch.setattr(pty_relay._reap_executor, "submit", tracking_submit)

    loop = asyncio.get_running_loop()
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    await asyncio.wait_for(pty_relay._await_child_exit(loop, pid), timeout=5.0)
    assert reap_submits == [pid], (
        f"fallback should route waitpid to the dedicated reap pool; "
        f"got submits={reap_submits}"
    )


async def test_many_concurrent_ptys_dont_starve_default_executor():
    """Acceptance test for the production hang: 20 concurrent
    PtyProcess instances (well above the default executor's ~12
    workers) must still let an unrelated asyncio.to_thread call
    complete promptly. Before the L7 fix this hung forever — every
    PtyProcess parked one default-executor worker in os.waitpid."""
    procs: list[PtyProcess] = []
    try:
        for _ in range(20):
            procs.append(await _spawn("/bin/sleep", "30"))
        done = await asyncio.wait_for(
            asyncio.to_thread(lambda: "ok"), timeout=2.0
        )
        assert done == "ok"
    finally:
        for p in procs:
            await p.terminate()
