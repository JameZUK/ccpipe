"""PTY relay: spawn a subprocess attached to a pseudo-terminal and expose async I/O."""
from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

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

        # Watch for child exit in background
        loop.create_task(self._wait_for_exit())
        # Drain pending writes asynchronously when the FD is writable.
        self._drain_task = loop.create_task(self._drain_writes())
        # Register the master-fd reader ONCE for the PTY's lifetime.
        # Chunks land in self._read_queue and read() awaits the queue.
        loop.add_reader(master_fd, self._on_master_readable)

    async def _wait_for_exit(self) -> None:
        pid = self._pid
        assert pid is not None
        loop = asyncio.get_running_loop()
        # Use a dedicated thread to call waitpid; cheap because it blocks once.
        # Always clear self._pid + set the event in finally so:
        #   1. terminate() can't os.kill() a recycled PID via a stale self._pid
        #      reference after the child has been reaped.
        #   2. If waitpid raises (ECHILD — child already reaped elsewhere, or
        #      ESRCH if the pid never existed) we still drop the executor
        #      worker; without finally those raise inside run_in_executor and
        #      _exit_event stays unset, leaking the thread for the process
        #      lifetime.
        try:
            await loop.run_in_executor(None, lambda: os.waitpid(pid, 0))
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
                if self._read_dropped % (1 << 20) < len(data):
                    log.warning("pty read queue full; dropped a chunk "
                                "(cumulative=%d bytes)", self._read_dropped)

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
