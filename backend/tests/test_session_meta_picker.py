"""Session picker data: created / last-opened records, pane titles and
directories in /api/sessions, screen previews, and /version.json."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time

import pytest

from ccpipe import session_meta, tmux


@pytest.fixture
def meta_file(tmp_path, monkeypatch):
    p = tmp_path / "meta.json"
    monkeypatch.setenv(session_meta.META_FILE_ENV, str(p))
    session_meta._opened_written.clear()
    return p


def test_created_survives_recreation_and_keeps_the_older_time(meta_file):
    session_meta.note_created("a", when=1000)
    # After a reboot tmux recreates the sticky session: its own created time
    # is the boot time, later than ours — ours wins.
    merged = session_meta.reconcile({"a": 5000}, keep={"a"})
    assert merged["a"]["created"] == 1000
    # A session ccpipe never saw created gets tmux's time, persisted.
    merged = session_meta.reconcile({"a": 5000, "b": 4000}, keep=set())
    assert merged["b"]["created"] == 4000
    assert session_meta.load()["b"]["created"] == 4000


def test_dead_non_sticky_names_are_dropped(meta_file):
    session_meta.note_created("gone", when=1)
    session_meta.note_created("pinned", when=2)
    merged = session_meta.reconcile({}, keep={"pinned"})
    assert "gone" not in merged and "pinned" in merged
    # …so a new session reusing the name starts afresh.
    session_meta.reconcile({"gone": 9000}, keep=set())
    assert session_meta.load()["gone"]["created"] == 9000


def test_opened_is_throttled_but_recorded(meta_file):
    session_meta.note_opened("s", when=1000)
    session_meta.note_opened("s", when=1030)            # within a minute: skipped
    assert session_meta.load()["s"]["opened"] == 1000
    session_meta.note_opened("s", when=1000 + session_meta.OPENED_WRITE_INTERVAL_S + 1)
    assert session_meta.load()["s"]["opened"] == 1061


def test_rename_and_forget(meta_file):
    session_meta.note_created("old", when=10)
    session_meta.note_opened("old", when=20)
    session_meta.rename("old", "new")
    assert session_meta.load() == {"new": {"created": 10, "opened": 20}}
    session_meta.forget("new")
    assert session_meta.load() == {}


def test_malformed_file_is_ignored(meta_file):
    meta_file.write_text("{not json")
    assert session_meta.load() == {}
    meta_file.write_text(json.dumps({"x": {"created": "soon", "opened": -1}, "y": 3}))
    assert session_meta.load() == {}


@pytest.fixture
def tmux_server(monkeypatch, meta_file, tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    d = tempfile.mkdtemp(prefix="ccm", dir="/tmp")      # short: socket path limit
    monkeypatch.setenv("TMUX_TMPDIR", d)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("CCPIPE_STICKY_FILE", str(tmp_path / "sticky.json"))
    env = {**os.environ, "TMUX_TMPDIR": d}
    script = ("printf '\\033]2;\\342\\234\\263 Fix the picker\\033\\\\'; seq 1 30; "
              "printf -- '────────────────\\n❯ \\n────────────────\\n  footer\\n'; sleep 600")
    subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "proj",
                    "-x", "80", "-y", "40", "-c", str(tmp_path), script], check=True, env=env)
    subprocess.run(["tmux", "new-session", "-d", "-s", "other", "-x", "80", "-y", "20",
                    "sleep 600"], check=True, env=env)
    time.sleep(0.4)
    tmux.invalidate_list_sessions_cache()
    yield tmp_path
    tmux.invalidate_list_sessions_cache()
    subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def test_session_list_carries_title_cwd_and_dates(tmux_server):
    from ccpipe.routes import sessions

    async def go():
        infos = {i.name: i for i in await sessions.list_sessions()}
        assert set(infos) == {"proj", "other"}
        p = infos["proj"]
        assert p.title == "✳ Fix the picker"
        assert p.cwd == str(tmux_server)
        assert p.created > 0 and p.clients == 0
        # Backfilled into the record on first listing.
        assert session_meta.load()["proj"]["created"] == p.created
        # "Last opened" comes from the record once a terminal opened it.
        session_meta.note_opened("proj", when=1234567890)
        tmux.invalidate_list_sessions_cache()
        infos = {i.name: i for i in await sessions.list_sessions()}
        assert infos["proj"].opened == 1234567890

    asyncio.run(go())


def test_previews_return_the_bottom_of_the_screen(tmux_server):
    from ccpipe.routes import sessions

    async def go():
        res = await sessions.session_previews(
            names="proj,other,nope,__ccpipe_ctrl,bad name,-x", lines=6)
        prev = res["previews"]
        assert set(prev) == {"proj", "other"}       # unknown / invalid / hidden left out
        assert prev["proj"][-1] == "  footer"       # trailing blank rows trimmed
        assert len(prev["proj"]) == 6
        assert "30" in prev["proj"]
        # Line count is clamped.
        res = await sessions.session_previews(names="proj", lines=10_000)
        assert len(res["previews"]["proj"]) <= sessions._PREVIEW_MAX_LINES

    asyncio.run(go())


def test_version_json_is_served_uncached(tmp_path, monkeypatch):
    from ccpipe.routes import static

    (tmp_path / "version.json").write_text('{"build": "abc · 2026-11-02 12:00Z"}\n')
    monkeypatch.setenv("CCPIPE_FRONTEND_DIST", str(tmp_path))
    resp = asyncio.run(static.version_json())
    assert resp.headers["cache-control"] == "no-store"
    assert str(resp.path).endswith("version.json")
