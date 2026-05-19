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

# The virtual-mic FIFO is configured by scripts/setup-virtual-mic.sh as
# 16 kHz mono Int16 PCM. Pulse consumes at exactly that rate — 32 KB/s.
# Used by `estimate_drain_seconds` to compute how long audio that has
# been written but not yet read by Pulse will take to flow through.
_MIC_SAMPLE_RATE_HZ = 16000
_MIC_BYTES_PER_SAMPLE = 2
_MIC_BYTES_PER_SEC = _MIC_SAMPLE_RATE_HZ * _MIC_BYTES_PER_SAMPLE  # 32000


class MicWriter:
    """Lazy, non-blocking writer for the virtual-mic FIFO.

    Also tracks per-recording stats (bytes written, drops, time of
    first write) so the WS handler can orchestrate the post-mic-stop
    release-PTT signal AFTER all the audio has actually drained
    through Pulse to claude. See `estimate_drain_seconds`.
    """

    def __init__(self, pipe_path: str = DEFAULT_PIPE_PATH) -> None:
        self.pipe_path = pipe_path
        self._fd: int | None = None
        self._last_open_attempt: float = 0.0
        self._last_error: str | None = None
        # Per-recording stats. Reset by `reset()` on ownership transitions.
        self._bytes_written: int = 0
        self._first_write_ts: float | None = None
        self._drops: int = 0

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
            # Stats — only count successful non-EAGAIN writes. Zero-byte
            # probes (used to check FD readiness) bump the timestamp but
            # not the counter.
            if self._first_write_ts is None and data:
                self._first_write_ts = time.monotonic()
            self._bytes_written += len(data)
            return True
        except BrokenPipeError:
            log.info("mic pipe broken; will retry open on next frame")
            self._close()
            return False
        except BlockingIOError:
            # Buffer full — drop frame. Counted so the WS handler can
            # surface "audio dropped because Pulse couldn't keep up"
            # rather than silently truncating the recording.
            self._drops += 1
            # Logarithmic logging — first drop is informative, every
            # 50th after that is enough to see the trend.
            if self._drops == 1 or self._drops % 50 == 0:
                log.warning(
                    "mic pipe write dropped (%d frame%s lost); "
                    "Pulse consuming slower than browser is sending",
                    self._drops, "" if self._drops == 1 else "s",
                )
            return True
        except OSError as exc:
            log.warning("mic pipe write failed: %s", exc)
            self._close()
            return False

    def reset(self) -> None:
        """Clear per-recording stats. Call on each ownership transition
        (new owner claims the mic, owner sends mic_stop, owner
        disconnects) so the next recording's drain calculation starts
        from a clean baseline."""
        self._bytes_written = 0
        self._first_write_ts = None
        self._drops = 0

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def drops(self) -> int:
        return self._drops

    def estimate_drain_seconds(self) -> float:
        """Approx wall-clock seconds until all audio written so far has
        been consumed by Pulse.

        Bytes-based: Pulse reads at exactly `_MIC_BYTES_PER_SEC` from
        the FIFO. Starting at the moment the first byte was written,
        Pulse has consumed `(now - first_write_ts) * BYTES_PER_SEC`
        bytes. Anything beyond that is still in flight.

        Pulse's own internal buffer (~tens of ms) is NOT subtracted —
        caller should add a safety pad to swallow it plus any
        downstream STT priming. The kernel pipe buffer holds the rest;
        we don't need to query its depth because the rate-based
        calculation already accounts for it implicitly (bytes written
        minus bytes consumed = bytes still in any buffer along the way).
        """
        if self._first_write_ts is None or self._bytes_written == 0:
            return 0.0
        now = time.monotonic()
        consumed = max(0.0, (now - self._first_write_ts)) * _MIC_BYTES_PER_SEC
        remaining = max(0.0, self._bytes_written - consumed)
        return remaining / _MIC_BYTES_PER_SEC

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
