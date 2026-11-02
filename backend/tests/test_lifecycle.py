"""Session lifecycle: what counts as busy, idle stops, boot selection, and a
real-tmux stop → remembered → resume round trip."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time

import pytest

from ccpipe import config as app_config
from ccpipe import lifecycle, proctree, session_meta, sticky, tmux
from ccpipe.proctree import Proc, Table

NOW = 1_800_000_000.0


def _table(*procs: tuple[int, int, str, list[str], float]) -> Table:
    t = {}
    for pid, ppid, comm, argv, cpu in procs:
        p = Proc(pid=pid, ppid=ppid, comm=comm, start=0.0, cpu=cpu)
        p.__dict__["argv"] = argv            # seed the cached_property
        t[pid] = p
    kids: dict[int, list[int]] = {}
    for p in t.values():
        kids.setdefault(p.ppid, []).append(p.pid)
    return Table(t, kids)


def _sess(name="s", clients=0, activity=NOW - 3600, created=NOW - 7200):
    return tmux.TmuxSession(name=name, windows=1, attached=clients > 0, created=int(created),
                            activity=int(activity), clients=clients)


# The shape 19 of 23 idle sessions had on 2026-11-02: the pane shell, Claude,
# and its MCP helpers (Playwright's browser below one of them).
IDLE = [
    (10, 1, "bash", ["/bin/bash", "-c", "claude --continue; exec /bin/bash -i"], 0.0),
    (11, 10, "claude", ["claude", "--continue"], 50.0),
    (12, 11, "npm exec @playw", ["npm", "exec", "@playwright/mcp@latest"], 1.0),
    (13, 12, "chrome", ["/home/u/.cache/ms-playwright/chrome", "--type=renderer"], 9.0),
    (14, 11, "python", ["/opt/opnsense-mcp/.venv/bin/python", "main.py"], 1.0),
]


@pytest.fixture(autouse=True)
def _reset():
    lifecycle._cpu_prev.clear()
    lifecycle._busy_at.clear()
    lifecycle._status.clear()
    yield


def reason(sess, table, title="✳ Fix the thing", mono=100.0):
    return lifecycle.busy_reason(sess, [10], table, title, NOW, mono)


def test_idle_session_with_only_helpers_is_idle():
    t = _table(*IDLE)
    assert reason(_sess(), t) is None
    assert reason(_sess(), t, mono=160.0) is None       # no CPU used between ticks


def test_attached_recent_output_and_working_are_busy():
    t = _table(*IDLE)
    assert reason(_sess(clients=1), t) == "open in a terminal"
    assert reason(_sess(activity=NOW - 30), t) == "recent output"
    assert reason(_sess(), t, title="⠐ Building") == "Claude is working"


def test_claude_status_file_says_working_or_background_task():
    t = _table(*IDLE)
    # The title shows ✳ even mid-turn; Claude's own status file is what counts.
    assert lifecycle.busy_reason(_sess(), [10], t, "✳ x", NOW, 100.0, "busy") == "Claude is working"
    assert lifecycle.busy_reason(_sess(), [10], t, "✳ x", NOW, 100.0, "shell") == "background task running"
    assert lifecycle.busy_reason(_sess(), [10], t, "✳ x", NOW, 100.0, "idle") is None


def test_a_claude_task_is_busy_even_at_zero_cpu():
    # The lyrion-anywhere shape: a background CI-wait loop sleeping.
    t = _table(*IDLE,
               (20, 11, "bash", ["/usr/bin/bash", "-c",
                                 "source /home/u/.claude/shell-snapshots/snapshot-bash-1.sh && eval 'ci-wait'"], 0.0),
               (21, 20, "sleep", ["sleep", "45"], 0.0))
    assert reason(_sess(), t) == "running a command"


def test_anything_else_in_the_pane_shell_is_busy():
    # Claude exited; the shell runs a dev server.
    t = _table((10, 1, "bash", ["/bin/bash", "-i"], 0.0),
               (30, 10, "node", ["node", "server.js"], 0.0))
    assert reason(_sess(), t) == "running node"


def test_cpu_use_is_busy():
    t1 = _table(*IDLE)
    assert reason(_sess(), t1, mono=100.0) is None
    hot = [p if p[0] != 13 else (13, 12, "chrome", ["chrome"], 9.0 + 6.0) for p in IDLE]
    assert reason(_sess(), _table(*hot), mono=160.0).startswith("using CPU")   # 10 % of a core


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv(session_meta.META_FILE_ENV, str(tmp_path / "meta.json"))
    monkeypatch.setenv("CCPIPE_STICKY_FILE", str(tmp_path / "sticky.json"))
    monkeypatch.setenv(app_config.CONFIG_FILE_ENV, str(tmp_path / "config.json"))
    app_config.reset_cache()
    session_meta._opened_written.clear()
    yield tmp_path
    app_config.reset_cache()


def test_boot_starts_only_the_most_recent_per_kind(state, monkeypatch):
    cfg = app_config.load()
    cfg.sessions.boot_sticky, cfg.sessions.boot_other = 2, 1
    app_config.save(cfg)
    for i, n in enumerate(["s1", "s2", "s3"]):
        sticky.set_sticky(n, f"/p/{n}")
        session_meta.note_created(n, when=100 + i, cwd=f"/p/{n}")
    for i, n in enumerate(["e1", "e2"]):
        session_meta.note_created(n, when=200 + i, cwd=f"/p/{n}")
    session_meta.note_opened("s1", when=9000)            # most recently used sticky
    started: list[str] = []

    async def fake_list():
        return []

    async def fake_resume(name, capped=True):
        started.append(name)

    monkeypatch.setattr(tmux, "list_sessions", fake_list)
    monkeypatch.setattr(lifecycle, "resume", fake_resume)
    asyncio.run(lifecycle.boot_restore())
    assert sorted(started) == ["e2", "s1", "s3"]


def test_boot_does_nothing_when_tmux_already_has_sessions(state, monkeypatch):
    sticky.set_sticky("s1", "/p/s1")

    async def fake_list():
        return [_sess("other")]

    monkeypatch.setattr(tmux, "list_sessions", fake_list)
    monkeypatch.setattr(lifecycle, "resume", lambda *a, **k: pytest.fail("resumed"))
    asyncio.run(lifecycle.boot_restore())


def _fake_tick_env(monkeypatch, sessions, table, screen=None, titles=None):
    async def fake_list():
        return sessions

    async def fake_panes():
        return {s.name: [10] for s in sessions}

    async def fake_active():
        return {s.name: ("/p/" + s.name, (titles or {}).get(s.name, "✳ x")) for s in sessions}

    async def fake_sid(name):
        return None, "/p/" + name

    async def fake_screen(name):
        return screen or []

    async def fake_states():
        return {}

    stopped: list[str] = []

    async def fake_stop(name, reason="stopped"):
        stopped.append(name)

    monkeypatch.setattr(tmux, "list_sessions", fake_list)
    monkeypatch.setattr(tmux, "all_panes", fake_panes)
    monkeypatch.setattr(tmux, "active_panes", fake_active)
    monkeypatch.setattr(tmux, "claude_sid_and_cwd", fake_sid)
    monkeypatch.setattr(tmux, "capture_screen", fake_screen)
    monkeypatch.setattr(tmux, "claude_states", fake_states)
    monkeypatch.setattr(proctree, "snapshot", lambda: table)
    monkeypatch.setattr(lifecycle, "stop", fake_stop)
    return stopped


def test_tick_stops_idle_ephemeral_but_not_busy_or_sticky(state, monkeypatch):
    monkeypatch.setattr(lifecycle, "_started", 0.0)
    now = time.time()
    old = now - 5 * 3600
    sticky.set_sticky("pinned", "/p/pinned")
    idle = _sess("idle", activity=old, created=old)
    pinned = _sess("pinned", activity=old, created=old)
    working = _sess("working", activity=old, created=old)
    stopped = _fake_tick_env(monkeypatch, [idle, pinned, working], _table(*IDLE),
                             titles={"working": "⠂ compiling"})
    asyncio.run(lifecycle.tick())
    # 5 h idle: past the 2 h ephemeral limit, inside the 24 h sticky one.
    assert stopped == ["idle"]
    st = lifecycle.status()
    assert st["working"]["busy"] == "Claude is working" and st["working"]["stops_at"] is None
    assert st["pinned"]["stops_at"] == int(old + 1440 * 60)


def test_tick_holds_off_on_a_question_on_screen(state, monkeypatch):
    monkeypatch.setattr(lifecycle, "_started", 0.0)
    old = time.time() - 5 * 3600
    stopped = _fake_tick_env(monkeypatch, [_sess("q", activity=old, created=old)], _table(*IDLE),
                             screen=["Bash command", "  rm -rf build", "Do you want to proceed?",
                                     "❯ 1. Yes", "  2. No"])
    asyncio.run(lifecycle.tick())
    assert stopped == []
    assert lifecycle.status()["q"]["busy"] == "waiting for an answer"


def test_never_stops_when_disabled(state, monkeypatch):
    monkeypatch.setattr(lifecycle, "_started", 0.0)
    cfg = app_config.load()
    cfg.sessions.idle_stop_other_min = 0
    app_config.save(cfg)
    old = time.time() - 50 * 3600
    stopped = _fake_tick_env(monkeypatch, [_sess("x", activity=old, created=old)], _table(*IDLE))
    asyncio.run(lifecycle.tick())
    assert stopped == [] and lifecycle.status()["x"]["stops_at"] is None


def test_resume_command_prefers_the_recorded_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cid = "0f0e0d0c-0b0a-4908-8706-050403020100"
    assert "claude --continue" in lifecycle.resume_command("/p/x", cid)   # no transcript
    d = tmp_path / ".claude" / "projects" / "-p-x"
    d.mkdir(parents=True)
    (d / f"{cid}.jsonl").write_text("{}\n")
    assert f"claude --resume {cid}" in lifecycle.resume_command("/p/x", cid)
    assert "claude --continue" in lifecycle.resume_command("/p/x", "not-a-uuid")


@pytest.fixture
def tmux_server(monkeypatch, state):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    d = tempfile.mkdtemp(prefix="ccl", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", d)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("CCPIPE_FS_ROOT", str(state))
    env = {**os.environ, "TMUX_TMPDIR": d}
    proj = state / "proj"
    proj.mkdir()
    subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 600"],
                   check=True, env=env)
    subprocess.run(["tmux", "new-session", "-d", "-s", "work", "-c", str(proj), "sleep 600"],
                   check=True, env=env)
    time.sleep(0.3)
    tmux.invalidate_list_sessions_cache()
    yield proj
    tmux.invalidate_list_sessions_cache()
    subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def test_stop_remember_resume_round_trip(tmux_server, monkeypatch):
    from ccpipe.routes import sessions

    # Resuming runs `claude …`; any command will do for the round trip.
    monkeypatch.setattr(lifecycle, "resume_command", lambda cwd, cid: "sleep 600")

    async def go():
        await sessions.list_sessions()                       # records the sessions
        session_meta.note_details("work", str(tmux_server), None)
        await sessions.stop_session("work")
        assert not await tmux.session_exists("work")
        stopped = {s.name: s for s in await sessions.remembered_sessions()}
        assert stopped["work"].cwd == str(tmux_server)
        assert stopped["work"].stop_reason == "stopped"
        info = await sessions.resume_session("work")
        assert info.name == "work" and info.cwd == str(tmux_server)
        assert "work" not in {s.name for s in await sessions.remembered_sessions()}
        await sessions.forget_session("work")
        assert not await tmux.session_exists("work")
        assert "work" not in session_meta.load()

    asyncio.run(go())


def test_session_config_round_trip(state):
    from ccpipe.routes import sessions

    async def go():
        assert (await sessions.session_config_get())["boot_sticky"] == 5
        out = await sessions.session_config_set(sessions.SessionsConfigBody(
            boot_other=3, idle_stop_sticky_min=5))
        assert out["boot_other"] == 3 and out["boot_sticky"] == 5
        assert out["idle_stop_sticky_min"] == 15            # clamped up to the floor
        assert (await sessions.session_config_get())["boot_other"] == 3

    asyncio.run(go())


def test_claude_states_reads_live_status_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    me = os.getpid()
    start = tmux._proc_start_time(me)
    import json as _json
    (d / f"{me}.json").write_text(_json.dumps(
        {"pid": me, "procStart": start, "status": "busy", "tmux": "work:@3.%7",
         "statusUpdatedAt": 1_793_000_000_000}))
    # A stale file from an earlier boot: same pid, different start time.
    (d / "stale.json").write_text(_json.dumps(
        {"pid": me, "procStart": "1", "status": "idle", "tmux": "old:@1.%1"}))
    got = tmux._read_claude_states({"%7": "work", "%1": "old"})
    assert got == {"work": ("busy", 1_793_000_000)}
