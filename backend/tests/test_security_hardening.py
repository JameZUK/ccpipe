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
