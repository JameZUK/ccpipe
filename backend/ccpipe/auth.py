"""Authentication.

ccpipe binds 0.0.0.0 by default, so auth is **always on**. Credentials are
resolved in this order:

  1. Both ``CCPIPE_AUTH_USERNAME`` and ``CCPIPE_AUTH_PASSWORD`` env vars
     (typical when deployed via a systemd drop-in).
  2. ``CCPIPE_AUTH_PASSWORD`` alone (username defaults to the system user).
  3. A persisted file at ``~/.local/state/ccpipe/credentials`` (0600).
  4. Auto-generate a random password, persist to (3), and log it to the
     journal so the operator can read it via ``cat`` or ``journalctl``.

The session secret used to sign cookies is handled separately in
``load_or_create_secret`` and shares the same state directory.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import pwd
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyotp
from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel
from starlette.websockets import WebSocket

log = logging.getLogger(__name__)

USERNAME_ENV = "CCPIPE_AUTH_USERNAME"
PASSWORD_ENV = "CCPIPE_AUTH_PASSWORD"
CREDENTIALS_FILE_ENV = "CCPIPE_CREDENTIALS_FILE"
_SECRET_FILE_ENV = "CCPIPE_SESSION_SECRET_FILE"
ALLOWED_ORIGINS_ENV = "CCPIPE_ALLOWED_ORIGINS"
CSRF_HEADER_NAME = "x-requested-by"
CSRF_HEADER_VALUE = "ccpipe"
BEHIND_TLS_ENV = "CCPIPE_BEHIND_TLS"


def behind_tls() -> bool:
    """True when nginx/Caddy/etc is terminating TLS in front of us.

    Toggles cookie-Secure, tightens cookie attrs, and enables
    TrustedHostMiddleware. Drives the `https_only` argument to
    SessionMiddleware and the cookie prefix.
    """
    return os.environ.get(BEHIND_TLS_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ─── State dir / paths ─────────────────────────────────────────────────────

def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ccpipe"


def _default_secret_path() -> Path:
    return _state_dir() / "session_secret"


def _default_credentials_path() -> Path:
    return _state_dir() / "credentials"


# ─── Session secret ────────────────────────────────────────────────────────

def load_or_create_secret() -> str:
    override = os.environ.get(_SECRET_FILE_ENV)
    path = Path(override) if override else _default_secret_path()
    if path.exists():
        try:
            secret = path.read_text().strip()
            if len(secret) >= 32:
                return secret
        except OSError as exc:
            log.warning("could not read session secret at %s: %s", path, exc)
        log.warning("session secret at %s looks short; regenerating", path)

    secret = secrets.token_urlsafe(48)
    _ensure_state_dir(path.parent)
    # Atomic 0600 create.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (secret + "\n").encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    log.info("generated new session secret at %s", path)
    return secret


# ─── Credentials ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Credential:
    username: str
    password: str
    # Monotonically increasing; bumped each time update_credential
    # rewrites the file. Sessions store the version they were issued
    # under; mismatched versions invalidate the session even though the
    # cookie's signature still validates.
    version: int = 0
    # Optional TOTP (RFC 6238) shared secret in base32. None = TOTP
    # disabled for this account. Enrolled via /api/auth/totp/* —
    # never returned to the client after the initial enroll exchange.
    totp_secret: str | None = None


def _system_username() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return "ccpipe"


def _generate_password() -> str:
    # ~96 bits of entropy; readable in a terminal and copy-pasteable.
    return secrets.token_urlsafe(12)


def _load_credentials_file(path: Path) -> Credential | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        # `version` is optional for backwards compatibility with files
        # written before the credential-version mechanism existed.
        raw_version = data.get("version", 0)
        try:
            version = max(0, int(raw_version))
        except (TypeError, ValueError):
            version = 0
        totp = data.get("totp_secret")
        if not (isinstance(totp, str) and totp.strip()):
            totp = None
        return Credential(
            username=str(data["username"]),
            password=str(data["password"]),
            version=version,
            totp_secret=totp,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("ignoring malformed credentials file %s: %s", path, exc)
        return None


def _ensure_state_dir(path: Path) -> None:
    """Create the state dir 0700 and tighten existing perms if it was
    pre-created (e.g. by an older ccpipe build) under the default umask.
    Without this, the directory is world-listable; local non-root users
    can confirm ccpipe is installed and read mtimes for usage patterns."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_credentials_file(path: Path, cred: Credential) -> None:
    _ensure_state_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Atomic 0600 create — closes the "world-readable for a microsecond" window.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    payload: dict[str, Any] = {
        "username": cred.username,
        "password": cred.password,
        "version": cred.version,
    }
    if cred.totp_secret:
        payload["totp_secret"] = cred.totp_secret
    try:
        os.write(fd, json.dumps(payload, indent=2).encode() + b"\n")
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _log_generated_banner(cred: Credential, path: Path) -> None:
    bar = "═" * 64
    log.warning(bar)
    log.warning("  GENERATED CCPIPE CREDENTIALS")
    log.warning("    username : %s", cred.username)
    log.warning("    password : %s", cred.password)
    log.warning("    file     : %s  (mode 0600)", path)
    log.warning("")
    log.warning("  To override, set environment in a systemd drop-in:")
    log.warning("    Environment=CCPIPE_AUTH_USERNAME=...")
    log.warning("    Environment=CCPIPE_AUTH_PASSWORD=...")
    log.warning("  Or delete the file above and restart ccpipe to regenerate.")
    log.warning(bar)


def _resolve_credential() -> Credential:
    env_user = os.environ.get(USERNAME_ENV, "").strip() or None
    env_pass = os.environ.get(PASSWORD_ENV, "").strip() or None
    path = Path(os.environ.get(CREDENTIALS_FILE_ENV) or _default_credentials_path())
    # TOTP enrollment is always file-backed even when username/password
    # come from the environment — otherwise CCPIPE_AUTH_PASSWORD would
    # silently disable any TOTP secret the user enrolled via the UI.
    file_cred = _load_credentials_file(path)
    file_totp = file_cred.totp_secret if file_cred else None
    if env_pass:
        return Credential(
            username=env_user or _system_username(),
            password=env_pass,
            totp_secret=file_totp,
        )
    if file_cred:
        return file_cred
    cred = Credential(
        username=env_user or _system_username(),
        password=_generate_password(),
    )
    try:
        _write_credentials_file(path, cred)
        _log_generated_banner(cred, path)
    except OSError as exc:
        log.error("could not persist generated credentials to %s: %s", path, exc)
        log.error("credentials will be regenerated on next restart unless persisted")
        _log_generated_banner(cred, path)
    return cred


_cached_credential: Credential | None = None


def get_credential() -> Credential:
    global _cached_credential
    if _cached_credential is None:
        _cached_credential = _resolve_credential()
    return _cached_credential


def reset_cached_credential() -> None:
    """For tests only — wipes the memoized credential so env/file changes
    take effect on the next get_credential() call."""
    global _cached_credential
    _cached_credential = None


# ─── Comparison ────────────────────────────────────────────────────────────

def _ct_eq(actual: str, expected: str) -> bool:
    """Constant-time compare with no length leak (both sides hashed first)."""
    a = hashlib.sha256(actual.encode("utf-8", errors="surrogateescape")).digest()
    b = hashlib.sha256(expected.encode("utf-8", errors="surrogateescape")).digest()
    return hmac.compare_digest(a, b)


def verify_credential(username: str, password: str) -> bool:
    cred = get_credential()
    user_ok = _ct_eq(username, cred.username)
    pass_ok = _ct_eq(password, cred.password)
    # Evaluate both regardless of which failed to avoid timing oracle on
    # username enumeration. `bool & bool` short-circuits at the Python level
    # only after both sides are evaluated; safe.
    return user_ok and pass_ok


# ─── TOTP ──────────────────────────────────────────────────────────────────

TOTP_ISSUER = "ccpipe"


def totp_enrolled() -> bool:
    return bool(get_credential().totp_secret)


def totp_verify(code: str) -> bool:
    """Constant-time-ish verify a 6-digit TOTP code against the
    enrolled secret. Accepts the previous and next 30-second window
    to tolerate clock drift between the server and the user's phone."""
    cred = get_credential()
    if not cred.totp_secret:
        return False
    if not isinstance(code, str):
        return False
    code = code.strip()
    if not code.isdigit() or len(code) not in (6, 7, 8):
        return False
    try:
        return pyotp.TOTP(cred.totp_secret).verify(code, valid_window=1)
    except Exception:
        return False


def totp_generate_secret() -> str:
    """Pyotp's helper returns a 32-char base32 string (160 bits)."""
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, username: str) -> str:
    """`otpauth://totp/...` URI suitable for QR-code rendering. The
    label includes the issuer + username so the user's authenticator
    app shows "ccpipe (alice)" rather than just "ccpipe"."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)


def set_totp_secret(secret: str | None) -> tuple[bool, str]:
    """Persist a TOTP secret (or clear it). Bumps the credential
    version so any existing session is invalidated — we want a
    just-enrolled user to log back in fresh, and a just-disabled
    user to lose any pending-OTP session they might be carrying."""
    cred = get_credential()
    new_cred = Credential(
        username=cred.username,
        password=cred.password,
        version=cred.version + 1,
        totp_secret=secret if secret else None,
    )
    path = Path(os.environ.get(CREDENTIALS_FILE_ENV) or _default_credentials_path())
    try:
        _write_credentials_file(path, new_cred)
    except OSError as exc:
        return False, f"failed to write credentials: {exc}"
    reset_cached_credential()
    return True, "updated"


def update_credential(*, current_password: str,
                       new_username: str | None,
                       new_password: str | None) -> tuple[bool, str]:
    """Verify current password, then write new credentials atomically.

    The current password is checked against the *current* persisted
    credential (regardless of session). Empty new_* values mean "keep
    existing". Returns ``(success, message)``.

    Bumps ``Credential.version`` on every successful write — sessions
    issued under the previous version are invalidated even though
    their signed cookie still verifies, so e.g. a stolen device that
    held an authenticated cookie can be locked out by changing the
    password.
    """
    cred = get_credential()
    if not _ct_eq(current_password, cred.password):
        return False, "current password is wrong"

    new_user = (new_username or "").strip() or cred.username
    new_pass = new_password if new_password is not None and new_password != "" else cred.password
    if not new_user or not new_pass:
        return False, "username and password must be non-empty"
    # Check "no change" BEFORE length so an existing short legacy
    # password isn't double-flagged ("too short" + "same") when the
    # user accidentally retypes it; the more specific message wins.
    if new_pass == cred.password and new_user == cred.username:
        return False, "no change — new credentials match the current ones"
    if len(new_pass) < 8:
        return False, "password too short (min 8 chars)"

    new_cred = Credential(
        username=new_user,
        password=new_pass,
        version=cred.version + 1,
    )
    path = Path(os.environ.get(CREDENTIALS_FILE_ENV) or _default_credentials_path())
    try:
        _write_credentials_file(path, new_cred)
    except OSError as exc:
        return False, f"failed to write credentials: {exc}"
    reset_cached_credential()
    return True, "updated"


# ─── FastAPI types & deps ──────────────────────────────────────────────────

class LoginBody(BaseModel):
    username: str
    password: str
    # Optional second-factor code. When the user has TOTP enrolled, a
    # password-only login returns AuthStatus(otp_required=True) without
    # setting the session, and the client resubmits with the same body
    # plus a six-digit code in this field.
    code: str | None = None


class AuthStatus(BaseModel):
    required: bool
    authenticated: bool
    username: str | None = None
    # `True` when the server expects an additional TOTP code before
    # granting the session. Clients render the code-entry step in
    # response. Always False on a fully-authenticated response.
    otp_required: bool = False
    # Whether the account has TOTP enrolled. Surfaced so the Settings
    # UI can show "two-factor: enrolled / disabled" without exposing
    # the secret itself.
    otp_enrolled: bool = False


def is_auth_enabled() -> bool:
    """Auth is always on. Kept for API compatibility and explicit reads."""
    return True


def is_session_authed(session: dict) -> bool:
    if not session.get("authed"):
        return False
    # Reject sessions issued under a stale credential version. Sessions
    # without a stored version are pre-versioning cookies — invalidate
    # them too so a password change has the same effect on old clients.
    stored = session.get("cred_version")
    if not isinstance(stored, int) or stored != get_credential().version:
        return False
    return True


def session_username(session: dict) -> str | None:
    v = session.get("username")
    return v if isinstance(v, str) else None


def require_auth(request: Request) -> None:
    """FastAPI dependency for HTTP routes."""
    if not is_session_authed(request.session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )


async def authorize_websocket(websocket: WebSocket) -> bool:
    """Returns True if the WS may proceed. Closes the socket if not.

    Defends against:
      - Cross-Site WebSocket Hijacking: the Origin header is matched against
        an allowlist (CCPIPE_ALLOWED_ORIGINS or the request's own Host).
        SameSite=Lax does NOT block WS upgrades, so without this check any
        malicious page you visit can steal an authenticated session.
      - Unauthenticated access: session cookie must indicate a valid login.
    """
    if not _origin_allowed(websocket):
        await websocket.close(code=1008, reason="origin not allowed")
        return False
    session = websocket.scope.get("session") or {}
    if not is_session_authed(session):
        await websocket.close(code=1008, reason="authentication required")
        return False
    return True


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    # No Origin header: browsers always set it for cross-origin upgrades;
    # most non-browser clients omit it. Reject to be safe.
    if not origin:
        log.warning("ws rejected: missing Origin header")
        return False
    allowed = _allowed_origins(websocket)
    if origin in allowed:
        return True
    log.warning("ws rejected: origin %r not in allowed list %r", origin, allowed)
    return False


def _allowed_origins(websocket: WebSocket) -> set[str]:
    extra = os.environ.get(ALLOWED_ORIGINS_ENV, "").strip()
    out: set[str] = set()
    if extra:
        for part in extra.split(","):
            part = part.strip()
            if part:
                out.add(part)
    # Always allow same-host upgrades. Host is the value the browser used
    # to reach us, so this naturally permits localhost, the LAN IP, or any
    # hostname the user has set up.
    host = websocket.headers.get("host")
    if host:
        # Browsers may send Origin with either scheme; allow both.
        out.add(f"http://{host}")
        out.add(f"https://{host}")
    return out


def require_csrf(request: Request) -> None:
    """FastAPI dependency for state-changing routes.

    Browsers can't add a custom request header from <form> elements; any
    cross-origin fetch() with this header triggers a CORS preflight that
    we don't answer. So presence of CSRF_HEADER_NAME == CSRF_HEADER_VALUE
    means the call came from our own frontend code.
    """
    if request.headers.get(CSRF_HEADER_NAME, "").lower() != CSRF_HEADER_VALUE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="missing csrf header",
        )


AuthDep = Depends(require_auth)
CsrfDep = Depends(require_csrf)
