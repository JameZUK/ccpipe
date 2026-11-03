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
    lifecycle._details_at.clear()
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

    async def fresh_server():
        return int(time.time())                       # tmux started with ccpipe

    monkeypatch.setattr(tmux, "list_sessions", fake_list)
    monkeypatch.setattr(tmux, "server_started_at", fresh_server)
    monkeypatch.setattr(lifecycle, "resume", fake_resume)
    asyncio.run(lifecycle.boot_restore())
    assert sorted(started) == ["e2", "s1", "s3"]


def test_a_restart_with_every_session_stopped_is_not_a_boot(state, monkeypatch):
    # A deploy restarts ccpipe; tmux keeps running with only ccpipe's hidden
    # anchor session (the user had stopped everything). Starting the boot
    # picks here would undo those stops.
    sticky.set_sticky("s1", "/p/s1")
    session_meta.note_created("s1", when=100, cwd="/p/s1")
    session_meta.mark_stopped("s1", "stopped")

    async def fake_list():
        return []

    async def old_server():
        return int(lifecycle._started) - 3600

    monkeypatch.setattr(tmux, "list_sessions", fake_list)
    monkeypatch.setattr(tmux, "server_started_at", old_server)
    monkeypatch.setattr(lifecycle, "resume", lambda *a, **k: pytest.fail("resumed"))
    asyncio.run(lifecycle.boot_restore())


def test_boot_does_nothing_when_tmux_already_has_sessions(state, monkeypatch):
    sticky.set_sticky("s1", "/p/s1")

    async def fake_list():
        return [_sess("other")]

    monkeypatch.setattr(tmux, "list_sessions", fake_list)
    monkeypatch.setattr(lifecycle, "resume", lambda *a, **k: pytest.fail("resumed"))
    asyncio.run(lifecycle.boot_restore())


def _fake_tick_env(monkeypatch, sessions, table, screen=None, titles=None, complete=True):
    async def fake_list():
        return sessions, complete

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

    monkeypatch.setattr(tmux, "list_sessions_checked", fake_list)
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


def test_tick_never_stops_a_session_without_claude(state, monkeypatch):
    # A plain shell (say, started over ssh) sitting at its prompt: idle by
    # every rule, but not ccpipe's to stop — and it costs next to nothing.
    monkeypatch.setattr(lifecycle, "_started", 0.0)
    old = time.time() - 50 * 3600
    shell_only = _table((10, 1, "bash", ["/bin/bash", "-i"], 0.0))
    stopped = _fake_tick_env(monkeypatch, [_sess("ssh", activity=old, created=old)], shell_only)
    asyncio.run(lifecycle.tick())
    assert stopped == []
    st = lifecycle.status()["ssh"]
    assert st["busy"] is None and st["stops_at"] is None
    assert session_meta.load()["ssh"].get("shell") is True     # resumes as a shell


def test_tick_decides_nothing_when_tmux_does_not_answer(state, monkeypatch):
    monkeypatch.setattr(lifecycle, "_started", 0.0)
    lifecycle._status["kept"] = {"busy": "x", "idle_since": 1, "stops_at": None}
    stopped = _fake_tick_env(monkeypatch, [], _table(*IDLE), complete=False)
    asyncio.run(lifecycle.tick())
    assert stopped == [] and "kept" in lifecycle.status()


def test_resume_command_prefers_the_recorded_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cid = "0f0e0d0c-0b0a-4908-8706-050403020100"
    # (Claude starts with the status-line recorder's --settings first:
    # plan_usage.claude_command.)
    assert lifecycle.resume_command("/p/x", cid).startswith("claude ")
    assert " --continue;" in lifecycle.resume_command("/p/x", cid)   # no transcript
    d = tmp_path / ".claude" / "projects" / "-p-x"
    d.mkdir(parents=True)
    (d / f"{cid}.jsonl").write_text("{}\n")
    assert f" --resume {cid};" in lifecycle.resume_command("/p/x", cid)
    assert " --continue;" in lifecycle.resume_command("/p/x", "not-a-uuid")


def test_transcript_lookup_uses_claude_codes_directory_encoding(tmp_path, monkeypatch):
    # Claude Code turns every non-alphanumeric character into "-": the
    # transcripts of /p/my.site_x live in -p-my-site-x (on this machine:
    # ~/Projects/jamez.me.uk → -home-james-Projects-jamez-me-uk).
    monkeypatch.setenv("HOME", str(tmp_path))
    cid = "0f0e0d0c-0b0a-4908-8706-050403020100"
    d = tmp_path / ".claude" / "projects" / "-p-my-site-x-y"
    d.mkdir(parents=True)
    (d / f"{cid}.jsonl").write_text("{}\n")
    assert f" --resume {cid};" in lifecycle.resume_command("/p/my.site_x y", cid)


def test_a_session_that_never_ran_claude_resumes_as_a_shell(state, monkeypatch):
    monkeypatch.setenv("SHELL", "/opt/weird shell/zsh")
    session_meta.note_details("ssh", "/p/ssh", None, has_claude=False)
    r = lifecycle.resumable("ssh")
    assert r is not None and r.shell is True
    cmd = lifecycle.resume_command(r.cwd, r.claude, r.shell)
    assert cmd == "exec '/opt/weird shell/zsh' -i"         # quoted; interactive
    # Once a conversation has been seen it is a Claude session for good —
    # even if Claude has exited to the shell when it is next checked.
    cid = "0f0e0d0c-0b0a-4908-8706-050403020100"
    session_meta.note_details("ssh", "/p/ssh", cid, has_claude=True)
    session_meta.note_details("ssh", "/p/ssh", None, has_claude=False)
    r = lifecycle.resumable("ssh")
    assert r.shell is False and r.claude == cid
    cmd = lifecycle.resume_command(r.cwd, None)
    assert cmd.startswith("claude ") and cmd.endswith(" --continue; exec '/opt/weird shell/zsh' -i")


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
    monkeypatch.setattr(lifecycle, "resume_command", lambda *a: "sleep 600")

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


def test_a_failed_config_save_leaves_the_live_settings_alone(state, monkeypatch):
    from fastapi import HTTPException

    from ccpipe.routes import sessions

    def broken_save(cfg):
        raise OSError("read-only file system")

    monkeypatch.setattr(app_config, "save", broken_save)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(sessions.session_config_set(sessions.SessionsConfigBody(boot_sticky=0)))
    assert exc.value.status_code == 500
    assert app_config.load().sessions.boot_sticky == 5


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


# ─── reconnects and failed resumes (ws.py) ───────────────────────────────

class _FakeWs:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def accept(self):
        pass

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def _no_create(monkeypatch):
    async def missing(name):
        return False

    async def create(*a, **k):
        pytest.fail("created a session")

    monkeypatch.setattr(tmux, "session_exists", missing)
    monkeypatch.setattr(tmux, "create_session", create)


def test_a_reconnect_never_brings_back_a_stopped_session(state, monkeypatch):
    # The terminal was attached when the session was stopped from the
    # picker; its reconnect must not resume it (nor, once forgotten, start
    # a fresh Claude in $HOME under the same name).
    from ccpipe import ws
    _no_create(monkeypatch)
    session_meta.note_created("work", when=100, cwd="/p/work")
    session_meta.mark_stopped("work", "stopped")
    fake = _FakeWs()
    asyncio.run(ws._TerminalConnection(fake, "work", reattach=True).run())
    assert fake.sent == [{"type": "session_gone", "session": "work", "note": "work was stopped"}]
    session_meta.forget("work")
    fake = _FakeWs()
    asyncio.run(ws._TerminalConnection(fake, "work", reattach=True).run())
    assert fake.sent[0]["note"] == "work was removed"


def test_a_failed_resume_reports_instead_of_starting_claude_in_home(state, monkeypatch):
    from ccpipe import ws
    _no_create(monkeypatch)
    session_meta.note_created("gone", when=100, cwd="/no/such/dir")
    cid = "0f0e0d0c-0b0a-4908-8706-050403020100"
    session_meta.note_details("gone", None, cid)
    before = session_meta.load()["gone"]
    fake = _FakeWs()
    asyncio.run(ws._TerminalConnection(fake, "gone").run())
    assert fake.sent[0]["type"] == "session_gone"
    assert fake.sent[0]["note"].startswith("couldn't resume gone: its directory no longer exists")
    assert session_meta.load()["gone"] == before                 # record untouched


# ─── tmux listing completeness, PSS cache ────────────────────────────────

def test_an_unreachable_tmux_lists_as_incomplete(monkeypatch, tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    d = tempfile.mkdtemp(prefix="ccn", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", d)
    monkeypatch.delenv("TMUX", raising=False)
    try:
        assert tmux._sync_list_sessions() == ([], False)       # no server at all
        assert asyncio.run(tmux.server_started_at()) is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_session_list_skips_reconcile_when_tmux_does_not_answer(state, monkeypatch):
    from ccpipe.routes import sessions
    session_meta.note_created("work", when=100, cwd="/p/work")
    before = session_meta.load()

    async def unanswered():
        return [], False

    async def nothing():
        return {}

    monkeypatch.setattr(tmux, "list_sessions_checked", unanswered)
    monkeypatch.setattr(tmux, "active_panes", nothing)
    monkeypatch.setattr(tmux, "claude_states", nothing)
    assert asyncio.run(sessions.list_sessions()) == []
    assert asyncio.run(sessions.remembered_sessions()) == []
    assert session_meta.load() == before                         # not marked "gone"


def test_pss_is_reread_at_most_every_pss_s(monkeypatch):
    from ccpipe import sysstats
    reads: list[int] = []

    def fake_pss(pid):
        reads.append(pid)
        return 1000 * len(reads)

    monkeypatch.setattr(proctree, "pss_bytes", fake_pss)
    sysstats._pss.clear()
    p = Proc(pid=4242, ppid=1, comm="claude", start=5.0, cpu=0.0)
    assert sysstats._pss_of(p, 100.0) == 1000
    assert sysstats._pss_of(p, 100.0 + sysstats.PSS_S - 1) == 1000       # cached
    assert sysstats._pss_of(p, 100.0 + sysstats.PSS_S) == 2000           # refreshed
    reused = Proc(pid=4242, ppid=1, comm="bash", start=9.0, cpu=0.0)     # pid reused
    assert sysstats._pss_of(reused, 100.0 + sysstats.PSS_S) == 3000
    assert reads == [4242, 4242, 4242]
