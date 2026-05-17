"""Idempotently merge ccpipe's recommended voice settings into the user's
``~/.claude/settings.json``.

Why: Claude Code's ``/voice`` reads its tap/hold mode from settings.json.
Setting it to tap mode means the browser PTT button works without the user
having to type ``/voice tap`` manually.

We only touch keys we own (``voice.enabled``, ``voice.mode``). If the user
has explicitly set either, we leave them alone — respect their choice.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

# Re-exports for tests / external callers.
__all__ = [
    "default_settings_path",
    "default_keybindings_path",
    "patch_settings",
    "patch_settings_safe",
    "patch_keybindings",
    "patch_keybindings_safe",
    "should_apply",
]

log = logging.getLogger(__name__)

_RECOMMENDED = {
    "enabled": True,
    "mode": "tap",
}

# Rebind /voice's PTT key away from Space so the browser mic FAB can trigger
# it without conflicting with normal typing in the prompt. Frontend sends the
# byte sequence "\x1bk" (= ESC k = "meta+k") on FAB pointerdown/up.
_VOICE_PTT_KEY = "meta+k"
_VOICE_PTT_ACTION = "voice:pushToTalk"
_VOICE_PTT_CONTEXT = "Chat"


def default_settings_path() -> Path:
    return Path(os.environ.get(
        "CCPIPE_CLAUDE_SETTINGS",
        str(Path.home() / ".claude" / "settings.json"),
    ))


def default_keybindings_path() -> Path:
    return Path(os.environ.get(
        "CCPIPE_CLAUDE_KEYBINDINGS",
        str(Path.home() / ".claude" / "keybindings.json"),
    ))


def patch_settings(path: Path | None = None) -> tuple[bool, str]:
    """Apply recommended voice keys. Returns (changed, reason)."""
    path = path or default_settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create parent dir: {exc}"

    if path.exists():
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return False, "settings.json is not a JSON object"
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"cannot read settings.json: {exc}"
    else:
        data = {}

    voice = data.get("voice")
    if voice is None:
        data["voice"] = dict(_RECOMMENDED)
        action = "added voice section"
    elif not isinstance(voice, dict):
        return False, "settings.voice is not an object; leaving alone"
    else:
        # Only fill in missing keys.
        changes: list[str] = []
        for k, v in _RECOMMENDED.items():
            if k not in voice:
                voice[k] = v
                changes.append(k)
        if not changes:
            return False, "voice keys already set"
        action = f"added voice keys: {', '.join(changes)}"

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_path, path)
    except OSError as exc:
        return False, f"failed to write settings.json: {exc}"
    return True, action


def patch_settings_safe(path: Path | None = None) -> None:
    """Call patch_settings, log result, never raise."""
    try:
        changed, reason = patch_settings(path)
    except Exception as exc:
        log.warning("settings.json patch failed: %s", exc)
        return
    if changed:
        log.info("settings.json patched: %s", reason)
    else:
        log.info("settings.json unchanged: %s", reason)


def patch_keybindings(path: Path | None = None) -> tuple[bool, str]:
    """Idempotently add ccpipe's voice PTT keybinding to the user's
    ``~/.claude/keybindings.json``.

    Adds (or merges into) a Chat-context block that:
      - binds ``meta+k`` to ``voice:pushToTalk``
      - removes the default ``space`` binding (so plain typing isn't
        misinterpreted as a voice trigger)

    Other bindings the user may have set are preserved.
    """
    path = path or default_keybindings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create parent dir: {exc}"

    data: dict[str, Any]
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return False, "keybindings.json is not a JSON object"
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"cannot read keybindings.json: {exc}"
    else:
        data = {}

    bindings = data.setdefault("bindings", [])
    if not isinstance(bindings, list):
        return False, "keybindings.bindings is not a list"

    # Find existing Chat-context block or append a new one.
    chat_block: dict[str, Any] | None = None
    for entry in bindings:
        if isinstance(entry, dict) and entry.get("context") == _VOICE_PTT_CONTEXT:
            chat_block = entry
            break
    if chat_block is None:
        chat_block = {"context": _VOICE_PTT_CONTEXT, "bindings": {}}
        bindings.append(chat_block)

    inner = chat_block.setdefault("bindings", {})
    if not isinstance(inner, dict):
        return False, f"keybindings[{_VOICE_PTT_CONTEXT}].bindings is not an object"

    changes: list[str] = []
    if inner.get(_VOICE_PTT_KEY) != _VOICE_PTT_ACTION:
        inner[_VOICE_PTT_KEY] = _VOICE_PTT_ACTION
        changes.append(f"bind {_VOICE_PTT_KEY}")
    # Remove the default Space binding for this action (set to null).
    if inner.get("space") != None:  # noqa: E711 (we want to detect non-null)
        # If the user has Space bound to something else, leave it alone.
        if inner.get("space") == _VOICE_PTT_ACTION:
            inner["space"] = None
            changes.append("unbind space")
    elif "space" not in inner:
        inner["space"] = None
        changes.append("unbind space (default)")

    if not changes:
        return False, "keybindings already set"

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        return False, f"failed to write keybindings.json: {exc}"
    return True, ", ".join(changes)


def patch_keybindings_safe(path: Path | None = None) -> None:
    try:
        changed, reason = patch_keybindings(path)
    except Exception as exc:
        log.warning("keybindings.json patch failed: %s", exc)
        return
    if changed:
        log.info("keybindings.json patched: %s", reason)
    else:
        log.info("keybindings.json unchanged: %s", reason)


def _apply(value: Any) -> bool:
    """Coerce the CCPIPE_PATCH_SETTINGS env var to a bool. Default true."""
    if value is None:
        return True
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def should_apply() -> bool:
    return _apply(os.environ.get("CCPIPE_PATCH_SETTINGS"))
