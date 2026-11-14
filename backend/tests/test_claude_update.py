"""Restarting a session whose Claude is older than the installed Claude
Code (claude_update.py): only when nothing can be lost."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from ccpipe import attention, claude_update, config as app_config, lifecycle, tmux

SID = "ab12cd34-0000-4000-8000-000000000000"


def _session(name="work", windows=1):
    return tmux.TmuxSession(name=name, windows=windows, attached=False, created=1, activity=1)


def _proc(status="idle", version="2.1.316", job=None, sid=SID):
    return tmux.ClaudeProc(100, status, version, sid, job)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """A session that may be restarted: idle for an hour, transcript there."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(attention, "asking", lambda name: False)
    monkeypatch.setattr(lifecycle, "_transcript_exists", lambda cwd, sid: True)
    now = time.time()
    return {"life": {"busy": None, "idle_since": now - 3600}, "meta": {"cwd": "/p/x"}, "now": now}


def _why(r, procs=None, panes=1, windows=1, manual=False, **over):
    life = None if over.get("life", 1) is None else {**r["life"], **{k: v for k, v in over.items() if k != "life"}}
    return claude_update.blocked_reason(_session(windows=windows), procs or [_proc()], panes,
                                        life, r["meta"], r["now"], manual)


def test_parse_version():
    assert claude_update.parse_version("2.1.322 (Claude Code)") == (2, 1, 322)
    assert claude_update.parse_version("2.1.9") < claude_update.parse_version("2.1.10")
    assert claude_update.parse_version("nonsense") is None and claude_update.parse_version(None) is None


def test_an_idle_settled_session_can_be_restarted(ready):
    assert _why(ready) is None


@pytest.mark.parametrize("status, word", [("busy", "working"), ("waiting", "answer"),
                                          ("shell", "background task"), ("mcp", "MCP")])
def test_claude_must_say_it_is_idle(ready, status, word):
    assert word in _why(ready, [_proc(status)])
    assert word in _why(ready, [_proc(status)], manual=True)        # asking doesn't override it


def test_anything_running_blocks_it(ready):
    assert _why(ready, busy="running make") == "running make"
    assert _why(ready, busy="running make", manual=True) == "running make"
    assert "CPU" in _why(ready, busy="using CPU (40% of a core)")


def test_an_open_terminal_blocks_only_the_automatic_restart(ready):
    assert _why(ready, busy="open in a terminal") == "open in a terminal"
    assert _why(ready, busy="open in a terminal", manual=True) is None


def test_it_waits_ten_idle_minutes_unless_asked(ready):
    assert "10 minutes" in _why(ready, idle_since=ready["now"] - 60)
    assert _why(ready, idle_since=ready["now"] - 60, manual=True) is None


def test_extra_panes_or_claudes_block_it(ready):
    assert "pane" in _why(ready, panes=2)
    assert "pane" in _why(ready, windows=2)
    assert "more than one Claude" in _why(ready, [_proc(), _proc()])


def test_a_question_on_screen_blocks_it(ready, monkeypatch):
    monkeypatch.setattr(attention, "asking", lambda name: True)
    assert "answer" in _why(ready)


def test_the_conversation_must_be_resumable_exactly(ready, monkeypatch):
    monkeypatch.setattr(lifecycle, "_transcript_exists", lambda cwd, sid: False)
    assert "conversation" in _why(ready)             # else `--continue` could pick another one
    monkeypatch.setattr(lifecycle, "_transcript_exists", lambda cwd, sid: True)
    assert "conversation" in _why(ready, [_proc(sid="")])


def test_not_before_the_idle_check_has_run(ready):
    assert _why(ready, life=None) == "not checked yet"


def test_a_background_session_needs_nothing_in_flight(ready, tmp_path):
    job = tmp_path / ".claude" / "jobs" / "ab12cd34"
    job.mkdir(parents=True)
    procs = [_proc(job="ab12cd34")]
    assert "can't be checked" in _why(ready, procs)                  # no record
    (job / "state.json").write_text(json.dumps({"inFlight": {"tasks": 2, "queued": 0}}))
    assert "background task" in _why(ready, procs)
    (job / "state.json").write_text(json.dumps({"inFlight": {"tasks": 0, "queued": 0}}))
    assert _why(ready, procs) is None


# ── the pass ────────────────────────────────────────────────────────────

@pytest.fixture
def world(ready, monkeypatch):
    """Two sessions behind (one blocked), one up to date."""
    restarted: list[str] = []
    procs = {"old": [_proc()], "busy": [_proc("busy")], "new": [_proc(version="2.1.322")]}

    async def installed(fresh=False):
        return "2.1.322"

    async def sessions():
        return [_session(n) for n in procs], True

    async def claude_procs():
        return procs

    async def panes():
        return {n: [1] for n in procs}

    async def do_restart(name):
        restarted.append(name)

    cfg = app_config.AppConfig()
    monkeypatch.setattr(app_config, "load", lambda: cfg)
    monkeypatch.setattr(claude_update, "installed_version", installed)
    monkeypatch.setattr(tmux, "list_sessions_checked", sessions)
    monkeypatch.setattr(tmux, "claude_procs", claude_procs)
    monkeypatch.setattr(tmux, "all_panes", panes)
    monkeypatch.setattr(lifecycle, "status", lambda: {n: ready["life"] for n in procs})
    monkeypatch.setattr(lifecycle, "restart_for_update", do_restart)
    monkeypatch.setattr(claude_update.session_meta, "load", lambda: {n: ready["meta"] for n in procs})
    monkeypatch.setattr(claude_update, "_tried", {})
    monkeypatch.setattr(claude_update, "_status", {})
    return cfg, restarted, procs


def test_only_sessions_behind_are_listed(world):
    out = asyncio.run(claude_update.survey())
    assert set(out) == {"old", "busy"}
    assert out["old"] == {"from": "2.1.316", "to": "2.1.322", "blocked": None}
    assert "working" in out["busy"]["blocked"]


def test_nothing_restarts_by_itself_unless_switched_on(world):
    cfg, restarted, _ = world
    asyncio.run(claude_update._pass())
    assert restarted == [] and claude_update.status("old") is not None
    cfg.sessions.auto_update = True
    asyncio.run(claude_update._pass())
    assert restarted == ["old"]


def test_a_session_is_restarted_once_per_version(world):
    cfg, restarted, _ = world
    cfg.sessions.auto_update = True
    asyncio.run(claude_update._pass())
    asyncio.run(claude_update._pass())            # still behind (the restart didn't help): leave it
    assert restarted == ["old"]


def test_asking_for_a_blocked_or_current_session_is_refused(world):
    _, restarted, _ = world
    with pytest.raises(lifecycle.LifecycleError) as e:
        asyncio.run(claude_update.restart("busy", manual=True))
    assert e.value.status == 409 and "working" in e.value.detail
    with pytest.raises(lifecycle.LifecycleError):
        asyncio.run(claude_update.restart("new", manual=True))
    assert restarted == []
    asyncio.run(claude_update.restart("old", manual=True))
    assert restarted == ["old"]


def test_a_newer_running_claude_is_left_alone(world):
    _, _, procs = world
    procs["old"] = [_proc(version="2.1.400")]      # the package was downgraded
    assert "old" not in asyncio.run(claude_update.survey())
