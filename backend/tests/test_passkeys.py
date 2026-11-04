"""Passkeys (WebAuthn) and sign-in combinations.

A small software authenticator below makes real registration and
assertion responses (P-256, "none" attestation), so py_webauthn's own
checks — signature, challenge, origin, RP hash, flags — run for real.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import struct
import time

import cbor2
import pyotp
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

ORIGIN = "https://testserver"
RP_ID = "testserver"
H = {"X-Requested-By": "ccpipe", "Origin": ORIGIN}


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Authenticator:
    """One discoverable passkey, like a phone or a password manager holds."""

    def __init__(self, rp_id: str = RP_ID, origin: str = ORIGIN):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = hashlib.sha256(str(id(self)).encode() + str(time.time()).encode()).digest()[:16]
        self.rp_id, self.origin = rp_id, origin
        self.count = 0
        self.user_handle: bytes | None = None

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _client(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin,
                           "crossOrigin": False}).encode()

    def register(self, options: dict, *, uv: bool = True) -> dict:
        self.user_handle = unb64u(options["user"]["id"])
        flags = 0x01 | (0x04 if uv else 0) | 0x40           # UP, UV, AT
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags])
                     + struct.pack(">I", self.count) + bytes(16)
                     + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(self._client("webauthn.create", options["challenge"])),
                             "attestationObject": b64u(att), "transports": ["internal"]},
                "clientExtensionResults": {}}

    def assert_(self, options: dict, *, uv: bool = True) -> dict:
        self.count += 1
        flags = 0x01 | (0x04 if uv else 0)
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags])
                     + struct.pack(">I", self.count))
        client = self._client("webauthn.get", options["challenge"])
        sig = self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig),
                             "userHandle": b64u(self.user_handle) if self.user_handle else None},
                "clientExtensionResults": {}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    monkeypatch.delenv("CCPIPE_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("CCPIPE_BEHIND_TLS", raising=False)
    import ccpipe.auth as auth
    import ccpipe.main as m
    import ccpipe.passkeys as pk
    import ccpipe.routes.auth as routes_auth
    auth.reset_cached_credential()
    routes_auth.reset_throttle_state()
    auth._totp_burned.clear()
    pk.reset_state()
    importlib.reload(m)
    c = TestClient(m.app)
    yield c
    routes_auth.reset_throttle_state()


def _reset_throttle():
    import ccpipe.routes.auth as routes_auth
    routes_auth.reset_throttle_state()


def login_pw(c, password="letmein", code=None, username="alice"):
    body = {"username": username, "password": password}
    if code:
        body["code"] = code
    return c.post("/api/auth/login", headers=H, json=body)


def register(c, a: Authenticator, name="phone", code=None, password="letmein"):
    body = {"currentPassword": password}
    if code:
        body["code"] = code
    r = c.post("/api/auth/passkey/register/options", headers=H, json=body)
    assert r.status_code == 200, r.text
    opts = r.json()
    assert opts["authenticatorSelection"]["userVerification"] == "required"
    assert opts["authenticatorSelection"]["residentKey"] == "required"
    return c.post("/api/auth/passkey/register", headers=H,
                  json={"name": name, "credential": a.register(opts)})


def login_passkey(c, a: Authenticator, **kw):
    r = c.post("/api/auth/passkey/options", headers=H)
    assert r.status_code == 200, r.text
    opts = r.json()
    assert opts.get("allowCredentials", []) == []        # reveals nothing
    assert opts["userVerification"] == "required"
    return c.post("/api/auth/login", headers=H, json={"passkey": a.assert_(opts, **kw)})


def logout(c):
    c.post("/api/auth/logout", headers=H)
    assert c.get("/api/auth/status").json()["authenticated"] is False


def set_methods(c, methods, code=None):
    body = {"currentPassword": "letmein", "methods": methods}
    if code:
        body["code"] = code
    return c.post("/api/auth/signin", headers=H, json=body)


def methods_on(c) -> set[tuple[str, ...]]:
    s = c.get("/api/auth/signin").json()
    return {tuple(m["factors"]) for m in s["methods"] if m["enabled"]}


def totp_codes(secret: str):
    """Three different valid codes (previous, current, next slot)."""
    t = pyotp.TOTP(secret)
    now = time.time()
    return [t.at(now - 30), t.at(now), t.at(now + 30)]


def enrol_totp(c) -> str:
    r = c.post("/api/auth/totp/enroll", headers=H, json={"currentPassword": "letmein"})
    secret = r.json()["secret"]
    r = c.post("/api/auth/totp/confirm", headers=H, json={
        "currentPassword": "letmein", "secret": secret, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    _reset_throttle()
    return secret


# ─── Registration ──────────────────────────────────────────────────────────

def test_register_then_sign_in_with_the_passkey_alone(client):
    assert login_pw(client).status_code == 200
    a = Authenticator()
    r = register(client, a)
    assert r.status_code == 200, r.text
    state = r.json()
    assert [k["name"] for k in state["passkeys"]] == ["phone"]
    assert state["passkeys"][0]["rp_id"] == RP_ID
    # The first passkey also turns on "passkey alone", keeping the password.
    assert methods_on(client) == {("passkey",), ("password",)}
    logout(client)
    r = login_passkey(client, a)
    assert r.status_code == 200, r.text
    assert r.json()["authenticated"] is True
    assert client.get("/api/sessions").status_code != 401
    # The use was recorded: counter and time.
    import ccpipe.auth as auth
    key = auth.get_credential().passkeys[0]
    assert key.sign_count == 1 and key.last_used > 0


def test_register_needs_the_current_password(client):
    login_pw(client)
    r = client.post("/api/auth/passkey/register/options", headers=H, json={"currentPassword": "wrong"})
    assert r.status_code == 401


def test_register_needs_the_code_when_totp_is_enrolled(client):
    login_pw(client)
    secret = enrol_totp(client)
    login_pw(client, code=totp_codes(secret)[1])
    _reset_throttle()
    r = client.post("/api/auth/passkey/register/options", headers=H, json={"currentPassword": "letmein"})
    assert r.status_code == 401
    r = register(client, Authenticator(), code=totp_codes(secret)[2])
    assert r.status_code == 200, r.text


def test_registration_is_bound_to_the_session_that_asked(client, tmp_path):
    login_pw(client)
    r = client.post("/api/auth/passkey/register/options", headers=H, json={"currentPassword": "letmein"})
    opts = r.json()
    import ccpipe.main as m
    other = TestClient(m.app)
    login_pw(other)
    r = other.post("/api/auth/passkey/register", headers=H,
                   json={"name": "x", "credential": Authenticator().register(opts)})
    assert r.status_code == 400
    assert client.get("/api/auth/signin").json()["passkeys"] == []


def test_registration_refused_without_user_verification(client):
    login_pw(client)
    r = client.post("/api/auth/passkey/register/options", headers=H, json={"currentPassword": "letmein"})
    r = client.post("/api/auth/passkey/register", headers=H,
                    json={"name": "x", "credential": Authenticator().register(r.json(), uv=False)})
    assert r.status_code == 400


def test_signin_state_reports_the_host_without_an_origin_header(client):
    """Same-origin GETs carry no Origin; the state still says whether
    passkeys can be made here (TestClient is http://testserver — plain
    HTTP on a non-localhost name, so: no)."""
    login_pw(client)
    assert client.get("/api/auth/signin").json()["rp_id"] is None
    r = client.get("/api/auth/signin", headers={"Origin": ORIGIN})
    assert r.json()["rp_id"] == RP_ID
    r = client.get("https://testserver/api/auth/signin")
    assert r.json()["rp_id"] == RP_ID


def test_passkeys_refused_from_another_origin_or_plain_http(client):
    for origin in ("https://evil.example", "http://testserver"):
        r = client.post("/api/auth/passkey/options", headers={**H, "Origin": origin})
        assert r.status_code == 400, origin


def test_delete_needs_reauth_and_removes(client):
    login_pw(client)
    a = Authenticator()
    key_id = register(client, a).json()["passkeys"][0]["id"]
    r = client.post("/api/auth/passkey/delete", headers=H, json={"id": key_id, "currentPassword": "nope"})
    assert r.status_code == 401
    _reset_throttle()
    r = client.post("/api/auth/passkey/delete", headers=H, json={"id": key_id, "currentPassword": "letmein"})
    assert r.status_code == 200 and r.json()["passkeys"] == []
    logout(client)
    assert login_passkey(client, a).status_code == 401


# ─── Sign-in ───────────────────────────────────────────────────────────────

def test_assertion_cannot_be_replayed(client):
    login_pw(client)
    a = Authenticator()
    register(client, a)
    logout(client)
    r = client.post("/api/auth/passkey/options", headers=H)
    cred = a.assert_(r.json())
    assert client.post("/api/auth/login", headers=H, json={"passkey": cred}).status_code == 200
    logout(client)
    assert client.post("/api/auth/login", headers=H, json={"passkey": cred}).status_code == 401


def test_unknown_passkey_and_missing_uv_are_refused(client):
    login_pw(client)
    register(client, Authenticator())
    logout(client)
    stranger = Authenticator()
    stranger.user_handle = b"x" * 16
    assert login_passkey(client, stranger).status_code == 401
    _reset_throttle()
    a = Authenticator()
    login_pw(client)
    register(client, a, name="laptop")
    logout(client)
    assert login_passkey(client, a, uv=False).status_code == 401


def test_passkey_plus_password(client):
    login_pw(client)
    a = Authenticator()
    register(client, a)
    assert set_methods(client, [["passkey", "password"]]).status_code == 200
    logout(client)
    _reset_throttle()
    # The password alone no longer works — and says nothing about why.
    r = login_pw(client)
    assert r.status_code == 401 and "need" not in (r.json().get("detail") or "")
    # Passkey first: the reply asks for the password, no username needed.
    r = login_passkey(client, a)
    assert r.status_code == 200
    assert r.json() == {**r.json(), "authenticated": False, "need": [["password"]]}
    r = client.post("/api/auth/login", headers=H, json={"password": "letmein"})
    assert r.status_code == 200 and r.json()["authenticated"] is True


def test_wrong_password_after_passkey_uses_up_the_ticket(client):
    login_pw(client)
    a = Authenticator()
    register(client, a)
    set_methods(client, [["passkey", "password"]])
    logout(client)
    _reset_throttle()
    assert login_passkey(client, a).json()["need"] == [["password"]]
    assert client.post("/api/auth/login", headers=H, json={"password": "nope"}).status_code == 401
    # The passkey step must be done again.
    assert client.post("/api/auth/login", headers=H, json={"password": "letmein"}).status_code == 401


def test_password_alone_cannot_redeem_another_browsers_ticket(client):
    login_pw(client)
    a = Authenticator()
    register(client, a)
    set_methods(client, [["passkey", "password"]])
    logout(client)
    assert login_passkey(client, a).json()["need"] == [["password"]]
    import ccpipe.main as m
    other = TestClient(m.app)
    _reset_throttle()
    assert other.post("/api/auth/login", headers=H, json={"username": "alice", "password": "letmein"}).status_code == 401


def test_passkey_plus_code_and_all_three(client):
    login_pw(client)
    secret = enrol_totp(client)
    codes = totp_codes(secret)
    login_pw(client, code=codes[0])
    _reset_throttle()
    a = Authenticator()
    register(client, a, code=codes[1])
    assert set_methods(client, [["code", "passkey"], ["code", "passkey", "password"]],
                       code=codes[2]).status_code == 200
    logout(client)
    _reset_throttle()
    # Either completes it; only the smallest asks are listed.
    r = login_passkey(client, a)
    assert r.json()["need"] == [["code"]]
    # All three valid slots' codes are used up (replay-protected); stand
    # in for half a minute passing.
    import ccpipe.auth as auth
    auth._totp_burned.clear()
    r = client.post("/api/auth/login", headers=H, json={"code": pyotp.TOTP(secret).now()})
    assert r.json()["authenticated"] is True, r.text


def test_need_lists_alternatives(client):
    login_pw(client)
    secret = enrol_totp(client)
    codes = totp_codes(secret)
    login_pw(client, code=codes[0])
    a = Authenticator()
    register(client, a, code=codes[1])
    _reset_throttle()
    set_methods(client, [["passkey", "password"], ["code", "passkey"]], code=codes[2])
    logout(client)
    _reset_throttle()
    assert login_passkey(client, a).json()["need"] == [["code"], ["password"]]


def test_password_and_code_still_works_alongside_passkeys(client):
    login_pw(client)
    secret = enrol_totp(client)
    codes = totp_codes(secret)
    login_pw(client, code=codes[0])
    register(client, Authenticator(), code=codes[1])
    logout(client)
    _reset_throttle()
    assert methods_on_after_login(client, secret) >= {("code", "password"), ("passkey",)}


def methods_on_after_login(c, secret) -> set[tuple[str, ...]]:
    time.sleep(1.0)
    r = login_pw(c, code=pyotp.TOTP(secret).at(time.time() + 30))
    assert r.status_code == 200, r.text
    return methods_on(c)


# ─── Choosing sign-in methods ──────────────────────────────────────────────

def test_cannot_choose_only_methods_that_cannot_be_met(client):
    login_pw(client)
    # No TOTP, no passkey: these all need something that isn't there.
    r = set_methods(client, [["code", "password"], ["passkey"]])
    assert r.status_code == 400
    # "code" alone is never a choice.
    assert set_methods(client, [["code"]]).status_code == 400
    assert set_methods(client, [["password"]]).status_code == 200


def test_removing_the_last_passkey_falls_back_to_the_password(client):
    login_pw(client)
    a = Authenticator()
    key_id = register(client, a).json()["passkeys"][0]["id"]
    assert set_methods(client, [["passkey"]]).status_code == 200
    logout(client)
    _reset_throttle()
    assert login_pw(client).status_code == 401           # passkey only now
    login_passkey(client, a)
    _reset_throttle()
    client.post("/api/auth/passkey/delete", headers=H, json={"id": key_id, "currentPassword": "letmein"})
    logout(client)
    _reset_throttle()
    assert login_pw(client).status_code == 200           # never locked out


def test_password_change_and_totp_keep_passkeys_and_methods(client):
    import ccpipe.auth as auth
    login_pw(client)
    a = Authenticator()
    register(client, a)
    r = client.post("/api/auth/credentials", headers=H,
                    json={"currentPassword": "letmein", "newPassword": "letmein2"})
    assert r.status_code == 200, r.text
    cred = auth.get_credential()
    assert len(cred.passkeys) == 1 and cred.signin == (("passkey",), ("password",))
    _reset_throttle()
    login_pw(client, password="letmein2")
    r = client.post("/api/auth/totp/enroll", headers=H, json={"currentPassword": "letmein2"})
    secret = r.json()["secret"]
    client.post("/api/auth/totp/confirm", headers=H, json={
        "currentPassword": "letmein2", "secret": secret, "code": pyotp.TOTP(secret).now()})
    cred = auth.get_credential()
    # Enrolling makes "password" mean "password + code", as before passkeys.
    assert len(cred.passkeys) == 1 and cred.signin == (("passkey",), ("code", "password"))
    assert login_passkey(client, a).json()["authenticated"] is True


def test_damaged_passkey_entry_does_not_discard_the_file(client, tmp_path):
    import ccpipe.auth as auth
    login_pw(client)
    register(client, Authenticator())
    path = tmp_path / "credentials"
    data = json.loads(path.read_text())
    data["passkeys"].append({"id": 5})
    data["signin"].append(["code"])
    path.write_text(json.dumps(data))
    auth.reset_cached_credential()
    cred = auth.get_credential()
    assert cred.username == "alice" and len(cred.passkeys) == 1
    assert cred.signin == (("passkey",), ("password",))


def test_signin_needs_and_totp_mapping_units():
    from ccpipe import auth
    cred = auth.Credential(username="a", password_hash="x", totp_secret="S",
                           passkeys=(auth.Passkey(id="k", public_key="p"),),
                           signin=(("passkey", "password"), ("code", "passkey"), ("code", "password")))
    assert auth.signin_needs({"passkey"}, cred) == [["code"], ["password"]]
    assert auth.signin_satisfied({"code", "password"}, cred)
    assert not auth.signin_satisfied({"password"}, cred)
    assert auth._signin_for_totp(cred.signin, enrolled=False) == (("passkey", "password"), ("passkey",), ("password",))
    assert auth._signin_for_totp((("password",), ("passkey",)), enrolled=True) == (("code", "password"), ("passkey",))
