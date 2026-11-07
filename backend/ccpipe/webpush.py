"""Web Push, the sending side: what gets a notification to a phone, tablet or
browser when no ccpipe page is open on it.

A device that turns notifications on hands the server a subscription: an
endpoint URL at its browser maker's push service (Mozilla's for Firefox,
Apple's for an iPad or iPhone home-screen app, Google's for Chrome) and two
keys. A notification is POSTed to that endpoint:

  - encrypted for the device (RFC 8291, "aes128gcm"): the push service
    relays it but can't read it;
  - signed with this server's own key (VAPID, RFC 8292), which the device
    was subscribed with, so no one else can push to it.

The push service still sees that a message went to that device, and when.
Built on ``cryptography`` and ``httpx`` (both already dependencies); the
encryption is checked against RFC 8291's worked example in the tests.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

log = logging.getLogger(__name__)

RECORD_SIZE = 4096
# A push service takes at least 4096 bytes of body; the header and the
# AEAD tag take 103 of them, the padding delimiter one.
MAX_PLAINTEXT = RECORD_SIZE - 86 - 16 - 1
JWT_TTL_S = 12 * 3600

# Where a subscription may point: the push services of the browsers there
# are. Anything else is refused, so the server can't be made to POST to an
# address of someone's choosing (the subscription comes from the page).
PUSH_HOST_SUFFIXES = (
    ".push.services.mozilla.com",    # Firefox
    ".push.apple.com",               # Safari, home-screen apps on iOS / iPadOS
    "fcm.googleapis.com",            # Chrome, Edge on Android, Brave …
    "android.googleapis.com",        # older Chrome
    ".notify.windows.com",           # Edge on Windows
)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def endpoint_allowed(endpoint: str) -> bool:
    try:
        u = urlsplit(endpoint)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or u.username or u.password:
        return False
    if u.port not in (None, 443):
        return False
    return any(host == s.lstrip(".") or host.endswith(s if s.startswith(".") else "." + s)
               for s in PUSH_HOST_SUFFIXES)


# ─── RFC 8291: the message, encrypted for one device ──────────────────────

def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint)


def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *,
            as_private: ec.EllipticCurvePrivateKey | None = None,
            salt: bytes | None = None) -> bytes:
    """The aes128gcm body for one device (RFC 8291 §3-4, one record).
    ``as_private`` and ``salt`` are fresh each time; tests pass the RFC's."""
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError("push message too long")
    if len(auth_secret) != 16:
        raise ValueError("bad auth secret")
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_private.public_key())
    salt = salt or os.urandom(16)
    shared = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    prk_cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = AESGCM(prk_cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + record


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """What a device does with the body (for tests)."""
    salt, rs, idlen = body[:16], *struct.unpack("!IB", body[16:21])
    as_public = body[21:21 + idlen]
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    ua_public = _public_bytes(ua_private.public_key())
    shared = ua_private.exchange(ec.ECDH(), as_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    padded = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    return padded.rstrip(b"\x00")[:-1]


# ─── RFC 8292: VAPID, this server's signature ─────────────────────────────

@dataclass
class Vapid:
    private: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> "Vapid":
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: str) -> "Vapid":
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("not a P-256 key")
        return cls(key)

    def pem(self) -> str:
        return self.private.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()).decode()

    @property
    def public_b64(self) -> str:
        """The applicationServerKey a page subscribes with."""
        return b64u(_public_bytes(self.private.public_key()))

    def jwt(self, audience: str, subject: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = b64u(json.dumps({"aud": audience, "exp": int(now) + JWT_TTL_S, "sub": subject},
                                 separators=(",", ":")).encode())
        signing = f"{head}.{claims}".encode()
        r, s = decode_dss_signature(self.private.sign(signing, ec.ECDSA(hashes.SHA256())))
        return f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


# ─── Sending ──────────────────────────────────────────────────────────────

@dataclass
class Result:
    status: int          # the push service's HTTP status; 0 = no answer
    gone: bool = False   # the subscription is dead: forget it
    detail: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


_client: httpx.AsyncClient | None = None
_jwts: dict[tuple[str, str], tuple[str, float]] = {}


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # No redirects: a push service answers, it doesn't send us elsewhere.
        _client = httpx.AsyncClient(timeout=15.0, follow_redirects=False)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def send(vapid: Vapid, subject: str, sub: dict[str, Any], payload: dict[str, Any], *,
               ttl: int, urgency: str = "normal", topic: str | None = None) -> Result:
    """POST one notification to one subscription ({endpoint, keys: {p256dh,
    auth}})."""
    endpoint = sub.get("endpoint", "")
    if not endpoint_allowed(endpoint):
        return Result(0, gone=True, detail="not a push service")
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    if len(plaintext) > MAX_PLAINTEXT:
        # The caller's to shorten; the subscription is fine.
        return Result(0, detail="message too long")
    try:
        keys = sub.get("keys") or {}
        body = encrypt(plaintext, unb64u(keys["p256dh"]), unb64u(keys["auth"]))
    except (KeyError, TypeError, ValueError) as e:
        return Result(0, gone=True, detail=f"bad subscription: {e}")
    u = urlsplit(endpoint)
    aud = f"{u.scheme}://{u.netloc}"
    hit = _jwts.get((aud, subject))
    if hit is None or hit[1] - time.time() < 3600:
        hit = (vapid.jwt(aud, subject), time.time() + JWT_TTL_S)
        _jwts[(aud, subject)] = hit
    headers = {
        "Authorization": f"vapid t={hit[0]}, k={vapid.public_b64}",
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(max(0, int(ttl))),
        "Urgency": urgency,
    }
    if topic:
        headers["Topic"] = topic
    try:
        r = await _http().post(endpoint, content=body, headers=headers)
    except httpx.HTTPError as e:
        return Result(0, detail=type(e).__name__)
    # 404 / 410: unsubscribed or expired. Anything else (a 403 for a
    # signature the service didn't take, a 429, a 5xx) counts as a failure,
    # and notify.py drops a subscription only after several in a row.
    gone = r.status_code in (404, 410)
    return Result(r.status_code, gone=gone, detail=r.text[:200] if r.status_code >= 300 else "")
