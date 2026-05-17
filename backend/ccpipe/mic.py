"""Mic writer: forwards PCM bytes from the WS binary frames into the
Pulse/PipeWire pipe-source that Claude Code's /voice reads from.

The pipe is created on the host by scripts/setup-virtual-mic.sh. If Pulse
hasn't loaded module-pipe-source yet (no reader on the FIFO), opening
O_WRONLY|O_NONBLOCK raises ENXIO; we treat that as "voice unavailable" and
keep retrying lazily on the next write.
"""
from __future__ import annotations

import errno
import logging
import os
import time

log = logging.getLogger(__name__)

DEFAULT_PIPE_PATH = "/tmp/ccpipe_mic.pipe"
_OPEN_RETRY_INTERVAL_S = 2.0


class MicWriter:
    """Lazy, non-blocking writer for the virtual-mic FIFO."""

    def __init__(self, pipe_path: str = DEFAULT_PIPE_PATH) -> None:
        self.pipe_path = pipe_path
        self._fd: int | None = None
        self._last_open_attempt: float = 0.0
        self._last_error: str | None = None

    @property
    def available(self) -> bool:
        return self._fd is not None

    def _try_open(self) -> bool:
        now = time.monotonic()
        if now - self._last_open_attempt < _OPEN_RETRY_INTERVAL_S:
            return False
        self._last_open_attempt = now
        try:
            self._fd = os.open(self.pipe_path, os.O_WRONLY | os.O_NONBLOCK)
            self._last_error = None
            log.info("mic pipe %s opened", self.pipe_path)
            return True
        except FileNotFoundError:
            self._last_error = "pipe not found (run scripts/setup-virtual-mic.sh)"
            return False
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                # No reader attached yet (Pulse hasn't loaded module-pipe-source).
                self._last_error = "no reader on pipe (Pulse module not loaded?)"
            else:
                self._last_error = f"{exc.__class__.__name__}: {exc}"
            return False

    def write(self, data: bytes) -> bool:
        """Best-effort write. Returns False if the pipe is unavailable."""
        if self._fd is None and not self._try_open():
            return False
        assert self._fd is not None
        try:
            os.write(self._fd, data)
            return True
        except BrokenPipeError:
            log.info("mic pipe broken; will retry open on next frame")
            self._close()
            return False
        except BlockingIOError:
            # Buffer full — drop frame, return success-ish so caller doesn't reopen.
            return True
        except OSError as exc:
            log.warning("mic pipe write failed: %s", exc)
            self._close()
            return False

    def _close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def close(self) -> None:
        self._close()

    def diagnostic(self) -> str:
        if self.available:
            return f"open: {self.pipe_path}"
        return f"closed: {self._last_error or 'not yet attempted'}"
