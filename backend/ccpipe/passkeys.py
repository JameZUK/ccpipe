"""Passkeys (WebAuthn): registering one, and signing in with it.

The cryptography is py_webauthn's (Duo Labs): it checks the signature,
the challenge, the origin, the relying-party hash and the flags. This
module keeps the one-use challenges, works out the relying party — the
site a passkey belongs to — from the page's own origin, and turns a
verified registration into an ``auth.Passkey``.

- **Relying party.** It's the host the page was loaded from, e.g.
  ``ccpipe.example.org``. The Origin header must be one ccpipe accepts
  anyway (``auth.allowed_origins``: CCPIPE_ALLOWED_ORIGINS, or the Host
  on plain-HTTP LAN installs). Browsers only offer WebAuthn over HTTPS
  or on ``localhost``, and never for an IP address, so a passkey made on
  one address doesn't work on another.
- **User verification is required**, both ways: the device's biometric
  or PIN is checked every time, which is what lets a passkey count as
  a complete sign-in on its own (when the account allows that).
- **Discoverable.** Sign-in sends no credential list, so the options
  reveal nothing about the account (no ids, not even whether a passkey
  exists); the authenticator offers what it holds for this site.
- **Challenges are single-use and server-side** (5 min), so a captured
  assertion can't be replayed even with a copy of the session cookie.
  Sign-in challenges are capped per client address (a pre-auth flood
  from one address only pushes out its own); a registration's is held
  per session, apart from them.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    parse_authentication_credential_json,
    parse_client_data_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import auth

log = logging.getLogger(__name__)

RP_NAME = "ccpipe"
TIMEOUT_MS = 120_000
CHALLENGE_TTL_S = 300.0
MAX_LOGIN_PER_CLIENT = 8  # sign-in challenges outstanding per client address
MAX_LOGIN = 512           # … and in all; the oldest goes first
MAX_REGISTER = 64         # registrations in progress (one per session)
PENDING_TTL_S = 300.0     # a passkey sign-in waiting for a password / code
MAX_PENDING = 32


class PasskeyError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class _Challenge:
    challenge: bytes
    rp_id: str
    origin: str
    expires: float
    client: str = ""      # sign-in: the address that asked


# Sign-in challenges by base64url challenge; registrations by session id.
# Routes run these from worker threads too, hence the lock.
_login: dict[str, _Challenge] = {}
_register: dict[str, _Challenge] = {}
# ticket → (factors, expires, credential version, passkey id)
_pending: dict[str, tuple[frozenset[str], float, int, str]] = {}
_lock = threading.Lock()


def reset_state() -> None:
    """For tests: forget outstanding challenges and pending sign-ins."""
    with _lock:
        _login.clear()
        _register.clear()
        _pending.clear()


def page_origin(conn: HTTPConnection) -> str:
    """The page's origin as the browser sees it. POSTs carry it in the
    Origin header; a same-origin GET doesn't, so build it from the Host —
    with https when ccpipe is told it's behind TLS, even if the proxy's
    X-Forwarded-Proto isn't trusted (uvicorn's --forwarded-allow-ips)."""
    origin = conn.headers.get("origin")
    if origin:
        return origin
    scheme = "https" if auth.behind_tls() else conn.url.scheme
    return f"{scheme}://{conn.headers.get('host', '')}"


# ─── Relying party ─────────────────────────────────────────────────────────

def relying_party(conn: HTTPConnection, origin: str | None = None) -> tuple[str, str]:
    """(rp_id, origin) for this request, or PasskeyError when passkeys
    can't work at this address. *origin* defaults to the Origin header
    (browsers send it on every POST; a same-origin GET has none)."""
    origin = origin or conn.headers.get("origin", "")
    if not origin or origin not in auth.allowed_origins(conn):
        raise PasskeyError(400, "passkeys only work at ccpipe's own address")
    parts = urlsplit(origin)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
        raise PasskeyError(400, "passkeys need a host name, not an IP address")
    except ValueError:
        pass
    local = host == "localhost" or host.endswith(".localhost")
    if not host or (parts.scheme != "https" and not local):
        raise PasskeyError(400, "passkeys need HTTPS")
    return host, origin


# ─── Challenges ────────────────────────────────────────────────────────────

def _gc(store: dict[str, _Challenge], now: float) -> None:
    for k in [k for k, c in store.items() if c.expires < now]:
        del store[k]


def _client_of(conn: HTTPConnection) -> str:
    return auth.client_bucket(conn)


def _remember_login(challenge: bytes, rp_id: str, origin: str, client: str) -> None:
    now = time.monotonic()
    with _lock:
        _gc(_login, now)
        mine = [k for k, c in _login.items() if c.client == client]
        for k in mine[:max(0, len(mine) - MAX_LOGIN_PER_CLIENT + 1)]:
            del _login[k]
        while len(_login) >= MAX_LOGIN:
            del _login[next(iter(_login))]
        _login[bytes_to_base64url(challenge)] = _Challenge(
            challenge, rp_id, origin, now + CHALLENGE_TTL_S, client)


def _remember_register(challenge: bytes, rp_id: str, origin: str, sid: str) -> None:
    now = time.monotonic()
    with _lock:
        _gc(_register, now)
        _register.pop(sid, None)
        while len(_register) >= MAX_REGISTER:
            del _register[next(iter(_register))]
        _register[sid] = _Challenge(challenge, rp_id, origin, now + CHALLENGE_TTL_S)


def _signed_challenge(credential: dict[str, Any]) -> bytes | None:
    """The challenge the browser signed (from clientDataJSON)."""
    try:
        raw = credential["response"]["clientDataJSON"]
        return parse_client_data_json(base64url_to_bytes(raw)).challenge
    except Exception:
        return None


def _usable(ch: _Challenge | None, signed: bytes, rp_id: str, origin: str) -> bool:
    return (ch is not None and ch.expires >= time.monotonic()
            and secrets.compare_digest(ch.challenge, signed)
            and ch.rp_id == rp_id and ch.origin == origin)


def _take_login(credential: dict[str, Any], rp_id: str, origin: str) -> bytes | None:
    """The sign-in challenge this response answers, consumed — or None
    when it isn't one we issued for this place."""
    signed = _signed_challenge(credential)
    if signed is None:
        return None
    with _lock:
        ch = _login.pop(bytes_to_base64url(signed), None)
    return signed if _usable(ch, signed, rp_id, origin) else None


def _take_register(credential: dict[str, Any], rp_id: str, origin: str,
                   sid: str | None) -> bytes | None:
    """This session's registration challenge, consumed, if the response
    answers it."""
    signed = _signed_challenge(credential)
    if signed is None or not sid:
        return None
    with _lock:
        ch = _register.pop(sid, None)
    return signed if _usable(ch, signed, rp_id, origin) else None


# ─── Registration ──────────────────────────────────────────────────────────

def registration_options(conn: HTTPConnection, sid: str | None) -> dict[str, Any]:
    rp_id, origin = relying_party(conn)
    cred = auth.get_credential()
    uid = auth.webauthn_user_id()
    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_name=cred.username,
        user_display_name=cred.username,
        user_id=base64url_to_bytes(uid),
        timeout=TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k.id))
                             for k in cred.passkeys if k.rp_id == rp_id],
    )
    if not sid:
        raise PasskeyError(400, "sign in again first")
    _remember_register(opts.challenge, rp_id, origin, sid)
    return json.loads(options_to_json(opts))


def finish_registration(conn: HTTPConnection, sid: str | None,
                        credential: dict[str, Any], name: str) -> auth.Passkey:
    rp_id, origin = relying_party(conn)
    challenge = _take_register(credential, rp_id, origin, sid)
    if challenge is None:
        raise PasskeyError(400, "that request expired; try again")
    try:
        v = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as exc:
        log.warning("passkey registration refused: %s", exc)
        raise PasskeyError(400, "the passkey couldn't be verified") from None
    transports = credential.get("response", {}).get("transports")
    return auth.Passkey(
        id=bytes_to_base64url(v.credential_id),
        public_key=bytes_to_base64url(v.credential_public_key),
        sign_count=v.sign_count,
        name=(name or "").strip()[:64] or "passkey",
        created=int(time.time()),
        rp_id=rp_id,
        transports=tuple(t for t in transports if isinstance(t, str))[:8]
        if isinstance(transports, list) else (),
        backed_up=bool(v.credential_backed_up),
    )


# ─── Sign-in ───────────────────────────────────────────────────────────────

def login_options(conn: HTTPConnection) -> dict[str, Any]:
    rp_id, origin = relying_party(conn)
    opts = generate_authentication_options(
        rp_id=rp_id,
        timeout=TIMEOUT_MS,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    _remember_login(opts.challenge, rp_id, origin, _client_of(conn))
    return json.loads(options_to_json(opts))


def verify_login(conn: HTTPConnection, credential: dict[str, Any]) -> str | None:
    """The passkey's id when *credential* is a fresh, user-verified
    assertion by one of this account's passkeys, else None. Records the
    use (counter + time), so call it off the event loop."""
    try:
        rp_id, origin = relying_party(conn)
    except PasskeyError:
        return None
    challenge = _take_login(credential, rp_id, origin)
    if challenge is None:
        return None
    cred = auth.get_credential()
    try:
        parsed = parse_authentication_credential_json(credential)
    except Exception:
        return None
    key_id = bytes_to_base64url(parsed.raw_id)
    key = next((k for k in cred.passkeys if k.id == key_id), None)
    if key is None:
        log.warning("passkey sign-in with an unknown credential")
        return None
    handle = parsed.response.user_handle
    if handle is not None and cred.webauthn_user_id \
            and handle != base64url_to_bytes(cred.webauthn_user_id):
        return None
    try:
        v = verify_authentication_response(
            credential=parsed,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=base64url_to_bytes(key.public_key),
            credential_current_sign_count=key.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        log.warning("passkey sign-in refused (%s): %s", key.name, exc)
        return None
    auth.passkey_used(key.id, v.new_sign_count, bool(v.credential_backed_up))
    return key.id


# ─── A passkey sign-in that still needs a password or code ────────────────

def hold(verified: set[str], key_id: str) -> str:
    """Park the factors proven so far (*key_id*: the passkey that proved
    one); the ticket goes in the session cookie and is good for one more
    attempt within PENDING_TTL_S."""
    now = time.monotonic()
    ticket = secrets.token_urlsafe(18)
    version = auth.get_credential().version
    with _lock:
        for k in [k for k, held in _pending.items() if held[1] < now]:
            del _pending[k]
        while len(_pending) >= MAX_PENDING:
            del _pending[next(iter(_pending))]
        _pending[ticket] = (frozenset(verified), now + PENDING_TTL_S, version, key_id)
    return ticket


def redeem(ticket: Any) -> tuple[set[str], str]:
    """The factors parked under *ticket* (consumed) and the passkey that
    proved them — or nothing, once expired, after a credential change, or
    when that passkey has been removed since."""
    if not isinstance(ticket, str):
        return set(), ""
    with _lock:
        held = _pending.pop(ticket, None)
    if held is None:
        return set(), ""
    factors, expires, version, key_id = held
    cred = auth.get_credential()
    if expires < time.monotonic() or version != cred.version \
            or not any(k.id == key_id for k in cred.passkeys):
        return set(), ""
    return set(factors), key_id
