"""ccpipe.mic_bridge_macos — macOS replacement for PulseAudio's pipe-source.

On Linux, ccpipe registers a PulseAudio `module-pipe-source` so the FIFO
at ``/tmp/ccpipe_mic.pipe`` shows up directly as a microphone. CoreAudio
has no equivalent: it has no API to declare "this FIFO is a virtual
input device." The workaround is the BlackHole virtual audio driver
(``brew install --cask blackhole-2ch``), which exposes a paired
output→input device. This daemon is the bridge in the middle:

    /tmp/ccpipe_mic.pipe (16 kHz mono Int16 LE)
        → upsample to device rate (e.g. 48 kHz) + duplicate mono → L+R
        → BlackHole 2ch *output*
        → ... CoreAudio loops it back as BlackHole 2ch *input* ...
        → claude /voice records from the system default input

Operator-side wiring: set System Settings → Sound → Input → "BlackHole
2ch" while ccpipe is in use, so anything reading the default mic
(including ``claude``) hears the browser's PCM.

Design notes
------------
* **Lazy stream**: we only open the BlackHole output stream while a
  writer is connected to the FIFO. /voice sessions span seconds-to-
  minutes with idle gaps in between; keeping the stream open all the
  time risks PortAudio underruns and we'd just be feeding silence
  anyway. Lazy lifecycle also makes "the daemon crashed mid-stream"
  recoverable — re-open and we're good.
* **Sample-and-hold upsampling** (``np.repeat``): introduces images
  above the source Nyquist (8 kHz) but speech intelligibility is fine.
  Swap in ``scipy.signal.resample_poly`` if a future use case needs
  cleaner spectrum.
* **Device pinned by name**, not index: indices shuffle as the user
  plugs/unplugs devices; the name "BlackHole 2ch" is stable.
"""

from __future__ import annotations

import logging
import os
import signal
import stat
import sys

import numpy as np
import sounddevice as sd

# Path the ccpipe backend writes into; must match ccpipe.mic.DEFAULT_PIPE_PATH.
PIPE_PATH = os.environ.get("CCPIPE_MIC_PIPE", "/tmp/ccpipe_mic.pipe")
# Substring match against sounddevice's device list; BlackHole appears
# as "BlackHole 2ch" out of the box.
DEVICE_HINT = os.environ.get("CCPIPE_MIC_DEVICE", "BlackHole 2ch")
# The backend writes 16 kHz mono Int16 (see ccpipe.mic and the
# WebSocket protocol docs); this MUST match or audio will be pitched.
SOURCE_SR = 16_000
# 100 ms read chunks. Larger = better throughput per syscall; smaller =
# lower latency. Voice is forgiving; 100 ms is a good middle.
CHUNK_FRAMES = 1600

log = logging.getLogger("ccpipe.mic_bridge")
_stop = False


def _handle_signal(signum: int, _frame: object) -> None:
    """SIGTERM/SIGINT → flag for graceful exit on the next loop turn."""
    global _stop
    _stop = True
    log.info("got signal %d; exiting after current chunk", signum)


def ensure_fifo(path: str) -> None:
    """Create the FIFO if missing; bail loudly if it exists as the wrong type.

    A common failure mode is a leftover directory at ``/tmp/ccpipe_mic.pipe``
    from an earlier Docker bind-mount on Linux. We don't try to remove it —
    that's the operator's call.
    """
    if not os.path.exists(path):
        os.mkfifo(path, 0o600)
        log.info("created FIFO %s (mode 0600)", path)
        return
    if not stat.S_ISFIFO(os.stat(path).st_mode):
        raise SystemExit(
            f"{path} exists but is not a FIFO. Remove it (rm or rmdir) and retry."
        )


def find_device(name_hint: str) -> tuple[int, int]:
    """Return ``(device_index, native_samplerate)`` for the first stereo
    output device whose name contains ``name_hint`` (case-insensitive).
    """
    for i, dev in enumerate(sd.query_devices()):
        if name_hint.lower() in dev["name"].lower() and dev["max_output_channels"] >= 2:
            return i, int(dev["default_samplerate"])
    raise SystemExit(
        f"no stereo output device matching {name_hint!r} found.\n"
        "  install BlackHole 2ch with: brew install --cask blackhole-2ch\n"
        "  then reboot for the audio plug-in to load."
    )


def upsample_mono_to_stereo(mono_i16: np.ndarray, factor: int) -> np.ndarray:
    """``(N,)`` mono Int16 at rate ``R`` → ``(N*factor, 2)`` stereo Int16 at ``R*factor``.

    Sample-and-hold via ``np.repeat`` — see module docstring for the
    quality trade-off.
    """
    up = np.repeat(mono_i16, factor)
    return np.column_stack((up, up))


def bridge_one_session(fifo_fd: int, device_idx: int, device_sr: int, factor: int) -> None:
    """Run one FIFO open → EOF cycle, streaming to BlackHole.

    ccpipe's mic.py opens the FIFO non-blocking and keeps it open for
    the life of the WebSocket connection — it doesn't close between
    /voice toggles, just stops writing. So in practice "writer closed"
    only fires when the browser tab disconnects. The diagnostic log
    below fires every ~1 s of audio data instead, so an operator can
    tell from the log whether speech (high RMS) or silence (RMS ≈ 0)
    is flowing without waiting for the WebSocket to close.
    """
    # Log a diagnostic line roughly every second of audio (16000 frames
    # × 2 bytes/Int16). Avoids spamming per-100 ms chunk but still gives
    # near-real-time visibility for end-to-end debugging.
    LOG_EVERY_BYTES = SOURCE_SR * 2
    with os.fdopen(fifo_fd, "rb") as fifo, sd.OutputStream(
        device=device_idx,
        samplerate=device_sr,
        channels=2,
        dtype="int16",
    ) as stream:
        log.info("writer connected; bridging %s → device %d", PIPE_PATH, device_idx)
        bytes_in = 0
        bytes_at_last_log = 0
        while not _stop:
            # Two bytes per Int16. Reads may return fewer than asked
            # (FIFO writer hasn't filled the buffer yet); that's fine,
            # sounddevice.write() blocks until the audio is consumed so
            # we naturally pace ourselves to wall-clock playback time.
            chunk = fifo.read(CHUNK_FRAMES * 2)
            if not chunk:
                break  # writer closed (rare — see docstring)
            # Defensive: an odd byte count would crash np.frombuffer.
            # We don't expect this from the backend, but if a process
            # ever wrote a half-sample we'd want a clear failure mode.
            if len(chunk) % 2:
                log.warning("dropping trailing byte (got odd-length read: %d)", len(chunk))
                chunk = chunk[:-1]
            mono = np.frombuffer(chunk, dtype=np.int16)
            stream.write(upsample_mono_to_stereo(mono, factor))
            bytes_in += len(chunk)
            if bytes_in - bytes_at_last_log >= LOG_EVERY_BYTES:
                # RMS on the most recent chunk only — cheaper than over
                # all-time, and what matters is "is the user speaking
                # RIGHT NOW", not lifetime average. 32-bit accumulator
                # prevents int16² overflow.
                rms = float(np.sqrt(np.mean(mono.astype(np.int32) ** 2)))
                kind = "speech" if rms > 200 else "silence"
                log.info(
                    "bridging: total=%d bytes  recent RMS=%.0f (%s)",
                    bytes_in, rms, kind,
                )
                bytes_at_last_log = bytes_in
        log.info("writer closed; %d bytes bridged this session", bytes_in)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    ensure_fifo(PIPE_PATH)

    device_idx, device_sr = find_device(DEVICE_HINT)
    if device_sr % SOURCE_SR != 0:
        raise SystemExit(
            f"device sample rate {device_sr} is not an integer multiple of {SOURCE_SR}.\n"
            "  In Audio MIDI Setup, set BlackHole 2ch to 48000 Hz (default) or 16000 Hz.\n"
            "  Non-integer ratios would need scipy.signal.resample_poly which isn't a dep."
        )
    factor = device_sr // SOURCE_SR
    log.info(
        "bridge ready: device=%d (%s) sr=%d upsample=x%d",
        device_idx, DEVICE_HINT, device_sr, factor,
    )

    while not _stop:
        log.info("waiting for writer on %s", PIPE_PATH)
        # os.open blocks on a FIFO until a writer connects. A signal
        # delivered while we're parked here interrupts the syscall
        # (EINTR → InterruptedError); loop back and re-check _stop.
        try:
            fifo_fd = os.open(PIPE_PATH, os.O_RDONLY)
        except InterruptedError:
            continue
        try:
            bridge_one_session(fifo_fd, device_idx, device_sr, factor)
        except sd.PortAudioError as e:
            # Most commonly: device disappeared (user unplugged or
            # changed sample rate in Audio MIDI Setup). Log and loop —
            # next session will re-resolve the device.
            log.error("audio stream error: %s", e)

    log.info("exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
