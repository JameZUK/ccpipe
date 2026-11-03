"""Claude stopping to ask: reading its dialog off the screen, and noticing
the wait in its session file. The screens are real captures (Claude Code
2.1.317, 2026-11-03), with the working directory replaced."""
import asyncio
import json

import pytest

from ccpipe import claude_asks
from ccpipe.claude_asks import Ask, describe

RULE = "─" * 80
DASH = "╌" * 80

QUESTION_SCREEN = f"""\
 ▐▛███▛█   Claude Code v2.1.317
▝▜██████▀  Haiku 5.5 · Claude Ultra
 ▝▝   ▝▝   /home/dev/proj


❯ Use the AskUserQuestion tool to ask me which colour I prefer, red or blue. Do nothing else.
{RULE}
 ☐ Colour

Which colour do you prefer, red or blue?

❯ 1. Red
     Choose red
  2. Blue
     Choose blue
  3. Type something.
{RULE}
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
""".split("\n")

PERMISSION_SCREEN = f"""\
❯ Run this exact Bash command: rm hello.txt

● Deleting hello.txt from the working directory
  ⎿  $ rm hello.txt

{RULE}
 Bash command
 Tip: auto mode handles these prompts for you — choose "switch to auto mode" below
 Delete hello.txt from the working directory
{DASH}
 rm hello.txt
{DASH}
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to /home/dev/proj from
      this project
   3. Yes, and switch to auto mode · auto mode handles these prompts for you
   4. No

 Esc to cancel · Tab to amend
""".split("\n")


def test_question():
    ask = describe(QUESTION_SCREEN, "input needed")
    assert ask == Ask("question", "Claude has a question",
                      "Which colour do you prefer, red or blue? Red, or Blue.")
    assert ask.spoken == "Claude asks: Which colour do you prefer, red or blue? Red, or Blue."


def test_permission_prompt_says_what_for():
    ask = describe(PERMISSION_SCREEN, "permission prompt")
    assert ask == Ask("permission", "Claude needs your permission",
                      "Bash command: Delete hello.txt from the working directory.")
    assert ask.spoken.startswith("Claude needs your permission. Bash command: Delete")


def test_permission_prompt_with_its_own_question():
    screen = [RULE, " Edit file", " backend/ccpipe/tts.py", DASH, "  1 -old", "  1 +new", DASH,
              " Do you want to make this edit to tts.py?", " ❯ 1. Yes", "   2. No"]
    assert describe(screen, "permission prompt").body == "Do you want to make this edit to tts.py?"


def test_several_questions_and_a_preview_column():
    screen = [RULE, " ←  ☐ Approach  ☐ Model  ✔ Submit  →", "",
              "How should I pick the new", "announcement model?", "",
              "❯ 1. Bake-off first (Recommended)      │ Runs 3 candidates side by side",
              "     I create the agent…                │",
              "  2. Just use Sonnet                    │",
              "  3. Type something.", RULE, "  4. Chat about this"]
    ask = describe(screen, "input needed")
    assert ask.title == "Claude has 2 questions"
    assert ask.body == ("How should I pick the new announcement model? "
                        "Bake-off first (Recommended), or Just use Sonnet.")
    assert ask.spoken.startswith("Claude has 2 questions. The first: How should")


def test_a_numbered_list_above_the_dialog_is_not_the_question():
    screen = ["● Two ways:", "  1. Rebuild", "  2. Patch", ""] + QUESTION_SCREEN
    assert describe(screen, "input needed").body.startswith("Which colour")


def test_no_dialog_found():
    assert describe(["just text"], "input needed") == Ask("question", "Claude has a question", "")
    ask = describe([], "permission prompt")
    assert ask.kind == "permission" and ask.spoken == "Claude needs your permission."


# ─── The watcher ──────────────────────────────────────────────────────────

class FakeClaude:
    """Claude's session file and screen, as the watcher sees them."""

    def __init__(self, monkeypatch, tmp_path):
        self.pid = 4242
        self.state = {"pid": self.pid, "procStart": "99", "status": "busy",
                      "statusUpdatedAt": 1, "tmux": "work:@1.%7"}
        self.screen = QUESTION_SCREEN
        monkeypatch.setattr(claude_asks.Path, "home", lambda: tmp_path)
        (tmp_path / ".claude" / "sessions").mkdir(parents=True)
        self.write()

        async def pid(_session):
            return self.pid

        async def tmux_out(*args):
            if args[0] == "list-panes":
                return "%3\n%7\n"
            assert args[:3] == ("capture-pane", "-p", "-t") and args[3] == "%7"
            return "\n".join(self.screen)
        monkeypatch.setattr(claude_asks.tmux, "cached_claude_pid", pid)
        monkeypatch.setattr(claude_asks.tmux, "_tmux_out", tmux_out)
        monkeypatch.setattr(claude_asks.tmux, "_proc_start_time", lambda p: "99")
        monkeypatch.setattr(claude_asks, "POLL_S", 0.01)
        monkeypatch.setattr(claude_asks, "_SCREEN_TRIES_S", (0.01, 0.01))
        self.path = tmp_path / ".claude" / "sessions" / f"{self.pid}.json"

    def write(self, **changes):
        self.state.update(changes)
        path = claude_asks.Path.home() / ".claude" / "sessions" / f"{self.pid}.json"
        path.write_text(json.dumps(self.state))


async def _run(fake, steps):
    asks: list[Ask] = []

    async def on_ask(ask):
        asks.append(ask)
    task = asyncio.create_task(claude_asks.watch("work", on_ask))
    try:
        for changes in steps:
            await asyncio.sleep(0.06)
            fake.write(**changes)
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return asks


async def test_a_new_question_is_reported_once(monkeypatch, tmp_path):
    fake = FakeClaude(monkeypatch, tmp_path)
    asks = await _run(fake, [{"status": "waiting", "waitingFor": "input needed", "statusUpdatedAt": 2}])
    assert [a.body for a in asks] == ["Which colour do you prefer, red or blue? Red, or Blue."]


async def test_one_already_up_when_you_attach_is_not(monkeypatch, tmp_path):
    fake = FakeClaude(monkeypatch, tmp_path)
    fake.write(status="waiting", waitingFor="input needed")
    assert await _run(fake, [{}]) == []


async def test_each_wait_is_reported(monkeypatch, tmp_path):
    fake = FakeClaude(monkeypatch, tmp_path)
    asks = await _run(fake, [
        {"status": "waiting", "waitingFor": "input needed", "statusUpdatedAt": 2},
        {"status": "busy", "statusUpdatedAt": 3},
        {"status": "waiting", "waitingFor": "permission prompt", "statusUpdatedAt": 4},
    ])
    # The screen didn't change, but the second wait is a permission prompt.
    assert [a.kind for a in asks] == ["question", "permission"]


async def test_answered_before_the_screen_was_read(monkeypatch, tmp_path):
    fake = FakeClaude(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_asks, "_SCREEN_TRIES_S", (0.2,))
    asks = await _run(fake, [{"status": "waiting", "statusUpdatedAt": 2},
                             {"status": "busy", "statusUpdatedAt": 3}])
    assert asks == []


async def test_a_stale_file_for_a_reused_pid_is_ignored(monkeypatch, tmp_path):
    fake = FakeClaude(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_asks.tmux, "_proc_start_time", lambda p: "12345")
    assert await _run(fake, [{"status": "waiting", "statusUpdatedAt": 2}]) == []
