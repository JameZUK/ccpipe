"""A pane running `claude attach <job>` stands for the background Claude
it shows (tmux.py, "Claude background sessions")."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from ccpipe import tmux


@pytest.mark.parametrize("argv, job", [
    (["/opt/claude-code/bin/claude", "attach", "b68c0aaf"], "b68c0aaf"),
    (["claude", "attach", "b68c0aaf-4741-4e75-8b7f-371c211bda7e"], "b68c0aaf-4741-4e75-8b7f-371c211bda7e"),
    (["node", "/usr/lib/claude-code/cli.js", "attach", "b68c0aaf"], "b68c0aaf"),
    (["claude", "attach"], None),
    (["claude", "attach", "--help"], None),
    (["claude", "attach", "peer-names-handshake"], "peer-names-handshake"),
    (["claude", "attach", "bad\x1bname"], None),
    (["claude", "--resume", "attach", "b68c0aaf"], None),      # "attach" is a prompt here
    (["vim", "attach", "b68c0aaf"], None),
    (["claude"], None),
    ([], None),
])
def test_attach_job(argv, job):
    assert tmux.attach_job(argv) == job


@pytest.fixture
def bg(tmp_path, monkeypatch):
    """This test process plays the background Claude; a fake /proc gives
    pane 100 → shell 101 → `claude attach ab12cd34`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tmux, "_bg_cache", None)
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    me = os.getpid()
    (d / f"{me}.json").write_text(json.dumps({
        "pid": me, "procStart": tmux._proc_start_time(me), "kind": "bg", "jobId": "ab12cd34",
        "sessionId": "ab12cd34-0000-4000-8000-000000000000", "status": "busy",
        "name": "peer-names-handshake-nand",
        "statusUpdatedAt": 1_794_000_000_000}))
    argvs = {100: ["bash", "-i"], 101: ["claude", "attach", "ab12cd34"],
             200: ["bash"], 201: ["claude"], 202: ["claude", "attach", "ab12cd34"]}
    kids = {100: [101], 200: [201], 201: [202]}

    def read_argv(pid):
        if pid not in argvs:
            raise OSError("gone")
        return argvs[pid]

    monkeypatch.setattr(tmux, "_read_argv", read_argv)
    monkeypatch.setattr(tmux, "_read_children", lambda pid: kids.get(pid, []))
    return me, d


def test_the_attached_session_owns_the_background_claude(bg):
    me, _ = bg
    # Session "other" has an ordinary Claude; what runs under it isn't a viewer of ours.
    assert tmux._find_bg_owners({"work": [100], "other": [200]}) == {me: "work"}
    assert tmux._behind_attach(101) == me
    assert tmux._behind_attach(100) is None


def test_status_of_a_background_claude_goes_to_its_session(bg):
    me, _ = bg
    assert tmux._read_claude_states({}, {me: "work"}) == {"work": ("busy", 1_794_000_000)}
    assert tmux._read_claude_states({}, {}) == {}          # nobody attached: nobody's


def test_roots_include_the_background_claude(bg, monkeypatch):
    me, _ = bg

    async def panes():
        return {"work": [100], "other": [200]}

    monkeypatch.setattr(tmux, "all_panes", panes)
    assert asyncio.run(tmux.session_roots()) == {"work": [100, me], "other": [200]}


def test_a_stale_or_ambiguous_job_is_nobodys(bg):
    me, d = bg
    B = tmux.BgClaude
    two = [B(1, "ab12cd34", "ab12cd34-0000-4000-8000-000000000000", "fix-the-parser"),
           B(2, "ab99cd34", "ab99cd34-0000-4000-8000-000000000000", "fix-the-tests")]
    assert tmux._job_pid("ab12cd34", two) == 1
    assert tmux._job_pid("ab99cd34-0000", two) == 2          # start of the conversation id
    assert tmux._job_pid("parser", two) == 1                 # part of the name
    assert tmux._job_pid("fix-the", two) is None             # two candidates
    assert tmux._job_pid("ab", two) is None                  # too short to be an id
    data = json.loads((d / f"{me}.json").read_text())
    data["procStart"] = "1"                                  # a reused pid
    (d / f"{me}.json").write_text(json.dumps(data))
    assert tmux._find_bg_owners({"work": [100]}) == {}


def test_attach_by_name_finds_the_same_claude(bg, monkeypatch):
    me, _ = bg
    monkeypatch.setattr(tmux, "_read_argv", lambda pid: ["claude", "attach", "handshake"])
    assert tmux._behind_attach(101) == me


# ── stopping and resuming such a session (lifecycle.py) ─────────────────

def test_stop_ends_the_background_claude_first(bg, monkeypatch):
    from ccpipe import lifecycle
    calls: list[str] = []

    async def panes():
        return {"work": [100]}

    async def stop_job(job):
        calls.append(f"stop {job}")
        return True

    async def kill(name):
        calls.append(f"kill {name}")
        return True

    monkeypatch.setattr(tmux, "all_panes", panes)
    monkeypatch.setattr(tmux, "stop_job", stop_job)
    monkeypatch.setattr(tmux, "kill_session", kill)
    asyncio.run(lifecycle._stop_background("work"))
    assert calls == ["stop ab12cd34"]

    async def refuse(job):
        return False

    monkeypatch.setattr(tmux, "stop_job", refuse)
    with pytest.raises(lifecycle.LifecycleError):
        asyncio.run(lifecycle._stop_background("work"))      # so the session isn't killed


def test_resume_attaches_while_claude_knows_the_job(tmp_path, monkeypatch):
    from ccpipe import lifecycle
    monkeypatch.setenv("HOME", str(tmp_path))
    cid = "ab12cd34-0000-4000-8000-000000000000"
    assert " --continue;" in lifecycle.resume_command("/p/x", cid, False, "ab12cd34")   # not on record
    (tmp_path / ".claude" / "jobs" / "ab12cd34").mkdir(parents=True)
    (tmp_path / ".claude" / "jobs" / "ab12cd34" / "state.json").write_text("{}")
    assert lifecycle.resume_command("/p/x", cid, False, "ab12cd34").startswith("claude attach ab12cd34;")
    assert "claude attach" not in lifecycle.resume_command("/p/x", cid, False, "../../x")
    assert "claude attach" not in lifecycle.resume_command("/p/x", cid, True, "ab12cd34")      # a shell session


def test_the_job_is_remembered_until_an_ordinary_claude_is_seen(tmp_path, monkeypatch):
    from ccpipe import session_meta, statefile
    monkeypatch.setenv(session_meta.META_FILE_ENV, str(tmp_path / "meta.json"))
    statefile.reset()
    cid = "ab12cd34-0000-4000-8000-000000000000"
    session_meta.note_details("work", "/p/x", cid, True, "ab12cd34")
    assert session_meta.load()["work"]["job"] == "ab12cd34"
    session_meta.note_details("work", "/p/x", None, True)             # nothing new learnt
    assert session_meta.load()["work"]["job"] == "ab12cd34"
    session_meta.note_details("work", "/p/x", "cd12cd34-0000-4000-8000-000000000000", True)
    assert "job" not in session_meta.load()["work"]
