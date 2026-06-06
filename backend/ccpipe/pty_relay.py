"""PTY relay: spawn a subprocess attached to a pseudo-terminal and expose async I/O."""
from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

# Dedicated executor for the *fallback* child-reap path (macOS / Linux
# < 5.3 / pidfd_open denied by seccomp). Isolated from asyncio's default
# executor so a long-lived PTY can't park a default-pool worker — which
# would starve argon2 verify, tmux.list_sessions, and every other
# asyncio.to_thread() call (review L7). On Linux 5.3+ the pidfd path
# below skips this entirely and reaps are thread-free.
_reap_executor = ThreadPoolExecutor(
    max_workers=256,
    thread_name_prefix="ccpipe-reap",
)


def _supports_pidfd() -> bool:
    """Cheap probe. ``hasattr`` only — the actual syscall is in
    _await_child_exit's try/except, where a runtime ENOSYS / EPERM
    (e.g. seccomp filter blocks pidfd_open) still falls back cleanly."""
    return hasattr(os, "pidfd_open")


async def _await_child_exit(loop: asyncio.AbstractEventLoop, pid: int) -> None:
    """Block until *pid* exits, then reap it. Uses pidfd_open() +
    add_reader() when available so no thread is held for the duration
    of the child's lifetime; falls back to a dedicated executor's
    blocking waitpid() otherwise."""
    if _supports_pidfd():
        try:
            pidfd = os.pidfd_open(pid)  # type: ignore[attr-defined]
        except OSError as exc:
            # ESRCH means the child has already been reaped (race with
            # an out-of-band waitpid) — treat as success. ENOSYS /
            # EPERM (seccomp) fall through to the executor path.
            if exc.errno == errno.ESRCH:
                return
            if exc.errno not in (errno.ENOSYS, errno.EPERM):
                raise
            pidfd = None
        if pidfd is not None:
            exit_fut: asyncio.Future[None] = loop.create_future()

            def _on_pidfd_readable() -> None:
                # pidfd becomes readable exactly once, when the child exits.
                if not exit_fut.done():
                    exit_fut.set_result(None)

            try:
                loop.add_reader(pidfd, _on_pidfd_readable)
                try:
                    await exit_fut
                finally:
                    loop.remove_reader(pidfd)
            finally:
                os.close(pidfd)
            # Child is dead; reap status non-blocking so the kernel
            # doesn't keep the zombie around. ChildProcessError if
            # something else reaped it first — fine either way.
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, os.WNOHANG)
            return
    # Fallback: dedicated thread pool, isolated from the default
    # executor. Same blocking semantics as before but can't starve
    # asyncio's general-purpose to_thread() slots.
    await loop.run_in_executor(_reap_executor, _waitpid_blocking, pid)


def _waitpid_blocking(pid: int) -> None:
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, 0)

# How long terminate() waits for SIGTERM to take effect before SIGKILLing.
_GRACEFUL_TERMINATE_S = 1.5

# Cap on the per-PTY async write buffer. Big pastes from the browser
# arrive as a single WS frame; if claude / tmux is slow to drain them
# we accumulate the unsent tail here. 4 MiB is well above any realistic
# paste while bounded enough that a stuck PTY can't grow the buffer
# without limit.
_WRITE_BUFFER_LIMIT = 4 * 1024 * 1024


class PtyProcess:
    """Async wrapper around a child process running on a PTY."""

    def __init__(self, argv: list[str], *, cwd: str | None = None,
                 env: dict[str, str] | None = None,
                 cols: int = 120, rows: int = 40) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self._cols = cols
        self._rows = rows
        self._master_fd: int | None = None
        self._pid: int | None = None
        self._exit_event = asyncio.Event()
        # Pending bytes for write(). Filled by write() when os.write
        # short-writes or raises BlockingIOError; drained by an async
        # task that waits on the master fd becoming writable.
        self._write_buffer: bytearray = bytearray()
        self._drain_task: asyncio.Task[None] | None = None
        # Child-reaper task. Held so asyncio doesn't GC it mid-flight
        # (it only keeps a weak ref to running tasks) and so terminate()
        # can cancel/await it during shutdown.
        self._exit_task: asyncio.Task[None] | None = None
        # Signalled by write() whenever it enqueues bytes the drain
        # task needs to push. asyncio.Event.set() is loop-thread-safe
        # so write() can call it directly from the receive loop.
        self._drain_signal: asyncio.Event = asyncio.Event()
        # Read pipeline. The master-fd reader is registered ONCE in
        # start() (not per-read()), which removes two epoll_ctl
        # syscalls + a future allocation per PTY chunk on the hot
        # output path. Chunks land in this queue; read() just drains
        # it. EOF is signalled by an empty bytes sentinel.
        #
        # maxsize=64 caps the queue at ~4 MiB worth of 64 KiB chunks
        # — if pump can't drain (e.g. the WS is stalled), the kernel
        # PTY buffer fills, then the callback drops chunks here and
        # records the loss rather than growing the heap unbounded.
        self._read_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self._read_eof = False
        # Bytes the reader couldn't enqueue because the queue was
        # full. Reported through PtyProcess.bytes_dropped() so the
        # WS layer can fold it into its WsCounters.bytes_lost.
        self._read_dropped = 0
        # Tracks the cumulative-dropped value at the most recent
        # "warning emitted" point so we can fire one log line per
        # MiB of loss regardless of chunk-size cadence.
        self._last_drop_reported = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        pid, master_fd = pty.fork()
        if pid == 0:
            # Child
            try:
                if self._cwd:
                    os.chdir(self._cwd)
                env = self._env if self._env is not None else os.environ.copy()
                env.setdefault("TERM", "xterm-256color")
                os.execvpe(self._argv[0], self._argv, env)
            except Exception:
                os._exit(127)
        # Parent
        self._pid = pid
        self._master_fd = master_fd
        self._set_winsize(self._cols, self._rows)
        # Make master FD non-blocking for asyncio
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Watch for child exit in background. Keep the reference (see
        # _exit_task docstring) — a bare create_task() is GC-vulnerable.
        self._exit_task = loop.create_task(self._wait_for_exit())
        # Drain pending writes asynchronously when the FD is writable.
        self._drain_task = loop.create_task(self._drain_writes())
        # Register the master-fd reader ONCE for the PTY's lifetime.
        # Chunks land in self._read_queue and read() awaits the queue.
        loop.add_reader(master_fd, self._on_master_readable)

    async def _wait_for_exit(self) -> None:
        pid = self._pid
        assert pid is not None
        loop = asyncio.get_running_loop()
        # L7 (proper fix): wait on the child via pidfd_open() + add_reader
        # so a long-lived PTY doesn't park a default-executor worker for
        # its entire lifetime. With many concurrent WS attaches (one
        # PtyProcess each, before this change one waitpid thread each),
        # the default ThreadPoolExecutor would saturate at min(32,
        # cpu_count+4) ≈ 12 workers and every subsequent
        # asyncio.to_thread() — argon2 verify, tmux.list_sessions, jsonl
        # I/O — would queue forever. The pidfd path is thread-free.
        #
        # Linux 5.3+ exposes pidfd_open; on macOS / older kernels we
        # fall back to a *dedicated* reap executor so PTY reaps still
        # work but are isolated from the default pool. Always clear
        # self._pid + set the event in finally so:
        #   1. terminate() can't os.kill() a recycled PID via a stale
        #      self._pid reference after the child has been reaped.
        #   2. waitpid raising (ECHILD / ESRCH) still drops state.
        try:
            await _await_child_exit(loop, pid)
        except (ChildProcessError, OSError):
            pass
        finally:
            self._pid = None
            self._exit_event.set()

    def _set_winsize(self, cols: int, rows: int) -> None:
        assert self._master_fd is not None
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, size)

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        if self._master_fd is not None:
            self._set_winsize(cols, rows)

    def _on_master_readable(self) -> None:
        """Persistent reader callback. Fires whenever the kernel says
        the PTY master fd is readable; pushes whatever's available into
        ``self._read_queue``. Stays registered for the PTY's lifetime
        so the hot output path doesn't pay add_reader + remove_reader
        + create_future per chunk.

        EOF is signalled by an empty payload landing in the queue;
        once seen we stop putting further chunks even if the kernel
        keeps firing the readiness signal.
        """
        fd = self._master_fd
        if fd is None or self._read_eof:
            return
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._read_eof = True
            # A PTY master fd at child-EOF stays *persistently* readable,
            # so leaving the reader registered makes the event loop invoke
            # this callback in a tight 100%-of-one-core spin until
            # terminate() finally removes it (a WS round-trip away, longer
            # on a half-dead transport). Drop the reader the instant we see
            # EOF; terminate()'s remove_reader is idempotent (try/except).
            with contextlib.suppress(ValueError, KeyError, OSError):
                asyncio.get_running_loop().remove_reader(fd)
        try:
            self._read_queue.put_nowait(data)
        except asyncio.QueueFull:
            # pump() isn't keeping up — almost certainly because the
            # WS is stalled. Drop this chunk and count the loss; the
            # natural backpressure into the kernel PTY buffer will
            # also slow claude. The client's reconnect path replays
            # via capture-pane so the bytes aren't permanently lost
            # from the user's perspective.
            if data:
                self._read_dropped += len(data)
                # Warn once per MiB of cumulative drops. Track the
                # last reported value separately so a bursty drop
                # pattern doesn't skip MiB-boundary crossings (the
                # previous `% (1<<20) < len(data)` heuristic missed
                # warnings when chunk sizes didn't line up with the
                # modulo).
                if self._read_dropped - self._last_drop_reported >= (1 << 20):
                    self._last_drop_reported = self._read_dropped
                    log.warning("pty read queue full; dropped a chunk "
                                "(cumulative=%d bytes)", self._read_dropped)
            else:
                # EOF marker race: os.read returned b"" exactly when
                # the queue was full. Without recovery, _read_eof is
                # set but no sentinel reaches the queue and pump
                # blocks forever on get(). Drain one slot and re-push
                # the EOF so the next read() call returns b"".
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._read_queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    self._read_queue.put_nowait(b"")

    def bytes_dropped(self) -> int:
        """Total bytes dropped because the read queue was saturated.
        Lets the WS layer fold this into its WsCounters.bytes_lost."""
        return self._read_dropped

    async def read(self, max_bytes: int = 65536) -> bytes:
        """Pop the next available output chunk from the queue.

        Returns b'' on EOF. The ``max_bytes`` argument is honoured by
        the persistent reader callback (which always reads up to 64
        KiB); we keep the parameter for backwards compatibility with
        existing callers but it's effectively informational now.
        """
        return await self._read_queue.get()

    def write(self, data: bytes) -> None:
        """Write to the PTY master. After terminate() this is a no-op so
        late frames arriving on a closing WS don't crash the handler.

        Large pastes are common in this app — the browser hands us
        multi-kilobyte single WS frames — and the kernel PTY buffer is
        small (~4 KiB on Linux). A naive single ``os.write`` would
        short-write or hit ``BlockingIOError`` and silently drop the
        tail, corrupting whatever the user pasted into claude. So we
        try the immediate write first, queue any remainder, and let
        ``_drain_writes`` push it through as the FD becomes writable.
        """
        fd = self._master_fd
        if fd is None:
            return
        if not data:
            return
        # If a tail is already queued, append rather than re-ordering.
        if self._write_buffer:
            if len(self._write_buffer) + len(data) > _WRITE_BUFFER_LIMIT:
                log.warning("pty write buffer full (%d bytes); dropping %d "
                            "incoming bytes — PTY drain has stalled",
                            len(self._write_buffer), len(data))
                return
            self._write_buffer.extend(data)
            self._drain_signal.set()
            return
        try:
            n = os.write(fd, data)
        except BlockingIOError:
            n = 0
        except OSError as exc:
            log.debug("pty write failed: %s", exc)
            return
        if n < len(data):
            self._write_buffer.extend(data[n:])
            self._drain_signal.set()

    async def _drain_writes(self) -> None:
        """Push queued bytes through the PTY master as it becomes
        writable. Edge-triggered: idles on an asyncio.Event when the
        buffer is empty, wakes via ``loop.add_writer`` when the FD is
        full. No timer-poll overhead.

        FD reuse safety: capture self._master_fd at the top of each
        iteration AND re-check after every await — terminate() may
        have cleared it while we were parked, and the same numeric
        fd could legally be returned by `os.open` for an unrelated
        socket before our next `os.write`.
        """
        loop = asyncio.get_running_loop()
        while True:
            fd = self._master_fd
            if fd is None:
                return
            if not self._write_buffer:
                self._drain_signal.clear()
                await self._drain_signal.wait()
                # After waking, the fd may have been swapped/cleared.
                continue
            try:
                n = os.write(fd, bytes(self._write_buffer))
            except BlockingIOError:
                n = 0
            except OSError as exc:
                # EBADF / EPIPE / ENOTTY are fatal: the PTY master is
                # gone or in a state we can't recover from, so bail
                # and let terminate()/the WS handler clean up. Anything
                # else (e.g. EINTR, EAGAIN-disguised, ENOSPC on the
                # pty buffer for some odd kernel config) is transient
                # — leave the buffer intact and try again on the next
                # writable wake. Without this distinction, a single
                # transient error used to permanently exit the drain
                # task; subsequent write()s would then accumulate up
                # to 4 MiB then silently drop bytes.
                if exc.errno in (errno.EBADF, errno.EPIPE, errno.ENOTTY):
                    log.debug("pty drain write fatal: %s", exc)
                    self._write_buffer.clear()
                    return
                log.warning("pty drain write transient error: %s; will retry", exc)
                n = 0
                # Brief cooldown before the next attempt. Otherwise a
                # persistent transient errno (ENOMEM under memory
                # pressure) pins a CPU core: add_writer fires the
                # callback immediately because the FD is already
                # writable, the next iteration hits the same errno,
                # repeat. 50 ms is short enough not to noticeably
                # delay a real recovery and long enough to keep CPU
                # off the floor.
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    raise
            if n > 0:
                del self._write_buffer[:n]
                if not self._write_buffer:
                    continue
            # FD wasn't ready (or has a tail) — wait for writable via
            # the event loop. Re-check fd identity after the await to
            # be safe against terminate() recycling the descriptor.
            fut: asyncio.Future[None] = loop.create_future()
            def _on_writable() -> None:
                if not fut.done():
                    fut.set_result(None)
            try:
                loop.add_writer(fd, _on_writable)
            except (ValueError, OSError):
                return
            try:
                await asyncio.wait_for(fut, timeout=5.0)
            except asyncio.TimeoutError:
                pass
            finally:
                try: loop.remove_writer(fd)
                except Exception: pass
            if self._master_fd is not fd:
                # terminate() flipped under us — bail before the next
                # os.write lands on a recycled fd.
                return

    async def wait(self) -> None:
        await self._exit_event.wait()

    async def terminate(self) -> None:
        """Async shutdown: unregister loop reader, signal, wait, force-kill,
        close FD. Safe to call multiple times.

        Doing reader removal BEFORE close prevents the loop calling the
        reader callback on a closed fd (which would raise EBADF and could
        leave the reader registered with a phantom fd that the kernel
        later recycles for an unrelated file).
        """
        loop = asyncio.get_running_loop()
        fd = self._master_fd

        # 1. Unregister the reader first so the loop stops poking the fd.
        if fd is not None:
            try:
                loop.remove_reader(fd)
            except (ValueError, KeyError):
                pass
        # Wake any pump() awaiting read() with an EOF marker. The
        # reader is gone now so no more chunks will arrive; without
        # this, pump's `await queue.get()` would block until the
        # outer task cancellation propagates.
        if not self._read_eof:
            self._read_eof = True
            try:
                self._read_queue.put_nowait(b"")
            except Exception:
                pass

        # 2. SIGTERM and wait briefly for the child to exit cleanly.
        if self._pid is not None and not self._exit_event.is_set():
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                self._exit_event.set()
        try:
            await asyncio.wait_for(self._exit_event.wait(),
                                    timeout=_GRACEFUL_TERMINATE_S)
        except asyncio.TimeoutError:
            # 3. SIGKILL the unresponsive child.
            if self._pid is not None:
                log.warning("pty child pid %s did not exit; SIGKILL", self._pid)
                try:
                    os.kill(self._pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self._exit_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    log.error("pty child pid %s still alive after SIGKILL",
                              self._pid)

        # 4. Stop the write drain. Clear _master_fd FIRST so the drain
        #    loop sees the change on its next wake and bails before
        #    issuing another os.write — without this, terminate() can
        #    race the kernel re-issuing the same numeric fd to an
        #    unrelated socket between cancel() and our close() below.
        self._master_fd = None
        self._drain_signal.set()
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                # Re-raise if this terminate() was itself cancelled —
                # otherwise we'd silently absorb the caller's cancel.
                if asyncio.current_task() is not None and \
                   asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            self._drain_task = None

        # 4b. Reap the exit-watcher task. Normally already finished (the
        #     child is dead and its finally set _exit_event), but cancel+
        #     await guarantees it's not left pending and surfaces no
        #     unobserved exception.
        if self._exit_task is not None:
            self._exit_task.cancel()
            try:
                await self._exit_task
            except asyncio.CancelledError:
                if asyncio.current_task() is not None and \
                   asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            self._exit_task = None

        # 5. Close the master FD.
        if fd is not None and self._master_fd is None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._master_fd = None


async def pump(pty_proc: PtyProcess, on_output: Callable[[bytes], Awaitable[None]]) -> None:
    """Continuously read from PTY and invoke the callback. Returns on EOF."""
    while True:
        data = await pty_proc.read()
        if not data:
            return
        await on_output(data)
