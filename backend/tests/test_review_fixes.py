"""Regression tests for the round-2 review punch list.

Each test pins one of the fixes so the bug can't slip back in:
  - #12 deque-based mic rate limiter total stays accurate over evictions
  - #20 /api/tts/preview rejects requests without Sec-Fetch-Site=same-origin
  - #23 tmux.create_session is idempotent under duplicate-session errors
  - #19 CSP no longer contains the bare `ws:`/`wss:` wildcard tokens
"""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ── #12  _MicRateLimiter ──────────────────────────────────────────────────

def test_mic_rate_limiter_running_total_stays_consistent():
    """Frames older than the window must drop out of `_total` so the
    limiter doesn't permanently consider the budget exhausted."""
    from ccpipe import ws as ws_mod
    from ccpipe.ws import _MicRateLimiter, _MIC_BUDGET_BYTES, _MIC_BUDGET_WINDOW_S
    lim = _MicRateLimiter()

    # Inject a known clock. ws_mod uses time.monotonic via `time` import.
    t = [0.0]
    with patch.object(ws_mod.time, "monotonic", lambda: t[0]):
        # Saturate at t=0.
        assert lim.allow(_MIC_BUDGET_BYTES // 2) is True
        assert lim.allow(_MIC_BUDGET_BYTES // 2) is True
        # Next frame exceeds the budget; rejected.
        assert lim.allow(1) is False

        # Advance past the window. Old entries must evict + reset total.
        t[0] = _MIC_BUDGET_WINDOW_S + 0.1
        assert lim.allow(_MIC_BUDGET_BYTES // 2) is True
        assert lim._total == _MIC_BUDGET_BYTES // 2


# ── #23  create_session is idempotent ─────────────────────────────────────

def test_create_session_idempotent_under_duplicate_error(monkeypatch):
    """Two near-simultaneous callers must both succeed even if one of
    them loses the race to libtmux."""
    import ccpipe.tmux as tmux_mod
    from libtmux.exc import LibTmuxException

    state = {"created": False}

    def fake_new_session(self, **kwargs):
        if state["created"]:
            raise LibTmuxException("duplicate session: x")
        state["created"] = True
        return None

    def fake_has_session(self, name): return state["created"]

    class FakeSession:
        def cmd(self, *args, **kwargs): pass

    class FakeSessions:
        def get(self, session_name=None): return FakeSession()

    class FakeServer:
        sessions = FakeSessions()
        def new_session(self, **kwargs):
            return fake_new_session(self, **kwargs)
        def has_session(self, name):
            return fake_has_session(self, name)

    monkeypatch.setattr(tmux_mod, "_server", lambda: FakeServer())

    # First call creates.
    tmux_mod._sync_create_session("x", "claude")
    # Second call sees duplicate; must not raise.
    tmux_mod._sync_create_session("x", "claude")


# ── #20  /api/tts/preview Sec-Fetch-Site gate ────────────────────────────

@pytest.fixture
def authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    import ccpipe.auth as auth
    import ccpipe.main as m
    auth.reset_cached_credential()
    importlib.reload(m)
    c = TestClient(m.app)
    c.post("/api/auth/login",
           headers={"X-Requested-By": "ccpipe"},
           json={"username": "alice", "password": "letmein"})
    return c


def test_preview_rejects_missing_sec_fetch_site(authed_client):
    """No Sec-Fetch-Site header → reject. Older browsers that wouldn't
    set it are a non-target for ccpipe; the gate intentionally fails closed."""
    r = authed_client.get("/api/tts/preview?voice=af_bella")
    assert r.status_code == 403
    assert "cross-origin" in r.json()["detail"]


def test_preview_rejects_cross_site(authed_client):
    r = authed_client.get("/api/tts/preview?voice=af_bella",
                          headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_preview_accepts_same_origin(authed_client):
    """Same-origin requests pass the gate. We mock Kokoro so the
    streaming response can actually complete; the point is that the
    Sec-Fetch-Site gate didn't reject us with a 403."""
    import respx
    import httpx
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:8880/v1/audio/speech").mock(
            return_value=httpx.Response(200, content=b"\x00\x01\x02"))
        r = authed_client.get("/api/tts/preview?voice=af_bella",
                              headers={"sec-fetch-site": "same-origin"})
        assert r.status_code == 200
        assert r.content == b"\x00\x01\x02"


# ── TTS session isolation ────────────────────────────────────────────────

async def test_claude_session_id_reads_pid_json(tmp_path, monkeypatch):
    """tmux.claude_session_id resolves a claude PID to its sessionId via
    ~/.claude/sessions/<pid>.json. Without this, two claudes sharing a
    cwd cross-talk on TTS."""
    import ccpipe.tmux as tmux_mod

    fake_home = tmp_path
    sessions_dir = fake_home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "12345.json").write_text(
        '{"pid": 12345, "sessionId": "abc-def-ghi", "cwd": "/p"}'
    )
    monkeypatch.setattr(tmux_mod.Path, "home", staticmethod(lambda: fake_home))

    async def fake_claude_pid(name):
        assert name == "s1"
        return 12345

    monkeypatch.setattr(tmux_mod, "claude_pid", fake_claude_pid)

    sid = await tmux_mod.claude_session_id("s1")
    assert sid == "abc-def-ghi"


async def test_claude_session_id_rejects_stale_pid_file(tmp_path, monkeypatch):
    """A leftover sessions/<pid>.json from a recycled PID must be ignored
    (pid field in the JSON disagrees with the filename's PID)."""
    import ccpipe.tmux as tmux_mod

    sessions_dir = tmp_path / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    # PID 99 written by a stale claude; current PID is 99 but the JSON
    # has been rewritten by some other process (or was never updated).
    (sessions_dir / "99.json").write_text(
        '{"pid": 7777, "sessionId": "stale-uuid"}'
    )
    monkeypatch.setattr(tmux_mod.Path, "home", staticmethod(lambda: tmp_path))

    async def fake_claude_pid(name):
        return 99

    monkeypatch.setattr(tmux_mod, "claude_pid", fake_claude_pid)
    sid = await tmux_mod.claude_session_id("s1")
    assert sid is None


async def test_tts_filter_sessionid_isolation(tmp_path, monkeypatch):
    """Two claudes in the same cwd produce records with different
    sessionIds. The filter must accept ours and reject the sibling's,
    even when the cwd matches both."""
    import ccpipe.tmux as tmux_mod
    from ccpipe.ws import _build_tts_filter

    sessions_dir = tmp_path / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "1000.json").write_text(
        '{"pid": 1000, "sessionId": "mine"}'
    )
    monkeypatch.setattr(tmux_mod.Path, "home", staticmethod(lambda: tmp_path))

    async def fake_claude_pid(name): return 1000
    monkeypatch.setattr(tmux_mod, "claude_pid", fake_claude_pid)

    accept = await _build_tts_filter("s1")
    # Our records — accepted.
    assert accept({"sessionId": "mine", "cwd": "/p",
                    "timestamp": "2099-01-01T00:00:00Z"}) is True
    # Sibling claude in same cwd — rejected.
    assert accept({"sessionId": "theirs", "cwd": "/p",
                    "timestamp": "2099-01-01T00:00:00Z"}) is False


# ── TOTP two-factor flow ─────────────────────────────────────────────────

def test_login_no_totp_returns_authenticated(authed_client):
    """Pristine accounts (no TOTP enrolled) must complete in one step."""
    # The authed_client fixture already logs in to verify password flow;
    # here we just check the response shape on a clean fresh post.
    authed_client.post("/api/auth/logout",
                        headers={"X-Requested-By": "ccpipe"})
    r = authed_client.post("/api/auth/login",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"username": "alice", "password": "letmein"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body.get("otp_required", False) is False


def test_totp_enroll_then_login_requires_code(authed_client):
    """End-to-end enrollment + two-step login. Uses the actual server
    pyotp instance, so a code generated here is valid."""
    import pyotp
    # 1. Enroll.
    r = authed_client.post("/api/auth/totp/enroll",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"currentPassword": "letmein"})
    assert r.status_code == 200, r.text
    enroll = r.json()
    secret = enroll["secret"]
    # SVG is stripped of its XML prolog server-side so the frontend
    # can render it via element.innerHTML cleanly. Just check we got
    # an SVG root element back.
    assert "qr_svg" in enroll and enroll["qr_svg"].lstrip().startswith("<svg")
    # 2. Confirm with a fresh code.
    code = pyotp.TOTP(secret).now()
    r = authed_client.post("/api/auth/totp/confirm",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"secret": secret, "code": code})
    assert r.status_code == 200
    # 3. Log out, then try password-only login — should signal otp_required.
    authed_client.post("/api/auth/logout",
                       headers={"X-Requested-By": "ccpipe"})
    r = authed_client.post("/api/auth/login",
                           headers={"X-Requested-By": "ccpipe"},
                           json={"username": "alice", "password": "letmein"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["otp_required"] is True
    # 4. Submit with a valid code → authenticated.
    code = pyotp.TOTP(secret).now()
    r = authed_client.post("/api/auth/login",
                           headers={"X-Requested-By": "ccpipe"},
                           json={"username": "alice", "password": "letmein",
                                 "code": code})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True


def test_totp_enroll_rejects_wrong_current_password(authed_client):
    r = authed_client.post("/api/auth/totp/enroll",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"currentPassword": "wrong"})
    assert r.status_code == 401


def test_totp_confirm_rejects_bad_code(authed_client):
    import pyotp
    secret = pyotp.random_base32()
    r = authed_client.post("/api/auth/totp/confirm",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"secret": secret, "code": "000000"})
    assert r.status_code == 401


# ── Login rate-limit ────────────────────────────────────────────────────

def test_login_rate_limit(authed_client):
    """After N consecutive failures the limiter must 429."""
    # Logout first so we're on a fresh authenticator session; the
    # client fixture's IP is constant so the limiter applies.
    authed_client.post("/api/auth/logout",
                       headers={"X-Requested-By": "ccpipe"})
    # The test_review_fixes.py module hasn't tripped the bucket yet,
    # but other tests in this module do log in successfully; reset
    # the in-memory bucket so this test is order-independent.
    import ccpipe.main as m
    m._login_attempts.clear()
    # 5 attempts is the cap.
    for i in range(5):
        r = authed_client.post("/api/auth/login",
                                headers={"X-Requested-By": "ccpipe"},
                                json={"username": "alice", "password": "wrong"})
        assert r.status_code == 401, f"attempt {i}: {r.status_code}"
    r = authed_client.post("/api/auth/login",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"username": "alice", "password": "wrong"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


# ── Control-session protection ──────────────────────────────────────────

def test_control_session_delete_returns_404(authed_client):
    """The hidden tmux control session is reserved; DELETE must
    refuse rather than wiping ccpipe's event channel."""
    r = authed_client.delete("/api/sessions/__ccpipe_ctrl",
                              headers={"X-Requested-By": "ccpipe"})
    assert r.status_code == 404


def test_control_session_rename_target_rejected(authed_client):
    """Renaming TO the control session name is also blocked."""
    r = authed_client.patch("/api/sessions/something",
                             headers={"X-Requested-By": "ccpipe"},
                             json={"newName": "__ccpipe_ctrl"})
    assert r.status_code == 404


# ── #R5/#R18 Resize clamps ───────────────────────────────────────────────

def test_resize_clamp_rejects_huge_dims():
    """A client sending cols=99999 must clamp to the resize ceiling, not
    crash struct.pack with 'unsigned short out of range' and tear down
    the WS on every reconnect."""
    from ccpipe.ws import _clamp_dim, _RESIZE_MAX
    assert _clamp_dim(99999) == _RESIZE_MAX
    assert _clamp_dim(-1) == 1
    assert _clamp_dim(0) == 1
    assert _clamp_dim(80) == 80


# ── #R11 Credential-version invalidation ─────────────────────────────────

def test_old_session_invalidated_after_credential_change(authed_client):
    """A session minted before the password changes must be rejected
    even though the signed cookie still verifies — otherwise a stolen
    cookie outlasts the credential rotation that's meant to revoke it."""
    # authed_client fixture logs in (so we have an authenticated cookie).
    r = authed_client.get("/api/auth/status")
    assert r.json()["authenticated"] is True

    # Change the password.
    r = authed_client.post("/api/auth/credentials",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"currentPassword": "letmein",
                                  "newPassword": "letmein-v2"})
    assert r.status_code == 200

    # The existing cookie should no longer be considered authenticated;
    # cred_version stored in the session is stale relative to current.
    r = authed_client.get("/api/auth/status")
    assert r.json()["authenticated"] is False


def test_update_credential_rejects_short_password(authed_client):
    r = authed_client.post("/api/auth/credentials",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"currentPassword": "letmein",
                                  "newPassword": "abc"})
    assert r.status_code == 400
    assert "too short" in r.json()["detail"]


def test_update_credential_rejects_same_password(authed_client):
    r = authed_client.post("/api/auth/credentials",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"currentPassword": "letmein",
                                  "newPassword": "letmein"})
    assert r.status_code == 400
    assert "no change" in r.json()["detail"]


# ── #R13 Lowercase sentence boundaries ───────────────────────────────────

def test_sentence_split_handles_lowercase_continuation():
    """Claude's prose sometimes starts a sentence in lowercase
    ("then we did x. and after, …"). The pipelined Kokoro fetcher
    relies on sentence splits to pre-fetch the next chunk; missing
    these means worse time-to-first-audio."""
    from ccpipe.tts import split_sentences
    s = split_sentences("This works fine. and after that, we continue. Then end.")
    assert s == [
        "This works fine.",
        "and after that, we continue.",
        "Then end.",
    ]


def test_sentence_split_still_respects_short_abbreviations():
    """3+ word-chars guard means short abbreviations don't split."""
    from ccpipe.tts import split_sentences
    # "e.g.", "i.e.", "Mr." — none should split.
    assert split_sentences("This is fine e.g. lower-case stuff. Done.") == [
        "This is fine e.g. lower-case stuff.",
        "Done.",
    ]
    assert split_sentences("Hi Mr. Smith. How are you?") == [
        "Hi Mr. Smith.",
        "How are you?",
    ]


# ── #R22 _dispatch logs tracebacks with exc_info ─────────────────────────

async def test_dispatch_logs_real_exc_info(caplog):
    """log.exception used to fire OUTSIDE an except block, which prints
    'NoneType: None' as the traceback. Confirm the new explicit
    exc_info path actually surfaces the real exception."""
    import logging
    from ccpipe.tmux_control import TmuxControlClient, TmuxEvent

    # ccpipe.main flips the ccpipe logger's propagate=False so journald
    # doesn't get duplicate lines. caplog's handler sits on root, so we
    # need to re-enable propagation just for this assertion.
    ccpipe_logger = logging.getLogger("ccpipe")
    prev_propagate = ccpipe_logger.propagate
    ccpipe_logger.propagate = True

    client = TmuxControlClient()

    async def bad(evt):
        raise RuntimeError("kaboom")

    client.subscribe(bad)
    try:
        with caplog.at_level(logging.ERROR, logger="ccpipe.tmux_control"):
            await client._dispatch(TmuxEvent(name="t", args=[], raw="%t"))
    finally:
        ccpipe_logger.propagate = prev_propagate

    # We expect at least one ERROR record whose exc_info points at the
    # real exception class, not None.
    err_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert err_records, "expected at least one ERROR log"
    assert any(r.exc_info and r.exc_info[0] is RuntimeError
                for r in err_records), \
        "real exception not in exc_info; the log.exception-outside-except bug regressed"


# ── Session management endpoints ─────────────────────────────────────────

def test_fs_list_rejects_relative_path(authed_client):
    r = authed_client.get("/api/fs/list?path=foo")
    assert r.status_code == 400
    assert "absolute" in r.json()["detail"]


def test_fs_list_404_on_missing(authed_client):
    r = authed_client.get("/api/fs/list?path=/nonexistent-ccpipe-test-xyz")
    assert r.status_code == 404


def test_fs_list_returns_subdirs_only(authed_client, tmp_path):
    """Browser only navigates directories — files in the response would
    confuse the picker (and the new-session backend rejects them anyway)."""
    (tmp_path / "a-dir").mkdir()
    (tmp_path / "b-dir").mkdir()
    (tmp_path / "a-file.txt").write_text("nope")
    (tmp_path / ".hidden").mkdir()

    r = authed_client.get(f"/api/fs/list?path={tmp_path}")
    assert r.status_code == 200
    body = r.json()
    names = [e["name"] for e in body["entries"]]
    assert names == ["a-dir", "b-dir"]  # sorted, hidden excluded, files excluded


def test_fs_list_show_hidden(authed_client, tmp_path):
    (tmp_path / ".dotted").mkdir()
    (tmp_path / "regular").mkdir()
    r = authed_client.get(f"/api/fs/list?path={tmp_path}&show_hidden=1")
    body = r.json()
    names = [e["name"] for e in body["entries"]]
    assert ".dotted" in names
    assert "regular" in names


def test_claude_sessions_lists_resumable_only(authed_client, tmp_path, monkeypatch):
    """Sessions whose JSONLs sit in the matching projects subdir should be
    returned; sessions currently running on the box (per the live
    ~/.claude/sessions/<pid>.json index) must be filtered OUT so we don't
    tempt the user into resuming a live conversation."""
    import ccpipe.main as m

    fake_home = tmp_path
    monkeypatch.setattr(m.Path, "home", staticmethod(lambda: fake_home))

    proj_cwd = tmp_path / "code" / "myproject"
    proj_cwd.mkdir(parents=True)

    # Two JSONLs live in the encoded projects dir.
    encoded_dir = (
        fake_home / ".claude" / "projects" /
        (str(proj_cwd).replace("/", "-"))
    )
    encoded_dir.mkdir(parents=True)
    a_id = "11111111-2222-3333-4444-555555555555"
    b_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (encoded_dir / f"{a_id}.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"first prompt here"}}\n'
    )
    (encoded_dir / f"{b_id}.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"<local-command-stdout>x</local-command-stdout>"}}\n'
        '{"type":"user","message":{"role":"user","content":"actual question after caveat"}}\n'
    )

    # b_id is "currently running" per the sessions index — should be filtered.
    sessions_dir = fake_home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "12345.json").write_text(
        f'{{"pid": 12345, "sessionId": "{b_id}"}}'
    )

    r = authed_client.get(f"/api/claude-sessions?cwd={proj_cwd}")
    assert r.status_code == 200
    body = r.json()
    ids = [s["id"] for s in body["sessions"]]
    assert a_id in ids
    assert b_id not in ids   # filtered as running
    a_entry = next(s for s in body["sessions"] if s["id"] == a_id)
    assert a_entry["firstUserMessage"] == "first prompt here"


def test_claude_sessions_skips_framework_caveat_messages(authed_client, tmp_path, monkeypatch):
    """The first record in a fresh transcript is often a framework caveat
    wrapped in <local-command-…> tags. _read_first_real_user_message must
    skip those and surface the first plain prompt instead."""
    import ccpipe.main as m

    monkeypatch.setattr(m.Path, "home", staticmethod(lambda: tmp_path))
    proj_cwd = tmp_path / "p"
    proj_cwd.mkdir()
    encoded = (
        tmp_path / ".claude" / "projects" /
        str(proj_cwd).replace("/", "-")
    )
    encoded.mkdir(parents=True)
    (encoded / "11111111-2222-3333-4444-555555555555.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"<local-command-caveat>blah"}}\n'
        '{"type":"user","message":{"role":"user","content":"real prompt"}}\n'
    )

    r = authed_client.get(f"/api/claude-sessions?cwd={proj_cwd}")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["firstUserMessage"] == "real prompt"


def test_create_session_rejects_bad_resume_id(authed_client):
    """resumeSessionId must look like a UUID — defense against shell
    injection into the window_command string."""
    r = authed_client.post("/api/sessions",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"name": "test",
                                  "resumeSessionId": "not-a-uuid; rm -rf /"})
    assert r.status_code == 400
    assert "resumeSessionId" in r.json()["detail"]


def test_create_session_rejects_bad_cwd(authed_client):
    r = authed_client.post("/api/sessions",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"name": "test", "cwd": "relative/path"})
    assert r.status_code == 400
    assert "absolute" in r.json()["detail"]


# ── File-transfer panel endpoints ────────────────────────────────────────

def test_fs_list_with_files_returns_files(authed_client, tmp_path):
    """`files=1` extends the directory-only listing with file entries
    that carry size + mtime."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "hello.txt").write_text("hi")
    r = authed_client.get(f"/api/fs/list?path={tmp_path}&files=1")
    assert r.status_code == 200
    body = r.json()
    names = {(e["name"], e["type"]) for e in body["entries"]}
    assert ("sub", "dir") in names
    assert ("hello.txt", "file") in names
    f_entry = next(e for e in body["entries"] if e["name"] == "hello.txt")
    assert f_entry["size"] == 2


def test_fs_read_write_round_trip(authed_client, tmp_path):
    p = tmp_path / "note.md"
    p.write_text("hello\nworld")
    r = authed_client.get(f"/api/fs/read?path={p}")
    assert r.status_code == 200
    assert r.json()["content"] == "hello\nworld"
    r = authed_client.post("/api/fs/write",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"path": str(p), "content": "new content"})
    assert r.status_code == 200
    assert p.read_text() == "new content"


def test_fs_read_rejects_binary(authed_client, tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\x00\x01\x02\x03" * 100)
    r = authed_client.get(f"/api/fs/read?path={p}")
    assert r.status_code == 415


def test_fs_read_rejects_oversize(authed_client, tmp_path):
    p = tmp_path / "big.txt"
    p.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB > editor cap
    r = authed_client.get(f"/api/fs/read?path={p}")
    assert r.status_code == 413


def test_fs_upload_respects_cap(authed_client, tmp_path, monkeypatch):
    """Cap honoured by /api/fs/upload."""
    import ccpipe.main as m
    monkeypatch.setattr(m.app_config, "load",
                         lambda: type("C", (), {"fs": type("F", (), {"upload_limit_mb": 1})()})())
    payload = b"x" * (2 * 1024 * 1024)
    r = authed_client.post(
        f"/api/fs/upload?path={tmp_path}/big.bin",
        headers={"X-Requested-By": "ccpipe",
                 "Content-Type": "application/octet-stream"},
        content=payload,
    )
    assert r.status_code == 413


def test_fs_upload_round_trip(authed_client, tmp_path):
    payload = b"hello world\n"
    r = authed_client.post(
        f"/api/fs/upload?path={tmp_path}/u.txt",
        headers={"X-Requested-By": "ccpipe",
                 "Content-Type": "application/octet-stream"},
        content=payload,
    )
    assert r.status_code == 200
    assert (tmp_path / "u.txt").read_bytes() == payload


def test_fs_rename_and_delete(authed_client, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi")
    r = authed_client.post("/api/fs/rename",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"src": str(src), "dst": str(tmp_path / "b.txt")})
    assert r.status_code == 200
    assert (tmp_path / "b.txt").exists()
    r = authed_client.post("/api/fs/delete",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"path": str(tmp_path / "b.txt")})
    assert r.status_code == 200
    assert not (tmp_path / "b.txt").exists()


def test_fs_mkdir(authed_client, tmp_path):
    new_dir = tmp_path / "fresh"
    r = authed_client.post("/api/fs/mkdir",
                            headers={"X-Requested-By": "ccpipe"},
                            json={"path": str(new_dir)})
    assert r.status_code == 200
    assert new_dir.is_dir()


def test_fs_download(authed_client, tmp_path):
    p = tmp_path / "dl.bin"
    p.write_bytes(b"deadbeef")
    r = authed_client.get(f"/api/fs/download?path={p}")
    assert r.status_code == 200
    assert r.content == b"deadbeef"
    assert "attachment" in r.headers["content-disposition"]


# ── #19  CSP tightening ──────────────────────────────────────────────────

def test_csp_connect_src_no_longer_includes_wildcard_ws(authed_client):
    """The pre-fix CSP included `connect-src 'self' ws: wss:` which
    allowed connections to ANY ws server. The fix narrows it to 'self'."""
    r = authed_client.get("/api/health")
    csp = r.headers.get("content-security-policy", "")
    assert "connect-src" in csp
    # The scheme-only tokens are out.
    assert " ws:" not in csp
    assert " wss:" not in csp


# ── #21 reconnect debounce is a frontend-only concern; verified by inspection.
