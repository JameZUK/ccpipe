"""Tests for the macOS-only mic backend.

The module is conditionally importable on any platform (it only spawns
whisper-cli at call time, not at import time), so we can exercise the
MicWriter-compatible surface here on Linux. The actual ``finalize`` path
mocks ``asyncio.create_subprocess_exec`` so no real ``whisper-cli`` is
required.
"""
from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# The module is intended for macOS but is importable everywhere; only
# the live whisper-cli spawn is platform-specific (and we mock it).
from ccpipe.transcriber_macos import (
    MicTranscriber,
    _MAX_BUFFER_BYTES,
    _write_wav,
)


# ── Capability probe ─────────────────────────────────────────────────────


def test_available_false_when_whisper_missing(tmp_path):
    """No whisper-cli on PATH → available=False, mic FAB hidden via the
    WS-hello capability probe (`write(b"")` returns False)."""
    t = MicTranscriber(model_path=tmp_path / "nope.bin", whisper_bin=None)
    assert t.available is False
    assert t.write(b"") is False


def test_available_false_when_model_missing(tmp_path):
    """whisper-cli present but no model file → still unavailable."""
    t = MicTranscriber(model_path=tmp_path / "missing.bin", whisper_bin="/usr/bin/true")
    assert t.available is False


def test_available_true_when_both_present(tmp_path):
    model = tmp_path / "fake-model.bin"
    model.write_bytes(b"not a real model, just needs to exist for the probe")
    t = MicTranscriber(model_path=model, whisper_bin="/usr/bin/true")
    assert t.available is True
    # Capability probe: zero-length write must return True without
    # mutating state.
    assert t.write(b"") is True
    assert t.bytes_written == 0


# ── Buffer accounting ────────────────────────────────────────────────────


def _make_available(tmp_path) -> MicTranscriber:
    model = tmp_path / "fake-model.bin"
    model.write_bytes(b"ok")
    return MicTranscriber(model_path=model, whisper_bin="/usr/bin/true")


def test_write_appends_and_counts(tmp_path):
    t = _make_available(tmp_path)
    assert t.write(b"\x01\x00\x02\x00") is True
    assert t.bytes_written == 4
    assert t.drops == 0


def test_write_respects_buffer_cap(tmp_path):
    """Over-cap write keeps the prefix that fits, drops the rest, and
    increments the drop counter (the operator sees the drop count in
    the mic_stop log line)."""
    t = _make_available(tmp_path)
    # Fill to one byte below the cap.
    fill = b"\x00" * (_MAX_BUFFER_BYTES - 1)
    assert t.write(fill) is True
    assert t.bytes_written == _MAX_BUFFER_BYTES - 1
    # A subsequent write of 100 bytes should accept exactly 1 (the
    # remaining spare) and count the call as one drop.
    assert t.write(b"\xff" * 100) is True
    assert t.bytes_written == _MAX_BUFFER_BYTES
    assert t.drops == 1


def test_write_when_unavailable_returns_false(tmp_path):
    t = MicTranscriber(model_path=tmp_path / "nope.bin", whisper_bin=None)
    # Non-empty write while unavailable: False, no counter movement.
    assert t.write(b"\x01\x02") is False
    assert t.bytes_written == 0


def test_reset_clears_buffer_and_stats(tmp_path):
    t = _make_available(tmp_path)
    t.write(b"\x00" * 100)
    assert t.bytes_written == 100
    t.reset()
    assert t.bytes_written == 0
    assert t.drops == 0


def test_estimate_drain_seconds_is_zero(tmp_path):
    """No external pipeline to drain — the finalize await is the wait."""
    t = _make_available(tmp_path)
    t.write(b"\x00" * 32_000)
    assert t.estimate_drain_seconds() == 0.0


def test_diagnostic_reflects_state(tmp_path):
    t_off = MicTranscriber(model_path=tmp_path / "no", whisper_bin=None)
    assert "unavailable" in t_off.diagnostic()
    t_on = _make_available(tmp_path)
    assert "transcriber:" in t_on.diagnostic()


# ── WAV header ───────────────────────────────────────────────────────────


def test_write_wav_produces_canonical_pcm_header(tmp_path):
    """_write_wav must emit a 16 kHz mono Int16 PCM WAV — whisper-cli
    is fussy and a wrong header silently produces empty transcription.
    Asserts the header bytes after a real file write."""
    pcm = b"\x00\x01" * 16_000  # 1 second of audio
    out = tmp_path / "x.wav"
    _write_wav(out, pcm)
    raw = out.read_bytes()
    # 44-byte header + len(pcm) data.
    assert len(raw) == 44 + len(pcm)
    h = raw[:44]
    assert h[0:4] == b"RIFF"
    assert struct.unpack("<I", h[4:8])[0] == 36 + len(pcm)
    assert h[8:12] == b"WAVE"
    assert h[12:16] == b"fmt "
    assert struct.unpack("<I", h[16:20])[0] == 16            # fmt chunk size
    assert struct.unpack("<H", h[20:22])[0] == 1             # PCM
    assert struct.unpack("<H", h[22:24])[0] == 1             # mono
    assert struct.unpack("<I", h[24:28])[0] == 16_000        # sample rate
    assert struct.unpack("<I", h[28:32])[0] == 32_000        # byte rate
    assert struct.unpack("<H", h[32:34])[0] == 2             # block align
    assert struct.unpack("<H", h[34:36])[0] == 16            # bits per sample
    assert h[36:40] == b"data"
    assert struct.unpack("<I", h[40:44])[0] == len(pcm)


# ── finalize() — the dictation flow ──────────────────────────────────────


class _FakePty:
    """Capture pty_proc.write() calls in-memory for assertion."""
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)


class _FakeProc:
    """asyncio.create_subprocess_exec drop-in for happy/error paths."""
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return (self._stdout, self._stderr)

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


async def test_finalize_empty_buffer_is_noop(tmp_path):
    t = _make_available(tmp_path)
    pty = _FakePty()
    await t.finalize(pty)
    assert pty.writes == []


async def test_finalize_types_text_into_pty(tmp_path):
    t = _make_available(tmp_path)
    t.write(b"\x00" * 1600)  # 0.05 s of audio
    pty = _FakePty()
    fake = _FakeProc(stdout=b"hello world")
    with patch("ccpipe.transcriber_macos.asyncio.create_subprocess_exec",
               return_value=fake):
        await t.finalize(pty)
    # First utterance: no leading separator space.
    assert pty.writes == [b"hello world"]


async def test_finalize_prepends_space_on_subsequent_utterances(tmp_path):
    """Two dictations in a row should join with a space — without it,
    `hello` + `world` becomes `helloworld` at the prompt boundary."""
    t = _make_available(tmp_path)
    pty = _FakePty()

    t.write(b"\x00" * 1600)
    with patch("ccpipe.transcriber_macos.asyncio.create_subprocess_exec",
               return_value=_FakeProc(stdout=b"hello")):
        await t.finalize(pty)

    t.write(b"\x00" * 1600)
    with patch("ccpipe.transcriber_macos.asyncio.create_subprocess_exec",
               return_value=_FakeProc(stdout=b"world")):
        await t.finalize(pty)

    assert pty.writes == [b"hello", b" world"]


async def test_finalize_strips_whitespace_and_newlines(tmp_path):
    """whisper-cli sometimes outputs a leading/trailing newline. Strip
    it so the result doesn't accidentally submit claude's prompt."""
    t = _make_available(tmp_path)
    t.write(b"\x00" * 1600)
    pty = _FakePty()
    with patch("ccpipe.transcriber_macos.asyncio.create_subprocess_exec",
               return_value=_FakeProc(stdout=b" \nhello there\n ")):
        await t.finalize(pty)
    assert pty.writes == [b"hello there"]


async def test_finalize_swallows_embedded_newlines(tmp_path):
    """Belt-and-braces: if whisper ever emits an embedded \\n in a
    transcription, we must not let it slip into the PTY as a submit."""
    t = _make_available(tmp_path)
    t.write(b"\x00" * 1600)
    pty = _FakePty()
    with patch("ccpipe.transcriber_macos.asyncio.create_subprocess_exec",
               return_value=_FakeProc(stdout=b"line one\nline two")):
        await t.finalize(pty)
    assert pty.writes == [b"line one line two"]
    assert b"\n" not in pty.writes[0]


async def test_finalize_empty_transcription_no_inject(tmp_path):
    """Silence/noise → whisper emits empty stdout → nothing is typed."""
    t = _make_available(tmp_path)
    t.write(b"\x00" * 1600)
    pty = _FakePty()
    with patch("ccpipe.transcriber_macos.asyncio.create_subprocess_exec",
               return_value=_FakeProc(stdout=b"")):
        await t.finalize(pty)
    assert pty.writes == []


async def test_finalize_handles_nonzero_exit(tmp_path):
    """whisper-cli non-zero exit → log + drop, no inject."""
    t = _make_available(tmp_path)
    t.write(b"\x00" * 1600)
    pty = _FakePty()
    with patch("ccpipe.transcriber_macos.asyncio.create_subprocess_exec",
               return_value=_FakeProc(stdout=b"ignored", returncode=1,
                                       stderr=b"model file is corrupted")):
        await t.finalize(pty)
    assert pty.writes == []


async def test_finalize_handles_timeout(tmp_path):
    """whisper-cli hanging → wait_for raises TimeoutError → log + drop."""
    class _Hanging:
        returncode = None
        async def communicate(self):
            await asyncio.sleep(10)
            return (b"never", b"")
        def kill(self): pass
        async def wait(self): return 0

    t = _make_available(tmp_path)
    t.write(b"\x00" * 1600)
    pty = _FakePty()
    with patch("ccpipe.transcriber_macos.asyncio.create_subprocess_exec",
               return_value=_Hanging()):
        with patch("ccpipe.transcriber_macos._WHISPER_TIMEOUT_S", 0.05):
            await t.finalize(pty)
    assert pty.writes == []


async def test_finalize_resets_buffer_even_on_error(tmp_path):
    """A successful or failed finalize must leave the buffer empty —
    otherwise the next recording transcribes the previous one's
    audio plus the new one's, producing nonsense text."""
    t = _make_available(tmp_path)
    t.write(b"\x00" * 32_000)
    assert t.bytes_written == 32_000
    pty = _FakePty()
    with patch("ccpipe.transcriber_macos.asyncio.create_subprocess_exec",
               return_value=_FakeProc(stdout=b"", returncode=1)):
        await t.finalize(pty)
    assert t.bytes_written == 0
