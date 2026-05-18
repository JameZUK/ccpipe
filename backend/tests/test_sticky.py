"""Sticky-session storage tests.

Exercises the JSON-on-disk persistence helpers in `ccpipe.sticky`. The
auto-restore-on-startup path and the HTTP toggle endpoint are covered
indirectly through the in-process TestClient suite — here we just pin
the storage layer's invariants (round-trip, idempotent clear,
rename-preservation, malformed-file resilience, file mode).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ccpipe import sticky


@pytest.fixture
def sticky_file(tmp_path, monkeypatch):
    p = tmp_path / "sticky.json"
    monkeypatch.setenv(sticky.STICKY_FILE_ENV, str(p))
    yield p


def test_load_returns_empty_when_file_missing(sticky_file):
    assert not sticky_file.exists()
    assert sticky.load() == {}
    assert sticky.sticky_names() == set()
    assert sticky.is_sticky("anything") is False


def test_round_trip(sticky_file):
    sticky.set_sticky("alpha", "/home/u/projects/a")
    sticky.set_sticky("beta", "/srv/work/b")
    assert sticky.is_sticky("alpha")
    assert sticky.is_sticky("beta")
    assert sticky.sticky_names() == {"alpha", "beta"}
    loaded = sticky.load()
    assert loaded == {
        "alpha": {"cwd": "/home/u/projects/a"},
        "beta": {"cwd": "/srv/work/b"},
    }


def test_set_overwrites_existing_entry(sticky_file):
    sticky.set_sticky("alpha", "/old/path")
    sticky.set_sticky("alpha", "/new/path")
    assert sticky.load()["alpha"]["cwd"] == "/new/path"


def test_clear_is_idempotent(sticky_file):
    sticky.set_sticky("alpha", "/p")
    sticky.clear("alpha")
    sticky.clear("alpha")            # second clear must not raise
    sticky.clear("never-existed")    # nor must clearing a missing entry
    assert sticky.load() == {}


def test_rename_preserves_entry(sticky_file):
    sticky.set_sticky("alpha", "/p")
    sticky.rename("alpha", "alpha-renamed")
    assert sticky.is_sticky("alpha-renamed")
    assert not sticky.is_sticky("alpha")
    assert sticky.load()["alpha-renamed"]["cwd"] == "/p"


def test_rename_of_missing_entry_is_noop(sticky_file):
    sticky.rename("never-existed", "irrelevant")
    assert sticky.load() == {}


def test_malformed_file_is_treated_as_empty(sticky_file):
    sticky_file.parent.mkdir(parents=True, exist_ok=True)
    sticky_file.write_text("{not valid json")
    # Should NOT raise — robust to a corrupt file so backend startup
    # isn't blocked by a single bad write.
    assert sticky.load() == {}


def test_entries_missing_cwd_are_dropped(sticky_file):
    sticky_file.parent.mkdir(parents=True, exist_ok=True)
    sticky_file.write_text(json.dumps({
        "good": {"cwd": "/p"},
        "bad-no-cwd": {},
        "bad-non-string-cwd": {"cwd": 12345},
        "bad-non-dict": "oops",
    }))
    assert sticky.load() == {"good": {"cwd": "/p"}}


def test_file_is_written_with_0600_perms(sticky_file):
    sticky.set_sticky("alpha", "/p")
    mode = stat.S_IMODE(os.stat(sticky_file).st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_parent_dir_created_with_0700_perms(tmp_path, monkeypatch):
    nested = tmp_path / "fresh" / "ccpipe" / "sticky.json"
    monkeypatch.setenv(sticky.STICKY_FILE_ENV, str(nested))
    sticky.set_sticky("alpha", "/p")
    assert nested.exists()
    mode = stat.S_IMODE(os.stat(nested.parent).st_mode)
    assert mode == 0o700, f"expected dir 0o700, got {oct(mode)}"


def test_build_restore_command_uses_provided_shell():
    cmd = sticky.build_restore_command(shell="/usr/bin/zsh")
    assert "claude --continue" in cmd
    assert "/usr/bin/zsh" in cmd
    assert "exec " in cmd
    # The interactive flag matters — without -i the shell would exit
    # immediately on EOF, defeating the "drop to prompt" intent.
    assert " -i" in cmd


def test_build_restore_command_falls_back_to_env_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    cmd = sticky.build_restore_command()
    assert "/bin/bash" in cmd


def test_build_restore_command_quotes_unusual_shell_paths():
    # Defence in depth — a shell path with a space would otherwise
    # break shell parsing of the wrapped command.
    cmd = sticky.build_restore_command(shell="/opt/weird shell/bash")
    assert "'/opt/weird shell/bash'" in cmd
