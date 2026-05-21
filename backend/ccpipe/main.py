"""FastAPI app factory.

Most concrete routes live in the ``routes/`` package; this file is the
glue that wires them up alongside middleware, the WebSocket endpoint,
and the lifespan that starts/stops the long-lived background services
(tmux control client, TTS watchdog).
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from . import tmux
from .auth import (
    authorize_websocket,
    behind_tls,
    get_credential,
    load_or_create_secret,
)
from .settings_patch import patch_keybindings_safe, patch_settings_safe, should_apply
from .tmux_control import CONTROL_SESSION_NAME, control_client
from .tmux_setup import apply_server_defaults
from .tts import tts_service
from .ws import handle_terminal_ws

# Surface our INFO messages in the systemd journal. uvicorn installs
# handlers on its own loggers but not on root, so ccpipe.* messages
# propagate up to root and fall through to lastResort (WARNING). Attach
# our own StreamHandler so anything from ccpipe.* lands in stderr, which
# systemd captures into the journal.
def _configure_ccpipe_logging() -> None:
    import sys
    level = os.environ.get("CCPIPE_LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger("ccpipe")
    logger.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(h)
    # Don't propagate to root — uvicorn's handler doesn't filter by
    # logger name and we'd get duplicate lines.
    logger.propagate = False

_configure_ccpipe_logging()

log = logging.getLogger(__name__)


def _warn_if_tls_with_public_bind() -> None:
    """When the operator has set CCPIPE_BEHIND_TLS=1 we expect TLS
    termination at nginx in front of ccpipe. The documented deployment
    binds uvicorn to 0.0.0.0 (nginx is off-host), which means the
    backend HTTP listener is also reachable to the LAN — bypassing
    TLS. Loudly remind the operator to firewall the port so only the
    nginx host can reach it.

    We can't reliably introspect uvicorn's --host from inside the
    process, so the warning fires whenever BEHIND_TLS is on; if the
    operator has already restricted :8080 (iptables, ufw, an internal
    interface bind) the warning is harmless noise."""
    if not behind_tls():
        return
    bar = "─" * 64
    log.warning(bar)
    log.warning("  CCPIPE_BEHIND_TLS=1 — TLS is terminating at the reverse proxy.")
    log.warning("  If uvicorn is bound to 0.0.0.0, ensure the backend HTTP")
    log.warning("  listener is firewalled to the nginx host only. Otherwise a")
    log.warning("  LAN attacker can hit ccpipe directly over plaintext HTTP")
    log.warning("  and bypass TLS entirely.")
    log.warning("  Suggested rule (ufw): ufw deny in on <iface> to any port 8080")
    log.warning("                        ufw allow from <nginx-host> to any port 8080")
    log.warning(bar)


def _warn_if_tls_with_open_host_validation() -> None:
    """Under TLS the operator is expected to pin both the Host header
    (``CCPIPE_TRUSTED_HOSTS``) and the WS Origin allowlist
    (``CCPIPE_ALLOWED_ORIGINS``). When either is missing or wildcarded
    the matching defense is silently disabled — TrustedHostMiddleware
    accepts ``Host: anything.attacker.example`` and the WS Origin gate
    falls back to that same Host header. Warn loudly so the operator
    doesn't deploy with a soft gate by accident."""
    if not behind_tls():
        return
    trusted_raw = os.environ.get("CCPIPE_TRUSTED_HOSTS", "").strip()
    trusted = [h.strip() for h in trusted_raw.split(",") if h.strip()]
    open_hosts = (not trusted) or ("*" in trusted)
    open_origins = not os.environ.get("CCPIPE_ALLOWED_ORIGINS", "").strip()
    if not (open_hosts or open_origins):
        return
    bar = "─" * 64
    log.warning(bar)
    log.warning("  CCPIPE_BEHIND_TLS=1 but host/origin validation is OPEN.")
    if open_hosts:
        log.warning("    CCPIPE_TRUSTED_HOSTS=%r → Host header is NOT validated.",
                    trusted_raw or "(unset)")
    if open_origins:
        log.warning("    CCPIPE_ALLOWED_ORIGINS unset → WS upgrades trust the")
        log.warning("    request's Host header verbatim (softer gate).")
    log.warning("  Set both to your public hostname, e.g.:")
    log.warning("    Environment=CCPIPE_TRUSTED_HOSTS=ccpipe.example.com")
    log.warning("    Environment=CCPIPE_ALLOWED_ORIGINS=https://ccpipe.example.com")
    log.warning(bar)


def _warn_if_multi_worker() -> None:
    """Loud-warn if ccpipe is starting with more than one worker process.

    Several pieces of module-level state are per-process by design and
    silently desync across workers:

      - login rate-limit buckets (``routes/auth.py:_login_attempts`` and
        ``_global_login_attempts``) — effective limit becomes N× the
        documented cap, which materially weakens brute-force defence
      - the credential cache (``auth.py:_cached_credential``) — a UI
        password change writes the file + resets one worker's cache, but
        the other workers continue serving with their own stale entries
        until each one's first cache miss
      - the mic FIFO writer + ownership tag (``ws.py:_mic_writer``,
        ``_mic_owner``) — concurrent ``/voice`` sessions on different
        workers will interleave audio in the system-wide pipe
      - the TTS Watchdog observer (``tts.py:tts_service``) — each worker
        opens its own observer on ``~/.claude/projects``, so a single
        assistant message produces N duplicate utterances
      - the tmux control client (``tmux_control.py:control_client``) —
        each worker maintains an independent ``tmux -C`` connection;
        event dispatch becomes nondeterministic

    Detection is best-effort: uvicorn doesn't surface worker count to
    the ASGI app, so we sniff ``WEB_CONCURRENCY`` (Gunicorn-style) and
    ``--workers`` in ``sys.argv``. Misses some setups but catches the
    common shapes."""
    worker_count = 1
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    if raw:
        try:
            worker_count = max(worker_count, int(raw))
        except ValueError:
            pass
    for i, arg in enumerate(sys.argv):
        if arg == "--workers" and i + 1 < len(sys.argv):
            try:
                worker_count = max(worker_count, int(sys.argv[i + 1]))
            except ValueError:
                pass
        elif arg.startswith("--workers="):
            try:
                worker_count = max(worker_count, int(arg.split("=", 1)[1]))
            except ValueError:
                pass
    if worker_count <= 1:
        return
    bar = "─" * 64
    log.warning(bar)
    log.warning("  ccpipe is starting with %d worker processes.", worker_count)
    log.warning("  Module-level state is per-process; running > 1 worker")
    log.warning("  silently desyncs the login throttle (effective limit N× the")
    log.warning("  documented cap), the credential cache (UI password changes")
    log.warning("  take time to propagate across workers), the mic FIFO writer")
    log.warning("  (concurrent /voice interleaves audio), the TTS observer")
    log.warning("  (duplicate utterances), and the tmux control client.")
    log.warning("  Recommended: drop --workers / WEB_CONCURRENCY back to 1.")
    log.warning(bar)


async def _restore_sticky_sessions() -> None:
    """Recreate any sticky session whose tmux side has vanished
    (typically a fresh boot, or `tmux kill-server`). Idempotent —
    sessions that already exist are skipped. Restore uses
    ``claude --continue`` so claude itself picks up the most recent
    conversation for the cwd; on claude exit we drop to an
    interactive shell, same as freshly-created sessions."""
    from . import sticky as _sticky
    from . import tmux as _tmux
    entries = _sticky.load()
    if not entries:
        return
    restore_cmd = _sticky.build_restore_command()
    for name, info in entries.items():
        cwd = info.get("cwd")
        if not cwd:
            continue
        try:
            if await _tmux.session_exists(name):
                continue
            await _tmux.create_session(name, command=restore_cmd, cwd=cwd)
            log.info("sticky: restored session %r in %s", name, cwd)
        except Exception:
            log.exception("sticky: failed to restore session %r", name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly resolve (or generate + persist) credentials so the operator
    # can see them in the journal right away rather than on first login.
    get_credential()
    _warn_if_tls_with_public_bind()
    _warn_if_tls_with_open_host_validation()
    _warn_if_multi_worker()
    # Reset the login throttle on every app launch. Module-level state
    # in routes.auth survives importlib.reload(main); without this, tests
    # that re-import main accumulate hits across runs and trip the global
    # 429 well before they should.
    from .routes.auth import reset_throttle_state
    reset_throttle_state()
    if should_apply():
        patch_settings_safe()
        patch_keybindings_safe()
    await apply_server_defaults()
    await _restore_sticky_sessions()
    await control_client.start()
    if os.environ.get("CCPIPE_TTS", "off").lower() in ("kokoro", "on", "1", "true"):
        tts_service.set_enabled(True)
        await tts_service.start()
    else:
        tts_service.set_enabled(False)
    try:
        yield
    finally:
        await tts_service.stop()
        await control_client.stop()


# Disable the OpenAPI surface (/docs, /redoc, /openapi.json) by default
# so an unauthenticated visitor can't enumerate every route, body schema,
# and Pydantic model name — pre-auth knowledge that materially helps an
# attacker map the API. The dev workflow can opt back in with
# CCPIPE_ENABLE_DOCS=1; everything else stays as-is.
_DOCS_ENABLED = os.environ.get("CCPIPE_ENABLE_DOCS", "").strip().lower() in (
    "1", "true", "yes", "on",
)
app = FastAPI(
    title="ccpipe",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

# When behind TLS, harden cookie attrs and pin trusted hosts. The
# CCPIPE_BEHIND_TLS env toggle exists so the default LAN-HTTP path
# isn't broken (Secure-flagged cookies wouldn't survive an HTTP page).
_BEHIND_TLS = behind_tls()
app.add_middleware(
    SessionMiddleware,
    secret_key=load_or_create_secret(),
    # __Host- requires Secure + Path=/; only safe when TLS is in front.
    session_cookie="__Host-ccpipe_session" if _BEHIND_TLS else "ccpipe_session",
    same_site="lax",
    https_only=_BEHIND_TLS,
    max_age=60 * 60 * 24 * 30,   # 30 days
)
if _BEHIND_TLS:
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    allowed = [h.strip() for h in os.environ.get(
        "CCPIPE_TRUSTED_HOSTS", "*").split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)


# ─── Pre-auth body-size cap (DoS hardening) ───────────────────────────────
# Pass-2 review finding #9: a deeply nested JSON body (~6 KB, ~990 levels)
# on POST /api/auth/login tripped an unhandled RecursionError → 500,
# generated BEFORE the rate-limit check so it didn't even cost the
# attacker their attempt budget. A realistic login body is <500 bytes;
# anything above a few KB on this endpoint is an attack. Reject early
# with 413 so the JSON parser never sees the payload.
_AUTH_LOGIN_BODY_CAP = 4 * 1024  # 4 KiB — generous over any real body


@app.middleware("http")
async def _cap_auth_login_body(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/api/auth/login":
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > _AUTH_LOGIN_BODY_CAP:
                    return PlainTextResponse(
                        "request body too large",
                        status_code=413,
                        headers={"Content-Type": "text/plain"},
                    )
            except ValueError:
                # Malformed Content-Length — let the server reject it.
                pass
    return await call_next(request)


# Belt-and-braces: if a payload ever slips past the size guard (chunked
# encoding, no Content-Length, etc.) and the JSON parser overflows the
# Python recursion limit, surface a clean 400 rather than the default
# unhandled-exception 500. RecursionError isn't expected from anything
# else in the request path.
@app.exception_handler(RecursionError)
async def _recursion_error_handler(_request: Request, _exc: RecursionError):
    log.warning("RecursionError on request — likely a deep-nested JSON DoS attempt")
    return PlainTextResponse(
        "request structure too deep",
        status_code=400,
        headers={"Content-Type": "text/plain"},
    )


# ─── HEAD→GET shim ────────────────────────────────────────────────────────
# Pass-2 review finding #15: FastAPI doesn't auto-pair HEAD with @app.get
# routes — every API endpoint 405'd on HEAD probes from monitoring agents
# and HTTP cache validators. Translate HEAD to GET at the router level,
# then drop the body on the way out per RFC 7231 §4.3.2.
@app.middleware("http")
async def _head_to_get(request: Request, call_next):
    if request.method != "HEAD":
        return await call_next(request)
    request.scope["method"] = "GET"
    response = await call_next(request)
    # Empty out any body so we comply with HEAD semantics regardless
    # of whether the ASGI server also strips one.
    if hasattr(response, "body_iterator"):
        async def _empty():
            if False:
                yield b""  # pragma: no cover — never executed
        response.body_iterator = _empty()
    if hasattr(response, "body"):
        response.body = b""
    # Content-Length should reflect that the body is now empty.
    if "content-length" in response.headers:
        response.headers["content-length"] = "0"
    return response


# Defense-in-depth response headers. Applied to every response.
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    headers = response.headers
    # Tight CSP; the frontend is single-bundle, fonts come from Google
    # (whitelisted), and audio data is delivered via WS rather than HTTP.
    headers.setdefault("Content-Security-Policy", (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "media-src 'self' blob:; "
        # 'self' in connect-src covers same-origin ws:/wss: under modern
        # CSP3; the wildcard scheme tokens were broader than needed and
        # let a compromised script connect anywhere.
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ))
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Permissions-Policy",
                       "microphone=(self), camera=(), geolocation=()")
    # Pass-2 review finding #10: cookie-bound JSON responses sent
    # ``Vary: Cookie`` but no Cache-Control, so a misconfigured shared
    # cache could in principle store and replay them. Apply
    # `private, no-store, max-age=0` ONLY to JSON responses — the
    # hashed `/assets/*` bundles are immutable-by-design and need to
    # stay cacheable, and the HTML / icons set their own no-store
    # headers in routes/static.py.
    ctype = headers.get("content-type", "")
    if ctype.startswith("application/json"):
        headers.setdefault("Cache-Control", "private, no-store, max-age=0")
    if _BEHIND_TLS:
        # HSTS preload-ready: 2-year max-age, include subdomains, preload.
        # Single canonical source — the bundled nginx sample no longer
        # emits this so we don't end up with two HSTS headers (browsers
        # honour only the first, dropping a "preload" directive that
        # only appeared on the second was the exact failure mode we hit).
        headers.setdefault(
            "Strict-Transport-Security",
            "max-age=63072000; includeSubDomains; preload",
        )
    return response




@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ─── /.well-known/security.txt (RFC 9116) ─────────────────────────────────
# Pass-2 review finding #14: external researchers had no machine-readable
# disclosure target. Point them at the GitHub Security Advisory channel
# we already document in SECURITY.md. Expires is set 1 year out per the
# RFC's MUST; bump it as part of any major SECURITY.md revision.
_SECURITY_TXT = (
    "# ccpipe security contact — see SECURITY.md in the repo for context.\n"
    "Contact: https://github.com/JameZUK/ccpipe/security/advisories/new\n"
    "Policy: https://github.com/JameZUK/ccpipe/blob/main/SECURITY.md\n"
    "Preferred-Languages: en\n"
    "Expires: 2027-05-18T00:00:00Z\n"
)


@app.get("/.well-known/security.txt", include_in_schema=False)
async def security_txt() -> PlainTextResponse:
    return PlainTextResponse(_SECURITY_TXT, media_type="text/plain")


# ─── Route packages ───────────────────────────────────────────────────────
from .routes.auth import router as _auth_router
from .routes.debug import router as _debug_router
from .routes.fs import router as _fs_router
from .routes.mic import router as _mic_router
from .routes.sessions import router as _sessions_router
from .routes.static import mount_static, router as _static_router
from .routes.tts import router as _tts_router

app.include_router(_auth_router)
app.include_router(_tts_router)
app.include_router(_mic_router)
app.include_router(_sessions_router)
app.include_router(_fs_router)
app.include_router(_debug_router)
app.include_router(_static_router)
mount_static(app)


@app.websocket("/ws")
async def ws(websocket: WebSocket, session: str, skip_history: int = 0) -> None:
    # ``skip_history`` is accepted but ignored — we always replay tmux's
    # current pane on every (re)connect so xterm's scrollback can't drift
    # from tmux's during a disconnect window. The frontend ``term.reset()``s
    # on hello so the replay replaces rather than appends. Kept in the
    # signature so a cached pre-fix frontend still negotiates.
    del skip_history
    if not await authorize_websocket(websocket):
        return
    try:
        name = tmux.safe_name(session)
    except ValueError:
        await websocket.close(code=1008, reason="invalid session name")
        return
    # Refuse to attach a user PTY to the hidden control-mode session;
    # tmux would allow it but the resulting client would just shovel
    # control protocol bytes into xterm.
    if name == CONTROL_SESSION_NAME:
        await websocket.close(code=1008, reason="reserved session name")
        return
    await handle_terminal_ws(websocket, name)


