"""voice_ptt: reading Claude Code's /voice state off the screen, and the
screen-checked start (arm) and stop (should_release) presses."""
import asyncio

import pytest

from ccpipe import voice_ptt

REC = ["  ⏵⏵ auto mode on", "", "● REC · tap to send"]
LISTENING = ["  ⏵⏵ auto mode on · listening… · tap to send"]
PROCESSING = ["❯ ", "  Voice: processing…"]
IDLE = ["❯ ", "  ⏵⏵ auto mode on (shift+tab to cycle)"]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(voice_ptt, "_footer_seen", False)
    monkeypatch.setattr(voice_ptt, "_last_press", None)
    monkeypatch.setattr(voice_ptt, "REPAINT_S", 0.5)
    monkeypatch.setattr(voice_ptt, "POLL_S", 0.001)
    monkeypatch.setattr(voice_ptt, "ARM_PROCESSING_WAIT_S", 0.05)
    monkeypatch.setattr(voice_ptt, "ARM_CONFIRM_S", 0.05)


def test_voice_state_reads_the_footer():
    assert voice_ptt.voice_state(REC) == "recording"
    assert voice_ptt.voice_state(LISTENING) == "recording"
    assert voice_ptt.voice_state(PROCESSING) == "processing"
    assert voice_ptt.voice_state(["❯ ", "  processing…"]) == "processing"
    assert voice_ptt.voice_state(IDLE) == "idle"
    assert voice_ptt.voice_state(None) is None


def test_voice_state_ignores_the_conversation_above_the_footer():
    screen = ["● Still processing… the import"] + [f"line {i}" for i in range(12)] + IDLE
    assert voice_ptt.voice_state(screen) == "idle"


class Screen:
    """A fake pane: a script of screens, advanced by each capture; pressing
    the key optionally switches to another script."""

    def __init__(self, frames, after_press=None):
        self.frames, self.after_press, self.presses = list(frames), after_press, 0

    async def capture(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def press(self):
        self.presses += 1
        if self.after_press is not None:
            self.frames = list(self.after_press)


def run(coro):
    return asyncio.run(coro)


def test_arm_waits_out_processing_then_presses_once():
    s = Screen([PROCESSING, PROCESSING, IDLE], after_press=[IDLE, REC])
    assert run(voice_ptt.arm(s.capture, s.press)) == (True, "armed")
    assert s.presses == 1


def test_arm_already_recording_does_not_press():
    s = Screen([REC])
    ok, why = run(voice_ptt.arm(s.capture, s.press))
    assert (ok, why, s.presses) == (True, "already recording", 0)


def test_arm_gives_up_if_still_transcribing():
    s = Screen([PROCESSING])
    ok, why = run(voice_ptt.arm(s.capture, s.press))
    assert not ok and "transcribing" in why and s.presses == 0


def test_arm_fails_when_claude_ignores_the_press():
    voice_ptt.voice_state(REC)                 # footer known from earlier
    s = Screen([IDLE], after_press=[IDLE])     # e.g. text in Claude's prompt box
    ok, why = run(voice_ptt.arm(s.capture, s.press))
    assert not ok and "prompt box" in why and s.presses == 1


def test_arm_unverified_when_the_footer_was_never_seen():
    s = Screen([IDLE], after_press=[IDLE])     # a Claude UI we don't recognise
    assert run(voice_ptt.arm(s.capture, s.press)) == (True, "armed (unverified)")


def test_arm_unverified_when_the_screen_cannot_be_read():
    s = Screen([None])
    assert run(voice_ptt.arm(s.capture, s.press)) == (True, "armed (unverified)")
    assert s.presses == 1


def test_should_release_only_while_recording():
    voice_ptt.voice_state(REC)
    assert run(voice_ptt.should_release(Screen([REC]).capture)) is True
    assert run(voice_ptt.should_release(Screen([IDLE]).capture)) is False
    assert run(voice_ptt.should_release(Screen([PROCESSING]).capture)) is False


def test_should_release_blind_until_the_footer_is_known():
    assert run(voice_ptt.should_release(Screen([IDLE]).capture)) is True
    assert run(voice_ptt.should_release(Screen([None]).capture)) is True


def test_arm_right_after_a_stop_press_waits_for_the_repaint():
    # Our stop press has gone in, but the screen still shows "REC": that's
    # the old recording, not a new one. Wait for Claude to move on, then
    # wait out the transcription and press.
    voice_ptt.voice_state(REC)
    voice_ptt.pressed("recording")
    s = Screen([REC, REC, PROCESSING, PROCESSING, IDLE], after_press=[IDLE, REC])
    assert run(voice_ptt.arm(s.capture, s.press)) == (True, "armed")
    assert s.presses == 1


def test_arm_right_after_a_start_press_sees_it_take_effect():
    # An arm cancelled just after its press, then a new one: the stale idle
    # screen mustn't cause a second press (which would stop the recording).
    voice_ptt.voice_state(REC)
    voice_ptt.pressed("idle")
    s = Screen([IDLE, IDLE, REC])
    assert run(voice_ptt.arm(s.capture, s.press)) == (True, "already recording")
    assert s.presses == 0


def test_arm_records_its_press():
    s = Screen([IDLE], after_press=[REC])
    run(voice_ptt.arm(s.capture, s.press))
    assert voice_ptt._last_press is not None and voice_ptt._last_press[1] == "idle"


def test_should_release_waits_for_the_repaint_of_a_start_press():
    voice_ptt.voice_state(REC)
    voice_ptt.pressed("idle")
    assert run(voice_ptt.should_release(Screen([IDLE, REC]).capture)) is True


def test_old_press_is_not_waited_on(monkeypatch):
    voice_ptt.voice_state(REC)
    monkeypatch.setattr(voice_ptt, "_last_press", (voice_ptt.time.monotonic() - 5, "recording"))
    s = Screen([REC])
    assert run(voice_ptt.arm(s.capture, s.press)) == (True, "already recording")
