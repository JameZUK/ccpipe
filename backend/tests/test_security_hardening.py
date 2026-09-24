"""Regression tests for the 2026-09 security-review fixes."""
from __future__ import annotations

import os

import pytest

from ccpipe import auth, tmux, tmux_setup


# ── #9: ccpipe's environment must not leak into tmux panes ─────────────

def test_tmux_env_drops_ccpipe_vars(monkeypatch):
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "hunter2")
    monkeypatch.setenv("CCPIPE_TRUSTED_HOSTS", "example.org")
    monkeypatch.setenv("HOME_LIKE_VAR", "kept")
    env = tmux.tmux_env()
    assert not any(k.startswith("CCPIPE_") for k in env)
    assert env["HOME_LIKE_VAR"] == "kept"


@pytest.mark.asyncio
async def test_apply_server_defaults_pins_clipboard_and_unsets_ccpipe_env(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str, capture: bool = True) -> tuple[int, str]:
        calls.append(args)
        if args[:2] == ("show-environment", "-g"):
            return 0, "PATH=/usr/bin\nCCPIPE_TTS=kokoro\n-CCPIPE_OLD\nLANG=C"
        return 0, ""

    monkeypatch.setattr(tmux_setup, "_run_tmux", fake_run_tmux)
    await tmux_setup.apply_server_defaults()

    assert ("set-option", "-g", "set-clipboard", "external") in calls
    assert ("set-window-option", "-g", "allow-passthrough", "off") in calls
    unset = {c[3] for c in calls if c[:3] == ("set-environment", "-g", "-u")}
    assert unset == {"CCPIPE_TTS", "CCPIPE_OLD"}


def test_drop_bootstrap_password_env_only_once_persisted(monkeypatch, tmp_path):
    creds = tmp_path / "credentials"
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "hunter2")
    # Not yet hashed to disk → the env value is still the live credential.
    auth.drop_bootstrap_password_env()
    assert "CCPIPE_AUTH_PASSWORD" in os.environ
    creds.write_text("{}")
    auth.drop_bootstrap_password_env()
    assert "CCPIPE_AUTH_PASSWORD" not in os.environ


# ── #5: session names are an allowlist; tmux targets match exactly ──────

def test_safe_name_rejects_tmux_target_syntax_and_non_allowlisted():
    for bad in ["%3", "@1", "=foo", "a!b", "tilde~", "café", "nul\x00x",
                "ctl\x07", "plus+x", "comma,x", "-lead"]:
        with pytest.raises(ValueError):
            tmux.safe_name(bad)
    for good in ["868-DMR-AESFH", "lyrion-audiomuseai", "__ccpipe_ctrl", "a_b-C9"]:
        assert tmux.safe_name(good) == good


def test_tmux_targets_are_exact_match():
    # Session targets take "=name"; pane targets need "=name:" (plain
    # "=name" matches nothing for capture-pane / display-message).
    assert tmux.session_target("foo") == "=foo"
    assert tmux.pane_target("foo") == "=foo:"
    assert tmux.attach_argv("foo")[-3:] == ["-t", "=foo", "--"]


# ── #4: sliding session lifetime + server-side revocation ──────────────

@pytest.fixture
def app_env(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    import ccpipe.main as m
    import ccpipe.routes.auth as routes_auth
    auth.reset_cached_credential()
    routes_auth.reset_throttle_state()
    auth._totp_burned.clear()
    monkeypatch.setattr(auth, "_revoked", None)
    importlib.reload(m)
    return m


def _login(app):
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post("/api/auth/login", headers={"X-Requested-By": "ccpipe"},
               json={"username": "alice", "password": "letmein"})
    assert r.status_code == 200
    return c


def _authed(client) -> bool:
    return client.get("/api/auth/status").json()["authenticated"]


def test_session_cookie_is_renewed_while_in_use(app_env, monkeypatch):
    c = _login(app_env.app)
    # Just logged in: nothing to renew yet.
    assert "set-cookie" not in c.get("/api/auth/status").headers
    # Once the renew interval has passed, an authed request re-issues the
    # cookie (fresh signature timestamp → the max_age window slides).
    monkeypatch.setattr(auth, "SESSION_RENEW_S", 0)
    r = c.get("/api/auth/status")
    assert r.json()["authenticated"] is True
    assert "set-cookie" in r.headers


def test_logout_revokes_copies_of_the_cookie(app_env):
    from fastapi.testclient import TestClient
    c = _login(app_env.app)
    stolen = TestClient(app_env.app, cookies=dict(c.cookies))
    assert _authed(stolen)
    c.post("/api/auth/logout", headers={"X-Requested-By": "ccpipe"})
    assert not _authed(c)
    assert not _authed(stolen), "a copied cookie must die with the logout"
    # Revocation survives a restart (it's persisted beside the credentials).
    auth._revoked = None
    assert not _authed(stolen)


def test_logout_all_invalidates_every_device(app_env):
    phone = _login(app_env.app)
    laptop = _login(app_env.app)
    r = laptop.post("/api/auth/logout-all", headers={"X-Requested-By": "ccpipe"})
    assert r.status_code == 200
    assert not _authed(phone) and not _authed(laptop)
    # The password still works — nothing but the sessions changed.
    assert _authed(_login(app_env.app))


def test_logout_all_requires_auth_and_csrf(app_env):
    from fastapi.testclient import TestClient
    anon = TestClient(app_env.app)
    assert anon.post("/api/auth/logout-all", headers={"X-Requested-By": "ccpipe"}).status_code == 401
    c = _login(app_env.app)
    assert c.post("/api/auth/logout-all").status_code == 403
    assert _authed(c)


# ── #8: a slow consumer gets backpressure, not silently dropped bytes ──

@pytest.mark.asyncio
async def test_pty_slow_consumer_loses_no_bytes():
    import asyncio
    from ccpipe.pty_relay import PtyProcess
    n = 8_000_000
    p = PtyProcess(["sh", "-c", f"head -c {n} /dev/zero | tr '\\0' a; printf END"],
                   cols=200, rows=50)
    await p.start()
    try:
        # Let the producer run far ahead so the 64-slot queue saturates
        # (previously: chunks dropped from here on), then drain slowly.
        await asyncio.sleep(1.0)
        total, tail = 0, b""
        while True:
            chunk = await asyncio.wait_for(p.read(), timeout=20)
            if not chunk:
                break
            total += chunk.count(b"a")
            tail = (tail + chunk)[-16:]
            await asyncio.sleep(0.002)
    finally:
        await p.terminate()
    assert p.bytes_dropped() == 0
    assert total == n, f"got {total} of {n} bytes"
    assert tail.endswith(b"END")


# ── #7: saves keep the file's mode, write through symlinks, unique temps ─

H = {"X-Requested-By": "ccpipe"}


@pytest.fixture
def fs_client(app_env, tmp_path, monkeypatch):
    root = tmp_path / "jail"
    root.mkdir()
    monkeypatch.setenv("CCPIPE_FS_ROOT", str(root))
    return _login(app_env.app), root


def _mode(p) -> int:
    return os.stat(p).st_mode & 0o777


def test_write_and_upload_keep_existing_mode(fs_client):
    c, root = fs_client
    env_file, script = root / ".env", root / "run.sh"
    env_file.write_text("A=1\n"); os.chmod(env_file, 0o600)
    script.write_text("#!/bin/sh\n"); os.chmod(script, 0o755)
    assert c.post("/api/fs/write", headers=H, json={"path": str(env_file), "content": "A=2\n"}).status_code == 200
    assert c.post(f"/api/fs/upload?path={script}", headers=H, content=b"#!/bin/sh\necho hi\n").status_code == 200
    assert env_file.read_text() == "A=2\n" and _mode(env_file) == 0o600
    assert script.read_text().endswith("echo hi\n") and _mode(script) == 0o755
    assert not [p for p in root.iterdir() if p.name.endswith(".tmp")], "temp file left behind"


def test_write_through_symlink_keeps_the_link(fs_client):
    c, root = fs_client
    (root / "dotfiles").mkdir()
    real = root / "dotfiles" / "bashrc"
    real.write_text("old\n"); os.chmod(real, 0o600)
    link = root / ".bashrc"
    link.symlink_to(real)
    r = c.post("/api/fs/write", headers=H, json={"path": str(link), "content": "new\n"})
    assert r.status_code == 200
    assert link.is_symlink() and os.readlink(link) == str(real)
    assert real.read_text() == "new\n" and _mode(real) == 0o600


def test_symlink_out_of_jail_or_into_denied_path_is_refused(fs_client, tmp_path):
    c, root = fs_client
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    (root / "escape").symlink_to(outside)
    (root / ".claude").mkdir()
    denied = root / ".claude" / "settings.json"
    denied.write_text("{}")
    (root / "sneaky").symlink_to(denied)
    for link in ("escape", "sneaky"):
        r = c.post("/api/fs/write", headers=H, json={"path": str(root / link), "content": "pwned"})
        assert r.status_code == 403, (link, r.status_code)
    assert outside.read_text() == "keep\n" and denied.read_text() == "{}"


def test_new_file_gets_default_mode(fs_client):
    c, root = fs_client
    new = root / "notes.md"
    assert c.post("/api/fs/write", headers=H, json={"path": str(new), "content": "x"}).status_code == 200
    umask = os.umask(0); os.umask(umask)
    assert _mode(new) == 0o644 & ~umask


def test_atomic_write_text_keeps_mode_and_symlink(tmp_path):
    from ccpipe.safe_write import atomic_write_text
    real = tmp_path / "repo" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}"); os.chmod(real, 0o600)
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    atomic_write_text(link, '{"a": 1}\n')
    assert link.is_symlink() and real.read_text() == '{"a": 1}\n' and _mode(real) == 0o600
    assert not list(real.parent.glob("*.tmp"))


@pytest.mark.asyncio
async def test_pty_large_paste_arrives_intact(tmp_path):
    import asyncio, hashlib
    from ccpipe.pty_relay import PtyProcess
    out = tmp_path / "out.bin"
    n = 2_000_000
    payload = bytes((i * 7) % 251 + 1 for i in range(n)).replace(b"\x03", b"A").replace(b"\x04", b"B")
    p = PtyProcess(["sh", "-c", f"stty raw -echo; head -c {n} > {out}; printf DONE"],
                   cols=80, rows=24)
    await p.start()
    try:
        await asyncio.sleep(0.3)          # let stty take effect before writing
        p.write(payload)
        seen = b""
        while b"DONE" not in seen:
            seen += await asyncio.wait_for(p.read(), timeout=30)
    finally:
        await p.terminate()
    assert hashlib.sha256(out.read_bytes()).digest() == hashlib.sha256(payload).digest()


@pytest.mark.asyncio
async def test_history_capture_keeps_newest_tail_and_whole_lines(monkeypatch):
    from unittest.mock import patch
    from ccpipe import ws
    lines = [f"line{i:06d} \x1b[31mred\x1b[0m".encode() for i in range(200_000)]
    blob = b"\n".join(lines) + b"\n"

    class _Stream:
        def __init__(self, d): self.d = d
        async def read(self, n=-1):
            c, self.d = self.d[:n], self.d[n:]
            return c

    class _Proc:
        returncode = 0
        stdout = _Stream(blob)
        async def wait(self): return 0
        def kill(self): pass

    ws._clear_history_cache()
    monkeypatch.setattr(ws, "_HISTORY_MAX_BYTES", 100_000)
    monkeypatch.setattr(ws, "_HISTORY_READ_SLACK", 10_000)
    with patch("ccpipe.ws.asyncio.create_subprocess_exec", return_value=_Proc()):
        out = await ws._capture_session_history("work", viewport_rows=30)
    assert len(out) <= 100_000
    assert out.endswith(lines[-1])                     # newest line kept
    assert out.startswith(b"line")                     # no partial first line
    assert all(l.startswith(b"line") for l in out.split(b"\r\n"))


# ── efficiency: cached claude pid, shared session list per event ──────

@pytest.mark.asyncio
async def test_claude_pid_is_cached_and_revalidated(monkeypatch):
    calls = []

    async def fake_claude_pid(name):
        calls.append(name)
        return os.getpid()                       # a live pid with a real start time

    monkeypatch.setattr(tmux, "claude_pid", fake_claude_pid)
    tmux._CLAUDE_PID_CACHE.clear()
    for _ in range(5):
        assert await tmux._cached_claude_pid("work") == os.getpid()
    assert calls == ["work"], "hits must not re-resolve"
    # A different start time (pid reused) forces a fresh resolve.
    pid, start, at = tmux._CLAUDE_PID_CACHE["work"]
    tmux._CLAUDE_PID_CACHE["work"] = (pid, "0", at)
    await tmux._cached_claude_pid("work")
    assert calls == ["work", "work"]
    # kill/rename/create drop the entry.
    tmux._CLAUDE_PID_CACHE["gone"] = (pid, start, at)
    monkeypatch.setattr(tmux, "_sync_kill_session", lambda n: True)
    await tmux.kill_session("gone")
    assert "gone" not in tmux._CLAUDE_PID_CACHE


@pytest.mark.asyncio
async def test_sessions_changed_invalidates_list_cache(monkeypatch):
    from ccpipe import tmux_control
    seen = []
    monkeypatch.setattr(tmux, "invalidate_list_sessions_cache", lambda: seen.append(1))
    client = tmux_control.TmuxControlClient()

    async def cb(event):
        pass

    sub = client.subscribe(cb)
    try:
        await client._dispatch(tmux_control.TmuxEvent(name="sessions-changed", args=[], raw="%sessions-changed"))
        await client._dispatch(tmux_control.TmuxEvent(name="window-add", args=[], raw="%window-add"))
    finally:
        sub.cancel()
    assert seen == [1]


# ── #8: malformed frames are dropped, not fatal; warnings rate-limited ─

def test_malformed_frames_are_ignored(caplog):
    from ccpipe import ws

    class _Pty:
        def __init__(self): self.writes, self.sizes = [], []
        def write(self, b): self.writes.append(b)
        def resize(self, c, r): self.sizes.append((c, r))

    p = _Pty()
    ws._warn_state.clear()
    for frame in ["5", "[]", '"str"', "null", '{"type":"resize","cols":1e999,"rows":40}',
                  '{"type":"resize","cols":"x"}', "not json", "not json either"]:
        ws._handle_client_text(frame, p)          # must not raise
    assert p.writes == [] and p.sizes == []
    ws._handle_client_text('{"type":"input","data":"ok"}', p)
    assert p.writes == [b"ok"]
    # Two non-JSON frames back to back → only one warning logged.
    assert sum("non-JSON" in r.message for r in caplog.records) == 1


# ── #9: concurrent credential writes don't lose each other ────────────

def test_concurrent_credential_writes_are_serialised(app_env):
    import threading
    auth.get_credential()                                  # seed the file
    start = auth.get_credential().version
    barrier = threading.Barrier(8)

    def bump():
        barrier.wait()
        assert auth.bump_credential_version()[0]

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    auth.reset_cached_credential()
    assert auth.get_credential().version == start + 8, "a concurrent write was lost"


# ── #7: session cap ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_cap_counts_user_sessions_only(monkeypatch):
    from ccpipe.tmux_control import CONTROL_SESSION_NAME

    def fake(names):
        async def _ls():
            return [tmux.TmuxSession(name=n, windows=1, attached=False, created=0) for n in names]
        return _ls

    monkeypatch.setattr(tmux, "MAX_SESSIONS", 2)
    monkeypatch.setattr(tmux, "list_sessions", fake([CONTROL_SESSION_NAME, "a"]))
    assert not await tmux.at_session_cap()          # control session doesn't count
    monkeypatch.setattr(tmux, "list_sessions", fake([CONTROL_SESSION_NAME, "a", "b"]))
    assert await tmux.at_session_cap()


def test_create_session_refused_at_cap(app_env, monkeypatch):
    c = _login(app_env.app)

    async def no(name): return False
    async def yes(): return True
    monkeypatch.setattr(tmux, "session_exists", no)
    monkeypatch.setattr(tmux, "at_session_cap", yes)
    r = c.post("/api/sessions", headers=H, json={"name": "overflow"})
    assert r.status_code == 429


# ── #12: delete/rename act on the entry; mkdir safe; rename never clobbers ─

def test_delete_and_rename_act_on_symlink_not_target(fs_client, tmp_path):
    c, root = fs_client
    target = root / "real.txt"
    target.write_text("keep")
    link = root / "link"
    link.symlink_to(target)
    r = c.post("/api/fs/rename", headers=H, json={"src": str(link), "dst": str(root / "link2")})
    assert r.status_code == 200
    assert (root / "link2").is_symlink() and target.read_text() == "keep"
    assert c.post("/api/fs/delete", headers=H, json={"path": str(root / "link2")}).status_code == 200
    assert not (root / "link2").exists() and target.read_text() == "keep"
    # A dangling link — and one pointing outside the jail — can be removed.
    (root / "dangling").symlink_to(root / "nowhere")
    (root / "out").symlink_to(tmp_path / "outside-file")
    for name in ("dangling", "out"):
        assert c.post("/api/fs/delete", headers=H, json={"path": str(root / name)}).status_code == 200
        assert not os.path.lexists(root / name)


def test_rename_never_replaces_existing(fs_client):
    c, root = fs_client
    (root / "a").write_text("A"); (root / "b").write_text("B")
    r = c.post("/api/fs/rename", headers=H, json={"src": str(root / "a"), "dst": str(root / "b")})
    assert r.status_code == 409
    assert (root / "a").read_text() == "A" and (root / "b").read_text() == "B"


def test_mkdir(fs_client):
    c, root = fs_client
    assert c.post("/api/fs/mkdir", headers=H, json={"path": str(root / "d")}).status_code == 200
    assert (root / "d").is_dir()
    assert c.post("/api/fs/mkdir", headers=H, json={"path": str(root / "d")}).status_code == 409


# ── #13: one same-origin gate on every authenticated GET ───────────────

def test_every_authed_get_has_the_same_origin_gate(app_env):
    from ccpipe.auth import require_auth, require_same_origin
    missing = []
    for route in app_env.app.routes:
        if "GET" not in getattr(route, "methods", set()):
            continue
        deps = {d.call for d in getattr(route, "dependant", None).dependencies} if hasattr(route, "dependant") else set()
        if require_auth in deps and require_same_origin not in deps:
            missing.append(route.path)
    assert not missing, missing


def test_same_origin_gate_blocks_cross_site(app_env):
    c = _login(app_env.app)
    assert c.get("/api/sessions/x/history", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.get("/api/tts/voices", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert c.get("/api/mic/config", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.get("/api/mic/config").status_code == 200          # non-browser: no header


@pytest.mark.asyncio
async def test_session_mutations_invalidate_list_cache(monkeypatch):
    # Regression (found in E2E): the cap check primed list_sessions()'s
    # cache, so POST /api/sessions couldn't find the session it had just
    # created and returned 500.
    seen = []
    monkeypatch.setattr(tmux, "invalidate_list_sessions_cache", lambda: seen.append(1))
    monkeypatch.setattr(tmux, "_sync_create_session", lambda *a: None)
    monkeypatch.setattr(tmux, "_sync_kill_session", lambda n: True)
    monkeypatch.setattr(tmux, "_sync_rename_session", lambda a, b: True)
    await tmux.create_session("x", command="true")
    await tmux.kill_session("x")
    await tmux.rename_session("x", "y")
    assert len(seen) == 3
