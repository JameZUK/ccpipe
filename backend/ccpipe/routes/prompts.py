"""Saved prompts: a small named library of composer prompts.

Global (usable from every session) and stored on the server, so the phone,
the iPad and any browser share one list. Persisted to ``prompts.json``
(0600, beside the credentials file); every mutation is serialised under a
lock and written atomically.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import AuthDep, CsrfDep, SameOriginDep, state_file, write_state_file
from ..drafts import MAX_DRAFT_BYTES

router = APIRouter()

MAX_NAME_CHARS = 80
MAX_PROMPTS = 500
# A saved prompt is loaded into the composer, so it shares the draft cap.
MAX_PROMPT_BYTES = MAX_DRAFT_BYTES

_lock = threading.Lock()


class SavePromptBody(BaseModel):
    name: str
    text: str
    # False: refuse (409) if the name is taken, so the client can ask
    # "replace?" against the server's current list, not a stale copy.
    overwrite: bool = False


class DeletePromptBody(BaseModel):
    name: str


def _path():
    return state_file("prompts.json")


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("text"), str)}


def _save(prompts: dict[str, dict[str, Any]]) -> None:
    try:
        write_state_file(_path(), json.dumps(prompts).encode())
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not save prompts: {exc}")


def _clean_name(name: str) -> str:
    name = " ".join(name.split())            # trim + collapse whitespace
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if len(name) > MAX_NAME_CHARS:
        raise HTTPException(status_code=400, detail=f"name too long (max {MAX_NAME_CHARS})")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise HTTPException(status_code=400, detail="name contains control characters")
    return name


@router.get("/api/prompts", dependencies=[AuthDep, SameOriginDep])
def list_prompts() -> list[dict[str, Any]]:
    """Every saved prompt, most recently saved first."""
    with _lock:
        prompts = _load()
    items = [{"name": k, "text": v["text"], "updated": v.get("updated", 0)}
             for k, v in prompts.items()]
    items.sort(key=lambda p: p["updated"], reverse=True)
    return items


@router.post("/api/prompts", dependencies=[AuthDep, CsrfDep])
def save_prompt(body: SavePromptBody) -> dict[str, Any]:
    name = _clean_name(body.name)
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")
    if len(body.text.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise HTTPException(status_code=413, detail="prompt too long")
    with _lock:
        prompts = _load()
        if name in prompts and not body.overwrite:
            raise HTTPException(status_code=409, detail="a prompt with that name exists")
        if name not in prompts and len(prompts) >= MAX_PROMPTS:
            raise HTTPException(status_code=409, detail=f"prompt limit reached ({MAX_PROMPTS})")
        prompts[name] = {"text": body.text, "updated": time.time()}
        _save(prompts)
    return {"name": name}


@router.post("/api/prompts/delete", dependencies=[AuthDep, CsrfDep])
def delete_prompt(body: DeletePromptBody) -> dict[str, bool]:
    with _lock:
        prompts = _load()
        if prompts.pop(body.name, None) is None:
            raise HTTPException(status_code=404, detail="no such prompt")
        _save(prompts)
    return {"deleted": True}
