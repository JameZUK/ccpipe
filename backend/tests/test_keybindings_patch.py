"""Tests for ~/.claude/keybindings.json patching."""
import json
from pathlib import Path

from ccpipe.settings_patch import patch_keybindings


def test_creates_keybindings_when_missing(tmp_path: Path):
    target = tmp_path / "keybindings.json"
    changed, reason = patch_keybindings(target)
    assert changed is True
    data = json.loads(target.read_text())
    chat = next(b for b in data["bindings"] if b["context"] == "Chat")
    assert chat["bindings"]["meta+k"] == "voice:pushToTalk"
    assert chat["bindings"]["space"] is None


def test_idempotent_when_already_patched(tmp_path: Path):
    target = tmp_path / "keybindings.json"
    target.write_text(json.dumps({
        "bindings": [
            {"context": "Chat", "bindings": {
                "meta+k": "voice:pushToTalk",
                "space": None,
            }}
        ]
    }))
    changed, reason = patch_keybindings(target)
    assert changed is False
    assert "already set" in reason


def test_merges_into_existing_chat_block(tmp_path: Path):
    target = tmp_path / "keybindings.json"
    target.write_text(json.dumps({
        "bindings": [
            {"context": "Chat", "bindings": {"ctrl+l": "clear"}},
        ]
    }))
    changed, _ = patch_keybindings(target)
    assert changed is True
    data = json.loads(target.read_text())
    chat = next(b for b in data["bindings"] if b["context"] == "Chat")
    # User's binding preserved
    assert chat["bindings"]["ctrl+l"] == "clear"
    # Ours added
    assert chat["bindings"]["meta+k"] == "voice:pushToTalk"
    assert chat["bindings"]["space"] is None


def test_appends_chat_block_when_only_other_contexts(tmp_path: Path):
    target = tmp_path / "keybindings.json"
    target.write_text(json.dumps({
        "bindings": [
            {"context": "Other", "bindings": {"ctrl+x": "x"}},
        ]
    }))
    changed, _ = patch_keybindings(target)
    assert changed is True
    data = json.loads(target.read_text())
    contexts = {b["context"] for b in data["bindings"]}
    assert "Other" in contexts and "Chat" in contexts


def test_respects_user_space_binding_to_other_action(tmp_path: Path):
    target = tmp_path / "keybindings.json"
    target.write_text(json.dumps({
        "bindings": [
            {"context": "Chat", "bindings": {"space": "some:other:action"}}
        ]
    }))
    changed, _ = patch_keybindings(target)
    data = json.loads(target.read_text())
    chat = next(b for b in data["bindings"] if b["context"] == "Chat")
    # User explicitly bound space elsewhere — don't clobber it.
    assert chat["bindings"]["space"] == "some:other:action"
    assert chat["bindings"]["meta+k"] == "voice:pushToTalk"
    assert changed is True  # because we added meta+k


def test_refuses_non_object_root(tmp_path: Path):
    target = tmp_path / "keybindings.json"
    target.write_text(json.dumps([]))
    changed, reason = patch_keybindings(target)
    assert changed is False
    assert "not a JSON object" in reason
