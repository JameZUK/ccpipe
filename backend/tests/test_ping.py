"""The pong echoes the ping's id, so the client can time each pong against
its own ping when several are out at once (a slow link, the ping's
drop-down pinging every 2 s)."""

import asyncio
from types import SimpleNamespace

from ccpipe import ws


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


def _pong(ping_id):
    fake = _FakeWs()
    ok = asyncio.run(ws._TerminalConnection.send_pong_unlocked(SimpleNamespace(ws=fake), ping_id))
    assert ok
    return fake.sent[-1]


def test_parse_ping():
    assert ws._parse_ping('{"type":"ping"}') == {"type": "ping"}
    assert ws._parse_ping('{"type":"ping","id":7}') == {"type": "ping", "id": 7}
    assert ws._parse_ping('{"type": "ping", "id": 7}') == {"type": "ping", "id": 7}
    # Not pings: other types, text that only mentions one, broken JSON.
    assert ws._parse_ping('{"type":"input","data":"\\"type\\":\\"ping\\""}') is None
    assert ws._parse_ping('{"type":"ping"') is None
    assert ws._parse_ping('{"type":"ping","pad":"' + "x" * 400 + '"}') is None


def test_pong_echoes_the_id():
    assert _pong(7) == {"type": "pong", "id": 7}
    assert _pong(0) == {"type": "pong", "id": 0}
    assert _pong(2**53) == {"type": "pong", "id": 2**53}


def test_pong_without_a_usable_id_is_bare():
    # Old clients send no id; anything that isn't a plain safe integer
    # isn't echoed.
    for bad in (None, -1, 2**53 + 1, True, 1.5, "7", [7], {"x": 1}):
        assert _pong(bad) == {"type": "pong"}, bad
