"""FastAPI app: session API + WebSocket endpoint + static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from starlette.middleware.sessions import SessionMiddleware

from . import config as app_config
from . import tmux
from .auth import (
    AuthDep,
    AuthStatus,
    CsrfDep,
    LoginBody,
    authorize_websocket,
    behind_tls,
    get_credential,
    is_auth_enabled,
    is_session_authed,
    load_or_create_secret,
    session_username,
    set_totp_secret,
    totp_enrolled,
    totp_generate_secret,
    totp_provisioning_uri,
    totp_verify,
    update_credential,
    verify_credential,
)
from .settings_patch import patch_keybindings_safe, patch_settings_safe, should_apply
from .tmux_control import CONTROL_SESSION_NAME, control_client
from .tmux_setup import apply_server_defaults
from .tts import tts_service
from .ws import handle_terminal_ws


def _reject_control_session(name: str) -> None:
    """Block external callers from touching the hidden control-mode
    session ccpipe maintains for its tmux event channel. Wiping or
    renaming it would force a backend supervisor restart and leave the
    browser blind to session-list changes until it reconnects."""
    if name == CONTROL_SESSION_NAME:
        raise HTTPException(status_code=404, detail="session not found")

FRONTEND_DIST = Path(os.environ.get("CCPIPE_FRONTEND_DIST", "/app/frontend"))

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly resolve (or generate + persist) credentials so the operator
    # can see them in the journal right away rather than on first login.
    get_credential()
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


class SessionInfo(BaseModel):
    name: str
    windows: int
    attached: bool
    created: int


class CreateSessionBody(BaseModel):
    name: str
    # The window command is hardcoded server-side to "claude" (or
    # "claude --resume <uuid>"). The previous user-controllable `command`
    # field was a remote-shell-execution vector if any auth gate was ever
    # bypassed; ccpipe is exclusively about attaching to Claude Code, so
    # arbitrary commands have no legitimate use.

    # Optional initial working directory for the new tmux session. If
    # omitted, the start directory is $HOME. Must be an absolute path
    # that resolves to a readable directory.
    cwd: str | None = None
    # Optional Claude sessionId (UUID) to resume. When set, the window
    # command becomes `claude --resume <uuid>` so the new tmux session
    # picks up the conversation where it left off.
    resumeSessionId: str | None = None


class RenameSessionBody(BaseModel):
    newName: str


# Used to validate resumeSessionId and to confirm a /api/claude-sessions
# JSONL filename matches the claude session UUID format.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _projects_subdir_for_cwd(cwd: str) -> Path:
    """Return ``~/.claude/projects/<encoded>/`` for a given cwd. Claude
    encodes the cwd by replacing each '/' with '-', so
    ``/home/you/Projects/foo`` becomes ``-home-you-Projects-foo``."""
    encoded = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def _read_first_real_user_message(path: Path) -> str | None:
    """Scan up to 200 lines for the first user message that isn't a
    framework caveat / command stdout (those wrap their content in
    XML-ish tags like ``<local-command-stdout>...</local-command-stdout>``).
    Returns up to 120 chars trimmed; ``None`` if no plain user prompt is
    found in the first 200 records."""
    try:
        with path.open("rb") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                content = msg.get("content")
                if isinstance(content, str):
                    if content.lstrip().startswith("<"):
                        continue
                    text = content.strip()
                    return text[:120] or None
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "text":
                            continue
                        text = (block.get("text") or "").strip()
                        if not text or text.startswith("<"):
                            continue
                        return text[:120]
    except OSError:
        return None
    return None


def _running_claude_session_ids() -> set[str]:
    """Currently-running Claude Code sessionIds, sourced from
    ``~/.claude/sessions/<pid>.json`` (one file per live claude process).
    Used to filter the resume picker — we don't want to tempt the user
    into a second `claude --resume` of a conversation already running."""
    out: set[str] = set()
    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return out
    for f in sessions_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        if isinstance(sid, str):
            out.add(sid)
    return out


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth/status", response_model=AuthStatus)
async def auth_status(request: Request) -> AuthStatus:
    authed = is_session_authed(request.session)
    return AuthStatus(
        required=is_auth_enabled(),
        authenticated=authed,
        username=session_username(request.session) if authed else None,
        otp_enrolled=totp_enrolled(),
    )


# Per-client-IP login rate limiter. Per-IP rather than global so a
# distributed brute-force across the LAN gets one attempt budget per
# source, and a legitimate user retrying their password doesn't lock
# out the whole tenant. Bucket = 5 attempts per minute, window slides.
_LOGIN_BUCKET_MAX = 5
_LOGIN_BUCKET_WINDOW_S = 60.0
_login_attempts: dict[str, list[float]] = {}


def _login_throttle_ok(ip: str) -> bool:
    import time
    now = time.monotonic()
    cutoff = now - _LOGIN_BUCKET_WINDOW_S
    bucket = _login_attempts.setdefault(ip, [])
    # Drop expired entries.
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= _LOGIN_BUCKET_MAX:
        return False
    bucket.append(now)
    return True


@app.post("/api/auth/login", response_model=AuthStatus, dependencies=[CsrfDep])
async def auth_login(body: LoginBody, request: Request) -> AuthStatus:
    client_ip = (request.client.host if request.client else "") or "unknown"
    if not _login_throttle_ok(client_ip):
        # 429 with Retry-After hint. Slight async delay so even a
        # successful 429 costs time on the attacker side.
        await asyncio.sleep(1.0)
        raise HTTPException(status_code=429,
                            detail="too many attempts; try again in a minute",
                            headers={"Retry-After": str(int(_LOGIN_BUCKET_WINDOW_S))})
    if not verify_credential(body.username, body.password):
        # Sleep on failures to slow online brute-force regardless of
        # whether the throttle window has elapsed.
        await asyncio.sleep(1.0)
        raise HTTPException(status_code=401, detail="invalid credentials")
    cred = get_credential()
    # Two-factor gate. When TOTP is enrolled we don't grant a session
    # until the client resubmits with a valid 6-digit code. The
    # password-step response signals `otp_required=true` so the
    # frontend can switch to the code-entry view.
    if cred.totp_secret:
        if not body.code:
            return AuthStatus(
                required=True,
                authenticated=False,
                username=None,
                otp_required=True,
                otp_enrolled=True,
            )
        if not totp_verify(body.code):
            await asyncio.sleep(1.0)
            raise HTTPException(status_code=401, detail="invalid code")
    request.session["authed"] = True
    request.session["username"] = cred.username
    # Stamp the version so a later credential change can invalidate this
    # session even though its signed cookie still verifies cleanly.
    request.session["cred_version"] = cred.version
    return AuthStatus(
        required=True,
        authenticated=True,
        username=cred.username,
        otp_enrolled=bool(cred.totp_secret),
    )


@app.post("/api/auth/logout", response_model=AuthStatus, dependencies=[CsrfDep])
async def auth_logout(request: Request) -> AuthStatus:
    request.session.clear()
    return AuthStatus(required=True, authenticated=False, username=None)


# ─── TOTP (two-factor) enrollment ──────────────────────────────────────

class TotpEnrollBody(BaseModel):
    currentPassword: str


class TotpConfirmBody(BaseModel):
    secret: str
    code: str


class TotpDisableBody(BaseModel):
    currentPassword: str
    code: str


@app.post("/api/auth/totp/enroll", dependencies=[AuthDep, CsrfDep])
async def totp_enroll(body: TotpEnrollBody) -> dict[str, str]:
    """Generate a fresh TOTP secret and return it along with the
    otpauth:// provisioning URI for QR rendering. The secret is NOT
    persisted yet — the client must round-trip it back through
    /api/auth/totp/confirm with a working code first, which proves the
    user actually scanned the QR and their authenticator is in sync.
    Requires the current password as a defence against an XSS-injected
    enrollment that bypasses the authenticator.
    """
    cred = get_credential()
    # _ct_eq lives in auth.py and isn't exported — replicate the
    # constant-time check via verify_credential, which already does
    # both username and password.
    if not verify_credential(cred.username, body.currentPassword):
        await asyncio.sleep(1.0)
        raise HTTPException(status_code=401, detail="current password is wrong")
    secret = totp_generate_secret()
    uri = totp_provisioning_uri(secret, cred.username)
    # Render the QR server-side as an SVG so the TOTP secret never
    # leaves this process — a third-party QR API would necessarily
    # see the otpauth:// URI (which embeds the secret).
    import qrcode
    import qrcode.image.svg
    import io
    buf = io.BytesIO()
    qrcode.make(uri, image_factory=qrcode.image.svg.SvgImage).save(buf)
    qr_svg = buf.getvalue().decode("utf-8")
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": qr_svg}


@app.post("/api/auth/totp/confirm", dependencies=[AuthDep, CsrfDep])
async def totp_confirm_endpoint(body: TotpConfirmBody) -> dict[str, bool]:
    """Validate a 6-digit code against the provided candidate secret
    BEFORE persisting it — if the user's authenticator app drifted or
    they scanned the wrong QR, we want to fail loudly here instead of
    locking them out on the next login."""
    import pyotp
    secret = (body.secret or "").strip()
    code = (body.code or "").strip()
    # Sanity-check the secret looks like base32 of the expected length.
    if not secret or len(secret) < 16 or len(secret) > 64:
        raise HTTPException(status_code=400, detail="invalid secret")
    if not code.isdigit() or len(code) not in (6, 7, 8):
        raise HTTPException(status_code=400, detail="invalid code")
    try:
        ok = pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid secret")
    if not ok:
        raise HTTPException(status_code=401, detail="code did not verify")
    saved, msg = set_totp_secret(secret)
    if not saved:
        raise HTTPException(status_code=500, detail=msg)
    return {"enrolled": True}


@app.post("/api/auth/totp/disable", dependencies=[AuthDep, CsrfDep])
async def totp_disable_endpoint(body: TotpDisableBody) -> dict[str, bool]:
    """Disable TOTP. Requires BOTH the current password AND a valid
    current code, so a stolen session cookie alone can't unenroll
    two-factor protection (which would then let the attacker keep
    using the stolen password indefinitely)."""
    cred = get_credential()
    if not cred.totp_secret:
        return {"enrolled": False}
    if not verify_credential(cred.username, body.currentPassword):
        await asyncio.sleep(1.0)
        raise HTTPException(status_code=401, detail="current password is wrong")
    if not totp_verify(body.code):
        await asyncio.sleep(1.0)
        raise HTTPException(status_code=401, detail="invalid code")
    saved, msg = set_totp_secret(None)
    if not saved:
        raise HTTPException(status_code=500, detail=msg)
    return {"enrolled": False}


class ChangeCredentialBody(BaseModel):
    currentPassword: str
    newUsername: str | None = None
    newPassword: str | None = None


@app.post("/api/auth/credentials", dependencies=[AuthDep, CsrfDep])
async def auth_change_credentials(body: ChangeCredentialBody,
                                   request: Request) -> dict[str, bool]:
    ok, msg = update_credential(
        current_password=body.currentPassword,
        new_username=body.newUsername,
        new_password=body.newPassword,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    # Force a fresh login on the next request since credentials changed.
    request.session.clear()
    return {"updated": True}


# ─── TTS settings + Kokoro proxy ───────────────────────────────────────────

class TtsConfigBody(BaseModel):
    voice: str | None = None
    speech_rate: float | None = Field(default=None, ge=0.5, le=2.0)
    enabled: bool | None = None
    scope: str | None = None  # one of ccpipe.config.VALID_SCOPES


def _kokoro_url() -> str:
    return os.environ.get("CCPIPE_KOKORO_URL", "http://localhost:8880").rstrip("/")


@app.get("/api/tts/voices", dependencies=[AuthDep])
async def tts_voices() -> dict[str, list[str]]:
    """List Kokoro voice names. Returns an empty list if Kokoro is
    unreachable so the UI can render a graceful 'no voices available'."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{_kokoro_url()}/v1/audio/voices")
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("kokoro voices fetch failed: %s", exc)
        return {"voices": []}
    voices = data.get("voices", []) if isinstance(data, dict) else data
    if not isinstance(voices, list):
        return {"voices": []}
    return {"voices": [str(v) for v in voices if isinstance(v, str)]}


@app.get("/api/tts/config", dependencies=[AuthDep])
async def tts_config_get() -> dict[str, object]:
    return dict(app_config.load().to_dict()["tts"])


@app.post("/api/tts/config", dependencies=[AuthDep, CsrfDep])
async def tts_config_set(body: TtsConfigBody) -> dict[str, object]:
    cfg = app_config.load()
    if body.voice is not None:
        v = body.voice.strip()
        if v:
            # Bound the persisted value — matches the preview endpoint
            # check below. Kokoro's actual voice ids are short tokens.
            if len(v) > 64:
                raise HTTPException(status_code=400, detail="voice too long")
            cfg.tts.voice = v
    if body.speech_rate is not None:
        cfg.tts.speech_rate = max(0.5, min(2.0, float(body.speech_rate)))
    if body.enabled is not None:
        cfg.tts.enabled = bool(body.enabled)
    if body.scope is not None:
        if body.scope in app_config.VALID_SCOPES:
            cfg.tts.scope = body.scope
        else:
            raise HTTPException(
                status_code=400,
                detail=f"invalid scope; one of {list(app_config.VALID_SCOPES)}",
            )
    app_config.save(cfg)
    return dict(cfg.to_dict()["tts"])


class SpeakBody(BaseModel):
    text: str
    voice: str | None = None


@app.post("/api/tts/speak", dependencies=[AuthDep, CsrfDep])
async def tts_speak(body: SpeakBody) -> StreamingResponse:
    """Synthesize arbitrary text and stream MP3 back. Distinct from
    /api/tts/preview (small text cap, voice-test path) — this is the
    "repeat last response" endpoint used by the statusbar replay pill.
    Voice defaults to the configured one when the body omits it.

    Implementation: open the upstream stream BEFORE constructing the
    FastAPI ``StreamingResponse`` so a Kokoro 5xx surfaces as our own
    502 with proper headers (a raise inside the generator would land
    after headers were sent → the client gets a truncated MP3 with no
    error indication).
    """
    cfg = app_config.load()
    voice = (body.voice or cfg.tts.voice or "").strip()
    if not voice or len(voice) > 64:
        raise HTTPException(status_code=400, detail="invalid voice")
    text = body.text or ""
    if not text or len(text) > 4000:
        raise HTTPException(status_code=400, detail="text empty or too long")
    payload = {
        "model": "kokoro",
        "input": text,
        "voice": voice,
        "speed": cfg.tts.speech_rate,
        "response_format": "mp3",
    }

    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=2.0, read=60.0,
                                                      write=10.0, pool=2.0))
    try:
        stream_cm = client.stream("POST", f"{_kokoro_url()}/v1/audio/speech",
                                    json=payload)
        resp = await stream_cm.__aenter__()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"kokoro unreachable: {exc}")
    if resp.status_code != 200:
        await stream_cm.__aexit__(None, None, None)
        await client.aclose()
        raise HTTPException(status_code=502, detail="kokoro error")

    async def stream():
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            try: await stream_cm.__aexit__(None, None, None)
            except Exception: pass
            await client.aclose()

    return StreamingResponse(stream(), media_type="audio/mpeg")


@app.get("/api/tts/preview", dependencies=[AuthDep])
async def tts_preview(request: Request, voice: str,
                       text: str = "Voice test, one two three.",
                       ) -> StreamingResponse:
    """Synthesize a short sample with the given voice and stream MP3
    back. Used by the settings modal's Test button.

    GETs with credentials can be triggered cross-origin via <img>,
    <audio>, etc., which would let a malicious page meter Kokoro work
    against the authenticated session. CsrfDep can't help (browsers
    don't send custom headers for such loads), so we rely on Fetch
    Metadata: Sec-Fetch-Site must be same-origin. All current browsers
    that this project targets send it; we deliberately reject when it's
    absent to keep the gate strict.
    """
    sfs = request.headers.get("sec-fetch-site", "").lower()
    if sfs != "same-origin":
        raise HTTPException(status_code=403, detail="cross-origin preview blocked")
    if not voice or len(voice) > 64:
        raise HTTPException(status_code=400, detail="invalid voice")
    if len(text) > 200:
        raise HTTPException(status_code=400, detail="preview text too long")
    cfg = app_config.load()
    body = {
        "model": "kokoro",
        "input": text,
        "voice": voice,
        "speed": cfg.tts.speech_rate,
        "response_format": "mp3",
    }

    # Same open-before-stream pattern as /api/tts/speak so a Kokoro
    # failure becomes a proper 502 instead of a truncated MP3.
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=2.0, read=60.0,
                                                      write=10.0, pool=2.0))
    try:
        stream_cm = client.stream("POST", f"{_kokoro_url()}/v1/audio/speech",
                                    json=body)
        resp = await stream_cm.__aenter__()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"kokoro unreachable: {exc}")
    if resp.status_code != 200:
        await stream_cm.__aexit__(None, None, None)
        await client.aclose()
        raise HTTPException(status_code=502, detail="kokoro error")

    async def stream():
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            try: await stream_cm.__aexit__(None, None, None)
            except Exception: pass
            await client.aclose()

    return StreamingResponse(stream(), media_type="audio/mpeg")


@app.get("/api/sessions", response_model=list[SessionInfo], dependencies=[AuthDep])
async def list_sessions() -> list[SessionInfo]:
    sessions = await tmux.list_sessions()
    return [SessionInfo(**s.__dict__) for s in sessions]


@app.post("/api/sessions", response_model=SessionInfo,
          dependencies=[AuthDep, CsrfDep])
async def create_session(body: CreateSessionBody) -> SessionInfo:
    try:
        name = tmux.safe_name(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    if await tmux.session_exists(name):
        raise HTTPException(status_code=409, detail="session already exists")

    cwd: str | None = None
    if body.cwd:
        if not body.cwd.startswith("/"):
            raise HTTPException(status_code=400, detail="cwd must be absolute")
        try:
            resolved = Path(body.cwd).resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(status_code=400, detail="cwd does not exist")
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail="cwd is not a directory")
        cwd = str(resolved)

    if body.resumeSessionId:
        if not _UUID_RE.match(body.resumeSessionId):
            raise HTTPException(status_code=400, detail="invalid resumeSessionId")
        # shlex.quote is belt-and-braces: the UUID regex already bounds
        # the value to [0-9a-fA-F-], but libtmux passes window_command
        # to a shell and we want zero ambiguity.
        command = f"claude --resume {shlex.quote(body.resumeSessionId)}"
    else:
        command = "claude"

    await tmux.create_session(name, command=command, cwd=cwd)
    for s in await tmux.list_sessions():
        if s.name == name:
            return SessionInfo(**s.__dict__)
    raise HTTPException(status_code=500, detail="session created but not found in list")


@app.patch("/api/sessions/{name}", response_model=SessionInfo,
           dependencies=[AuthDep, CsrfDep])
async def rename_session_endpoint(name: str, body: RenameSessionBody) -> SessionInfo:
    try:
        name = tmux.safe_name(name)
        new_name = tmux.safe_name(body.newName)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    _reject_control_session(new_name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="session not found")
    if name != new_name:
        if await tmux.session_exists(new_name):
            raise HTTPException(status_code=409, detail="target name already in use")
        if not await tmux.rename_session(name, new_name):
            raise HTTPException(status_code=500, detail="rename failed")
    for s in await tmux.list_sessions():
        if s.name == new_name:
            return SessionInfo(**s.__dict__)
    raise HTTPException(status_code=500, detail="renamed but session vanished")


@app.delete("/api/sessions/{name}", dependencies=[AuthDep, CsrfDep])
async def delete_session_endpoint(name: str) -> dict[str, bool]:
    try:
        name = tmux.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_control_session(name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="session not found")
    if not await tmux.kill_session(name):
        raise HTTPException(status_code=500, detail="kill failed")
    return {"deleted": True}


@app.get("/api/fs/list", dependencies=[AuthDep])
async def fs_list(path: str, show_hidden: int = 0) -> dict[str, Any]:
    """List subdirectories under *path*. Only directories (not files) are
    returned — the picker uses this purely to navigate to a project root
    for ``claude``. Symlinks are followed so a `~/code` shortcut works.
    Caps results at 2000 to keep the payload bounded.
    """
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path must be absolute")
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="path not found")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="path is not a directory")

    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(resolved) as it:
            for e in it:
                if len(entries) >= 2000:
                    break
                if not show_hidden and e.name.startswith("."):
                    continue
                try:
                    if not e.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                entries.append({"name": e.name})
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")

    entries.sort(key=lambda x: x["name"].lower())
    parent = resolved.parent
    return {
        "path": str(resolved),
        "parent": str(parent) if parent != resolved else None,
        "entries": entries,
    }


def _iter_jsonl_as_markdown(path: Path):
    """Yield UTF-8 markdown chunks for a claude-code JSONL transcript
    without materialising the full document in memory. Skips framework
    caveats (``<local-command-…>``), tool-use / tool-result records,
    and any non-text content blocks. Used by the export endpoint as
    a true streaming response body."""
    try:
        f = path.open("rb")
    except OSError:
        return
    try:
        first = True
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = obj.get("type")
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = msg.get("content")
            if rtype == "user":
                text = _stringify_content(content)
                if not text or text.lstrip().startswith("<"):
                    continue
                header = "## User"
            elif rtype == "assistant":
                text = _stringify_content(content)
                if not text:
                    continue
                header = "## Claude"
            else:
                continue
            sep = "" if first else "\n"
            first = False
            yield f"{sep}{header}\n\n{text.strip()}\n".encode("utf-8")
    finally:
        try: f.close()
        except OSError: pass


def _stringify_content(content: Any) -> str:
    """Flatten claude-code JSONL ``message.content`` into plain text.
    Accepts a string or a list of typed blocks; only ``text`` blocks
    survive."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    return ""


@app.get("/api/claude-sessions/{session_id}/export", dependencies=[AuthDep])
async def claude_session_export(session_id: str, cwd: str) -> StreamingResponse:
    """Stream a markdown rendering of a claude session's JSONL transcript.

    The frontend uses this for the per-session export button on the
    resume list. ``cwd`` is required to locate the projects-dir-encoded
    parent directory; ``session_id`` is the JSONL filename stem.
    """
    if not _UUID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    if not cwd.startswith("/"):
        raise HTTPException(status_code=400, detail="cwd must be absolute")
    try:
        resolved = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="cwd does not exist")
    projects_dir = _projects_subdir_for_cwd(str(resolved))
    jsonl = projects_dir / f"{session_id}.jsonl"
    # Resolve and confirm the file is still inside the projects dir
    # after symlink expansion — same path-traversal hygiene as the TTS
    # service.
    try:
        target = jsonl.resolve(strict=True)
        target.relative_to(projects_dir.resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="session not found")
    # Probe by reading the first chunk so we can return 404 cleanly
    # rather than serving an empty 200 for an unreadable transcript.
    def _probe() -> bytes | None:
        for chunk in _iter_jsonl_as_markdown(target):
            return chunk
        return None
    first_chunk = await asyncio.to_thread(_probe)
    if not first_chunk:
        raise HTTPException(status_code=404, detail="empty or unreadable transcript")

    # Streaming body — re-iterates the file from the top. Cheap given
    # the probe was just one record; avoids leaking the probe's fd
    # into the response generator.
    filename = f"ccpipe-{session_id[:8]}.md"
    return StreamingResponse(
        _iter_jsonl_as_markdown(target),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/claude-sessions", dependencies=[AuthDep])
async def claude_sessions(cwd: str) -> dict[str, Any]:
    """List Claude Code sessions persisted under the project dir
    corresponding to *cwd*, with their first user message preview so
    the user can identify the right one to resume.

    Excludes sessionIds for any claude process currently running on
    this machine — resuming a live session would create a conflicting
    second claude with the same sessionId.
    """
    if not cwd.startswith("/"):
        raise HTTPException(status_code=400, detail="cwd must be absolute")
    try:
        resolved = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="cwd does not exist")
    projects_dir = _projects_subdir_for_cwd(str(resolved))
    if not projects_dir.is_dir():
        return {"sessions": []}

    running = _running_claude_session_ids()
    out: list[dict[str, Any]] = []
    for jsonl in projects_dir.glob("*.jsonl"):
        sid = jsonl.stem
        if not _UUID_RE.match(sid):
            continue
        if sid in running:
            continue
        try:
            stat = jsonl.stat()
        except OSError:
            continue
        first_msg = await asyncio.to_thread(_read_first_real_user_message, jsonl)
        out.append({
            "id": sid,
            "mtime": int(stat.st_mtime),
            "size": stat.st_size,
            "firstUserMessage": first_msg,
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"sessions": out[:50]}


@app.websocket("/ws")
async def ws(websocket: WebSocket, session: str, skip_history: int = 0) -> None:
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
    await handle_terminal_ws(websocket, name, skip_history=bool(skip_history))


# Static frontend served by FastAPI. Routes are registered unconditionally
# so the operator sees a useful 503 rather than a confusing 404 when the
# Vite build hasn't run yet (or CCPIPE_FRONTEND_DIST points somewhere wrong).
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")
else:
    log.warning("CCPIPE_FRONTEND_DIST=%s does not exist; static routes will 503 "
                "until the frontend is built (cd frontend && npm run build)",
                FRONTEND_DIST)


def _serve_file(relative: str) -> FileResponse:
    target = FRONTEND_DIST / relative
    if not target.exists():
        raise HTTPException(
            status_code=503,
            detail=f"frontend not built; missing {target}. "
                   f"Run: cd frontend && npm run build",
        )
    # HTML (and any other non-hashed top-level asset) MUST be revalidated
    # on every load — without this, browsers heuristically cache index.html
    # for hours and keep referencing stale hashed asset URLs even after a
    # rebuild + service restart. Hashed bundles under /assets/* are
    # immutable by Vite's design and stay cacheable.
    return FileResponse(target, headers={"Cache-Control": "no-store"})


@app.get("/")
async def index() -> FileResponse: return _serve_file("index.html")

@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse: return _serve_file("manifest.webmanifest")

@app.get("/sw.js")
async def service_worker() -> FileResponse: return _serve_file("sw.js")

@app.get("/icon.svg")
async def icon_svg() -> FileResponse: return _serve_file("icon.svg")

@app.get("/icon-192.svg")
async def icon_192() -> FileResponse: return _serve_file("icon-192.svg")

@app.get("/icon-512.svg")
async def icon_512() -> FileResponse: return _serve_file("icon-512.svg")

@app.get("/mic-worklet.js")
async def mic_worklet() -> FileResponse: return _serve_file("mic-worklet.js")
