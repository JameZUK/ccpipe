"""Push notifications: the encryption and signature (webpush.py), who gets
one (notify.py), the routes, and what open pages are told (ws.py)."""
from __future__ import annotations

import asyncio
import importlib
import json
import time

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from ccpipe import attention, notify, webpush, ws
from ccpipe.webpush import b64u, unb64u

H = {"X-Requested-By": "ccpipe"}
MOZ = "https://updates.push.services.mozilla.com/wpush/v2/gAAAAABk"


def _priv(b: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64u(b), "big"), ec.SECP256R1())


# ─── RFC 8291 ─────────────────────────────────────────────────────────────

def test_rfc8291_worked_example():
    """RFC 8291 §5: the same keys and salt give the same body, byte for byte."""
    as_priv = _priv("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")
    ua_priv = _priv("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94")
    ua_pub = unb64u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4")
    auth = unb64u("BTBZMqHH6r4Tts7J_aSIgg")
    body = webpush.encrypt(b"When I grow up, I want to be a watermelon", ua_pub, auth,
                           as_private=as_priv, salt=unb64u("DGv6ra1nlYgDCS1FRnbzlw"))
    assert b64u(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS"
        "6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qu"
        "lcy4a-fN")
    assert webpush.decrypt(body, ua_priv, auth) == b"When I grow up, I want to be a watermelon"


def test_fresh_keys_each_time_and_a_size_limit():
    ua = ec.generate_private_key(ec.SECP256R1())
    ua_pub = webpush._public_bytes(ua.public_key())
    a, b = webpush.encrypt(b"hi", ua_pub, b"x" * 16), webpush.encrypt(b"hi", ua_pub, b"x" * 16)
    assert a != b and webpush.decrypt(a, ua, b"x" * 16) == b"hi"
    with pytest.raises(ValueError):
        webpush.encrypt(b"x" * (webpush.MAX_PLAINTEXT + 1), ua_pub, b"x" * 16)


def test_vapid_jwt_verifies_with_the_public_key():
    v = webpush.Vapid.generate()
    assert webpush.Vapid.from_pem(v.pem()).public_b64 == v.public_b64
    token = v.jwt("https://push.example", "https://me.example", now=1000)
    head, claims, sig = token.split(".")
    assert json.loads(unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(unb64u(claims)) == {"aud": "https://push.example",
                                          "exp": 1000 + webpush.JWT_TTL_S, "sub": "https://me.example"}
    raw = unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(v.public_b64))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize("endpoint,ok", [
    (MOZ, True),
    ("https://web.push.apple.com/QGuQ", True),
    ("https://fcm.googleapis.com/fcm/send/abc", True),
    ("https://wns2-par02p.notify.windows.com/w/?token=x", True),
    ("http://updates.push.services.mozilla.com/x", False),     # not https
    ("https://push.services.mozilla.com.evil.example/x", False),
    ("https://evilfcm.googleapis.com/x", False),
    ("https://127.0.0.1/x", False),
    ("https://user@fcm.googleapis.com/x", False),
    ("https://fcm.googleapis.com:8443/x", False),
    ("not a url", False),
])
def test_only_push_services(endpoint, ok):
    assert webpush.endpoint_allowed(endpoint) is ok


class Device:
    """A browser's side of a subscription."""

    def __init__(self, endpoint=MOZ):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        self.endpoint = endpoint

    @property
    def sub(self):
        return {"endpoint": self.endpoint,
                "keys": {"p256dh": b64u(webpush._public_bytes(self.key.public_key())),
                         "auth": b64u(self.auth)}}

    def read(self, body: bytes) -> dict:
        return json.loads(webpush.decrypt(body, self.key, self.auth))


@respx.mock
async def test_send_encrypts_signs_and_says_how_urgent():
    dev = Device()
    route = respx.post(MOZ).mock(return_value=httpx.Response(201))
    v = webpush.Vapid.generate()
    r = await webpush.send(v, "https://me.example", dev.sub, {"title": "hi"},
                           ttl=60, urgency="high", topic="ask-1")
    assert r.ok and not r.gone
    req = route.calls.last.request
    assert dev.read(req.content) == {"title": "hi"}
    assert req.headers["TTL"] == "60" and req.headers["Urgency"] == "high"
    assert req.headers["Topic"] == "ask-1" and req.headers["Content-Encoding"] == "aes128gcm"
    assert req.headers["Authorization"].startswith("vapid t=")
    assert req.headers["Authorization"].endswith(f", k={v.public_b64}")
    await webpush.close()


@respx.mock
async def test_a_dead_subscription_is_gone():
    respx.post(MOZ).mock(return_value=httpx.Response(410, text="expired"))
    r = await webpush.send(webpush.Vapid.generate(), "https://me.example", Device().sub, {}, ttl=1)
    assert r.gone and r.status == 410
    bad = await webpush.send(webpush.Vapid.generate(), "x", {"endpoint": "https://127.0.0.1/"}, {}, ttl=1)
    assert bad.gone and bad.status == 0
    respx.post(MOZ).mock(return_value=httpx.Response(403, text="BadJwtToken"))
    r = await webpush.send(webpush.Vapid.generate(), "x", Device().sub, {}, ttl=1)
    assert not r.gone and r.status == 403 and r.detail == "BadJwtToken"
    long = await webpush.send(webpush.Vapid.generate(), "x", Device().sub, {"b": "é" * 3000}, ttl=1)
    assert not long.gone and long.detail == "message too long"
    await webpush.close()


# ─── Who gets one ─────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("CCPIPE_CONFIG_FILE", str(tmp_path / "config.json"))
    from ccpipe import config
    config.reset_cache()
    monkeypatch.setattr(notify, "_vapid", None)
    monkeypatch.setattr(notify, "_landing", {})
    monkeypatch.setattr(notify, "_recent", {})
    monkeypatch.setattr(notify, "_done_at", {})
    monkeypatch.setattr(ws, "_draft_peers", {})
    attention._reset_for_tests()
    yield tmp_path
    attention._reset_for_tests()
    config.reset_cache()


def _subscribe(dev: Device, device="phone", **prefs):
    return notify.subscribe(dev.endpoint, dev.sub["keys"]["p256dh"], dev.sub["keys"]["auth"],
                            device=device, label="Firefox on Android",
                            origin="https://ccpipe.example", prefs=prefs or None)


def test_subscriptions_are_kept_private_and_renewed(store):
    import os
    a = _subscribe(Device())
    assert a["prefs"] == {"ask": True, "done": False, "details": True}
    assert a["host"] == "updates.push.services.mozilla.com" and "endpoint" not in a
    # The same device with a new endpoint replaces its old one, keeping its id.
    b = _subscribe(Device(MOZ + "new"), done=True)
    assert b["id"] == a["id"] and len(notify.subscriptions()) == 1
    assert notify.set_prefs(MOZ + "new", {"ask": False})["prefs"] == {"ask": False, "done": True, "details": True}
    assert os.stat(store / "push.json").st_mode & 0o777 == 0o600
    # The browser renewed it (the service worker knows only the old endpoint).
    c = notify.subscribe(MOZ + "renewed", b64u(webpush._public_bytes(ec.generate_private_key(
        ec.SECP256R1()).public_key())), b64u(b"r" * 16), device="", label="", origin="o",
        prefs=None, renew=MOZ + "new")
    assert (c["id"], c["device"], c["label"], c["prefs"]["done"]) == (a["id"], "phone", "Firefox on Android", True)
    assert len(notify.subscriptions()) == 1
    assert notify.unsubscribe(sub_id=a["id"]) and notify.subscriptions() == []
    with pytest.raises(ValueError):
        _subscribe(Device("https://127.0.0.1/x"))
    with pytest.raises(ValueError):
        notify.subscribe(MOZ, "AAAA", "BBBB", device="d", label="", origin="", prefs=None)


def test_the_server_key_is_made_once(store):
    k = notify.vapid().public_b64
    notify._vapid = None
    assert notify.vapid().public_b64 == k


def _item(level="ask", session="work"):
    item = attention.Item(next(attention._seq), session, level,
                          "question" if level == "ask" else "done",
                          "Claude has a question" if level == "ask" else "Claude finished",
                          "Red or blue?", time.time())
    attention._items[(session, level)] = item
    return item


class _Page:
    """An open session page, as notify sees it."""

    def __init__(self, device, session="work", visible=True):
        self.device, self.session, self.visible = device, session, visible
        self.tile = False

    def outdated(self):
        return False


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, dict]] = []

    async def send(vapid, subject, sub, payload, **kw):
        out.append((sub["device"], payload | kw | {"subject": subject}))
        return webpush.Result(201)
    monkeypatch.setattr(notify.webpush, "send", send)
    return out


def test_topics_are_letters_and_digits():
    import re
    for level in ("ask", "done", "test"):
        for item in (None, _item(level if level != "test" else "ask", session="a_b-c")):
            t = notify._topic(item, level)
            assert re.fullmatch(r"[A-Za-z0-9]{1,32}", t), t


async def test_pushed_straight_away_when_no_page_is_on_screen(store, sent):
    _subscribe(Device(), device="phone")
    _subscribe(Device(MOZ + "2"), device="laptop", ask=False)
    await notify._deliver(_item())
    assert [(d, p["title"], p["body"], p["urgency"]) for d, p in sent] == [
        ("phone", "Claude has a question · work", "Red or blue?", "high")]
    assert sent[0][1]["subject"] == "https://ccpipe.example"
    assert sent[0][1]["tag"] == "ccpipe-ask-work"


async def test_not_to_a_device_with_a_page_on_screen(store, sent, monkeypatch):
    monkeypatch.setattr(notify, "RECHECK_S", 0.01)
    monkeypatch.setattr(notify, "HOLD_S", 0.05)
    _subscribe(Device(), device="phone")
    ws._draft_peers["work"] = {_Page("phone")}
    await notify._deliver(_item())
    notify.note_page("phone", True)            # the session list on screen
    ws._draft_peers.clear()
    await notify._deliver(_item())
    assert sent == []


async def test_but_once_it_is_put_away_with_the_question_waiting(store, sent, monkeypatch):
    monkeypatch.setattr(notify, "RECHECK_S", 0.01)
    _subscribe(Device(), device="phone")
    page = _Page("phone")
    ws._draft_peers["work"] = {page}
    task = asyncio.create_task(notify._deliver(_item()))
    await asyncio.sleep(0.05)
    assert sent == []
    page.visible = False                        # the phone is locked
    await asyncio.wait_for(task, 1)
    assert [d for d, _ in sent] == ["phone"]


async def test_waits_while_a_page_is_on_screen_elsewhere(store, sent, monkeypatch):
    from ccpipe import config
    cfg = config.load_for_edit()
    cfg.notify.push_wait_s = 1
    config.save(cfg)
    _subscribe(Device(), device="phone")
    ws._draft_peers["other"] = {_Page("laptop", session="other")}
    sleeps: list[float] = []

    async def fast_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(notify.asyncio, "sleep", fast_sleep)
    answered = _item()
    real_sleep = fast_sleep

    async def answer_meanwhile(s):
        await real_sleep(s)
        attention._items.pop(("work", "ask"))
    monkeypatch.setattr(notify.asyncio, "sleep", answer_meanwhile)
    await notify._deliver(answered)               # answered on the laptop: no push
    assert sent == [] and sleeps == [1]
    monkeypatch.setattr(notify.asyncio, "sleep", fast_sleep)
    await notify._deliver(_item())                # still waiting after the wait
    assert [d for d, _ in sent] == ["phone"]


async def test_a_long_question_still_fits(store, sent):
    _subscribe(Device(), device="phone")
    item = _item()
    item.body = "é" * 2000
    await notify._deliver(item)
    p = sent[0][1]
    assert len(json.dumps(p, ensure_ascii=False).encode()) < webpush.MAX_PLAINTEXT
    assert p["body"].endswith("…")


async def test_details_off_and_finished_turns(store, sent):
    _subscribe(Device(), device="phone", done=True, details=False)
    await notify._deliver(_item("done"))
    await notify._deliver(_item("done"))          # the same session again, at once
    await notify._deliver(_item("done", session="b"))
    assert [(p["title"], p["body"], p["urgency"]) for _, p in sent] == [
        ("Claude finished · work", "Its turn is over.", "normal"),
        ("Claude finished · b", "Its turn is over.", "normal")]


async def test_a_burst_is_capped(store, sent, monkeypatch):
    monkeypatch.setattr(notify, "BURST", 3)
    _subscribe(Device(), device="phone")
    for _ in range(5):
        await notify._deliver(_item())
    assert len(sent) == 3


async def test_a_dead_device_is_dropped(store, monkeypatch):
    _subscribe(Device(), device="phone")

    async def send(*a, **kw):
        return webpush.Result(410, gone=True)
    monkeypatch.setattr(notify.webpush, "send", send)
    await notify._deliver(_item())
    assert notify.subscriptions() == []


async def test_a_bad_evening_doesnt_drop_anyone(store, monkeypatch):
    _subscribe(Device(), device="phone")
    results = iter([webpush.Result(0, detail="ConnectError")] * 12 + [webpush.Result(503)] * 3
                   + [webpush.Result(403, detail="BadJwtToken")])

    async def send(*a, **kw):
        return next(results)
    monkeypatch.setattr(notify.webpush, "send", send)
    monkeypatch.setattr(notify, "BURST", 100)
    for _ in range(16):
        await notify._deliver(_item())
    assert len(notify.subscriptions()) == 1
    # A week of refusals does.
    monkeypatch.setattr(notify, "FAILING_DROP_S", -1)
    results = iter([webpush.Result(403, detail="BadJwtToken")])
    await notify._deliver(_item())
    assert notify.subscriptions() == []


async def test_answered_before_it_goes_cancels_it(store, sent, monkeypatch):
    from ccpipe import config
    cfg = config.load_for_edit()
    cfg.notify.push_wait_s = 30
    config.save(cfg)
    _subscribe(Device(), device="phone")
    ws._draft_peers["other"] = {_Page("laptop", session="other")}
    item = _item()
    notify._on_attention("new", item)
    await asyncio.sleep(0)
    assert item.id in notify._pending
    notify._on_attention("end", item)
    await asyncio.sleep(0)
    assert item.id not in notify._pending and sent == []


# ─── Routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("CCPIPE_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    import ccpipe.auth as auth
    import ccpipe.main as m
    import ccpipe.routes.auth as routes_auth
    from ccpipe import config
    config.reset_cache()
    auth.reset_cached_credential()
    routes_auth.reset_throttle_state()
    monkeypatch.setattr(auth, "_revoked", None)
    monkeypatch.setattr(notify, "_vapid", None)
    monkeypatch.setattr(notify, "_landing", {})
    attention._reset_for_tests()
    importlib.reload(m)
    c = TestClient(m.app)
    assert c.post("/api/auth/login", headers=H,
                  json={"username": "alice", "password": "letmein"}).status_code == 200
    yield c
    attention._reset_for_tests()
    config.reset_cache()


def test_routes(client, monkeypatch):
    dev = Device()
    state = client.get("/api/push").json()
    assert len(unb64u(state["key"])) == 65 and state["devices"] == [] and state["push_wait_s"] == 60
    body = {**dev.sub, "device": "phone", "label": "Firefox\x07 on Android", "prefs": {"done": True}}
    r = client.post("/api/push/subscribe", headers=H, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Firefox  on Android" and r.json()["prefs"]["done"] is True
    assert client.post("/api/push/subscribe", headers=H,
                       json={**body, "endpoint": "https://127.0.0.1/"}).status_code == 400
    assert client.post("/api/push/prefs", headers=H,
                       json={"endpoint": dev.endpoint, "prefs": {"details": False}}).json()["prefs"] == {
        "ask": True, "done": True, "details": False}
    assert client.post("/api/push/config", headers=H, json={"push_wait_s": 120}).json() == {"push_wait_s": 120}
    assert client.post("/api/push/config", headers=H, json={"push_wait_s": 5000}).status_code == 422
    sends = []

    async def send(*a, **kw):
        sends.append(a[3])
        return webpush.Result(201)
    monkeypatch.setattr(notify.webpush, "send", send)
    assert client.post("/api/push/test", headers=H, json={"endpoint": dev.endpoint}).json()["ok"] is True
    assert sends[0]["tag"] == "ccpipe-test"
    assert client.post("/api/push/status", headers=H, json={"endpoint": dev.endpoint}).json()["device"]["prefs"]["done"]
    assert client.post("/api/push/unsubscribe", headers=H, json={"endpoint": dev.endpoint}).json() == {"removed": True}
    assert client.post("/api/push/status", headers=H, json={"endpoint": dev.endpoint}).json() == {"device": None}

    _item()
    got = client.get("/api/attention?device=phone&visible=1").json()["items"]
    assert [(i["session"], i["level"]) for i in got] == [("work", "ask")]
    assert notify.device_visible("phone")
    _item("done", session="b")
    assert client.post("/api/attention/dismiss", headers=H, json={}).json()["items"][0]["level"] == "ask"
    assert client.post("/api/attention/dismiss", headers=H, json={"session": "a;b"}).status_code == 400


def test_the_session_list_says_which_session(client, monkeypatch):
    """After its chime, the session list asks for the name to be said: the
    server's words, once per device, nothing once it's answered or when the
    voice reads nothing."""
    from ccpipe import config
    from ccpipe.routes import tts as tts_routes
    asked = []

    class Stream:
        async def aiter_bytes(self):
            yield b"MP3"

    class Cm:
        async def __aexit__(self, *a):
            return None

    async def open_stream(payload):
        asked.append(payload)
        return Cm(), Stream()
    monkeypatch.setattr(tts_routes, "_open_kokoro_stream", open_stream)
    cfg = config.load_for_edit()
    cfg.tts.voice = "af_heart"
    config.save(cfg)

    item = _item(session="weather-api")
    r = client.post("/api/attention/say", headers=H, json={"id": item.id, "device": "phone"})
    assert r.status_code == 200 and r.content == b"MP3"
    assert asked[0]["input"] == "weather api has a question." and asked[0]["voice"] == "af_heart"
    # Another page on the phone (a session page asks over its socket, ws.py).
    assert client.post("/api/attention/say", headers=H,
                       json={"id": item.id, "device": "phone"}).status_code == 204
    assert not attention.first_say("phone", item.id)
    # The iPad hasn't heard it.
    assert client.post("/api/attention/say", headers=H,
                       json={"id": item.id, "device": "ipad"}).status_code == 200
    # Answered since: nothing to say.
    assert client.post("/api/attention/say", headers=H,
                       json={"id": item.id + 99, "device": "desk"}).status_code == 204
    cfg = config.load_for_edit()
    cfg.tts.scope = "off"
    config.save(cfg)
    other = _item(session="infra")
    assert client.post("/api/attention/say", headers=H,
                       json={"id": other.id, "device": "phone"}).status_code == 204
    assert len(asked) == 2


def test_routes_need_auth_and_csrf(client):
    anon = TestClient(client.app)
    assert anon.get("/api/attention").status_code == 401
    assert anon.get("/api/push").status_code == 401
    assert client.post("/api/push/subscribe", json={}).status_code == 403
    assert client.post("/api/attention/dismiss", json={}).status_code == 403
    assert client.post("/api/attention/say", json={"id": 1}).status_code == 403


# ─── What open pages are told ─────────────────────────────────────────────

def _conn(session, *, tile=False, device="", build=""):
    c = ws._TerminalConnection.__new__(ws._TerminalConnection)
    c.session, c.tile, c.device, c.build = session, tile, device, build
    c.visible, c.attn_ready, c.alive = True, True, True
    c.tts_sub = ws._NoTts() if tile else object()
    c.sent = []

    async def send_json(msg):
        c.sent.append(msg)
        return True
    c.send_json = send_json
    asks = c.asks = []

    async def on_ask(item):
        asks.append(item)
    c._on_claude_ask = on_ask
    ws._draft_peers.setdefault(session, set()).add(c)
    return c


async def test_pages_are_told_and_the_sessions_own_terminal_speaks(store):
    own, tile, other = _conn("work"), _conn("work", tile=True), _conn("b")
    item = _item()
    attention._emit("new", item)
    await asyncio.sleep(0.01)
    assert own.asks == [item] and tile.asks == [] and other.asks == []
    msg = other.sent[-1]
    assert msg["type"] == "attention" and msg["new"] == [item.id]
    assert [i["session"] for i in msg["items"]] == ["work"]
    assert own.sent and not tile.sent


def test_the_page_says_what_it_sees_and_wants(store, monkeypatch):
    c = _conn("work", device="phone")
    said = []
    monkeypatch.setattr(ws.tts_service, "speak_to", lambda text, sub: said.append(text))
    _item("done")
    c.visible = False
    assert not ws.session_watched("work") and not ws.device_visible("phone")
    c._on_attention_frame('{"type":"attention","visible":true}')
    assert c.visible and attention.get("work", "done") is None
    assert ws.session_watched("work") and ws.device_visible("phone") and ws.anyone_visible()
    item = _item(session="build_2")
    c._on_attention_frame(json.dumps({"type": "attention", "say": item.id}))
    c._on_attention_frame(json.dumps({"type": "attention", "say": item.id}))   # another page
    assert said == ["build 2 has a question."]
    from ccpipe import config
    cfg = config.load_for_edit()
    cfg.tts.scope = "off"                       # voice set to read nothing
    config.save(cfg)
    c._on_attention_frame(json.dumps({"type": "attention", "say": _item(session="z").id}))
    assert said == ["build 2 has a question."]
    _item("done", session="x")
    c._on_attention_frame('{"type":"attention","dismiss":"*"}')
    assert attention.get("x", "done") is None


async def test_an_attention_frame_never_reaches_the_terminal(store, monkeypatch):
    c = _conn("work")
    c.watch, c.counters = False, None
    typed = []
    monkeypatch.setattr(ws, "_handle_client_text", lambda *a: typed.append(a))

    async def authed(_what):
        return True
    c._still_authed = authed
    assert await c._on_text('{"type":"attention","visible":false}')
    assert typed == [] and not c.visible


def test_a_page_says_if_its_on_screen_as_it_dials():
    class FakeWs:
        def __init__(self, q):
            self.query_params = q
    assert ws._TerminalConnection(FakeWs({"visible": "0", "device": "ph-1"}), "work").visible is False
    assert ws._TerminalConnection(FakeWs({"visible": "1"}), "work").visible is True
    # A page from before this says nothing: on screen, as before.
    assert ws._TerminalConnection(FakeWs({}), "work").visible is True
    assert ws._TerminalConnection(FakeWs({"device": "x/../y"}), "w").device == "xy"
