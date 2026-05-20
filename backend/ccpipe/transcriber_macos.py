"""Local-transcription mic sink for macOS — drop-in replacement for ``mic.MicWriter``.

On Linux, voice flows: browser PCM → ``mic.MicWriter`` writes a FIFO →
PulseAudio's ``module-pipe-source`` exposes it as a microphone → claude's
``/voice`` reads from that mic and transcribes via the Anthropic API.

On macOS that chain is broken at the very end. ``claude``'s ``/voice``
keybinding handler has had a regression since v2.1.83 that's still
unfixed as of v2.1.144 (see anthropics/claude-code#38690). Even a
perfectly-functioning BlackHole-based virtual mic — which we proved out
in an earlier iteration of this port — produces no transcription on the
``claude`` side. We sidestep the upstream bug entirely by running the
transcription ourselves with ``whisper-cpp`` and injecting the result
into the PTY as if the user had typed it.

The interface mirrors ``mic.MicWriter`` so ``ws.py`` can pick one or the
other at module init based on ``sys.platform``; no other code paths need
to know. The capability probe (``write(b"")``) returns True iff
``whisper-cli`` is on PATH and the model file exists.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Defaults match scripts/install.sh on macOS. The env vars are intended
# for operators who keep models elsewhere or build whisper-cpp from
# source — they don't appear in the upstream Linux config so we don't
# bother making them first-class settings.
DEFAULT_MODEL_PATH = (
    Path.home() / "Library/Application Support/ccpipe/whisper-models/ggml-base.en.bin"
)
WHISPER_BIN_ENV = "CCPIPE_WHISPER_BIN"
WHISPER_MODEL_ENV = "CCPIPE_WHISPER_MODEL"

# Hard cap on the buffer. 60s @ 16 kHz mono Int16 = 1.92 MB — cheap, and
# more than enough for any realistic dictation utterance. Guards against
# a client that forgot to send ``mic_stop`` (e.g. tab crash mid-record).
_MAX_BUFFER_BYTES = 60 * 16000 * 2

# Audio format the browser writes and whisper expects. Matches mic.py.
_SAMPLE_RATE_HZ = 16000


class MicTranscriber:
    """In-memory PCM buffer + local whisper-cpp transcription on flush.

    Public interface is the subset of ``mic.MicWriter`` that ``ws.py``
    touches: ``write``, ``available``, ``bytes_written``, ``drops``,
    ``reset``, ``estimate_drain_seconds``, ``diagnostic``. Adds one new
    method, ``finalize(pty_proc)``, which the macOS branch of the
    mic_stop handler calls in place of the Linux ``_release_ptt_after``
    scheduling — runs the transcription and types the resulting text
    into the PTY.

    There's no pipe to drain on this path so ``estimate_drain_seconds``
    always returns 0 — ``finalize`` itself is the wait.
    """

    def __init__(
        self,
        model_path: Path | None = None,
        whisper_bin: str | None = None,
    ) -> None:
        self._buf = bytearray()
        self._bytes_written = 0
        self._drops = 0
        self.model_path = model_path or Path(
            os.environ.get(WHISPER_MODEL_ENV, str(DEFAULT_MODEL_PATH))
        )
        self._whisper_bin = whisper_bin or os.environ.get(WHISPER_BIN_ENV) or shutil.which(
            "whisper-cli"
        )
        self._available = bool(self._whisper_bin) and self.model_path.exists()
        if not self._available:
            log.warning(
                "transcriber unavailable: whisper-cli=%r model=%s (exists=%s); "
                "install with `brew install whisper-cpp` and run scripts/install.sh",
                self._whisper_bin, self.model_path, self.model_path.exists(),
            )

    # ── MicWriter-compatible interface ────────────────────────────────

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
        """Append PCM to the buffer. Returns True iff transcription is
        wired up — matches MicWriter.write semantics so the WS-hello
        capability probe (``write(b"")``) tells the frontend whether to
        show the mic FAB."""
        if not self._available:
            return False
        if not data:
            return True
        if len(self._buf) + len(data) > _MAX_BUFFER_BYTES:
            # Forgot mic_stop. Keep the prefix that fits, count as a
            # drop so the operator sees it in the mic_stop log line.
            spare = _MAX_BUFFER_BYTES - len(self._buf)
            if spare > 0:
                self._buf.extend(data[:spare])
                self._bytes_written += spare
            self._drops += 1
            if self._drops == 1 or self._drops % 50 == 0:
                log.warning(
                    "transcriber buffer cap hit (%d frame%s); "
                    "client forgot mic_stop?",
                    self._drops, "" if self._drops == 1 else "s",
                )
            return True
        self._buf.extend(data)
        self._bytes_written += len(data)
        return True

    def reset(self) -> None:
        self._buf.clear()
        self._bytes_written = 0
        self._drops = 0

    def estimate_drain_seconds(self) -> float:
        # No external pipeline — finalize() does the waiting synchronously.
        return 0.0

    def diagnostic(self) -> str:
        if self._available:
            return f"transcriber: {self._whisper_bin} model={self.model_path}"
        return (
            f"transcriber: unavailable (bin={self._whisper_bin} "
            f"model_exists={self.model_path.exists()})"
        )

    # ── New: finalize for the macOS mic_stop path ─────────────────────

    async def finalize(self, pty_proc) -> None:
        """Transcribe the buffer and inject the result into ``pty_proc``.

        Called from the mic_stop handler in ws.py via ``create_task`` so
        the WS receive loop doesn't block on whisper. Safe to call with
        an empty buffer (no-op + reset).

        whisper-cpp runs as a subprocess; we offload to a worker thread
        so the event loop stays responsive even when the model is large
        or the user dictated a long utterance.
        """
        if not self._buf:
            log.debug("mic_stop with empty buffer; nothing to transcribe")
            return
        pcm = bytes(self._buf)
        self.reset()
        try:
            text = await asyncio.to_thread(self._transcribe_sync, pcm)
        except Exception:
            log.exception("transcription crashed; no text injected")
            return
        text = text.strip()
        if not text:
            log.info("transcription produced empty text (silence / noise?)")
            return
        log.info(
            "transcribed %.2fs of audio → %r",
            len(pcm) / (_SAMPLE_RATE_HZ * 2), text,
        )
        # Append a trailing space so successive dictations don't collide
        # at word boundaries. If the user dislikes the space they can
        # backspace once; cheaper than risking "helloworld".
        pty_proc.write((text + " ").encode("utf-8"))

    # ── Internals ─────────────────────────────────────────────────────

    def _transcribe_sync(self, pcm: bytes) -> str:
        """Wrap PCM in a minimal WAV header, invoke whisper-cli, return text.

        whisper-cli only accepts file inputs (no stdin), so we have to
        materialise a temp file. ~32 KB per second of audio — trivial.
        """
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav:
            wav_path = Path(wav.name)
            _write_wav_header(wav, len(pcm))
            wav.write(pcm)
        try:
            t0 = time.monotonic()
            result = subprocess.run(
                [
                    self._whisper_bin,
                    "-m", str(self.model_path),
                    "-f", str(wav_path),
                    # --no-prints suppresses whisper-cli's own startup
                    # banner and per-segment progress logging — the
                    # transcript goes to stdout via --no-timestamps.
                    "--no-prints",
                    "--no-timestamps",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            elapsed = time.monotonic() - t0
            if result.returncode != 0:
                # Log the FIRST line of stderr; whisper's startup chatter
                # makes the rest noise.
                err = result.stderr.strip().splitlines()
                head = err[0] if err else "<no stderr>"
                log.warning(
                    "whisper-cli exited %d after %.2fs: %s",
                    result.returncode, elapsed, head[:300],
                )
                return ""
            log.info("whisper-cli: %.2fs wall-clock", elapsed)
            return result.stdout
        finally:
            wav_path.unlink(missing_ok=True)


def _write_wav_header(fh, data_size: int) -> None:
    """Write a 44-byte canonical PCM WAV header for 16 kHz mono Int16 LE.

    Kept inline (rather than importing ``wave``) because the wave module
    insists on opening its own file handle and we already own one. 44
    bytes of static structure isn't worth the import dance.
    """
    fh.write(b"RIFF")
    fh.write(struct.pack("<I", 36 + data_size))   # RIFF chunk size
    fh.write(b"WAVEfmt ")
    fh.write(struct.pack("<I", 16))                # fmt chunk size
    fh.write(struct.pack("<H", 1))                  # format = PCM
    fh.write(struct.pack("<H", 1))                  # channels = 1
    fh.write(struct.pack("<I", _SAMPLE_RATE_HZ))    # sample rate
    fh.write(struct.pack("<I", _SAMPLE_RATE_HZ * 2))  # byte rate
    fh.write(struct.pack("<H", 2))                  # block align
    fh.write(struct.pack("<H", 16))                 # bits per sample
    fh.write(b"data")
    fh.write(struct.pack("<I", data_size))
