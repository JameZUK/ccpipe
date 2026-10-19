"""Screen layouts API + the tmux toolbar actions (against a throwaway tmux)."""
from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient

H = {"X-Requested-By": "ccpipe"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    import ccpipe.auth as auth
    import ccpipe.main as m
    import ccpipe.routes.auth as routes_auth
    auth.reset_cached_credential()
    routes_auth.reset_throttle_state()
    monkeypatch.setattr(auth, "_revoked", None)
    importlib.reload(m)
    c = TestClient(m.app)
    assert c.post("/api/auth/login", headers=H,
                  json={"username": "alice", "password": "letmein"}).status_code == 200
    return c, tmp_path


def _leaf(s, m="watch"):
    return {"t": "leaf", "s": s, "m": m}


def test_layout_roundtrip_and_rename(client):
    c, tmp = client
    assert c.get("/api/layouts/main").json() == {"layout": None}
    tree = {"t": "split", "d": "h", "sz": [3, 1],
            "c": [_leaf("main", "live"),
                  {"t": "split", "d": "v", "c": [_leaf("a"), _leaf("b", "live")]}]}
    got = c.put("/api/layouts/main", headers=H, json={"layout": tree}).json()["layout"]
    assert got["sz"] == [0.75, 0.25]                       # normalised
    assert got["c"][1]["sz"] == [0.5, 0.5]                 # missing → equal
    assert c.get("/api/layouts/main").json()["layout"] == got
    assert os.stat(tmp / "layouts.json").st_mode & 0o777 == 0o600

    from ccpipe.routes import layouts
    layouts.rename("a", "a2")
    layouts.rename("main", "main2")
    assert c.get("/api/layouts/main").json() == {"layout": None}
    moved = c.get("/api/layouts/main2").json()["layout"]
    assert moved["c"][1]["c"][0]["s"] == "a2"

    # A lone tile is no layout: stored as cleared.
    assert c.put("/api/layouts/main2", headers=H,
                 json={"layout": _leaf("main2")}).json() == {"layout": None}
    assert c.get("/api/layouts/main2").json() == {"layout": None}


@pytest.mark.parametrize("tree", [
    {"t": "leaf", "s": "bad name"},
    {"t": "leaf", "s": "-x"},
    {"t": "leaf", "s": "a", "m": "rw"},
    {"t": "split", "d": "x", "c": [_leaf("a"), _leaf("b")]},
    {"t": "split", "d": "h", "c": [_leaf("a")]},
    {"t": "split", "d": "h", "c": [_leaf(f"s{i}") for i in range(5)]},
    {"t": "split", "d": "h", "c": [{"t": "split", "d": "v", "c": [_leaf(f"s{i}{j}") for j in range(4)]}
                                    for i in range(3)]},          # 12 tiles
    {"t": "nope"},
])
def test_layout_validation(client, tree):
    c, _ = client
    assert c.put("/api/layouts/main", headers=H, json={"layout": tree}).status_code == 400


def test_layout_needs_csrf_header(client):
    c, _ = client
    assert c.put("/api/layouts/main", json={"layout": None}).status_code in (401, 403)


# ── tmux actions, against a real but private tmux server ──────────────────

@pytest.fixture
def tmux_server(monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    d = tempfile.mkdtemp(prefix="cct", dir="/tmp")      # short: socket path limit
    monkeypatch.setenv("TMUX_TMPDIR", d)
    monkeypatch.delenv("TMUX", raising=False)
    env = {**os.environ, "TMUX_TMPDIR": d}
    env.pop("TMUX", None)
    subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "t1",
                    "-x", "100", "-y", "30", "sleep 600"], check=True, env=env)
    yield env
    subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def test_tmux_actions(tmux_server):
    from ccpipe import tmux_ui

    async def go():
        perform, windows = tmux_ui.perform, tmux_ui.list_windows
        assert await tmux_ui.window_size("t1") is not None
        # Can't close the only pane or the only window.
        for action in ("pane-close", "window-close"):
            with pytest.raises(tmux_ui.ActionError) as e:
                await perform("t1", action)
            assert e.value.status == 409
        await perform("t1", "split-h")
        await perform("t1", "split-v")
        assert await tmux_ui.window_panes("t1") == 3
        await perform("t1", "pane-next")
        await perform("t1", "pane-zoom")
        assert (await windows("t1"))[0]["zoomed"] is True
        await perform("t1", "pane-zoom")
        await perform("t1", "pane-close")
        assert await tmux_ui.window_panes("t1") == 2
        await perform("t1", "window-new")
        ws = await windows("t1")
        assert len(ws) == 2 and ws[1]["active"]
        await perform("t1", "window-rename", "logs; kill-server")   # just a name
        assert (await windows("t1"))[1]["name"] == "logs; kill-server"
        await perform("t1", "window-select", 0)
        assert (await windows("t1"))[0]["active"]
        await perform("t1", "window-next")
        await perform("t1", "window-prev")
        await perform("t1", "page-up")
        await perform("t1", "page-down")
        await perform("t1", "copy-exit")
        await perform("t1", "copy-exit")                 # not in a mode: fine
        await perform("t1", "window-close")
        assert len(await windows("t1")) == 1
        for bad, arg in (("rm-rf", None), ("window-select", "0"),
                         ("window-select", True), ("window-rename", "a\nb"),
                         ("window-rename", "")):
            with pytest.raises(tmux_ui.ActionError) as e:
                await perform("t1", bad, arg)
            assert e.value.status == 400
        # Exact-name targeting: a prefix doesn't reach "t1".
        with pytest.raises(tmux_ui.ActionError):
            await perform("t", "split-h")
    asyncio.run(go())


def test_watch_attach_argv():
    from ccpipe import tmux
    assert tmux.attach_argv("a")[-3:] == ["-t", "=a", "--"]
    argv = tmux.attach_argv("a", watch=True)
    assert argv[argv.index("-f") + 1] == "read-only,ignore-size"
