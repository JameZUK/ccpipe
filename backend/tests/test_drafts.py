"""Composer drafts: per-session store + live relay between devices."""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from ccpipe import drafts, ws


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    drafts.reset()
    yield tmp_path
    drafts.reset()


def test_put_get_clear_and_persist(isolated):
    assert drafts.get("work") == ""
    assert drafts.put("work", "hello") == "hello"
    assert drafts.get("work") == "hello"
    drafts.flush()
    f = isolated / "drafts.json"
    assert json.loads(f.read_text())["work"]["text"] == "hello"
    assert os.stat(f).st_mode & 0o777 == 0o600
    drafts.reset()                                  # "restart"
    assert drafts.get("work") == "hello"
    drafts.put("work", "")                          # sending clears it
    assert drafts.get("work") == ""


def test_cap_rename_and_expiry(isolated):
    big = "é" * drafts.MAX_DRAFT_BYTES              # 2 bytes each
    stored = drafts.put("a", big)
    assert len(stored.encode()) <= drafts.MAX_DRAFT_BYTES
    drafts.rename("a", "b")
    assert drafts.get("a") == "" and drafts.get("b") == stored
    old = time.time() - drafts.DRAFT_TTL_S - 10
    (isolated / "drafts.json").write_text(json.dumps({"stale": {"text": "x", "ts": old}}))
    drafts.reset()
    assert drafts.get("stale") == ""


@pytest.mark.asyncio
async def test_draft_relayed_to_other_devices_only():
    class _Conn:
        def __init__(self, session):
            self.session, self.sent = session, []
        async def send_json(self, msg):
            self.sent.append(msg)
            return True

    phone, ipad, other = _Conn("work"), _Conn("work"), _Conn("elsewhere")
    ws._draft_peers.clear()
    ws._draft_peers.update({"work": {phone, ipad}, "elsewhere": {other}})
    try:
        ws._TerminalConnection._on_draft(phone, json.dumps({"type": "draft", "text": "fix the tests"}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        ws._draft_peers.clear()
    assert ipad.sent == [{"type": "draft", "text": "fix the tests"}]
    assert phone.sent == [] and other.sent == []      # no echo, no cross-session leak
    assert drafts.get("work") == "fix the tests"


def test_draft_frames_are_routed_before_control_sniffs():
    frame = json.dumps({"type": "draft", "text": '{"type":"ping"}'})
    assert ws._is_draft_frame(frame)
    assert not ws._is_draft_frame('{"type":"ping"}')
