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


# ─── T7: Credentialed WebSocket message-type fuzz ────────────────────────
#
# The threat model (docs/threat-model.md) flagged the post-auth WS
# protocol as the highest-value next-pass target. Questions it asks:
#
#   - Are resize cols/rows bounded?
#   - Is input.data size-limited?
#   - Are unknown text/binary types silently dropped?
#   - Are messages sent before `hello` rejected, or do they reach the mux?
#   - Can a single client open many WS connections?
#   - Does the server crash / OOM under any of this?
#
# These tests pin the answers. Each one asserts "the malformed input
# doesn't crash the server" by verifying /api/health stays responsive
# afterwards. Gated by CCPIPE_TEST_PASSWORD because we need a real
# session cookie; TOTP secret is read from ~/.local/state/ccpipe/
# credentials by default (you can override with CCPIPE_TEST_TOTP_SECRET).
#
# Side effect: tests create a tmux session named "t7-fuzz" running
# claude. The fixture teardown best-effort kills it; if a test crashes
# mid-run the session may linger and you can remove it from the
# session picker.

import json
from pathlib import Path
import pwd


def _system_username() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return "ccpipe"


def _read_totp_from_credentials_file() -> str:
    """Best-effort: read the totp_secret out of ccpipe's own credentials
    file. The file is mode 0600 + owned by the operator, so this only
    works when the test runs AS that operator on the same machine."""
    try:
        creds_path = Path.home() / ".local" / "state" / "ccpipe" / "credentials"
        creds = json.loads(creds_path.read_text())
        secret = creds.get("totp_secret")
        return secret if isinstance(secret, str) else ""
    except Exception:
        return ""


TEST_USERNAME = os.environ.get("CCPIPE_TEST_USERNAME") or _system_username()
TEST_PASSWORD = os.environ.get("CCPIPE_TEST_PASSWORD", "")
TEST_TOTP_SECRET = (
    os.environ.get("CCPIPE_TEST_TOTP_SECRET")
    or _read_totp_from_credentials_file()
)

credentialed_skip = pytest.mark.skipif(
    not TEST_PASSWORD,
    reason="set CCPIPE_TEST_PASSWORD to enable T7 credentialed WS fuzz tests",
)

T7_SESSION_NAME = "t7-fuzz"


@pytest.fixture(scope="module")
def auth_client():
    """Logged-in httpx.Client. One login per module (5 attempts/min cap)."""
    if not TEST_PASSWORD:
        pytest.skip("no test password set")
    import pyotp
    client = httpx.Client(
        timeout=10.0,
        headers={"Host": HOST_HEADER},
        base_url=BASE,
    )
    body = {"username": TEST_USERNAME, "password": TEST_PASSWORD}
    if TEST_TOTP_SECRET:
        body["code"] = pyotp.TOTP(TEST_TOTP_SECRET).now()
    r = client.post(
        "/api/auth/login",
        headers={**JSON_HEADERS},
        json=body,
    )
    assert r.status_code == 200, (
        f"login failed: {r.status_code} {r.text[:200]} — "
        f"check CCPIPE_TEST_PASSWORD and (if TOTP enrolled) "
        f"CCPIPE_TEST_TOTP_SECRET or credentials file"
    )
    yield client
    # Best-effort teardown: nuke the t7-fuzz tmux session + logout.
    try:
        client.delete(
            f"/api/sessions/{T7_SESSION_NAME}",
            headers=CSRF,
        )
    except Exception:
        pass
    try:
        client.post("/api/auth/logout", headers=CSRF)
    except Exception:
        pass
    client.close()


def _ws_connect_with_host_override(*, cookie_header: str, origin: str):
    """Open a WebSocket carrying the auth cookie + an explicit Host
    header for TrustedHostMiddleware.

    The websockets library auto-derives the Host header from the URI,
    so a naive ``ws://127.0.0.1:8080/...`` sends ``Host: 127.0.0.1:8080``
    which the prod-style TrustedHostMiddleware rejects with HTTP 400.
    Fix: open a raw TCP socket to the BASE host:port, pass it to
    websockets via ``sock=``, and give the library a URI whose
    hostname IS the trusted host. The library then sends
    ``Host: <HOST_HEADER>`` on the upgrade request and the middleware
    is happy, while the actual TCP connection still goes to BASE."""
    from websockets.sync.client import connect as ws_connect
    parsed = urlparse(BASE)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    raw_sock = socket.create_connection((host, port), timeout=10)
    if parsed.scheme == "https":
        ctx = ssl.create_default_context()
        raw_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    # URI uses HOST_HEADER so the lib sends Host: ccpipe.example.com
    ws_uri = f"{ws_scheme}://{HOST_HEADER}/ws?session={T7_SESSION_NAME}"
    return ws_connect(
        ws_uri,
        sock=raw_sock,
        additional_headers={
            "Cookie": cookie_header,
            "Origin": origin,
        },
    )


TEST_ORIGIN = os.environ.get("CCPIPE_TEST_ORIGIN") or f"https://{HOST_HEADER}"
# ^^^ Default to https-scheme origin because production deployments
# set CCPIPE_ALLOWED_ORIGINS=https://<host>. Even when we're hitting
# the dev backend over plain http://127.0.0.1:8080 (to avoid the
# Cloudflare/nginx layer), the server still validates Origin against
# its allowlist, which is https in any BEHIND_TLS deployment. Override
# with CCPIPE_TEST_ORIGIN for a different setup.


def _open_ws(auth_client: httpx.Client, *, origin: str | None = None):
    """Open a WebSocket carrying the auth cookie. Origin must match
    the server's allowlist (CCPIPE_ALLOWED_ORIGINS or Host fallback)."""
    cookies = "; ".join(f"{c.name}={c.value}" for c in auth_client.cookies.jar)
    return _ws_connect_with_host_override(
        cookie_header=cookies,
        origin=origin or TEST_ORIGIN,
    )


def _read_until_hello(ws, timeout: float = 3.0):
    """Drain frames until we see the server's hello JSON message. The
    server sends hello AFTER it has waited briefly for our initial
    resize, so we send one first to unblock that wait."""
    ws.send(json.dumps({"type": "resize", "cols": 80, "rows": 24}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ws.recv(timeout=0.5)
        except TimeoutError:
            continue
        except Exception:
            break
        # We don't need to parse — once anything has flowed back, the
        # hello side of the handshake has fired. The pump-loop is
        # active and the server is in steady-state.
        return


def _health_ok() -> bool:
    """Verify /api/health responds 200 within a reasonable window."""
    with _client() as c:
        r = _get(c, "/api/health")
        return r.status_code == 200


# ── Resize fuzz ──────────────────────────────────────────────────────────

@credentialed_skip
@pytest.mark.parametrize("cols,rows", [
    (2**31, 100),                   # huge int
    (-1, 100),                      # negative
    (0, 0),                         # zero
    (1.5, 2.7),                     # floats
    ("notanumber", 100),            # wrong type (string)
    (None, None),                   # null
    ([1, 2, 3], {"a": 1}),          # objects/arrays
])
def test_ws_resize_malformed_does_not_crash(auth_client, cols, rows):
    """Server must clamp / ignore malformed resize values, not crash."""
    with _open_ws(auth_client) as ws:
        _read_until_hello(ws)
        ws.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))
        # Send a ping to confirm the WS is still alive.
        ws.send(json.dumps({"type": "ping"}))
        # Read until pong (or timeout). Any reply proves the receive
        # loop is still running.
        ws.recv(timeout=2.0)
    assert _health_ok(), "/api/health failed after malformed resize"


# ── Input-data fuzz ──────────────────────────────────────────────────────

@credentialed_skip
def test_ws_input_huge_string_does_not_crash(auth_client):
    """A large input.data payload should be either accepted, truncated,
    or rejected — but never crash the server. PTY write buffer is
    capped at 4 MiB; the WS lib's max_size default is 1 MiB so the
    frame itself is bounded before reaching our handler."""
    with _open_ws(auth_client) as ws:
        _read_until_hello(ws)
        # 500 KB — below the websockets default 1 MiB frame cap.
        ws.send(json.dumps({"type": "input", "data": "A" * 500_000}))
        ws.send(json.dumps({"type": "ping"}))
        ws.recv(timeout=2.0)
    assert _health_ok()


@credentialed_skip
@pytest.mark.parametrize("data", [
    None,
    123,
    [1, 2, 3],
    {"nested": "object"},
    True,
])
def test_ws_input_wrong_type_does_not_crash(auth_client, data):
    """input.data must be a string; anything else is silently dropped."""
    with _open_ws(auth_client) as ws:
        _read_until_hello(ws)
        ws.send(json.dumps({"type": "input", "data": data}))
        ws.send(json.dumps({"type": "ping"}))
        ws.recv(timeout=2.0)
    assert _health_ok()


# ── Unknown message-type fuzz ────────────────────────────────────────────

@credentialed_skip
@pytest.mark.parametrize("msg", [
    {"type": "shell", "cmd": "id"},        # unknown type, common name
    {"type": "exec", "data": "ls /"},      # ditto
    {"type": "__proto__"},                  # prototype-pollution probe
    {"type": "input"},                      # missing data field
    {"type": ""},                           # empty type
    {"type": None},                         # null type
    {"type": ["array", "type"]},            # type as array
    {},                                     # no type field
    {"no_type_key": "x"},                   # no recognisable shape
])
def test_ws_unknown_message_types_are_silently_dropped(auth_client, msg):
    with _open_ws(auth_client) as ws:
        _read_until_hello(ws)
        ws.send(json.dumps(msg))
        ws.send(json.dumps({"type": "ping"}))
        ws.recv(timeout=2.0)
    assert _health_ok()


# ── Malformed JSON / non-JSON text frames ────────────────────────────────

@credentialed_skip
@pytest.mark.parametrize("text", [
    "not valid json",
    "",
    "{",
    "null",
    '"just a string"',
    "[1,2,3",
    '\x00\x01\x02',
])
def test_ws_non_json_text_does_not_crash(auth_client, text):
    """Text frames that don't parse as JSON should be logged + ignored."""
    with _open_ws(auth_client) as ws:
        _read_until_hello(ws)
        ws.send(text)
        ws.send(json.dumps({"type": "ping"}))
        ws.recv(timeout=2.0)
    assert _health_ok()


# ── Binary frame fuzz ────────────────────────────────────────────────────

@credentialed_skip
@pytest.mark.parametrize("payload", [
    b"",                            # empty
    b"\x99",                        # unknown prefix only
    b"\x99" + b"A" * 1000,          # unknown prefix + data
    b"\x00",                        # FRAME_PTY_OUTPUT prefix (server→client only, here as garbage)
    b"\x02" + b"audio?",            # FRAME_TTS_AUDIO prefix (server→client only)
    bytes(range(256)),              # every byte value as the "prefix"
])
def test_ws_unknown_binary_frames_dropped(auth_client, payload):
    """Binary frames must be dispatched by their first byte; unknown
    prefixes get a log line and are dropped, not crashed on."""
    with _open_ws(auth_client) as ws:
        _read_until_hello(ws)
        ws.send(payload)
        ws.send(json.dumps({"type": "ping"}))
        ws.recv(timeout=2.0)
    assert _health_ok()


@credentialed_skip
def test_ws_mic_frame_oversized_rate_limited(auth_client):
    """A burst of mic frames exceeding the per-second budget must be
    silently dropped, not crash the limiter."""
    with _open_ws(auth_client) as ws:
        _read_until_hello(ws)
        # FRAME_MIC_PCM prefix (0x01) + 31 KB of payload, repeated 50x
        # — far above the 1 MiB/s budget. Server should drop the
        # excess; no crash.
        for _ in range(50):
            ws.send(b"\x01" + b"\x00" * 31_000)
        ws.send(json.dumps({"type": "ping"}))
        ws.recv(timeout=3.0)
    assert _health_ok()


# ── Pre-hello frames ─────────────────────────────────────────────────────

@credentialed_skip
def test_ws_pre_hello_input_is_processed(auth_client):
    """The receive loop processes non-resize text frames sent BEFORE
    the server emits hello (the server queues them as `leftover` then
    drains after PTY spawn). Verify this doesn't crash — even though
    semantically the client shouldn't send input before knowing the
    session is ready, the user is already authenticated so this is
    a quirk, not an escalation."""
    with _open_ws(auth_client) as ws:
        # Send input + ping IMMEDIATELY — before the initial resize
        # round-trip. The server should buffer them and apply once
        # the PTY is up. Then send the initial resize to unblock the
        # handshake.
        ws.send(json.dumps({"type": "input", "data": "echo pre-hello\n"}))
        ws.send(json.dumps({"type": "ping"}))
        ws.send(json.dumps({"type": "resize", "cols": 80, "rows": 24}))
        # Pump for a moment so anything in flight settles.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                ws.recv(timeout=0.3)
            except TimeoutError:
                break
    assert _health_ok()


# ── Many concurrent WS connections ──────────────────────────────────────

@credentialed_skip
@pytest.mark.slow
def test_ws_many_concurrent_connections_dont_kill_server(auth_client):
    """Open N WS connections in parallel from one authenticated client.
    All should accept; /api/health must stay responsive. Each connection
    attaches another tmux client to the t7-fuzz session — bounded by
    PTY / FD limits, but a single-digit-count must not OOM the server."""
    cookies = "; ".join(f"{c.name}={c.value}" for c in auth_client.cookies.jar)
    origin = TEST_ORIGIN

    n_conns = 10
    sockets = []
    try:
        for _ in range(n_conns):
            ws = _ws_connect_with_host_override(
                cookie_header=cookies, origin=origin,
            )
            ws.send(json.dumps({"type": "resize", "cols": 80, "rows": 24}))
            sockets.append(ws)
        # All open. Health still good?
        assert _health_ok()
    finally:
        for ws in sockets:
            try: ws.close()
            except Exception: pass


# ── Ping flood ──────────────────────────────────────────────────────────

@credentialed_skip
def test_ws_ping_flood_handled(auth_client):
    """A flood of pings exercises the session-auth-recheck path (which
    fires on every ping). Server must keep up + send pongs without
    crashing."""
    with _open_ws(auth_client) as ws:
        _read_until_hello(ws)
        n = 200
        for _ in range(n):
            ws.send(json.dumps({"type": "ping"}))
        # Drain — we don't need all N pongs, just confirm the receive
        # loop is still healthy after the burst.
        deadline = time.monotonic() + 5.0
        replies = 0
        while time.monotonic() < deadline and replies < 5:
            try:
                ws.recv(timeout=0.3)
                replies += 1
            except TimeoutError:
                break
        assert replies > 0, "no replies after ping flood"
    assert _health_ok()
