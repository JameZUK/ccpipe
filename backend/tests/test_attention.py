"""What needs you (attention.py): Claude's questions and its finished turns,
noticed in every session from Claude Code's session files."""
import asyncio
import json

import pytest

from ccpipe import attention, claude_asks
from ccpipe.paths import claude_project_dir

from .test_claude_asks import QUESTION_SCREEN, RULE

SEP = "\x1f"
SID = "0b6c1d1e-6f0e-4b7a-9c39-2f2c2b0e8f11"


class Claudes:
    """Claude Code's session files, tmux's panes and Claude's screens, as
    the watcher sees them."""

    def __init__(self, monkeypatch, tmp_path):
        attention._reset_for_tests()
        self.dir = tmp_path / ".claude" / "sessions"
        self.dir.mkdir(parents=True)
        self.states: dict[int, dict] = {}
        self.panes: dict[str, str] = {}          # pane → session
        self.screens: dict[str, list[str]] = {}  # pane → screen
        monkeypatch.setattr(attention.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(attention.tmux, "_tmux_out", self.tmux_out)
        monkeypatch.setattr(attention.tmux, "_proc_start_time", lambda p: "99")
        monkeypatch.setattr(attention, "POLL_S", 0.01)
        monkeypatch.setattr(attention, "SETTLE_S", 0.05)
        monkeypatch.setattr(attention, "REREAD_S", 0.02)
        monkeypatch.setattr(claude_asks, "_SCREEN_TRIES_S", (0.01, 0.01))
        monkeypatch.setattr(attention, "PENDING_S", 0.1)
        # The pids are made up: say each runs under its pane's process.
        monkeypatch.setattr(attention, "_descends", lambda pid, anc: anc == 1000 + int(self.panes_pid.get(pid, 0)))
        self.panes_pid: dict[int, int] = {}
        self.events: list[tuple[str, attention.Item]] = []
        self.cancel = attention.subscribe(lambda e, i: self.events.append((e, i)))

    def add(self, session="work", pid=4242, pane="%7", **state):
        self.panes[pane] = session
        self.panes_pid[pid] = int(pane[1:])
        self.screens.setdefault(pane, QUESTION_SCREEN)
        self.states[pid] = {"pid": pid, "procStart": "99", "status": "busy",
                            "statusUpdatedAt": 1, "tmux": f"{session}:@1.{pane}",
                            "sessionId": SID, "cwd": "/home/dev/proj", **state}
        self.write(pid)

    def write(self, pid=4242, **changes):
        self.states[pid].update(changes)
        (self.dir / f"{pid}.json").write_text(json.dumps(self.states[pid]))

    def remove(self, pid=4242):
        (self.dir / f"{pid}.json").unlink()

    async def tmux_out(self, *args):
        if args[:2] == ("list-panes", "-a"):
            return "".join(f"{p}{SEP}{s}{SEP}{1000 + int(p[1:])}\n" for p, s in self.panes.items())
        if args[0] == "list-panes":            # one session's panes
            name = args[3].strip("=:")
            return "".join(f"{p}\n" for p, s in self.panes.items() if s == name)
        assert args[:3] == ("capture-pane", "-p", "-t")
        return "\n".join(self.screens[args[3]])

    def kinds(self, event="new"):
        return [(i.level, i.kind) for e, i in self.events if e == event]


async def _run(claudes, steps, gap=0.06):
    task = asyncio.create_task(attention.run())
    try:
        for step in steps:
            await asyncio.sleep(gap)
            step() if callable(step) else claudes.write(**step)
        await asyncio.sleep(0.12)
    finally:
        task.cancel()
        claudes.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Its last look mustn't stand in for tmux in the next test file.
        attention._snapshot_at = float("-inf")


WAIT = {"status": "waiting", "waitingFor": "input needed", "statusUpdatedAt": 2}


# ─── Questions ────────────────────────────────────────────────────────────

async def test_a_new_question_is_announced_once(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    await _run(c, [WAIT])
    assert [i.body for e, i in c.events if e == "new"] == [
        "Which colour do you prefer, red or blue? Red, or Blue."]
    assert [(i.session, i.level) for i in attention.items()] == [("work", "ask")]
    assert attention.asking("work")


async def test_one_already_up_when_ccpipe_starts_is_listed_not_announced(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add(**WAIT)
    await _run(c, [{}])
    assert c.kinds("new") == []
    assert c.kinds("quiet") == [("ask", "question")]
    assert attention.asking("work")


async def test_each_wait_is_announced_and_ends_when_answered(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    await _run(c, [WAIT, {"status": "busy", "statusUpdatedAt": 3},
                   {"status": "waiting", "waitingFor": "permission prompt", "statusUpdatedAt": 4}])
    # The screen didn't change, but the second wait is a permission prompt.
    assert c.kinds("new") == [("ask", "question"), ("ask", "permission")]
    assert c.kinds("end") == [("ask", "question")]


async def test_answered_before_the_screen_was_read(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_asks, "_SCREEN_TRIES_S", (0.2,))
    c.add()
    await _run(c, [WAIT, {"status": "busy", "statusUpdatedAt": 3}])
    assert c.kinds("new") == []
    assert attention.items() == []


async def test_a_stale_file_for_a_reused_pid_is_ignored(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    monkeypatch.setattr(attention.tmux, "_proc_start_time", lambda p: "12345")
    c.add()
    await _run(c, [WAIT])
    assert c.events == []


async def test_back_to_back_dialogs_while_still_waiting(monkeypatch, tmp_path):
    # Two questions asked at once: the status never leaves "waiting".
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    second = [ln.replace("Colour", "Size").replace("Which colour do you prefer, red or blue?",
                                                  "Which size?") for ln in QUESTION_SCREEN]
    await _run(c, [WAIT, lambda: c.screens.update({"%7": second})], gap=0.12)
    assert [i.body.split("?")[0] for e, i in c.events if e == "new"] == [
        "Which colour do you prefer, red or blue", "Which size"]


def _two_tab_screen(current: str, done: str = "☐") -> list[str]:
    return [RULE, f"←  {done} Fruit  ☐ Season  ✔ Submit  →", "", current, "",
            "❯ 1. Yes", "  2. No", "  3. Type something.", RULE, "  4. Chat about this"]


async def test_moving_between_the_questions_of_one_dialog_is_not_new(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    c.screens["%7"] = _two_tab_screen("Apple?")
    await _run(c, [WAIT, lambda: c.screens.update({"%7": _two_tab_screen("Summer?", done="☒")})],
               gap=0.12)
    assert [i.body.split("?")[0] for e, i in c.events if e == "new"] == ["Apple"]


async def test_an_unreadable_dialog_is_listed_and_filled_in_later(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add(**WAIT)
    c.screens["%7"] = ["nothing to see"]
    await _run(c, [lambda: c.screens.update({"%7": QUESTION_SCREEN})], gap=0.1)
    assert c.kinds("quiet") == [("ask", "question")]
    assert c.kinds("new") == []          # the same wait: not a second alert
    assert [i.body.split("?")[0] for e, i in c.events if e == "update"] == [
        "Which colour do you prefer, red or blue"]


async def test_split_panes_the_waiting_claude_wins(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add(pid=1, pane="%1", status="idle")
    c.add(pid=2, pane="%2")
    await _run(c, [lambda: c.write(2, **WAIT)])
    assert c.kinds("new") == [("ask", "question")]


async def test_claude_exiting_ends_it(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    await _run(c, [WAIT, c.remove])
    assert c.kinds("end") == [("ask", "question")]
    assert attention.items() == []


# ─── Finished turns ───────────────────────────────────────────────────────

def _transcript(records):
    d = claude_project_dir("/home/dev/proj")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SID}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


def _reply(text, stop="end_turn"):
    return {"type": "assistant", "message": {"role": "assistant", "stop_reason": stop,
                                             "content": [{"type": "text", "text": text}]}}


async def test_a_finished_turn_says_how_the_reply_ended(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    _transcript([{"type": "user", "message": {"role": "user", "content": "go"}},
                 _reply("Checking.", stop="tool_use"),
                 _reply("Done: the **tests** pass.\n\n```\nok\n```")])
    await _run(c, [{"status": "idle", "statusUpdatedAt": 2}])
    done = [i for e, i in c.events if e == "new"]
    assert [(i.level, i.title, i.body) for i in done] == [
        ("done", "Claude finished", "Done: the tests pass.")]
    assert attention.get("work", "done") is done[0]


async def test_not_while_a_page_shows_the_session(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    monkeypatch.setattr(attention, "_watched", lambda s: s == "work")
    c.add()
    await _run(c, [{"status": "idle", "statusUpdatedAt": 2}])
    assert c.events == []


async def test_a_moment_of_idle_between_steps_is_not_finished(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    monkeypatch.setattr(attention, "SETTLE_S", 0.3)
    c.add()
    await _run(c, [{"status": "idle", "statusUpdatedAt": 2}, {"status": "busy", "statusUpdatedAt": 3}])
    assert c.events == []


async def test_seen_cleared_or_working_again_ends_it(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    await _run(c, [{"status": "idle", "statusUpdatedAt": 2}, lambda: attention.seen("work"),
                   {"status": "busy", "statusUpdatedAt": 3}, {"status": "shell", "statusUpdatedAt": 4},
                   lambda: attention.dismiss(), {"status": "busy", "statusUpdatedAt": 5},
                   {"status": "idle", "statusUpdatedAt": 6}, {"status": "busy", "statusUpdatedAt": 7}],
               gap=0.1)
    assert c.kinds("new") == [("done", "done")] * 3
    assert c.kinds("end") == [("done", "done")] * 3
    assert attention.items() == []


async def test_idle_when_first_seen_is_not_finished(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add(status="idle")
    await _run(c, [{}])
    assert c.events == []


def test_last_reply_stops_at_your_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(attention.Path, "home", lambda: tmp_path)
    state = {"sessionId": SID, "cwd": "/home/dev/proj"}
    _transcript([_reply("Old reply."), {"type": "user", "message": {"role": "user", "content": "new"}}])
    assert attention.last_reply(state) == ""
    _transcript([_reply("x" * 400)])
    assert len(attention.last_reply(state)) == 300
    assert attention.last_reply({"sessionId": "../etc", "cwd": "/"}) == ""
    assert attention.last_reply({}) == ""


# ─── Review fixes ─────────────────────────────────────────────────────────

async def test_a_file_caught_mid_write_is_not_a_new_question(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    path = c.dir / "4242.json"
    await _run(c, [WAIT, lambda: path.write_text('{"pid": 42'), lambda: c.write()], gap=0.1)
    assert c.kinds("new") == [("ask", "question")]
    assert c.kinds("end") == []


async def test_a_claude_briefly_missing_keeps_its_items(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    saved = {}

    def hide():
        saved["s"] = (c.dir / "4242.json").read_text()
        c.remove()
    await _run(c, [WAIT, hide, lambda: (c.dir / "4242.json").write_text(saved["s"])], gap=0.02)
    assert c.kinds("new") == [("ask", "question")] and c.kinds("end") == []


async def test_a_claude_in_another_tmux_server_is_not_this_sessions(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()                                  # ours, in %7
    c.add(pid=999, pane="%7", session="work")  # the same pane id, another server
    c.panes_pid[999] = 55                       # runs under some other process
    await _run(c, [lambda: c.write(999, **WAIT)])
    assert c.events == []
    assert attention._snapshot == {"work": ("busy", 0)}


async def test_a_new_question_is_announced_with_its_words(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    c.screens["%7"] = ["drawing…"]
    await _run(c, [WAIT, lambda: c.screens.update({"%7": QUESTION_SCREEN})], gap=0.05)
    news = [i for e, i in c.events if e == "new"]
    assert [i.body.split("?")[0] for i in news] == ["Which colour do you prefer, red or blue"]


async def test_or_without_them_if_they_never_come(monkeypatch, tmp_path):
    c = Claudes(monkeypatch, tmp_path)
    c.add()
    c.screens["%7"] = ["drawing…"]
    await _run(c, [WAIT, {}, {}], gap=0.1)
    assert [(i.title, i.body) for e, i in c.events if e == "new"] == [("Claude has a question", "")]
