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
