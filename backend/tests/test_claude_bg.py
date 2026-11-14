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
    (["claude", "attach", "../../etc"], None),
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
    assert tmux._job_pid("ab", {"ab12cd34": 1, "ab99cd34": 2}) is None       # two candidates
    assert tmux._job_pid("ab1", {"ab12cd34": 1, "ab99cd34": 2}) == 1
    data = json.loads((d / f"{me}.json").read_text())
    data["procStart"] = "1"                                                  # a reused pid
    (d / f"{me}.json").write_text(json.dumps(data))
    assert tmux._find_bg_owners({"work": [100]}) == {}
