"""Authentication routes.

Two login paths supported:

  - **password-only** when no TOTP enrolled → one round-trip, session
    set immediately.
  - **two-factor** when TOTP enrolled → first round-trip returns
    ``otp_required=true`` and *no* session; client resubmits with
    ``code`` populated; we then verify and grant the session.

A per-IP + global sliding-window rate limit fronts the login endpoint
so a brute-force across the LAN gets one budget per source AND a
distributed flood is capped overall. See ``_login_throttle_ok``.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..auth import (
    AuthDep,
    AuthStatus,
    CsrfDep,
    LoginBody,
    get_credential,
    is_auth_enabled,
    is_session_authed,
    session_username,
    set_totp_secret,
    totp_enrolled,
    totp_generate_secret,
    totp_provisioning_uri,
    totp_verify,
    update_credential,
    verify_credential,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/auth/status", response_model=AuthStatus)
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

# Global ceiling that complements the per-IP bucket. The per-IP limit
# alone is bypassed when ccpipe sits behind nginx (the documented
# deployment), where request.client.host is always the loopback /
# nginx IP, so all clients share one IP-bucket. A global cap caps the
# absolute rate while still letting a legit user retry.
_GLOBAL_LOGIN_MAX = 30
_GLOBAL_LOGIN_WINDOW_S = 60.0
_global_login_attempts: list[float] = []


def reset_throttle_state() -> None:
    """For tests + lifespan startup: drop any per-IP and global login
    attempt history. Without this, importlib.reload(ccpipe.main) doesn't
    reset the throttle (this module is cached in sys.modules), so a
    test suite that issues > _GLOBAL_LOGIN_MAX logins trips a 429."""
    _login_attempts.clear()
    _global_login_attempts.clear()


def _login_throttle_ok(ip: str) -> bool:
    now = time.monotonic()
    # ── Global window ──
    g_cutoff = now - _GLOBAL_LOGIN_WINDOW_S
    while _global_login_attempts and _global_login_attempts[0] < g_cutoff:
        _global_login_attempts.pop(0)
    if len(_global_login_attempts) >= _GLOBAL_LOGIN_MAX:
        return False
    # ── Per-IP window ──
    cutoff = now - _LOGIN_BUCKET_WINDOW_S
    bucket = _login_attempts.setdefault(ip, [])
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= _LOGIN_BUCKET_MAX:
        if not bucket:
            _login_attempts.pop(ip, None)
        return False
    bucket.append(now)
    _global_login_attempts.append(now)
    # Opportunistic GC of empty IP entries — keeps the dict from
    # accumulating one entry per distinct attacker source IP forever.
    if len(_login_attempts) > 256:
        for k, b in list(_login_attempts.items()):
            if not b:
                _login_attempts.pop(k, None)
    return True


@router.post("/api/auth/login", response_model=AuthStatus, dependencies=[CsrfDep])
async def auth_login(body: LoginBody, request: Request) -> AuthStatus:
    # `request.client.host` reflects whatever uvicorn resolved as the
    # peer. With `--proxy-headers --forwarded-allow-ips=<nginx-ip>` it's
    # the real client; without those flags it's the immediate TCP peer
    # (usually nginx itself), which collapses the per-IP throttle into
    # a global one. See README §"Reverse proxy" for the recommended unit.
    client_ip = (request.client.host if request.client else "") or "unknown"
    if not _login_throttle_ok(client_ip):
        log.warning("login throttle tripped for ip=%s (per-IP %d/%ds + global %d/%ds)",
                     client_ip, _LOGIN_BUCKET_MAX, int(_LOGIN_BUCKET_WINDOW_S),
                     _GLOBAL_LOGIN_MAX, int(_GLOBAL_LOGIN_WINDOW_S))
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


@router.post("/api/auth/logout", response_model=AuthStatus, dependencies=[CsrfDep])
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


@router.post("/api/auth/totp/enroll", dependencies=[AuthDep, CsrfDep])
async def totp_enroll(body: TotpEnrollBody) -> dict[str, str]:
    """Generate a fresh TOTP secret and return it along with the
    otpauth:// provisioning URI for QR rendering. The secret is NOT
    persisted yet — the client must round-trip it back through
    /api/auth/totp/confirm with a working code first, which proves the
    user actually scanned the QR and their authenticator is in sync."""
    cred = get_credential()
    if not verify_credential(cred.username, body.currentPassword):
        await asyncio.sleep(1.0)
        raise HTTPException(status_code=401, detail="current password is wrong")
    secret = totp_generate_secret()
    uri = totp_provisioning_uri(secret, cred.username)
    # Render the QR server-side so the TOTP secret never leaves this
    # process — a third-party QR API would necessarily see the
    # otpauth:// URI (which embeds the secret).
    import qrcode
    from qrcode.image.svg import SvgPathImage
    img = qrcode.QRCode(border=2, box_size=10)
    img.add_data(uri)
    img.make(fit=True)
    svg = img.make_image(image_factory=SvgPathImage)
    buf = io.BytesIO()
    svg.save(buf)
    qr_svg = buf.getvalue().decode("utf-8")
    # Strip a possible XML prolog so the SVG renders cleanly via innerHTML.
    if qr_svg.startswith("<?xml"):
        qr_svg = qr_svg.split("?>", 1)[-1].lstrip()
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": qr_svg}


@router.post("/api/auth/totp/confirm", dependencies=[AuthDep, CsrfDep])
async def totp_confirm_endpoint(body: TotpConfirmBody) -> dict[str, bool]:
    """Validate a 6-digit code against the provided candidate secret
    BEFORE persisting it — if the user's authenticator app drifted or
    they scanned the wrong QR, we want to fail loudly here instead of
    locking them out on the next login."""
    import pyotp
    secret = (body.secret or "").strip()
    code = (body.code or "").strip()
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


@router.post("/api/auth/totp/disable", dependencies=[AuthDep, CsrfDep])
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


@router.post("/api/auth/credentials", dependencies=[AuthDep, CsrfDep])
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
