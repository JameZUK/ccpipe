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
import json
import logging
import math
from collections import deque
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

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
    # Don't leak account-level security state (e.g. whether TOTP is
    # enrolled) to unauthenticated callers — that hands an attacker
    # writing automation a target/skip signal for free. Surface
    # otp_enrolled only once we've established the session is real.
    return AuthStatus(
        required=is_auth_enabled(),
        authenticated=authed,
        username=session_username(request.session) if authed else None,
        otp_enrolled=totp_enrolled() if authed else False,
    )


# Per-client-IP login rate limiter. Per-IP rather than global so a
# distributed brute-force across the LAN gets one attempt budget per
# source, and a legitimate user retrying their password doesn't lock
# out the whole tenant. Bucket = 5 attempts per minute, window slides.
_LOGIN_BUCKET_MAX = 5
_LOGIN_BUCKET_WINDOW_S = 60.0
# deque, not list — popleft() is O(1) where list.pop(0) is O(n).
# Under sustained burst within the window the list version was
# quadratic in bucket length per call.
_login_attempts: dict[str, deque[float]] = {}

# Global ceiling that complements the per-IP bucket. The per-IP limit
# alone is bypassed when ccpipe sits behind nginx (the documented
# deployment), where request.client.host is always the loopback /
# nginx IP, so all clients share one IP-bucket. A global cap caps the
# absolute rate while still letting a legit user retry.
_GLOBAL_LOGIN_MAX = 30
_GLOBAL_LOGIN_WINDOW_S = 60.0
_global_login_attempts: deque[float] = deque()


def reset_throttle_state() -> None:
    """For tests + lifespan startup: drop any per-IP and global login
    attempt history. Without this, importlib.reload(ccpipe.main) doesn't
    reset the throttle (this module is cached in sys.modules), so a
    test suite that issues > _GLOBAL_LOGIN_MAX logins trips a 429."""
    _login_attempts.clear()
    _global_login_attempts.clear()


def _login_throttle_ok(ip: str) -> bool:
    now = time.monotonic()
    # Sweep stale entries BEFORE the throttle decision so a sustained
    # flood that always trips the limit doesn't leak entries forever.
    # Previously the GC sat at the bottom of the function, gated on
    # the success path — under attack we'd return False well before
    # reaching it and the per-IP dict grew with each distinct source.
    cutoff = now - _LOGIN_BUCKET_WINDOW_S
    if len(_login_attempts) > 256:
        for k in list(_login_attempts.keys()):
            b = _login_attempts[k]
            while b and b[0] < cutoff:
                b.popleft()
            if not b:
                _login_attempts.pop(k, None)
    # ── Global window ──
    g_cutoff = now - _GLOBAL_LOGIN_WINDOW_S
    while _global_login_attempts and _global_login_attempts[0] < g_cutoff:
        _global_login_attempts.popleft()
    if len(_global_login_attempts) >= _GLOBAL_LOGIN_MAX:
        return False
    # ── Per-IP window ──
    bucket = _login_attempts.setdefault(ip, deque())
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _LOGIN_BUCKET_MAX:
        if not bucket:
            _login_attempts.pop(ip, None)
        return False
    bucket.append(now)
    _global_login_attempts.append(now)
    return True


def _reject_non_finite_json_const(name: str) -> float:
    """``parse_constant`` callback for stdlib json. Refuses NaN /
    Infinity / -Infinity which RFC 8259 forbids but stdlib accepts
    by default. Closes pass-3 #17."""
    raise ValueError(f"non-finite JSON literal: {name}")


def _reject_non_finite_json_float(s: str) -> float:
    """``parse_float`` callback for stdlib json. Catches the
    `1e1000` → inf overflow path that bypasses parse_constant."""
    f = float(s)
    if not math.isfinite(f):
        raise ValueError(f"non-finite number: {s}")
    return f


@router.post("/api/auth/login", response_model=AuthStatus, dependencies=[CsrfDep])
async def auth_login(request: Request) -> AuthStatus:
    # `request.client.host` reflects whatever uvicorn resolved as the
    # peer. With `--proxy-headers --forwarded-allow-ips=<nginx-ip>` it's
    # the real client; without those flags it's the immediate TCP peer
    # (usually nginx itself), which collapses the per-IP throttle into
    # a global one. See README §"Reverse proxy" for the recommended unit.
    client_ip = (request.client.host if request.client else "") or "unknown"
    # ── Rate-limit BEFORE body parsing.
    # Pass-3 review finding #18: the previous code put the throttle
    # check after FastAPI's automatic ``body: LoginBody`` parsing, so
    # any payload that crashed the parser (NaN / Infinity / overflow /
    # lone surrogates per #17, the deep-nest class per pass-2 #9)
    # returned a 5xx without ever counting toward the attacker's
    # attempt budget. Moving the throttle here means every login POST
    # — successful, 401'd, or 400'd as malformed — costs the source
    # exactly one bucket slot.
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

    # ── Parse + validate body manually.
    # Pass-3 review finding #17: FastAPI's automatic ``body: LoginBody``
    # parameter delegates to stdlib json.loads, which accepts non-RFC
    # JSON literals (NaN, ±Infinity) and float overflow (1e1000 → inf).
    # Pydantic then crashed downstream and the response was 500. We
    # parse with strict callbacks here (parse_constant rejects NaN/Inf,
    # parse_float catches overflow) and treat any failure as 400 — same
    # semantic class as "wrong credentials" from the attacker's POV.
    try:
        raw = await request.body()
        data = (
            json.loads(
                raw,
                parse_constant=_reject_non_finite_json_const,
                parse_float=_reject_non_finite_json_float,
            )
            if raw else {}
        )
        body = LoginBody.model_validate(data)
        # Reject lone surrogates / other non-encodable UTF-16 codepoints
        # before they reach verify_credential's .encode() call, which
        # would otherwise raise UnicodeEncodeError → 500.
        body.username.encode("utf-8")
        body.password.encode("utf-8")
        if body.code is not None:
            body.code.encode("utf-8")
    except (ValueError, TypeError, UnicodeError, ValidationError):
        await asyncio.sleep(1.0)
        raise HTTPException(status_code=400, detail="invalid request")

    # Single-step login. The previous two-step flow returned a 200 with
    # `otp_required=true` once the password was correct but before any
    # TOTP check — that response was a positive password-correctness
    # oracle, useful for triaging credential-stuffing dumps. We now
    # validate password AND code together and return a single uniform
    # 401 on any failure (wrong password, missing/wrong code), so the
    # response distinguishes only "authenticated" vs "not".
    password_ok = verify_credential(body.username, body.password)
    cred = get_credential()
    if cred.totp_secret:
        # When TOTP is enrolled, a missing or wrong code is just another
        # form of "invalid credentials" — no leak about which part was
        # right. Note: totp_verify is called on a code-only when present
        # so the burn-list isn't poked by empty submissions.
        code = (body.code or "").strip()
        code_ok = bool(code) and totp_verify(code)
        if not (password_ok and code_ok):
            await asyncio.sleep(1.0)
            raise HTTPException(status_code=401, detail="invalid credentials")
    else:
        if not password_ok:
            await asyncio.sleep(1.0)
            raise HTTPException(status_code=401, detail="invalid credentials")

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
    # currentPassword is required so a session-stealer (XSS, shared
    # device) can't enrol their own TOTP and lock the legitimate user
    # out — without it, AuthDep alone gates this endpoint, and the
    # caller picks both the secret and the code that "verifies" it.
    currentPassword: str
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
    locking them out on the next login.

    Also re-verifies the current password: without this, an attacker
    holding only a session cookie could persist a TOTP secret of their
    choosing and lock the legitimate user out.
    """
    import pyotp
    cred = get_credential()
    if not verify_credential(cred.username, body.currentPassword):
        await asyncio.sleep(1.0)
        raise HTTPException(status_code=401, detail="current password is wrong")
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
        await asyncio.sleep(1.0)
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
