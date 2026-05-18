"""ccpipe — live-HTTP security regression suite.

Pins the security properties verified across pass-1 / pass-2 / pass-3
of the external review so they can't regress silently. Unlike
``test_review_fixes.py`` which runs against the in-process FastAPI
TestClient, this file talks to a **real running instance** over HTTP —
so it also catches deployment-layer regressions (nginx config, systemd
drop-ins, reverse-proxy header injection, etc.) that an in-process
test can't see.

Default behaviour: ``pytest`` skips the whole module when
``CCPIPE_EXTERNAL_BASE`` is not set, so CI and dev runs are unaffected.

Usage:

    # Run against a local uvicorn-direct dev instance
    CCPIPE_EXTERNAL_BASE=http://localhost:8080 \
        CCPIPE_EXTERNAL_HOST=ccpipe.example.com \
        pytest -v backend/tests/test_external_security.py

    # Include rate-limit tests (will lock the operator's IP out
    # for ~60s — only use against a non-production instance):
    CCPIPE_EXTERNAL_BASE=http://localhost:8080 \
        CCPIPE_ALLOW_DESTRUCTIVE_TESTS=1 \
        pytest -v backend/tests/test_external_security.py

Origin: this file is an adapted copy of
``ccpipe-pentest/findings/security_tests.py``, ported from requests →
httpx (already a project dep), with module-level skip gating so
normal pytest runs ignore it.
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

import httpx
import pytest

BASE = os.environ.get("CCPIPE_EXTERNAL_BASE", "").rstrip("/")
HOST_HEADER = os.environ.get("CCPIPE_EXTERNAL_HOST", "ccpipe.example.com")
DESTRUCTIVE = os.environ.get("CCPIPE_ALLOW_DESTRUCTIVE_TESTS", "").lower() in (
    "1", "true", "yes", "on",
)

# Skip the whole module unless the operator has opted in by setting
# CCPIPE_EXTERNAL_BASE. This file talks to a real HTTP server; in the
# default pytest run we want it ignored entirely.
pytestmark = pytest.mark.skipif(
    not BASE, reason="set CCPIPE_EXTERNAL_BASE to enable external security tests",
)

CSRF = {"X-Requested-By": "ccpipe"}
JSON_HEADERS = {"Content-Type": "application/json", **CSRF}


def _client() -> httpx.Client:
    # Long-ish timeout so slowloris-probe failures show up as assertion
    # failures rather than socket timeouts.
    return httpx.Client(timeout=10.0, headers={"Host": HOST_HEADER})


def _post(client: httpx.Client, path: str, body=None, headers=None, raw=False):
    h = dict(JSON_HEADERS)
    if headers: h.update(headers)
    if raw and isinstance(body, (bytes, str)):
        return client.post(BASE + path, headers=h, content=body)
    return client.post(BASE + path, headers=h, json=body)


def _get(client: httpx.Client, path: str, headers=None):
    return client.get(BASE + path, headers=headers)


# ─── Static fingerprint / surface ────────────────────────────────────────

def test_fastapi_docs_disabled():
    """FastAPI auto-generated docs must not be exposed (pass-1 #1)."""
    with _client() as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert _get(c, path).status_code == 404, f"{path} must be 404 in production"


def test_security_txt_present():
    """`/.well-known/security.txt` provides a disclosure contact (pass-2 #14)."""
    with _client() as c:
        r = _get(c, "/.well-known/security.txt")
        assert r.status_code == 200
        assert "Contact:" in r.text


def test_health_unauth():
    """`/api/health` is the only unauth GET that returns data; payload is fixed."""
    with _client() as c:
        r = _get(c, "/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ─── Auth gate — every /api/* (except whitelist) requires auth or CSRF ──

@pytest.mark.parametrize("path", [
    "/api/sessions",
    "/api/claude-sessions?cwd=/tmp",
    "/api/fs/config",
    "/api/fs/list?path=/",
    "/api/fs/read?path=/etc/hosts",
    "/api/fs/download?path=/etc/hosts",
    "/api/tts/voices",
    "/api/tts/config",
    "/api/tts/preview?voice=x&text=y",
])
def test_unauth_get_endpoints_require_auth(path):
    with _client() as c:
        r = _get(c, path)
        assert r.status_code == 401, f"{path} returned {r.status_code} unauth"


@pytest.mark.parametrize("path,body", [
    ("/api/auth/credentials", {"currentPassword": "x"}),
    ("/api/auth/totp/enroll",  {"currentPassword": "x"}),
    ("/api/auth/totp/confirm", {"currentPassword": "x", "secret": "x", "code": "1"}),
    ("/api/auth/totp/disable", {"currentPassword": "x", "code": "1"}),
    ("/api/sessions",          {"name": "x", "cwd": "/tmp"}),
    ("/api/fs/write",          {"path": "/tmp/x", "content": "y"}),
    ("/api/fs/mkdir",          {"path": "/tmp/x"}),
    ("/api/fs/delete",         {"path": "/tmp/x"}),
    ("/api/fs/rename",         {"src": "/tmp/x", "dst": "/tmp/y"}),
    ("/api/tts/config",        {"voice": "x"}),
    ("/api/tts/speak",         {"text": "x"}),
])
def test_unauth_post_endpoints_require_auth(path, body):
    with _client() as c:
        r = _post(c, path, body)
        assert r.status_code == 401, f"{path} returned {r.status_code} unauth"


# ─── CSRF protection ─────────────────────────────────────────────────────

def test_login_requires_csrf_header():
    """Pass-1: POST /api/auth/login must require X-Requested-By header."""
    with httpx.Client(timeout=10.0) as c:
        r = c.post(
            BASE + "/api/auth/login",
            headers={"Host": HOST_HEADER, "Content-Type": "application/json"},
            json={"username": "x", "password": "y"},
        )
        assert r.status_code == 403
        assert "csrf" in r.json().get("detail", "").lower()


@pytest.mark.parametrize("ct", [
    "text/plain", "application/x-www-form-urlencoded", "multipart/form-data",
    "text/json", "",
])
def test_login_csrf_rejects_alternate_content_types(ct):
    """CORS-simple Content-Types should still require the CSRF header."""
    with httpx.Client(timeout=10.0) as c:
        r = c.post(
            BASE + "/api/auth/login",
            headers={"Host": HOST_HEADER, "Content-Type": ct},
            content='{"username":"x","password":"y"}',
        )
        assert r.status_code in (403, 400, 422), (
            f"CT={ct!r} returned {r.status_code} unexpectedly"
        )


# ─── Login wire — no password-correctness leak (pass-1 #4) ──────────────

def test_login_returns_identical_response_with_or_without_code():
    """Login must reply identically for {u,p} and {u,p,code}; no early signal."""
    with _client() as c:
        r1 = _post(c, "/api/auth/login", {"username": "x", "password": "y"})
        r2 = _post(c, "/api/auth/login", {"username": "x", "password": "y", "code": "000000"})
        assert r1.status_code == r2.status_code == 401
        assert r1.json() == r2.json() == {"detail": "invalid credentials"}


def test_auth_status_does_not_leak_otp_enrolled_pre_login():
    """`/api/auth/status` should not expose `otp_enrolled` to anonymous callers
    (pass-1 #3)."""
    with _client() as c:
        r = _get(c, "/api/auth/status")
        body = r.json()
        assert body.get("otp_enrolled") in (None, False), (
            "otp_enrolled must not be True for an unauthenticated caller"
        )


# ─── Rate limiter ────────────────────────────────────────────────────────
#
# These tests trip the per-IP login throttle and then SLEEP 65s to let the
# bucket drain. They lock the operator's IP out for that duration, so we
# require explicit opt-in via CCPIPE_ALLOW_DESTRUCTIVE_TESTS=1.

_destructive_skip = pytest.mark.skipif(
    not DESTRUCTIVE,
    reason="trips rate limit + sleeps 65s; set CCPIPE_ALLOW_DESTRUCTIVE_TESTS=1",
)


@_destructive_skip
def test_login_rate_limit_triggers():
    """5–10 failed logins in quick succession should produce 429s."""
    codes = []
    with _client() as c:
        for _ in range(10):
            r = _post(c, "/api/auth/login", {"username": "x", "password": "y"})
            codes.append(r.status_code)
    assert 429 in codes, f"Expected 429 in {codes}"
    time.sleep(65)


@_destructive_skip
def test_login_rate_limit_is_per_ip_not_per_username():
    """Limit must count attempts across usernames (pass-3); ensures attacker
    can't cycle usernames to bypass."""
    codes = []
    with _client() as c:
        for u in "abcdefghij":
            r = _post(c, "/api/auth/login", {"username": u, "password": "x"})
            codes.append(r.status_code)
    assert 429 in codes, f"Per-username pivot bypassed limiter: {codes}"
    time.sleep(65)


# ─── JSON parser hardening (pass-2 #9, pass-3 #17) ──────────────────────

@pytest.mark.parametrize("body,description", [
    (b'{' + b'"a":{' * 1000 + b'}' * 1001, "1000-deep object"),
    (b'[' * 1000 + b']' * 1000,             "1000-deep array"),
])
def test_json_depth_dos_returns_4xx_not_500(body, description):
    """Pre-auth JSON depth attacks must not bubble to HTTP 500 (pass-2 #9)."""
    with _client() as c:
        r = _post(c, "/api/auth/login", body, raw=True)
        assert 400 <= r.status_code < 500, (
            f"{description}: got {r.status_code}; body={r.text[:200]!r}"
        )


@pytest.mark.parametrize("body,description", [
    ('{"username":NaN,"password":"a"}',       "NaN value"),
    ('{"username":-Infinity,"password":"a"}', "-Infinity value"),
    ('{"username":1e1000,"password":"a"}',    "float overflow to inf"),
    ('{"username":"\\ud834","password":"a"}', "lone Unicode surrogate"),
])
def test_json_nonstandard_values_return_4xx_not_500(body, description):
    """Pre-auth JSON with non-standard JSON values must not 500 (pass-3 #17)."""
    with _client() as c:
        r = _post(c, "/api/auth/login", body, raw=True)
        assert 400 <= r.status_code < 500, (
            f"{description}: got {r.status_code}; body={r.text[:200]!r}"
        )


def test_login_body_size_capped():
    """Pass-2 fix: oversized bodies on /login must be rejected pre-parse."""
    with _client() as c:
        r = _post(c, "/api/auth/login", b"A" * (1 << 16), raw=True)  # 64 KB
        assert r.status_code in (400, 413, 422), (
            f"Expected 413 for oversize body, got {r.status_code}"
        )


# ─── Caching of cookie-bound responses (pass-2 #10) ─────────────────────

@pytest.mark.parametrize("path", ["/api/auth/status", "/api/sessions"])
def test_cookie_vary_endpoints_set_private_cache(path):
    with _client() as c:
        r = _get(c, path)
        vary = r.headers.get("Vary", "").lower()
        cc = r.headers.get("Cache-Control", "").lower()
        if "cookie" in vary:
            assert "no-store" in cc or "private" in cc, (
                f"{path}: Vary:Cookie present but Cache-Control={cc!r}"
            )


# ─── Security headers (deployment-layer headers excluded — see HSTS memo) ─

REQUIRED_APP_HEADERS = {
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options":  "nosniff",
    "permissions-policy":      "microphone=",
}


@pytest.mark.parametrize("name,must_contain", REQUIRED_APP_HEADERS.items())
def test_security_headers_set_by_app(name, must_contain):
    """App-side security headers (excluding XFO + HSTS handled by proxy)."""
    with _client() as c:
        r = _get(c, "/api/health")
        val = r.headers.get(name, "").lower()
        assert must_contain.lower() in val, f"{name}: missing or wrong; got {val!r}"


def test_csp_locks_down_third_party_origins():
    """CSP must keep script-src to 'self' only (no third-party scripts)."""
    with _client() as c:
        r = _get(c, "/api/health")
        csp = r.headers["content-security-policy"].lower()
        assert "script-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp


# ─── Host allow-list ────────────────────────────────────────────────────

def test_host_header_allowlist_enforced():
    """The app must reject requests with unexpected Host headers."""
    with httpx.Client(timeout=5.0) as c:
        for bad in ("evil.example", "localhost", "127.0.0.1"):
            r = c.get(BASE + "/api/health", headers={"Host": bad})
            assert r.status_code in (400, 404, 421), (
                f"Host={bad!r} accepted with {r.status_code}"
            )


# ─── WebSocket auth gate ────────────────────────────────────────────────

def test_websocket_rejects_unauth():
    """`/ws` must return 403 to unauthenticated clients."""
    parsed = urlparse(BASE)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET /ws?session=test HTTP/1.1\r\n"
        f"Host: {HOST_HEADER}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode()
    if parsed.scheme == "https":
        ctx = ssl.create_default_context()
        s = ctx.wrap_socket(socket.create_connection((host, port), timeout=5),
                            server_hostname=host)
    else:
        s = socket.create_connection((host, port), timeout=5)
    try:
        s.sendall(req)
        line = b""
        while not line.endswith(b"\r\n"):
            line += s.recv(1)
    finally:
        s.close()
    assert b"403" in line, f"WS upgrade unexpectedly: {line!r}"


# ─── Method routing ─────────────────────────────────────────────────────

@pytest.mark.parametrize("method", [
    "OPTIONS", "PATCH", "PUT", "DELETE", "TRACE", "CONNECT",
])
def test_login_endpoint_only_accepts_post(method):
    with httpx.Client(timeout=5.0) as c:
        r = c.request(method, BASE + "/api/auth/login",
                      headers={"Host": HOST_HEADER})
        assert r.status_code == 405


# ─── Slowloris-style concurrent connections — informational ────────────

@pytest.mark.slow
def test_concurrent_slow_connections_do_not_block_health():
    """50 half-open POSTs must not prevent /api/health responding within 2s."""
    parsed = urlparse(BASE)
    host = parsed.hostname
    port = parsed.port or 80
    socks = []

    def hold():
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.sendall(b"POST /api/auth/login HTTP/1.1\r\n"
                      b"Host: " + HOST_HEADER.encode() + b"\r\n"
                      b"Content-Type: application/json\r\n"
                      b"X-Requested-By: ccpipe\r\n"
                      b"Content-Length: 100\r\n\r\n")
            socks.append(s)
        except Exception:
            pass

    threads = [threading.Thread(target=hold) for _ in range(50)]
    for t in threads: t.start()
    for t in threads: t.join()
    try:
        with _client() as c:
            r = _get(c, "/api/health")
            assert r.status_code == 200
            assert r.elapsed.total_seconds() < 2.0
    finally:
        for s in socks:
            try: s.close()
            except Exception: pass
