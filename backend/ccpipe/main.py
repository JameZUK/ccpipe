"""FastAPI app factory.

Most concrete routes live in the ``routes/`` package; this file is the
glue that wires them up alongside middleware, the WebSocket endpoint,
and the lifespan that starts/stops the long-lived background services
(tmux control client, TTS watchdog).
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly resolve (or generate + persist) credentials so the operator
    # can see them in the journal right away rather than on first login.
    get_credential()
    _warn_if_tls_with_public_bind()
    _warn_if_tls_with_open_host_validation()
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


app = FastAPI(title="ccpipe", version="0.1.0", lifespan=lifespan)

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
    if _BEHIND_TLS:
        headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response




@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ─── Route packages ───────────────────────────────────────────────────────
from .routes.auth import router as _auth_router
from .routes.fs import router as _fs_router
from .routes.sessions import router as _sessions_router
from .routes.static import mount_static, router as _static_router
from .routes.tts import router as _tts_router

app.include_router(_auth_router)
app.include_router(_tts_router)
app.include_router(_sessions_router)
app.include_router(_fs_router)
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


