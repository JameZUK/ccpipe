"""Starting and stopping Claude Code's /voice from ccpipe's mic button.

ccpipe drives /voice in tap mode (settings_patch.py): ESC k (meta+k) toggles
recording on and off. A toggle is not always obeyed, though. Claude Code
2.1's tap-mode handler ignores the key while it is still transcribing the
last recording (voiceState "processing"), and while idle with text in its
prompt box. Pressing blind (the client sent the start, the server sent the
stop after the audio drained) therefore lost a press whenever a new
recording began within a second or two of the last one. After that every
press inverted the state: Claude recorded while you weren't talking, and
the mic pipe overflowed while you were (2026-11-03, phone).

So the server presses the key and checks the result on screen. While
recording, Claude's footer reads "REC · tap to send" (or "listening… · tap
to send"); while transcribing it reads "processing…"; idle shows nothing.

- arm(): wait out "processing…", press only if Claude isn't already
  recording, then wait for "tap to send". An already-recording Claude (a
  leftover from an earlier desync) counts as armed, which puts the two
  back in step.
- should_release(): the stop press is sent only while Claude is recording.

Claude repaints its footer a moment after a key press, so right after one
of our presses the screen still shows the old state. Each check therefore
waits (up to REPAINT_S) for the screen to move on from the state the press
was made in. Without that, a start right after a stop read the stale "REC"
as "already recording" and skipped its press; Claude then stopped, and the
new recording went nowhere (2026-11-03, phone: stop, then start a second
later).

If this Claude Code version never shows the footer we look for (a UI
change), screen checks can't be trusted. Until "tap to send" has been seen
once, arm() presses and proceeds and releases are sent blind, as before.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Literal

VoiceState = Literal["recording", "processing", "idle"]

_RECORDING_RE = re.compile(r"tap to send")
_PROCESSING_RE = re.compile(r"processing…|Voice: processing")
# The footer sits under the prompt box; look only at the bottom of the
# screen so a "processing…" in the conversation can't match.
_FOOTER_LINES = 8

ARM_PROCESSING_WAIT_S = 8.0     # longest we wait for a transcription to finish
ARM_CONFIRM_S = 1.5             # how long the "tap to send" footer may take
REPAINT_S = 1.0                 # longest we wait for a press to show on screen
POLL_S = 0.12

# Set once "tap to send" has been seen: from then on the screen checks are
# trusted (see the module docstring).
_footer_seen = False
# Our last press of the voice key: when, and the state it was made in.
_last_press: tuple[float, VoiceState] | None = None


def voice_state(lines: list[str] | None) -> VoiceState | None:
    """Claude's voice state from the screen, or None if it couldn't be read."""
    global _footer_seen
    if lines is None:
        return None
    tail = "\n".join([ln for ln in lines if ln.strip()][-_FOOTER_LINES:])
    if _RECORDING_RE.search(tail):
        _footer_seen = True
        return "recording"
    if _PROCESSING_RE.search(tail):
        return "processing"
    return "idle"


def footer_seen() -> bool:
    return _footer_seen


def pressed(before: VoiceState | None) -> None:
    """Note a press of the voice key, made while Claude was in *before*."""
    global _last_press
    _last_press = (time.monotonic(), before) if before is not None else None


Capture = Callable[[], Awaitable[list[str] | None]]


async def _wait_for(capture: Capture, done: Callable[[VoiceState | None], bool],
                    timeout: float) -> VoiceState | None:
    deadline = time.monotonic() + timeout
    while True:
        state = voice_state(await capture())
        if done(state) or time.monotonic() >= deadline:
            return state
        await asyncio.sleep(POLL_S)


async def _settled(capture: Capture) -> VoiceState | None:
    """Claude's state, read once it has repainted after our last press."""
    if _last_press is not None:
        at, before = _last_press
        left = REPAINT_S - (time.monotonic() - at)
        if left > 0:
            return await _wait_for(capture, lambda s: s != before, left)
    return voice_state(await capture())


async def arm(capture: Capture, press: Callable[[], None]) -> tuple[bool, str]:
    """Get Claude recording. Returns (ok, why) — why is shown to the user
    when ok is False, and logged either way."""
    state = await _settled(capture)
    if state == "processing":
        state = await _wait_for(capture, lambda s: s != "processing", ARM_PROCESSING_WAIT_S)
    if state == "recording":
        return True, "already recording"
    if state == "processing":
        return False, "Claude is still transcribing the last recording"
    press()
    pressed(state)
    state = await _wait_for(capture, lambda s: s == "recording", ARM_CONFIRM_S)
    if state == "recording":
        return True, "armed"
    if state is None or not _footer_seen:
        # Can't read the screen, or this Claude never shows the footer we
        # know: carry on unverified, as before.
        return True, "armed (unverified)"
    return False, "Claude didn't start recording. Is there text in its prompt box?"


async def should_release(capture: Capture) -> bool:
    """Send the stop press? Only while Claude is recording; blind when the
    screen can't be trusted."""
    state = await _settled(capture)
    if state is None or not _footer_seen:
        return True
    return state == "recording"
