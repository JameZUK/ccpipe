import json
from pathlib import Path

from ccpipe.settings_patch import patch_settings


def test_creates_settings_when_missing(tmp_path: Path):
    target = tmp_path / "settings.json"
    changed, reason = patch_settings(target)
    assert changed is True
    assert target.exists()
    data = json.loads(target.read_text())
    assert data["voice"] == {"enabled": True, "mode": "tap"}
    assert "added" in reason


def test_idempotent_when_keys_already_set(tmp_path: Path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"voice": {"enabled": True, "mode": "tap"}}))
    changed, reason = patch_settings(target)
    assert changed is False
    assert "already set" in reason


def test_preserves_unrelated_keys(tmp_path: Path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"language": "en", "theme": "dark"}))
    changed, _ = patch_settings(target)
    assert changed is True
    data = json.loads(target.read_text())
    assert data["language"] == "en"
    assert data["theme"] == "dark"
    assert data["voice"] == {"enabled": True, "mode": "tap"}


def test_only_fills_missing_voice_keys(tmp_path: Path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"voice": {"enabled": False}}))
    changed, _ = patch_settings(target)
    assert changed is True
    data = json.loads(target.read_text())
    # User's explicit choice respected; only mode added.
    assert data["voice"]["enabled"] is False
    assert data["voice"]["mode"] == "tap"


def test_refuses_non_object_voice(tmp_path: Path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"voice": "not-an-object"}))
    changed, reason = patch_settings(target)
    assert changed is False
    assert "not an object" in reason


def test_refuses_non_object_root(tmp_path: Path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps([]))
    changed, reason = patch_settings(target)
    assert changed is False
    assert "not a JSON object" in reason


def test_handles_malformed_json(tmp_path: Path):
    target = tmp_path / "settings.json"
    target.write_text("not json {")
    changed, reason = patch_settings(target)
    assert changed is False
    assert "cannot read" in reason
