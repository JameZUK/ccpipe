"""PTY relay: spawn a subprocess attached to a pseudo-terminal and expose async I/O."""
from __future__ import annotations

import asyncio
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

    async def read(self, max_bytes: int = 65536) -> bytes:
        """Read available output. Returns b'' on EOF."""
        assert self._master_fd is not None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()

        def _on_readable() -> None:
            try:
                data = os.read(self._master_fd, max_bytes)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            try:
                loop.remove_reader(self._master_fd)
            except Exception:
                pass
            if not fut.done():
                fut.set_result(data)

        loop.add_reader(self._master_fd, _on_readable)
        try:
            return await fut
        finally:
            try:
                loop.remove_reader(self._master_fd)
            except Exception:
                pass

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

    async def _drain_writes(self) -> None:
        """Push queued bytes through the PTY master as it becomes
        writable. Uses ``loop.add_writer`` for edge-triggered wake-ups
        so the polling cost is zero when the buffer is empty."""
        loop = asyncio.get_running_loop()
        while self._master_fd is not None:
            if not self._write_buffer:
                # Nothing to do; yield until write() pushes data in.
                # Cheap polling — write() runs synchronously so we can't
                # use an asyncio.Event without thread-safety concerns,
                # but a 50 ms idle poll is invisible at this scale.
                await asyncio.sleep(0.05)
                continue
            fd = self._master_fd
            try:
                n = os.write(fd, bytes(self._write_buffer))
            except BlockingIOError:
                n = 0
            except OSError as exc:
                log.debug("pty drain write failed: %s", exc)
                self._write_buffer.clear()
                return
            if n > 0:
                del self._write_buffer[:n]
                if not self._write_buffer:
                    continue
            # FD wasn't ready or we still have a tail — wait for it
            # to become writable via the event loop.
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
                # Defensive: if the FD never becomes writable in 5s,
                # poll again next iteration.
                pass
            finally:
                try: loop.remove_writer(fd)
                except Exception: pass

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

        # 4. Stop the write drain so it doesn't keep trying os.write
        #    on a closing FD.
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):
                pass
            self._drain_task = None

        # 5. Close the master FD.
        if fd is not None and self._master_fd is fd:
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
