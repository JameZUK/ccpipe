"""Local-transcription mic sink for macOS — drop-in for ``mic.MicWriter``.

On Linux, voice flows: browser PCM → ``MicWriter`` writes a FIFO →
PulseAudio's ``module-pipe-source`` exposes it as a microphone →
claude's ``/voice`` reads from that mic and transcribes via the
Anthropic API.

On macOS that chain is broken at the very last link. claude's ``/voice``
keybinding handler has had a regression since v2.1.83 (still unfixed as
of v2.1.144 — anthropics/claude-code#38690). Even a perfectly-wired
BlackHole-based virtual mic produces no transcription because claude
never actually starts recording when meta+K arrives. We sidestep the
upstream bug entirely by running transcription ourselves with
``whisper-cpp`` and typing the result into the PTY as if the user had
typed it.

The public interface mirrors :class:`ccpipe.mic.MicWriter` so ``ws.py``
can pick one or the other at module init based on ``sys.platform`` — no
other code paths need to know. The capability flag (``available``) is
True iff ``whisper-cli`` is on PATH and the model file exists.

Adds one new method, :meth:`MicTranscriber.finalize`, which the macOS
branch of the ``mic_stop`` handler calls in place of the Linux
release-PTT scheduling: runs the transcription off the event loop and
writes the resulting text into the PTY.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
import wave
from pathlib import Path

log = logging.getLogger(__name__)

# Default location matches scripts/install.sh on macOS. The env vars
# exist for operators who keep models elsewhere or build whisper-cpp
# from source; they aren't first-class settings because they don't
# appear in the Linux config either.
DEFAULT_MODEL_PATH = (
    Path.home() / "Library/Application Support/ccpipe/whisper-models/ggml-base.en.bin"
)
WHISPER_BIN_ENV = "CCPIPE_WHISPER_BIN"
WHISPER_MODEL_ENV = "CCPIPE_WHISPER_MODEL"

# 60 s @ 16 kHz mono Int16 = 1.92 MB. Cap protects against a client
# that forgot to send ``mic_stop`` (tab crash mid-record, network
# partition) and would otherwise stream indefinitely. 60 s comfortably
# covers any realistic dictation utterance.
_MAX_BUFFER_BYTES = 60 * 16_000 * 2

# Audio format the browser writes and whisper expects. Matches mic.py
# (the browser-side AudioWorklet sends raw PCM at this rate).
_SAMPLE_RATE_HZ = 16_000
_BYTES_PER_SAMPLE = 2
_CHANNELS = 1

# Whisper transcription wall-clock cap. base.en on Apple Silicon runs
# ~real-time, but a stuck process (model file truncated, CPU pinned by
# another tenant) could hang indefinitely without this. 60 s is well
# beyond any expected run for a 60 s buffer.
_WHISPER_TIMEOUT_S = 60.0


class MicTranscriber:
    """In-memory PCM buffer + local whisper-cpp transcription on flush.

    Mirrors the parts of :class:`ccpipe.mic.MicWriter` that ``ws.py``
    touches — ``available``, ``write``, ``reset``, ``bytes_written``,
    ``drops``, ``estimate_drain_seconds``, ``close``, ``diagnostic`` —
    so ``ws.py`` can pick one at module init and the rest of the file
    treats them interchangeably.

    There's no external pipe to drain, so ``estimate_drain_seconds``
    returns 0; :meth:`finalize` is the actual wait, and it's awaited
    asynchronously rather than padded.
    """

    def __init__(
        self,
        model_path: Path | None = None,
        whisper_bin: str | None = None,
    ) -> None:
        self._buf = bytearray()
        self._bytes_written = 0
        self._drops = 0
        # Tracks whether we've already injected at least one
        # transcription this session. Used to decide whether to
        # prepend a separator space: the first utterance lands at
        # the prompt's current position (no leading space), every
        # subsequent utterance gets one prepended so consecutive
        # dictations don't collide ("hello" + "world" → "hello world").
        self._prev_injected = False
        self.model_path = model_path or Path(
            os.environ.get(WHISPER_MODEL_ENV) or str(DEFAULT_MODEL_PATH)
        )
        env_bin = os.environ.get(WHISPER_BIN_ENV)
        self._whisper_bin = whisper_bin or env_bin or shutil.which("whisper-cli")
        self._available = bool(self._whisper_bin) and self.model_path.exists()
        if not self._available:
            log.warning(
                "transcriber unavailable: whisper-cli=%r model=%s (exists=%s); "
                "install with `brew install whisper-cpp` and re-run scripts/install.sh",
                self._whisper_bin, self.model_path, self.model_path.exists(),
            )

    # ── MicWriter-compatible interface ─────────────────────────────────

    @property
    def available(self) -> bool:
        return self._available

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def drops(self) -> int:
        return self._drops

    def write(self, data: bytes) -> bool:
        """Append PCM to the buffer.

        Returns True iff transcription is wired up — matches the
        :meth:`MicWriter.write` capability-probe semantics so the
        WS-hello probe (``write(b"")``) tells the frontend whether
        to show the mic FAB.
        """
        if not self._available:
            return False
        if not data:
            return True
        if len(self._buf) + len(data) > _MAX_BUFFER_BYTES:
            # Forgot mic_stop. Keep the prefix that fits; the tail
            # falls on the floor and is reported as a drop so the
            # operator sees it in the mic_stop log line.
            spare = _MAX_BUFFER_BYTES - len(self._buf)
            if spare > 0:
                self._buf.extend(data[:spare])
                self._bytes_written += spare
            self._drops += 1
            if self._drops == 1 or self._drops % 50 == 0:
                log.warning(
                    "transcriber buffer cap hit (%d drop%s); "
                    "client forgot mic_stop?",
                    self._drops, "" if self._drops == 1 else "s",
                )
            return True
        self._buf.extend(data)
        self._bytes_written += len(data)
        return True

    def reset(self) -> None:
        """Clear per-recording stats and buffer.

        ``_prev_injected`` is intentionally NOT cleared here — it
        survives ``reset()`` so the prepend-separator logic spans
        multiple recordings within the same session. It only resets
        on :meth:`close` (full teardown).
        """
        self._buf.clear()
        self._bytes_written = 0
        self._drops = 0

    def estimate_drain_seconds(self) -> float:
        # No external pipeline — :meth:`finalize` does the waiting.
        return 0.0

    def close(self) -> None:
        self._buf.clear()
        self._bytes_written = 0
        self._drops = 0
        self._prev_injected = False

    def diagnostic(self) -> str:
        if self._available:
            return f"transcriber: {self._whisper_bin} model={self.model_path}"
        return (
            f"transcriber: unavailable "
            f"(bin={self._whisper_bin} model_exists={self.model_path.exists()})"
        )

    # ── New: finalize for the macOS mic_stop path ──────────────────────

    async def finalize(self, pty_writer) -> None:
        """Transcribe the buffer and inject the result into the PTY.

        ``pty_writer`` is anything with a ``write(bytes)`` method —
        ``ws.py``'s :class:`PtyProcess` qualifies. Called from the
        ``mic_stop`` handler via ``create_task`` so the WS receive
        loop doesn't block on whisper. Safe to call with an empty
        buffer (no-op + reset).
        """
        if not self._buf:
            log.debug("mic_stop with empty buffer; nothing to transcribe")
            return
        pcm = bytes(self._buf)
        self.reset()
        try:
            text = await self._transcribe(pcm)
        except Exception:
            # Last-resort catch: whisper subprocess bugs, FS issues
            # creating the temp WAV, etc. Logging the traceback rather
            # than dropping it silently is the only way the operator
            # learns voice is broken on macOS.
            log.exception("transcription crashed; no text injected")
            return
        text = text.strip()
        if not text:
            log.info("transcription produced empty text (silence / noise?)")
            return
        # Prepend a separator space on the second-and-subsequent
        # utterance only — see the ``_prev_injected`` docstring above
        # for the reasoning.
        payload = (" " + text) if self._prev_injected else text
        self._prev_injected = True
        log.info(
            "transcribed %.2fs of audio → %r",
            len(pcm) / (_SAMPLE_RATE_HZ * _BYTES_PER_SAMPLE), text,
        )
        # Defensive newline strip: whisper-cli output should never
        # contain a stray \n (we already strip), but if a future
        # release adds one, an inadvertent newline would submit
        # claude's prompt before the user has reviewed the transcription.
        payload = payload.replace("\r", "").replace("\n", " ")
        pty_writer.write(payload.encode("utf-8"))

    # ── Internals ──────────────────────────────────────────────────────

    async def _transcribe(self, pcm: bytes) -> str:
        """Wrap PCM in WAV, invoke whisper-cli, return decoded text.

        whisper-cli accepts file input only (verified upstream — no
        stdin path), so we materialise a temp file. ~32 KB per
        second of audio; even at the buffer cap (60 s) that's <2 MB.

        Subprocess invocation uses ``asyncio.create_subprocess_exec``
        to match the existing async-subprocess pattern in ws.py and
        tts.py, rather than wrapping ``subprocess.run`` in
        ``to_thread``. Keeps the event loop responsive while whisper
        runs; the worker is the kernel subprocess, not a Python thread.
        """
        assert self._whisper_bin is not None, "guarded by self._available"
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            _write_wav(wav_path, pcm)
            t0 = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                self._whisper_bin,
                "-m", str(self.model_path),
                "-f", str(wav_path),
                # --no-prints drops whisper-cli's startup banner +
                # per-segment progress logs; --no-timestamps makes the
                # transcript stdout a clean run of text rather than
                # `[00:00.000 --> 00:01.500] hello` lines.
                "--no-prints",
                "--no-timestamps",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_WHISPER_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                log.warning(
                    "whisper-cli timed out after %.0fs; killed", _WHISPER_TIMEOUT_S,
                )
                return ""
            elapsed = time.monotonic() - t0
            if proc.returncode != 0:
                # Log only the first stderr line — whisper's startup
                # chatter makes the rest noise.
                err_head = (stderr.decode("utf-8", "replace").strip()
                            .splitlines() or ["<no stderr>"])[0]
                log.warning(
                    "whisper-cli exited %d after %.2fs: %s",
                    proc.returncode, elapsed, err_head[:300],
                )
                return ""
            log.info("whisper-cli: %.2fs wall-clock", elapsed)
            return stdout.decode("utf-8", "replace")
        finally:
            wav_path.unlink(missing_ok=True)


def _write_wav(path: Path, pcm: bytes) -> None:
    """Write a 16 kHz mono Int16 WAV at ``path`` containing ``pcm``.

    Uses :mod:`wave` from the stdlib so the header is canonical and
    we don't carry hand-rolled :func:`struct.pack` calls. The stdlib
    module insists on opening its own file handle, which is fine —
    we own the path lifecycle.
    """
    with wave.open(str(path), "wb") as w:
        w.setnchannels(_CHANNELS)
        w.setsampwidth(_BYTES_PER_SAMPLE)
        w.setframerate(_SAMPLE_RATE_HZ)
        w.writeframes(pcm)
