"""Saved prompts API."""
from __future__ import annotations

import importlib
import os

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


def test_save_list_replace_delete(client):
    c, tmp = client
    assert c.get("/api/prompts").json() == []
    assert c.post("/api/prompts", headers=H, json={"name": "  review  diff ", "text": "Review the diff"}).json() == {"name": "review diff"}
    c.post("/api/prompts", headers=H, json={"name": "tests", "text": "Run the tests"})
    names = [p["name"] for p in c.get("/api/prompts").json()]
    assert names == ["tests", "review diff"]              # newest first
    # Same name without overwrite → 409 (client asks "replace?"), then with.
    assert c.post("/api/prompts", headers=H, json={"name": "tests", "text": "x"}).status_code == 409
    assert c.post("/api/prompts", headers=H, json={"name": "tests", "text": "Run ALL tests", "overwrite": True}).status_code == 200
    assert {p["name"]: p["text"] for p in c.get("/api/prompts").json()}["tests"] == "Run ALL tests"
    assert c.post("/api/prompts/delete", headers=H, json={"name": "tests"}).json() == {"deleted": True}
    assert c.post("/api/prompts/delete", headers=H, json={"name": "tests"}).status_code == 404
    assert os.stat(tmp / "prompts.json").st_mode & 0o777 == 0o600


def test_validation(client):
    c, _ = client
    bad = [({"name": "", "text": "x"}, 400), ({"name": "n" * 81, "text": "x"}, 400),
           ({"name": "a\x07b", "text": "x"}, 400), ({"name": "ok", "text": "   "}, 400),
           ({"name": "ok", "text": "x" * (32 * 1024 + 1)}, 413)]
    for body, code in bad:
        assert c.post("/api/prompts", headers=H, json=body).status_code == code, body


def test_requires_auth_csrf_and_same_origin(client):
    c, _ = client
    anon = TestClient(c.app)
    assert anon.get("/api/prompts").status_code == 401
    assert c.post("/api/prompts", json={"name": "a", "text": "b"}).status_code == 403   # no CSRF header
    assert c.get("/api/prompts", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
