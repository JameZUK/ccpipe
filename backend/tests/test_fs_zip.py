"""Directory download as .zip (/api/fs/download-zip, /api/fs/dir-summary)."""
from __future__ import annotations

import importlib
import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

H = {"X-Requested-By": "ccpipe"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(state / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(state / "credentials"))
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    monkeypatch.setenv("CCPIPE_FS_ROOT", str(root))
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

    # root/proj: a small project tree.
    proj = root / "proj"
    (proj / "src" / "deep").mkdir(parents=True)
    (proj / "empty").mkdir()
    (proj / ".git").mkdir()
    (proj / "README.md").write_text("# hello\n")
    (proj / "src" / "main.py").write_text("print('hi')\n" * 1000)
    (proj / "src" / "deep" / "data.bin").write_bytes(os.urandom(300_000))
    (proj / "src" / "pic.png").write_bytes(b"\x89PNG" + os.urandom(1000))
    (proj / ".env").write_text("SECRET=1\n")
    (proj / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    os.chmod(proj / "src" / "main.py", 0o755)
    # Symlinks — to a file and a dir outside the tree, and inside it.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    os.symlink(outside / "secret.txt", proj / "link-file")
    os.symlink(outside, proj / "link-dir")
    os.symlink(proj / "README.md", proj / "src" / "link-inside")
    os.mkfifo(proj / "a-fifo")
    # Deny-listed dir inside the jail root.
    (root / ".claude").mkdir()
    (root / ".claude" / "creds.json").write_text("token")
    (root / "notes.txt").write_text("notes")
    return c, root


def _zip(c, path, hidden=0):
    r = c.get(f"/api/fs/download-zip?path={path}&hidden={hidden}")
    assert r.status_code == 200, r.text
    return r, zipfile.ZipFile(io.BytesIO(r.content))


def test_zip_roundtrip(env):
    c, root = env
    r, z = _zip(c, root / "proj")
    assert r.headers["content-type"] == "application/zip"
    assert 'filename="proj.zip"' in r.headers["content-disposition"]
    assert z.testzip() is None
    names = set(z.namelist())
    assert names == {"proj/README.md", "proj/src/main.py", "proj/src/deep/data.bin",
                     "proj/src/pic.png", "proj/empty/"}
    assert z.read("proj/src/main.py") == (root / "proj/src/main.py").read_bytes()
    assert z.read("proj/src/deep/data.bin") == (root / "proj/src/deep/data.bin").read_bytes()
    assert z.getinfo("proj/src/pic.png").compress_type == zipfile.ZIP_STORED
    assert z.getinfo("proj/src/main.py").compress_type == zipfile.ZIP_DEFLATED
    assert (z.getinfo("proj/src/main.py").external_attr >> 16) & 0o777 == 0o755


def test_zip_hidden_only_on_request(env):
    c, root = env
    _, z = _zip(c, root / "proj", hidden=1)
    names = set(z.namelist())
    assert {"proj/.env", "proj/.git/HEAD"} <= names
    # Symlinks / FIFOs never, hidden or not.
    assert not any("link" in n or "fifo" in n or "secret" in n for n in names)


def test_zip_skips_denied_paths(env):
    c, root = env
    _, z = _zip(c, root, hidden=1)
    names = set(z.namelist())
    assert "root/notes.txt" in names
    assert not any(".claude" in n for n in names)


def test_zip_refusals(env):
    c, root = env
    assert c.get(f"/api/fs/download-zip?path={root}/notes.txt").status_code == 400
    assert c.get(f"/api/fs/download-zip?path={root}/.claude").status_code == 403
    assert c.get(f"/api/fs/download-zip?path={root.parent}/outside").status_code == 403
    assert c.get(f"/api/fs/download-zip?path={root}/proj/link-dir").status_code == 403
    assert c.get(f"/api/fs/download-zip?path={root}/nope").status_code == 404
    assert c.get("/api/fs/download-zip?path=relative").status_code == 400


def test_dir_summary(env):
    c, root = env
    r = c.get(f"/api/fs/dir-summary?path={root}/proj")
    assert r.status_code == 200
    s = r.json()
    assert s["name"] == "proj"
    assert s["files"] == 4 and s["hidden_files"] == 2
    expect = sum((root / "proj" / p).stat().st_size for p in
                 ("README.md", "src/main.py", "src/deep/data.bin", "src/pic.png"))
    assert s["bytes"] == expect
    assert s["truncated"] is False
