import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def state_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate session-secret + credentials files to tmp_path."""
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    return tmp_path


@pytest.fixture
def app_env_creds(state_files: Path, monkeypatch: pytest.MonkeyPatch):
    """Credentials supplied via env (user 'alice', pass 'letmein')."""
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    import importlib
    import ccpipe.auth as auth
    import ccpipe.main as m
    auth.reset_cached_credential()
    importlib.reload(m)
    return m.app


@pytest.fixture
def app_generated_creds(state_files: Path, monkeypatch: pytest.MonkeyPatch):
    """No env vars set → ccpipe should generate + persist credentials."""
    monkeypatch.delenv("CCPIPE_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("CCPIPE_AUTH_PASSWORD", raising=False)
    import importlib
    import ccpipe.auth as auth
    import ccpipe.main as m
    auth.reset_cached_credential()
    importlib.reload(m)
    return m.app, state_files / "credentials"


# ── Auth is always required ────────────────────────────────────────────────

def test_status_always_required(app_env_creds):
    with TestClient(app_env_creds) as c:
        r = c.get("/api/auth/status")
        assert r.status_code == 200
        body = r.json()
        assert body["required"] is True
        assert body["authenticated"] is False
        assert body["username"] is None


def test_sessions_blocked_when_unauthed(app_env_creds):
    with TestClient(app_env_creds) as c:
        assert c.get("/api/sessions").status_code == 401


def test_health_always_open(app_env_creds):
    with TestClient(app_env_creds) as c:
        assert c.get("/api/health").status_code == 200


# ── Env-supplied credentials ───────────────────────────────────────────────

def test_login_rejects_wrong_password(app_env_creds):
    with TestClient(app_env_creds) as c:
        r = c.post("/api/auth/login", headers={"X-Requested-By": "ccpipe"}, json={"username": "alice", "password": "nope"})
        assert r.status_code == 401


def test_login_rejects_wrong_username(app_env_creds):
    with TestClient(app_env_creds) as c:
        r = c.post("/api/auth/login", headers={"X-Requested-By": "ccpipe"}, json={"username": "bob", "password": "letmein"})
        assert r.status_code == 401


def test_login_rejects_missing_fields(app_env_creds):
    with TestClient(app_env_creds) as c:
        r = c.post("/api/auth/login", headers={"X-Requested-By": "ccpipe"}, json={"password": "letmein"})
        # Manual body parsing in the route now collapses all
        # parse/validation failures (including missing required
        # fields) to 400 — same response shape as malformed JSON.
        # That's the pass-3 #17/#18 fix; 401/422 are kept here for
        # back-compat with hypothetical older behaviours.
        assert r.status_code in (400, 401, 422)


def test_login_succeeds_then_access(app_env_creds):
    with TestClient(app_env_creds) as c:
        r = c.post("/api/auth/login", headers={"X-Requested-By": "ccpipe"}, json={"username": "alice", "password": "letmein"})
        assert r.status_code == 200
        body = r.json()
        assert body["authenticated"] is True
        assert body["username"] == "alice"
        # Cookie set; subsequent calls succeed.
        st = c.get("/api/auth/status").json()
        assert st["authenticated"] is True
        assert st["username"] == "alice"


def test_login_blocked_without_csrf_header(app_env_creds):
    with TestClient(app_env_creds) as c:
        r = c.post("/api/auth/login",
                   json={"username": "alice", "password": "letmein"})
        assert r.status_code == 403
        assert "csrf" in r.json()["detail"]


def test_login_rejects_oversized_body(app_env_creds):
    """Login must refuse bodies above the documented cap so a misbehaving
    or hostile client can't force the worker to buffer an arbitrary-size
    body in memory before the throttle decides anything.

    Two paths each reject independently: the Content-Length pre-check in
    main.py:_cap_auth_login_body (returns a plain-text 413 before the
    route ever runs) and the streaming read in routes/auth.py:auth_login
    (closes the chunked-encoding / missing-Content-Length gap, returns a
    JSON 413 with a `detail` field). This test covers the honest-CL path;
    chunked-encoding tests are awkward to drive from TestClient."""
    with TestClient(app_env_creds) as c:
        huge_password = "a" * (128 * 1024)
        r = c.post("/api/auth/login",
                   headers={"X-Requested-By": "ccpipe"},
                   json={"username": "alice", "password": huge_password})
        assert r.status_code == 413, r.text
        # Either path returns a body containing "too large"; the
        # middleware uses plaintext and the route uses JSON, so just
        # assert against the response text directly.
        assert "too large" in r.text.lower()


def test_logout_clears_session(app_env_creds):
    with TestClient(app_env_creds) as c:
        c.post("/api/auth/login", headers={"X-Requested-By": "ccpipe"}, json={"username": "alice", "password": "letmein"})
        assert c.get("/api/auth/status").json()["authenticated"] is True
        c.post("/api/auth/logout", headers={"X-Requested-By": "ccpipe"})
        body = c.get("/api/auth/status").json()
        assert body["authenticated"] is False
        assert body["username"] is None


# ── Auto-generated credentials ─────────────────────────────────────────────

def test_credentials_are_generated_when_no_env(app_generated_creds):
    app, creds_path = app_generated_creds
    # Trigger credential resolution by hitting the status endpoint.
    with TestClient(app) as c:
        c.get("/api/auth/status")
    assert creds_path.exists()
    data = json.loads(creds_path.read_text())
    assert "username" in data and isinstance(data["username"], str) and data["username"]
    # The file now contains a hash, not the plaintext password.
    assert "password_hash" in data
    assert data["password_hash"].startswith("$argon2")
    assert "password" not in data
    # Plaintext lives in a sidecar (mode 0400).
    sidecar = creds_path.parent / "initial_password.txt"
    assert sidecar.exists()
    body = sidecar.read_text()
    assert "username:" in body and "password:" in body


def test_credentials_file_is_0600(app_generated_creds):
    app, creds_path = app_generated_creds
    with TestClient(app) as c:
        c.get("/api/auth/status")
    mode = creds_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    sidecar = creds_path.parent / "initial_password.txt"
    sidecar_mode = sidecar.stat().st_mode & 0o777
    assert sidecar_mode == 0o400, f"sidecar expected 0400, got {oct(sidecar_mode)}"


def test_login_with_generated_credentials(app_generated_creds):
    app, creds_path = app_generated_creds
    with TestClient(app) as c:
        c.get("/api/auth/status")  # force generation
        # The credentials file no longer contains plaintext; read the
        # sidecar to recover it.
        sidecar = creds_path.parent / "initial_password.txt"
        body = sidecar.read_text()
        username = next(l.split(":", 1)[1].strip() for l in body.splitlines() if l.startswith("username:"))
        password = next(l.split(":", 1)[1].strip() for l in body.splitlines() if l.startswith("password:"))
        r = c.post("/api/auth/login", headers={"X-Requested-By": "ccpipe"},
                   json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        assert r.json()["username"] == username


def test_existing_credentials_file_is_reused(state_files: Path,
                                              monkeypatch: pytest.MonkeyPatch):
    """Legacy plaintext credentials files are accepted and migrated to
    argon2id on first read. The user can still log in with the original
    plaintext, and the file is rewritten with the hash."""
    creds_path = state_files / "credentials"
    creds_path.write_text(json.dumps({"username": "preset", "password": "preset-pw"}))
    creds_path.chmod(0o600)
    monkeypatch.delenv("CCPIPE_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("CCPIPE_AUTH_PASSWORD", raising=False)
    import importlib
    import ccpipe.auth as auth
    import ccpipe.main as m
    auth.reset_cached_credential()
    importlib.reload(m)
    with TestClient(m.app) as c:
        r = c.post("/api/auth/login", headers={"X-Requested-By": "ccpipe"}, json={"username": "preset", "password": "preset-pw"})
        assert r.status_code == 200
    # After login (which forced credential resolution), the file should
    # have been migrated to the new schema.
    data = json.loads(creds_path.read_text())
    assert "password_hash" in data
    assert data["password_hash"].startswith("$argon2")
    assert "password" not in data


def test_malformed_credentials_file_regenerates(state_files: Path,
                                                 monkeypatch: pytest.MonkeyPatch):
    creds_path = state_files / "credentials"
    creds_path.write_text("not-json")
    monkeypatch.delenv("CCPIPE_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("CCPIPE_AUTH_PASSWORD", raising=False)
    import importlib
    import ccpipe.auth as auth
    import ccpipe.main as m
    auth.reset_cached_credential()
    importlib.reload(m)
    with TestClient(m.app) as c:
        c.get("/api/auth/status")
    # File should now contain valid JSON with the new schema.
    data = json.loads(creds_path.read_text())
    assert "username" in data and "password_hash" in data
    assert data["password_hash"].startswith("$argon2")
