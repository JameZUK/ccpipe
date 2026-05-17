"""Tests for the WS Origin allowlist and CSRF header gate.

The reviewer's #1 finding (cross-site WebSocket hijacking) and #2 finding
(CSRF on POST endpoints) are both addressed here.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_env_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    import importlib
    import ccpipe.auth as auth
    import ccpipe.main as m
    auth.reset_cached_credential()
    importlib.reload(m)
    return m.app


# ── CSRF ──────────────────────────────────────────────────────────────────

def test_login_without_csrf_header_rejected(app_env_creds):
    with TestClient(app_env_creds) as c:
        r = c.post("/api/auth/login",
                   json={"username": "alice", "password": "letmein"})
        assert r.status_code == 403
        assert "csrf" in r.json()["detail"]


def test_create_session_without_csrf_header_rejected(app_env_creds):
    with TestClient(app_env_creds) as c:
        # First log in (with header) so we'd otherwise have access.
        c.post("/api/auth/login",
               headers={"X-Requested-By": "ccpipe"},
               json={"username": "alice", "password": "letmein"})
        r = c.post("/api/sessions", json={"name": "test"})
        assert r.status_code == 403


def test_logout_without_csrf_header_rejected(app_env_creds):
    with TestClient(app_env_creds) as c:
        c.post("/api/auth/login",
               headers={"X-Requested-By": "ccpipe"},
               json={"username": "alice", "password": "letmein"})
        r = c.post("/api/auth/logout")
        assert r.status_code == 403


# ── WS Origin allowlist (unit-test the helper) ────────────────────────────

class _FakeWS:
    def __init__(self, headers: dict[str, str]):
        # Starlette WS headers are case-insensitive; lower-case keys here.
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.scope = {}


def test_origin_check_rejects_missing_origin():
    from ccpipe.auth import _origin_allowed
    ws = _FakeWS({"host": "10.0.0.5:8080"})
    assert _origin_allowed(ws) is False  # type: ignore[arg-type]


def test_origin_check_allows_same_host():
    from ccpipe.auth import _origin_allowed
    ws = _FakeWS({"host": "10.0.0.5:8080",
                  "origin": "http://10.0.0.5:8080"})
    assert _origin_allowed(ws) is True  # type: ignore[arg-type]


def test_origin_check_rejects_cross_site():
    from ccpipe.auth import _origin_allowed
    ws = _FakeWS({"host": "10.0.0.5:8080",
                  "origin": "https://evil.example.com"})
    assert _origin_allowed(ws) is False  # type: ignore[arg-type]


def test_origin_check_extra_allowlist(monkeypatch):
    monkeypatch.setenv("CCPIPE_ALLOWED_ORIGINS",
                       "https://ccpipe.int.example.com,https://other.example.com")
    from ccpipe.auth import _origin_allowed
    ws = _FakeWS({"host": "10.0.0.5:8080",
                  "origin": "https://ccpipe.int.example.com"})
    assert _origin_allowed(ws) is True  # type: ignore[arg-type]


def test_origin_check_https_or_http_variant_of_host():
    from ccpipe.auth import _origin_allowed
    ws = _FakeWS({"host": "ccpipe.lan",
                  "origin": "https://ccpipe.lan"})
    assert _origin_allowed(ws) is True  # type: ignore[arg-type]
