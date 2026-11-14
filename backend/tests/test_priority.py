"""Background sessions (priority.py): what a scope is given, and that only
a pane's own scope is ever touched."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ccpipe import priority, session_meta, statefile

GB = 1 << 30


# ── the memory limit ────────────────────────────────────────────────────

def test_spare_memory_is_shared_out_on_top_of_current_use():
    # 32 GB machine, 20 GB available: reserve 4.8 GB, so ~15.2 GB spare.
    out = priority.memory_limits({"a": 2 * GB, "b": 6 * GB}, 20 * GB, 32 * GB)
    spare = 20 * GB - int(32 * GB * priority.RESERVE_FRACTION)
    assert out == {"a": 2 * GB + spare // 2, "b": 6 * GB + spare // 2}


def test_at_the_reserve_a_background_session_cannot_grow():
    reserve = int(32 * GB * priority.RESERVE_FRACTION)
    assert priority.memory_limits({"a": 3 * GB}, reserve, 32 * GB) == {"a": 3 * GB}


def test_below_the_reserve_memory_is_taken_back_in_proportion():
    reserve = int(32 * GB * priority.RESERVE_FRACTION)
    out = priority.memory_limits({"a": 2 * GB, "b": 6 * GB}, reserve - GB, 32 * GB)
    assert out["a"] == 2 * GB - GB // 4 and out["b"] == 6 * GB - 3 * GB // 4


def test_takes_back_at_most_a_step_and_never_below_the_floor():
    out = priority.memory_limits({"a": 8 * GB, "tiny": 100 << 20}, 0, 32 * GB)
    assert out["a"] == 8 * GB - int(8 * GB * priority.STEP)
    assert out["tiny"] == priority.LIMIT_FLOOR


def test_small_machine_keeps_at_least_the_minimum_reserve():
    # 4 GB: 15 % would be 0.6 GB; the reserve is 1 GB.
    assert priority.memory_limits({"a": GB}, GB, 4 * GB) == {"a": GB}


# ── which cgroups may be touched ────────────────────────────────────────

@pytest.fixture
def cgroups(tmp_path, monkeypatch):
    """A fake /sys/fs/cgroup and a fake pid → cgroup table."""
    root = tmp_path / "cg"
    table: dict[int, str] = {}
    monkeypatch.setattr(priority, "CGROUP_ROOT", root)
    monkeypatch.setattr(priority, "_cgroup_path", lambda pid: table.get(pid))
    monkeypatch.setattr(priority, "_ours", set())
    base = "/user.slice/user-1000.slice/user@1000.service/app.slice"

    def add(pid: int, unit: str, weight: int | None = None) -> Path:
        table[pid] = f"{base}/{unit}"
        d = root / base.lstrip("/") / unit
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory.current").write_text("1000000\n")
        (d / "memory.high").write_text("max\n")
        if weight is not None:
            (d / "cpu.weight").write_text(f"{weight}\n")
        return d

    return add, table


def test_only_a_panes_own_scope_counts(cgroups):
    add, table = cgroups
    pane = add(10, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000001.scope")
    add(11, "ccpipe.service")
    add(12, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000002.scope")   # the tmux server's
    table[13] = "/user.slice/../../etc/tmux-spawn-11111111-aaaa-bbbb-cccc-000000000003.scope"
    never = {table[12]}
    assert priority._scope_dir(10, never) == pane
    assert priority._scope_dir(11, never) is None      # not a pane scope
    assert priority._scope_dir(12, never) is None      # a new pane's first moments
    assert priority._scope_dir(13, never) is None
    assert priority._scope_dir(99, never) is None      # gone


def test_survey_finds_background_scopes_and_leftovers(cgroups):
    add, _ = cgroups
    bg1 = add(10, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000001.scope")
    bg2 = add(11, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000002.scope")
    add(20, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000003.scope", weight=100)
    left = add(30, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000004.scope",
               weight=priority.BG_WEIGHT)             # was background before a restart
    background, restore, seen = priority._survey(
        {"work": [10, 11], "normal": [20], "was": [30]}, {"work"}, set())
    assert background == {"work": sorted([bg1, bg2])}
    assert restore == [left] and seen


def test_memory_limit_is_written_and_reported_as_held(cgroups, monkeypatch):
    add, _ = cgroups
    scope = add(10, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000001.scope")
    (scope / "memory.current").write_text(str(4 * GB))
    monkeypatch.setattr(priority, "_meminfo", lambda: {"MemTotal": 32 * GB, "MemAvailable": 20 * GB})
    assert priority._apply_memory({"work": [scope]}) == set()
    assert int((scope / "memory.high").read_text()) > 4 * GB
    monkeypatch.setattr(priority, "_meminfo", lambda: {"MemTotal": 32 * GB, "MemAvailable": 2 * GB})
    assert priority._apply_memory({"work": [scope]}) == {"work"}
    assert int((scope / "memory.high").read_text()) < 4 * GB


def test_restoring_lifts_the_memory_limit(cgroups, monkeypatch):
    add, _ = cgroups
    scope = add(10, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000001.scope")
    (scope / "memory.high").write_text("123456789")
    (scope / "cgroup.procs").write_text("")
    monkeypatch.setattr(priority, "_meminfo", lambda: {})
    priority._apply_files({}, [scope])
    assert (scope / "memory.high").read_text() == "max"


def test_a_background_claude_is_moved_into_its_sessions_scope(cgroups, monkeypatch):
    add, table = cgroups
    a = add(10, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000001.scope")   # session a's pane
    b = add(20, "tmux-spawn-11111111-aaaa-bbbb-cccc-000000000002.scope")   # session b's pane
    # b's background Claude (30, child 31) runs under the daemon in a's scope.
    table[30] = table[31] = table[10]
    from ccpipe import proctree
    P = proctree.Proc
    snap = proctree.Table({30: P(30, 1, "claude", 0, 0), 31: P(31, 30, "bash", 0, 0)}, {30: [31]})
    monkeypatch.setattr(proctree, "snapshot", lambda: snap)
    monkeypatch.setattr(priority, "_recheck", set())
    panes = {"a": [10], "b": [20]}
    # Moved whatever the levels are: its memory is then counted in b's scope.
    assert priority._adopt({30: "b"}, panes, set()) == 2
    assert (b / "cgroup.procs").read_text() == "31"            # (a real cgroup file takes one pid a write)
    # Already in its own scope: nothing to do (after the one look back).
    table[30] = table[31] = table[20]
    assert priority._adopt({30: "b"}, panes, set()) == 0
    assert priority._recheck == set()
    # b's pane is still in the tmux server's scope (its first moments): wait.
    table[30] = table[31] = table[10]
    assert priority._adopt({30: "b"}, panes, {table[20]}) == 0
    assert a.exists()


# ── the level is kept with the session ──────────────────────────────────

def test_level_is_remembered_and_follows_a_rename(tmp_path, monkeypatch):
    monkeypatch.setenv(session_meta.META_FILE_ENV, str(tmp_path / "meta.json"))
    statefile.reset()
    session_meta.note_created("work", 1_700_000_000, "/tmp")
    assert priority.level_of(session_meta.load()["work"]) == "normal"
    asyncio.run(priority.set_level("work", "background"))
    assert priority.level_of(session_meta.load()["work"]) == "background"
    session_meta.mark_stopped("work", "idle")
    session_meta.mark_running("work")                  # a stop and a resume keep it
    session_meta.rename("work", "job")
    assert priority.level_of(session_meta.load()["job"]) == "background"
    asyncio.run(priority.set_level("job", "normal"))
    assert "priority" not in session_meta.load()["job"]
    with pytest.raises(ValueError):
        asyncio.run(priority.set_level("job", "realtime"))
