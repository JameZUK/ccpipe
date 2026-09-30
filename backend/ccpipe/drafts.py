"""Per-session composer drafts.

Whatever is typed into a session's prompt box is kept here, keyed by tmux
session name, so it's restored whenever the session is opened again — on
this device or any other (the WebSocket layer broadcasts every change to
the other connections on the same session; last write wins).

Held in memory and persisted to ``drafts.json`` (0600, beside the
credentials file) on a short debounce, so a restart keeps them. Bounded:
each draft is capped, the number of drafts is capped, and drafts untouched
for a month are dropped on load.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .auth import state_file, write_state_file

log = logging.getLogger(__name__)

MAX_DRAFT_BYTES = 32 * 1024
MAX_DRAFTS = 200
DRAFT_TTL_S = 30 * 24 * 3600
SAVE_DELAY_S = 1.0

_drafts: dict[str, dict[str, Any]] | None = None     # name → {"text", "ts"}
_save_handle: asyncio.TimerHandle | None = None


def _path():
    return state_file("drafts.json")


def _load() -> dict[str, dict[str, Any]]:
    global _drafts
    if _drafts is None:
        _drafts = {}
        try:
            data = json.loads(_path().read_text())
        except (OSError, ValueError):
            data = {}
        cutoff = time.time() - DRAFT_TTL_S
        if isinstance(data, dict):
            for name, v in data.items():
                if (isinstance(name, str) and isinstance(v, dict)
                        and isinstance(v.get("text"), str) and v["text"]
                        and isinstance(v.get("ts"), (int, float)) and v["ts"] >= cutoff):
                    _drafts[name] = {"text": v["text"], "ts": v["ts"]}
    return _drafts


def cap(text: str) -> str:
    """Truncate *text* to MAX_DRAFT_BYTES of UTF-8 (never mid-codepoint)."""
    b = text.encode("utf-8")
    return text if len(b) <= MAX_DRAFT_BYTES else b[:MAX_DRAFT_BYTES].decode("utf-8", "ignore")


def get(session: str) -> str:
    entry = _load().get(session)
    return entry["text"] if entry else ""


def put(session: str, text: str) -> str:
    """Store *text* as *session*'s draft (empty clears it). Returns what was
    actually stored (possibly truncated) so callers broadcast exactly that."""
    drafts = _load()
    text = cap(text)
    if text:
        drafts[session] = {"text": text, "ts": time.time()}
        if len(drafts) > MAX_DRAFTS:
            oldest = min(drafts, key=lambda k: drafts[k]["ts"])
            del drafts[oldest]
    else:
        drafts.pop(session, None)
    _schedule_save()
    return text


def rename(old: str, new: str) -> None:
    drafts = _load()
    if old in drafts:
        drafts[new] = drafts.pop(old)
        _schedule_save()


def _schedule_save() -> None:
    global _save_handle
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        flush()
        return
    if _save_handle is None:
        _save_handle = loop.call_later(SAVE_DELAY_S, _save_soon)


def _save_soon() -> None:
    global _save_handle
    _save_handle = None
    payload = _snapshot()
    asyncio.get_running_loop().run_in_executor(None, _write, payload)


def _snapshot() -> bytes:
    return json.dumps(_load()).encode()


def _write(payload: bytes) -> None:
    try:
        write_state_file(_path(), payload)
    except OSError as exc:
        log.error("could not persist drafts: %s", exc)


def flush() -> None:
    """Write pending changes now (shutdown / no running loop)."""
    global _save_handle
    if _save_handle is not None:
        _save_handle.cancel()
        _save_handle = None
    if _drafts is not None:
        _write(_snapshot())


def reset() -> None:
    """For tests: forget in-memory state so the next access reloads."""
    global _drafts, _save_handle
    if _save_handle is not None:
        _save_handle.cancel()
    _drafts, _save_handle = None, None
