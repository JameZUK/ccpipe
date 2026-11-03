"""Byte-range downloads, the inline file view, and resumable uploads
(fs_range.py, routes/fs_transfer.py)."""
from __future__ import annotations

import importlib
import os
import stat

import pytest
from fastapi.testclient import TestClient

from ccpipe import fs_range

H = {"X-Requested-By": "ccpipe"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(state / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(state / "credentials"))
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    monkeypatch.setenv("CCPIPE_FS_ROOT", str(root))
    (tmp_path / "xdg" / "ccpipe").mkdir(parents=True)
    import ccpipe.auth as auth
    import ccpipe.main as m
    import ccpipe.routes.auth as routes_auth
    import ccpipe.routes.fs_transfer as ft
    auth.reset_cached_credential()
    routes_auth.reset_throttle_state()
    monkeypatch.setattr(auth, "_revoked", None)
    monkeypatch.setattr(ft, "_uploads", {})
    monkeypatch.setattr(ft, "_loaded", False)
    importlib.reload(m)
    c = TestClient(m.app)
    assert c.post("/api/auth/login", headers=H,
                  json={"username": "alice", "password": "letmein"}).status_code == 200
    data = bytes(range(256)) * 40          # 10240 bytes
    (root / "data.bin").write_bytes(data)
    for name in ("pic.png", "draw.svg", "doc.pdf", "clip.mp4", "page.html", "notes.txt"):
        (root / name).write_bytes(b"x" * 100)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"nope")
    os.symlink(outside / "secret.png", root / "link.png")
    (root / ".claude").mkdir()
    (root / ".claude" / "x.png").write_bytes(b"token")
    return c, root, data, ft, tmp_path


# ── fs_range.parse_range ─────────────────────────────────────────────────

@pytest.mark.parametrize("header,want", [
    (None, None), ("", None), ("bytes=0-9", (0, 9)), ("bytes=10-", (10, 99)),
    ("bytes=-5", (95, 99)), ("bytes=-500", (0, 99)), ("bytes=90-200", (90, 99)),
    ("bytes=100-", "unsatisfiable"), ("bytes=5-2", "unsatisfiable"),
    ("bytes=-0", "unsatisfiable"), ("bytes=0-1,5-6", None), ("items=0-1", None),
])
def test_parse_range(header, want):
    assert fs_range.parse_range(header, 100) == want


# ── /api/fs/download with ranges ─────────────────────────────────────────

def test_download_whole_and_ranges(env):
    c, root, data, *_ = env
    p = f"/api/fs/download?path={root}/data.bin"
    r = c.get(p)
    assert r.status_code == 200 and r.content == data
    assert r.headers["accept-ranges"] == "bytes" and r.headers["etag"]
    assert r.headers["content-disposition"].startswith("attachment")
    r = c.get(p, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == data[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{len(data)}"
    assert c.get(p, headers={"Range": "bytes=-10"}).content == data[-10:]
    assert c.get(p, headers={"Range": "bytes=10000-"}).content == data[10000:]
    r = c.get(p, headers={"Range": "bytes=99999-"})
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(data)}"


def test_download_if_range(env):
    c, root, data, *_ = env
    p = f"/api/fs/download?path={root}/data.bin"
    tag = c.get(p).headers["etag"]
    r = c.get(p, headers={"Range": "bytes=0-9", "If-Range": tag})
    assert r.status_code == 206
    # The file changed since the first piece: the whole new file, not a splice.
    (root / "data.bin").write_bytes(b"new content")
    r = c.get(p, headers={"Range": "bytes=0-9", "If-Range": tag})
    assert r.status_code == 200 and r.content == b"new content"


def test_download_still_jailed(env):
    c, root, *_ = env
    assert c.get(f"/api/fs/download?path={root}/link.png").status_code == 403
    assert c.get(f"/api/fs/download?path={root}/.claude/x.png").status_code == 403
    assert c.get(f"/api/fs/download?path={root}/nope").status_code == 404


# ── /api/fs/inline ───────────────────────────────────────────────────────

def test_inline_types_and_headers(env):
    c, root, *_ = env
    r = c.get(f"/api/fs/inline?path={root}/pic.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["content-security-policy"].startswith("sandbox")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-disposition"].startswith("inline")
    r = c.get(f"/api/fs/inline?path={root}/draw.svg")
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["content-security-policy"].startswith("sandbox")
    r = c.get(f"/api/fs/inline?path={root}/doc.pdf")
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    r = c.get(f"/api/fs/inline?path={root}/clip.mp4", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.headers["content-type"] == "video/mp4"


def test_inline_refuses_other_types_and_jail(env):
    c, root, *_ = env
    assert c.get(f"/api/fs/inline?path={root}/page.html").status_code == 415
    assert c.get(f"/api/fs/inline?path={root}/notes.txt").status_code == 415
    assert c.get(f"/api/fs/inline?path={root}/link.png").status_code == 403
    assert c.get(f"/api/fs/inline?path={root}/.claude/x.png").status_code == 403


# ── resumable uploads ────────────────────────────────────────────────────

def start(c, path, size):
    r = c.post("/api/fs/uploads", headers=H, json={"path": str(path), "size": size})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def put(c, uid, offset, body):
    return c.put(f"/api/fs/uploads/{uid}?offset={offset}", headers=H, content=body)


def parts(root):
    return [n for n in os.listdir(root) if n.startswith(".ccpipe-")]


def test_upload_in_pieces(env):
    c, root, *_ = env
    payload = os.urandom(3 * 1024 * 1024 + 123)       # bigger than the 64 KiB default body cap
    uid = start(c, root / "big.bin", len(payload))
    assert len(parts(root)) == 1
    step = 1024 * 1024
    for off in range(0, len(payload), step):
        r = put(c, uid, off, payload[off:off + step])
        assert r.status_code == 200 and r.json()["received"] == min(off + step, len(payload))
    r = c.post(f"/api/fs/uploads/{uid}/finish", headers=H)
    assert r.status_code == 200
    assert (root / "big.bin").read_bytes() == payload
    assert parts(root) == []
    assert stat.S_IMODE((root / "big.bin").stat().st_mode) == 0o644 & ~_umask()


def test_upload_retry_and_resume(env):
    c, root, *_ = env
    payload = b"abcdefghij" * 100
    uid = start(c, root / "r.txt", len(payload))
    assert put(c, uid, 0, payload[:300]).json()["received"] == 300
    # A retry of a piece that already (half-)arrived rewrites the same bytes.
    assert put(c, uid, 200, payload[200:600]).json()["received"] == 600
    # A gap is refused, with how much has arrived, to resume from.
    r = put(c, uid, 700, payload[700:])
    assert r.status_code == 409 and r.json()["received"] == 600
    assert c.get(f"/api/fs/uploads/{uid}").json()["received"] == 600
    # Finishing early is refused too.
    r = c.post(f"/api/fs/uploads/{uid}/finish", headers=H)
    assert r.status_code == 409 and r.json()["received"] == 600
    assert put(c, uid, 600, payload[600:]).status_code == 200
    assert c.post(f"/api/fs/uploads/{uid}/finish", headers=H).status_code == 200
    assert (root / "r.txt").read_bytes() == payload


def test_upload_survives_a_restart(env):
    c, root, data, ft, _ = env
    uid = start(c, root / "s.bin", 20)
    put(c, uid, 0, b"x" * 8)
    ft._uploads.clear()
    ft._loaded = False                      # as after a ccpipe restart
    assert c.get(f"/api/fs/uploads/{uid}").json()["received"] == 8
    put(c, uid, 8, b"y" * 12)
    assert c.post(f"/api/fs/uploads/{uid}/finish", headers=H).status_code == 200
    assert (root / "s.bin").read_bytes() == b"x" * 8 + b"y" * 12


def test_upload_keeps_the_replaced_files_mode(env):
    c, root, *_ = env
    target = root / "run.sh"
    target.write_text("old")
    os.chmod(target, 0o555)                 # read-only and executable
    uid = start(c, target, 3)
    assert put(c, uid, 0, b"new").status_code == 200
    assert c.post(f"/api/fs/uploads/{uid}/finish", headers=H).status_code == 200
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o555


def test_upload_limits(env):
    c, root, *_ = env
    uid = start(c, root / "l.bin", 10)
    assert put(c, uid, 0, b"z" * 11).status_code == 413          # past the declared size
    r = c.post("/api/fs/uploads", headers=H, json={"path": f"{root}/huge", "size": 10**12})
    assert r.status_code == 413
    r = c.post("/api/fs/uploads", headers=H, json={"path": f"{root}/.claude/x", "size": 1})
    assert r.status_code == 403
    r = c.post("/api/fs/uploads", json={"path": f"{root}/nocsrf", "size": 1})
    assert r.status_code == 403
    assert c.get("/api/fs/uploads/nope").status_code == 404


def test_upload_cancel_and_expiry(env, monkeypatch):
    c, root, data, ft, _ = env
    uid = start(c, root / "c.bin", 5)
    assert c.delete(f"/api/fs/uploads/{uid}", headers=H).json() == {"ok": True}
    assert parts(root) == [] and c.get(f"/api/fs/uploads/{uid}").status_code == 404
    uid = start(c, root / "e.bin", 5)
    monkeypatch.setattr(ft, "_UPLOAD_TTL_S", -1)
    ft._sweep()
    assert parts(root) == [] and uid not in ft._uploads


def _umask() -> int:
    u = os.umask(0)
    os.umask(u)
    return u


# ── review fixes (2026-11-03) ────────────────────────────────────────────

def test_fifo_is_refused_without_hanging(env):
    c, root, *_ = env
    os.mkfifo(root / "pipe.png")
    os.mkfifo(root / "pipe.bin")
    # Before: os.open blocked waiting for a writer, holding a worker for good.
    assert c.get(f"/api/fs/download?path={root}/pipe.bin").status_code == 400
    assert c.get(f"/api/fs/inline?path={root}/pipe.png").status_code == 400


def test_abandoned_uploads_swept_on_load_and_on_start(env, monkeypatch):
    c, root, data, ft, _ = env
    uid = start(c, root / "old.bin", 10)
    assert len(parts(root)) == 1
    # As after a restart a day later: the registry is reloaded and swept.
    ft._uploads.clear()
    ft._loaded = False
    monkeypatch.setattr(ft, "_UPLOAD_TTL_S", -1)
    ft._load()
    assert parts(root) == [] and uid not in ft._uploads
    # And a new upload sweeps any that went stale while running.
    uid2 = start(c, root / "a.bin", 10)
    uid3 = start(c, root / "b.bin", 10)
    assert uid2 not in ft._uploads and uid3 in ft._uploads


def test_sweep_loop_runs_at_startup(env, monkeypatch):
    import asyncio
    c, root, data, ft, _ = env
    start(c, root / "x.bin", 10)
    ft._uploads.clear()
    ft._loaded = False
    monkeypatch.setattr(ft, "_UPLOAD_TTL_S", -1)

    async def one_pass():
        task = asyncio.create_task(ft.sweep_loop())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(one_pass())
    assert parts(root) == []


def test_read_umask_matches_the_process():
    import ccpipe.routes.fs_transfer as ft
    assert ft._read_umask() == _umask()


def test_piece_bigger_than_a_write_batch(env):
    c, root, *_ = env
    payload = os.urandom(3 * 1024 * 1024 + 7)        # several 1 MiB batches in one PUT
    uid = start(c, root / "batch.bin", len(payload))
    assert put(c, uid, 0, payload).json()["received"] == len(payload)
    assert c.post(f"/api/fs/uploads/{uid}/finish", headers=H).status_code == 200
    assert (root / "batch.bin").read_bytes() == payload
