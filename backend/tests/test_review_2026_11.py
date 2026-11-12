"""Fixes from the security / stability review of 2026-11-12."""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from fastapi import HTTPException

from ccpipe import auth, drafts, fs_range, fs_zip, statefile, statusline_record
from ccpipe.routes import debug
from ccpipe.routes import fs as fs_routes


class _Conn:
    def __init__(self, host: str | None):
        self.client = type("C", (), {"host": host})() if host is not None else None


# ── login limit: an IPv6 client is its /64 ──────────────────────────────

@pytest.mark.parametrize("host, bucket", [
    ("203.0.113.9", "203.0.113.9"),
    ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
    ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
    ("::ffff:203.0.113.9", "203.0.113.9"),
    ("not-an-address", "not-an-address"),
    (None, "unknown"),
])
def test_client_bucket(host, bucket):
    assert auth.client_bucket(_Conn(host)) == bucket


# ── state files: a failed read is not an empty file ─────────────────────

@pytest.fixture(autouse=True)
def _fresh_statefile():
    statefile.reset()
    yield
    statefile.reset()


def test_missing_state_file_is_empty_and_writable(tmp_path):
    p = tmp_path / "s.json"
    assert statefile.read_json(p) is None
    statefile.write(p, b'{"a": 1}')
    assert statefile.read_json(p) == {"a": 1}
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert [f.name for f in tmp_path.iterdir()] == ["s.json"]      # no temp left


def test_unreadable_state_file_is_not_overwritten(tmp_path):
    p = tmp_path / "s.json"
    p.mkdir()                               # reading it fails, and not with "no such file"
    assert statefile.read_json(p) is None
    with pytest.raises(OSError):
        statefile.write(p, b"{}")
    p.rmdir()                               # readable again (absent): writes resume
    assert statefile.read_json(p) is None
    statefile.write(p, b"{}")
    assert statefile.read_json(p) == {}


def test_corrupt_state_file_is_moved_aside(tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(b'{"half": ')
    assert statefile.read_json(p) is None
    kept = [f for f in tmp_path.iterdir() if f.name.startswith("s.json.corrupt-")]
    assert len(kept) == 1 and kept[0].read_bytes() == b'{"half": '
    statefile.write(p, b"{}")               # and a fresh file can be started


# ── file API ────────────────────────────────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("CCPIPE_FS_ROOT", raising=False)
    (tmp_path / ".local" / "state" / "ccpipe").mkdir(parents=True)
    (tmp_path / "project").mkdir()
    return tmp_path


def test_cannot_move_a_folder_that_holds_protected_files(home):
    for above in (home / ".local", home / ".local" / "state"):
        with pytest.raises(HTTPException) as e:
            fs_routes._enforce_not_above_denied(above)
        assert e.value.status_code == 403
    fs_routes._enforce_not_above_denied(home / "project")          # fine


def test_state_dir_is_denied_wherever_it_is(home, monkeypatch):
    elsewhere = home / "xdg"
    (elsewhere / "ccpipe").mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(elsewhere))
    with pytest.raises(HTTPException):
        fs_routes._enforce_fs_jail(elsewhere / "ccpipe" / "credentials")
    with pytest.raises(HTTPException):
        fs_routes._enforce_not_above_denied(elsewhere)


def test_listing_survives_a_name_that_is_not_utf8(home):
    d = home / "project"
    (d / "ok.txt").write_text("x")
    with open(os.path.join(os.fsencode(d), b"bad\xff.txt"), "wb") as f:
        f.write(b"x")
    entries = fs_routes._scan_dir_entries(d, show_hidden=False, include_files=True)
    assert [e["name"] for e in entries] == ["ok.txt"]
    json.dumps(entries)
    # A download of it still gets a well-formed header.
    name = os.fsdecode(b"bad\xff.txt")
    assert "%FF" in fs_routes.content_disposition_attachment(name)


def test_zip_dates_are_clamped():
    assert fs_zip._date_time(0)[0] == 1980
    assert fs_zip._date_time(5_000_000_000)[0] <= 2107           # year 2128
    assert fs_zip._date_time(1e30)[0] in (1980, 2107)             # absurd: no crash
    assert not fs_zip._zippable(os.fsdecode(b"bad\xff"))


def test_range_with_absurd_digits_is_ignored():
    assert fs_range.parse_range("bytes=" + "9" * 5000 + "-", 100) is None
    assert fs_range.parse_range("bytes=0-9", 100) == (0, 9)


# ── debug snapshot diff stays fast ──────────────────────────────────────

def test_diff_is_fast_on_repetitive_buffers():
    n = 3000
    start = time.monotonic()
    out = debug._diff_buffers([""] * n, (["", "z"] * n)[: n + 64])
    assert time.monotonic() - start < 2.0     # difflib alone: minutes
    assert out["lines_compared"] == n


def test_diff_still_finds_a_small_difference():
    front = [f"line {i}" if i % 3 else "" for i in range(600)]
    back = front[:300] + ["extra"] + front[300:]
    out = debug._diff_buffers(front, back)
    assert out["matches"] == 600 and out["mismatches"] == 0
    front[100] = "changed"
    out = debug._diff_buffers(front, back)
    assert out["mismatches"] == 1
    assert any(e["frontend"] == "changed" for e in out["mismatch_examples"])


# ── half a surrogate pair is not fatal ──────────────────────────────────

def test_draft_with_a_lone_surrogate_is_made_sendable():
    out = drafts.cap("hi \ud83d there")
    out.encode("utf-8")
    assert out.startswith("hi ") and out.endswith(" there")


# ── status line: a project's command only from a trusted folder ─────────

def test_project_status_line_needs_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "clone"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "touch pwned"}}))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "mine"}}))
    info = {"workspace": {"project_dir": str(project)}}
    env = {"HOME": str(tmp_path)}
    # No record of trust: the project's command is ignored, yours runs.
    assert statusline_record.own_command(info, env) == "mine"
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"projects": {str(project): {"hasTrustDialogAccepted": False}}}))
    assert statusline_record.own_command(info, env) == "mine"
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"projects": {str(project): {"hasTrustDialogAccepted": True}}}))
    assert statusline_record.own_command(info, env) == "touch pwned"
    # Trust of a folder above it counts too.
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"projects": {str(tmp_path): {"hasTrustDialogAccepted": True}}}))
    assert statusline_record.trusted(str(project / "sub"), env)


# ── TTS: a stalled listener doesn't hold up the rest ────────────────────

def test_stalled_tts_listener_is_dropped_for_the_utterance(monkeypatch):
    from ccpipe import tts

    monkeypatch.setattr(tts, "_SUB_CB_TIMEOUT_S", 0.05)
    got: list[str] = []

    class Sub:
        def __init__(self, name: str, stall: bool):
            self.name, self.stall = name, stall

        async def on_chunk(self, _payload: bytes) -> None:
            if self.stall:
                await asyncio.sleep(30)
            got.append(self.name)

    async def go() -> list:
        targets = [Sub("phone", True), Sub("desk", False)]
        svc = tts.TtsService.__new__(tts.TtsService)
        start = time.monotonic()
        for _ in range(5):
            await svc._fan_out(targets, lambda s: s.on_chunk(b"x"), "on_chunk")
        assert time.monotonic() - start < 1.0       # one timeout, not five
        return targets

    left = asyncio.run(go())
    assert [s.name for s in left] == ["desk"] and got == ["desk"] * 5
