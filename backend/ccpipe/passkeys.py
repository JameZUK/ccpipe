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
"""
from __future__ import annotations

import ipaddress
import json
import logging
import secrets
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
MAX_CHALLENGES = 64       # outstanding at once; the oldest goes first
PENDING_TTL_S = 300.0     # a passkey sign-in waiting for a password / code
MAX_PENDING = 32


class PasskeyError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class _Challenge:
    purpose: str          # "login" | "register"
    rp_id: str
    origin: str
    expires: float
    sid: str | None       # registration: the session that asked for it


_challenges: dict[str, _Challenge] = {}
_pending: dict[str, tuple[frozenset[str], float, int]] = {}


def reset_state() -> None:
    """For tests: forget outstanding challenges and pending sign-ins."""
    _challenges.clear()
    _pending.clear()


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

def _remember(challenge: bytes, purpose: str, rp_id: str, origin: str,
              sid: str | None) -> None:
    now = time.monotonic()
    for k in [k for k, c in _challenges.items() if c.expires < now]:
        del _challenges[k]
    while len(_challenges) >= MAX_CHALLENGES:
        del _challenges[next(iter(_challenges))]
    _challenges[bytes_to_base64url(challenge)] = _Challenge(
        purpose, rp_id, origin, now + CHALLENGE_TTL_S, sid)


def _take(credential: dict[str, Any], purpose: str, rp_id: str, origin: str,
          sid: str | None) -> bytes | None:
    """The challenge this response answers, consumed — or None when it
    isn't one we issued for this purpose, place and session."""
    try:
        raw = credential["response"]["clientDataJSON"]
        client = parse_client_data_json(base64url_to_bytes(raw))
    except Exception:
        return None
    key = bytes_to_base64url(client.challenge)
    ch = _challenges.pop(key, None)
    if ch is None or ch.expires < time.monotonic():
        return None
    if ch.purpose != purpose or ch.rp_id != rp_id or ch.origin != origin:
        return None
    if purpose == "register" and (sid is None or ch.sid != sid):
        return None
    return client.challenge


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
    _remember(opts.challenge, "register", rp_id, origin, sid)
    return json.loads(options_to_json(opts))


def finish_registration(conn: HTTPConnection, sid: str | None,
                        credential: dict[str, Any], name: str) -> auth.Passkey:
    rp_id, origin = relying_party(conn)
    challenge = _take(credential, "register", rp_id, origin, sid)
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
    _remember(opts.challenge, "login", rp_id, origin, None)
    return json.loads(options_to_json(opts))


def verify_login(conn: HTTPConnection, credential: dict[str, Any]) -> bool:
    """True when *credential* is a fresh, user-verified assertion by one
    of this account's passkeys. Records the use (counter + time)."""
    try:
        rp_id, origin = relying_party(conn)
    except PasskeyError:
        return False
    challenge = _take(credential, "login", rp_id, origin, None)
    if challenge is None:
        return False
    cred = auth.get_credential()
    try:
        parsed = parse_authentication_credential_json(credential)
    except Exception:
        return False
    key_id = bytes_to_base64url(parsed.raw_id)
    key = next((k for k in cred.passkeys if k.id == key_id), None)
    if key is None:
        log.warning("passkey sign-in with an unknown credential")
        return False
    handle = parsed.response.user_handle
    if handle is not None and cred.webauthn_user_id \
            and handle != base64url_to_bytes(cred.webauthn_user_id):
        return False
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
        return False
    auth.passkey_used(key.id, v.new_sign_count, bool(v.credential_backed_up))
    return True


# ─── A passkey sign-in that still needs a password or code ────────────────

def hold(verified: set[str]) -> str:
    """Park the factors proven so far; the ticket goes in the session
    cookie and is good for one more attempt within PENDING_TTL_S."""
    now = time.monotonic()
    for k in [k for k, (_f, exp, _v) in _pending.items() if exp < now]:
        del _pending[k]
    while len(_pending) >= MAX_PENDING:
        del _pending[next(iter(_pending))]
    ticket = secrets.token_urlsafe(18)
    _pending[ticket] = (frozenset(verified), now + PENDING_TTL_S, auth.get_credential().version)
    return ticket


def redeem(ticket: Any) -> set[str]:
    """The factors parked under *ticket* (consumed), or none."""
    if not isinstance(ticket, str):
        return set()
    held = _pending.pop(ticket, None)
    if held is None:
        return set()
    factors, expires, version = held
    if expires < time.monotonic() or version != auth.get_credential().version:
        return set()
    return set(factors)
