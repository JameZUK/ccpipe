"""Tests for the session-secret on-disk persistence."""
import os
from pathlib import Path

import pytest

from ccpipe.auth import load_or_create_secret


def test_creates_secret_when_missing(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "secret"
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(target))
    secret = load_or_create_secret()
    assert len(secret) >= 32
    assert target.exists()
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


def test_reuses_existing_secret(tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "secret"
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(target))
    first = load_or_create_secret()
    second = load_or_create_secret()
    assert first == second


def test_regenerates_when_short(tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "secret"
    target.write_text("toosh")  # too short
    target.chmod(0o600)
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(target))
    secret = load_or_create_secret()
    assert len(secret) >= 32
    # The file should now contain the new secret (atomic replace).
    assert target.read_text().strip() == secret
