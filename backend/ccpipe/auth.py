"""Authentication.

ccpipe binds 0.0.0.0 by default, so auth is **always on**. Credentials are
resolved in this order:

  1. A persisted file at ``~/.local/state/ccpipe/credentials`` (0600).
     Once this file exists it is **authoritative** — the in-UI password
     change writes here, and subsequent restarts read from here.
  2. ``CCPIPE_AUTH_PASSWORD`` (optionally with ``CCPIPE_AUTH_USERNAME``)
     as a **bootstrap seed** when no credentials file exists yet. On
     first run the env value is hashed and written to the file; from
     then on the file wins and the env var is ignored. This means an
     in-UI password change persists across restarts (previous behaviour
     silently let the env value override the file on every read, making
     the UI change a no-op).
  3. Auto-generate a random password, argon2id-hash it into the credentials
     file, and write the *plaintext* into a sidecar
     ``~/.local/state/ccpipe/initial_password.txt`` (0400). The plaintext is
     never logged — the operator is told once at startup to ``cat`` the
     sidecar and then delete it.

To rotate to a new env-pinned password after the file has been seeded,
delete the credentials file (the service will re-seed from env on next
start) — or just use Settings → Account in the UI.

Stored passwords are argon2id hashes; the plaintext only exists in memory
when verifying a login. Legacy credential files containing a cleartext
``"password"`` field are migrated to ``"password_hash"`` on first read.

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
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError, VerificationError
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
        except OSError as exc:
            # DO NOT silently regenerate on a transient I/O error — that
            # would invalidate every active session because of a hiccup
            # like EIO/EACCES/FUSE-stale. Re-raise so the operator sees
            # the underlying issue and can fix permissions / disk.
            log.error("failed to read session secret at %s: %s", path, exc)
            raise
        if len(secret) >= 32:
            return secret
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

# argon2id with library defaults — fast enough for an interactive login,
# slow enough to make an offline brute-force expensive. The PasswordHasher
# instance is module-level so the parameters live in one place.
_password_hasher = PasswordHasher()


@dataclass(frozen=True)
class Credential:
    username: str
    # argon2id PHC-encoded hash. Always a hash; never plaintext. For
    # env-provided passwords we hash at load time and discard the
    # plaintext.
    password_hash: str
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


def _hash_password(plain: str) -> str:
    return _password_hasher.hash(plain)


def _looks_like_argon2_hash(s: str) -> bool:
    # PHC-formatted argon2 hashes always begin with "$argon2".
    return isinstance(s, str) and s.startswith("$argon2")


def _load_credentials_file(path: Path) -> tuple[Credential | None, bool]:
    """Return (credential, needs_migration_write). If the file held a
    plaintext password (legacy schema), we hash it in-memory and signal
    that the caller should rewrite the file."""
    if not path.exists():
        return None, False
    try:
        data = json.loads(path.read_text())
        raw_version = data.get("version", 0)
        try:
            version = max(0, int(raw_version))
        except (TypeError, ValueError):
            version = 0
        totp = data.get("totp_secret")
        if not (isinstance(totp, str) and totp.strip()):
            totp = None
        # Prefer the new field; fall back to the legacy plaintext field
        # and hash it. Either way we end up with a hash in memory.
        raw_hash = data.get("password_hash")
        needs_migration = False
        if isinstance(raw_hash, str) and _looks_like_argon2_hash(raw_hash):
            pw_hash = raw_hash
        else:
            legacy = data.get("password")
            if not isinstance(legacy, str) or not legacy:
                raise KeyError("password")
            pw_hash = _hash_password(legacy)
            needs_migration = True
        return (
            Credential(
                username=str(data["username"]),
                password_hash=pw_hash,
                version=version,
                totp_secret=totp,
            ),
            needs_migration,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("ignoring malformed credentials file %s: %s", path, exc)
        return None, False


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
        "password_hash": cred.password_hash,
        "version": cred.version,
    }
    if cred.totp_secret:
        payload["totp_secret"] = cred.totp_secret
    try:
        os.write(fd, json.dumps(payload, indent=2).encode() + b"\n")
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _initial_password_sidecar_for(creds_path: Path) -> Path:
    # Sit next to the credentials file so an operator who overrides
    # CCPIPE_CREDENTIALS_FILE finds both in the same directory.
    return creds_path.parent / "initial_password.txt"


def _write_initial_password_sidecar(username: str, password: str, creds_path: Path) -> Path:
    """Write the generated plaintext password to a read-once sidecar
    file (mode 0400) so the operator can recover it after first boot.
    The plaintext is NEVER logged — only the path is."""
    sidecar = _initial_password_sidecar_for(creds_path)
    _ensure_state_dir(sidecar.parent)
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    body = (
        f"ccpipe generated initial credentials\n"
        f"username: {username}\n"
        f"password: {password}\n"
        f"\n"
        f"The hash is stored in {creds_path} (mode 0600).\n"
        f"Delete this file after you have read the password once.\n"
    )
    try:
        os.write(fd, body.encode())
    finally:
        os.close(fd)
    os.replace(tmp, sidecar)
    return sidecar


def _announce_generated_credentials(username: str, creds_path: Path, sidecar: Path) -> None:
    """Tell the operator that initial credentials exist and where to
    find them — without exposing the plaintext to the logger (and
    thereby to the journal, backups, log shippers, etc.)."""
    bar = "═" * 64
    log.warning(bar)
    log.warning("  GENERATED CCPIPE CREDENTIALS")
    log.warning("    username : %s", username)
    log.warning("    password : (not logged — see sidecar file)")
    log.warning("    creds    : %s  (mode 0600, hashed)", creds_path)
    log.warning("    sidecar  : %s  (mode 0400, plaintext, read-once)", sidecar)
    log.warning("")
    log.warning("  Recover the password ONCE with:")
    log.warning("    cat %s", sidecar)
    log.warning("  Then delete the sidecar:")
    log.warning("    shred -u %s", sidecar)
    log.warning("")
    log.warning("  Change the password later: Settings → Account in the UI.")
    log.warning("  To re-seed from env, delete the credentials file and set")
    log.warning("    Environment=CCPIPE_AUTH_USERNAME=...")
    log.warning("    Environment=CCPIPE_AUTH_PASSWORD=...")
    log.warning("  in a systemd drop-in / launchd plist before next start.")
    log.warning(bar)


def _resolve_credential() -> Credential:
    env_user = os.environ.get(USERNAME_ENV, "").strip() or None
    env_pass = os.environ.get(PASSWORD_ENV, "").strip() or None
    path = Path(os.environ.get(CREDENTIALS_FILE_ENV) or _default_credentials_path())
    file_cred, needs_migration = _load_credentials_file(path)
    # Opportunistic migration: if the on-disk file was legacy plaintext
    # we just rewrote it in memory as an argon2 hash; flush that hash
    # back to disk so subsequent reads are clean.
    if file_cred and needs_migration:
        try:
            _write_credentials_file(path, file_cred)
            log.warning("migrated plaintext credentials at %s to argon2id hash", path)
        except OSError as exc:
            log.error("could not rewrite migrated credentials to %s: %s", path, exc)
    # File wins once it exists. CCPIPE_AUTH_PASSWORD is a bootstrap seed
    # only; before this change env_pass was re-evaluated on every read
    # and silently overrode the file, making the in-UI password change
    # a no-op (the file was rewritten, the next read ignored it). Once
    # the file exists, the only way to change credentials is via the UI
    # or by deleting the file to force a re-seed from env on next start.
    # A side benefit: env_pass no longer needs to be re-hashed (argon2id)
    # on every process start to satisfy a comparison that never happens.
    if file_cred:
        if env_pass:
            log.info(
                "CCPIPE_AUTH_PASSWORD is set but %s already exists — "
                "env value ignored (file is authoritative). Delete the "
                "file to re-seed from env.", path,
            )
        return file_cred
    if env_pass:
        # First run with env-pinned creds: hash and persist so the file
        # becomes authoritative from the next read onward.
        cred = Credential(
            username=env_user or _system_username(),
            password_hash=_hash_password(env_pass),
        )
        try:
            _write_credentials_file(path, cred)
        except OSError as exc:
            log.error(
                "could not persist env-seeded credentials to %s: %s "
                "(login will work, but an in-UI password change cannot "
                "be saved)", path, exc,
            )
        return cred
    plain = _generate_password()
    cred = Credential(
        username=env_user or _system_username(),
        password_hash=_hash_password(plain),
    )
    try:
        _write_credentials_file(path, cred)
        sidecar = _write_initial_password_sidecar(cred.username, plain, path)
        _announce_generated_credentials(cred.username, path, sidecar)
    except OSError as exc:
        log.error("could not persist generated credentials to %s: %s", path, exc)
        log.error("the plaintext is being printed to stderr ONCE — capture it now:")
        # Fallback: if we couldn't write the sidecar (read-only state
        # dir?), we have no choice but to surface the password somewhere
        # the operator can see. Use stderr directly, not the logger, so
        # at least it doesn't pass through any logging.handlers config.
        print(
            f"\nccpipe initial credentials (regen on every restart):\n"
            f"  username: {cred.username}\n"
            f"  password: {plain}\n",
            file=sys.stderr,
            flush=True,
        )
    # Wipe the plaintext from this function's locals — defence-in-depth
    # against memory dumps. (Python doesn't guarantee this, but it's
    # cheap and unambiguously signals intent.)
    plain = ""  # noqa: F841
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
    """Constant-time compare with no length leak (both sides hashed first).
    Used for usernames; passwords go through argon2.verify which is
    already constant-time."""
    a = hashlib.sha256(actual.encode("utf-8", errors="surrogateescape")).digest()
    b = hashlib.sha256(expected.encode("utf-8", errors="surrogateescape")).digest()
    return hmac.compare_digest(a, b)


# Spare hash for the "wrong username" branch of verify_credential. We
# always run argon2.verify against *something* so the timing of a
# wrong-username login matches that of a wrong-password login, denying
# the attacker a cheap "is this username valid?" oracle. Generated once
# at import for a fixed dummy password.
#
# Timing equivalence depends on the decoy hash and the real stored hash
# using the same argon2 parameters (m, t, p). Today both are produced by
# the same `_password_hasher` with library defaults, so they match. If
# we ever bump the params (e.g. to harden against a future GPU attack),
# regenerate this constant from the same params or the oracle reappears.
_DECOY_HASH = _password_hasher.hash("ccpipe-decoy")


def _verify_password(candidate: str, stored_hash: str) -> bool:
    try:
        _password_hasher.verify(stored_hash, candidate)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_credential(username: str, password: str) -> bool:
    cred = get_credential()
    user_ok = _ct_eq(username, cred.username)
    # Always run a verify — against the real hash if the username matched,
    # or against a decoy if it didn't — so timing doesn't leak whether
    # the username was right.
    if user_ok:
        pass_ok = _verify_password(password, cred.password_hash)
    else:
        _verify_password(password, _DECOY_HASH)
        pass_ok = False
    return user_ok and pass_ok


# ─── TOTP ──────────────────────────────────────────────────────────────────

TOTP_ISSUER = "ccpipe"


def totp_enrolled() -> bool:
    return bool(get_credential().totp_secret)


# In-memory burn-list of (code, slot) pairs that have already been used
# successfully. pyotp.TOTP.verify(valid_window=1) accepts the previous,
# current, and next 30-second slots — without this burn-list, a code
# shoulder-surfed once is usable for ~90s on a second device. We expire
# burned entries after 120s, comfortably past the verify window.
_totp_burned: dict[tuple[str, int], float] = {}
_TOTP_BURN_TTL_S = 120.0


def _gc_burned_codes(now: float) -> None:
    """Evict entries older than the burn TTL. Called from totp_verify so
    the dict can't grow unbounded under a flood of valid codes."""
    cutoff = now - _TOTP_BURN_TTL_S
    stale = [k for k, t in _totp_burned.items() if t < cutoff]
    for k in stale:
        _totp_burned.pop(k, None)


def totp_verify(code: str) -> bool:
    """Constant-time-ish verify a 6-digit TOTP code against the
    enrolled secret. Accepts the previous and next 30-second window
    to tolerate clock drift between the server and the user's phone.

    Successfully-verified codes are recorded in an in-memory burn-list
    for the next 120 seconds; presenting the same code a second time
    (even within the still-valid window) is refused. Stops a single
    shoulder-surfed code from being reusable across the verify window."""
    cred = get_credential()
    if not cred.totp_secret:
        return False
    if not isinstance(code, str):
        return False
    code = code.strip()
    if not code.isdigit() or len(code) not in (6, 7, 8):
        return False
    now = time.time()
    _gc_burned_codes(now)
    totp = pyotp.TOTP(cred.totp_secret)
    # Identify which slot accepted the code so the burn key includes
    # the slot timestamp. We try the same window pyotp does internally.
    current_slot = int(now // totp.interval)
    for delta in (-1, 0, 1):
        slot = current_slot + delta
        try:
            expected = totp.at(slot * totp.interval)
        except Exception:
            continue
        if hmac.compare_digest(expected, code):
            key = (code, slot)
            if key in _totp_burned:
                # Replay within the burn window — refuse.
                return False
            _totp_burned[key] = now
            return True
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
        password_hash=cred.password_hash,
        version=cred.version + 1,
        totp_secret=secret if secret else None,
    )
    path = Path(os.environ.get(CREDENTIALS_FILE_ENV) or _default_credentials_path())
    try:
        _write_credentials_file(path, new_cred)
    except OSError as exc:
        return False, f"failed to write credentials: {exc}"
    reset_cached_credential()
    # Drop burn-list entries from the previous secret — they'd never
    # collide in practice (different secret → different code digits at
    # the same slot) but they'd sit in memory for 120s after every
    # rotation. Small hygiene win.
    _totp_burned.clear()
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
    if not _verify_password(current_password, cred.password_hash):
        return False, "current password is wrong"

    new_user = (new_username or "").strip() or cred.username
    new_pass_plain = new_password if new_password is not None and new_password != "" else None

    # When the user didn't supply a new password, keep the existing
    # hash verbatim — re-hashing the same plaintext would change the
    # salt, which is fine, but we can't even do that here since we
    # don't have the plaintext. So pass-through is the only option.
    if new_pass_plain is None:
        if new_user == cred.username:
            return False, "no change — new credentials match the current ones"
        new_hash = cred.password_hash
    else:
        # Check "no change" BEFORE length so an existing short legacy
        # password isn't double-flagged when the user accidentally
        # retypes it; the more specific message wins. We can only
        # detect "same password" by verifying against the stored hash,
        # since we don't have the old plaintext.
        if _verify_password(new_pass_plain, cred.password_hash) and new_user == cred.username:
            return False, "no change — new credentials match the current ones"
        if len(new_pass_plain) < 8:
            return False, "password too short (min 8 chars)"
        new_hash = _hash_password(new_pass_plain)

    if not new_user:
        return False, "username must be non-empty"

    new_cred = Credential(
        username=new_user,
        password_hash=new_hash,
        version=cred.version + 1,
        totp_secret=cred.totp_secret,
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
    """Build the WS upgrade allowlist.

    When ``CCPIPE_ALLOWED_ORIGINS`` is set, use that exclusively — the
    operator has declared the canonical origin(s) and we don't second-
    guess. This is the recommended posture for any non-localhost
    deployment.

    When it's unset, fall back to ``http(s)://<host-header>`` to keep
    the zero-config LAN-HTTP path working (the default deployment).
    The Host header is browser-determined, so this is safe against
    script-driven CSWSH (SameSite-Lax keeps the auth cookie off the
    upgrade) but it's a softer gate than a configured allowlist — under
    TLS the operator should always set ``CCPIPE_ALLOWED_ORIGINS``.
    """
    extra = os.environ.get(ALLOWED_ORIGINS_ENV, "").strip()
    if extra:
        out: set[str] = set()
        for part in extra.split(","):
            part = part.strip()
            if part:
                out.add(part)
        return out
    # Fallback: derive from the request's Host header.
    out = set()
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
