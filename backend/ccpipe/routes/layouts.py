"""Screen layouts: which other sessions are tiled beside a session, and how.

A layout belongs to the session it was made in ("open X → X's tiles come
back") and is stored on the server so every device gets the same one.
Persisted to ``layouts.json`` (0600, beside the credentials file); every
mutation is serialised under a lock and written atomically.

A layout is a split tree:
  leaf:  {"t": "leaf", "s": <session>, "m": "live" | "watch"}
  split: {"t": "split", "d": "h" | "v", "sz": [weights…], "c": [nodes…]}
"h" lays children side by side, "v" stacks them.
"""
from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import tmux
from ..auth import AuthDep, CsrfDep, SameOriginDep, state_file, write_state_file

router = APIRouter()

MAX_LEAVES = 9
MAX_DEPTH = 4
MAX_CHILDREN = 4
MAX_LAYOUTS = 200

_lock = threading.Lock()


class PutLayoutBody(BaseModel):
    layout: dict[str, Any] | None = None


def _path():
    return state_file("layouts.json")


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("tree"), dict)}


def _save(layouts: dict[str, dict[str, Any]]) -> None:
    try:
        write_state_file(_path(), json.dumps(layouts).encode())
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not save layouts: {exc}")


def clean_tree(node: Any, depth: int = 0, leaves: list[int] | None = None) -> dict[str, Any]:
    """Validate a layout tree and return a normalised copy. ValueError if bad."""
    if leaves is None:
        leaves = [0]
    if not isinstance(node, dict) or depth > MAX_DEPTH:
        raise ValueError("bad layout node")
    kind = node.get("t")
    if kind == "leaf":
        leaves[0] += 1
        if leaves[0] > MAX_LEAVES:
            raise ValueError(f"at most {MAX_LEAVES} tiles")
        name = tmux.safe_name(str(node.get("s", "")))
        mode = node.get("m", "watch")
        if mode not in ("live", "watch"):
            raise ValueError("bad tile mode")
        return {"t": "leaf", "s": name, "m": mode}
    if kind == "split":
        d = node.get("d")
        kids = node.get("c")
        sizes = node.get("sz")
        if d not in ("h", "v") or not isinstance(kids, list) \
                or not 2 <= len(kids) <= MAX_CHILDREN:
            raise ValueError("bad split")
        if not isinstance(sizes, list) or len(sizes) != len(kids) or not all(
                isinstance(x, (int, float)) and not isinstance(x, bool)
                and math.isfinite(x) and x > 0 for x in sizes):
            sizes = [1] * len(kids)
        total = float(sum(sizes))
        return {"t": "split", "d": d,
                "sz": [round(x / total, 4) for x in sizes],
                "c": [clean_tree(k, depth + 1, leaves) for k in kids]}
    raise ValueError("bad layout node")


def _owner(name: str) -> str:
    try:
        return tmux.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/layouts/{name}", dependencies=[AuthDep, SameOriginDep])
def get_layout(name: str) -> dict[str, Any]:
    name = _owner(name)
    with _lock:
        entry = _load().get(name)
    return {"layout": entry["tree"] if entry else None}


@router.put("/api/layouts/{name}", dependencies=[AuthDep, CsrfDep])
def put_layout(name: str, body: PutLayoutBody) -> dict[str, Any]:
    name = _owner(name)
    tree = None
    if body.layout is not None:
        try:
            tree = clean_tree(body.layout)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if tree["t"] == "leaf":
            tree = None                   # a single tile is no layout at all
    with _lock:
        layouts = _load()
        if tree is None:
            if layouts.pop(name, None) is not None:
                _save(layouts)
            return {"layout": None}
        layouts[name] = {"tree": tree, "updated": time.time()}
        if len(layouts) > MAX_LAYOUTS:
            oldest = min(layouts, key=lambda k: layouts[k].get("updated", 0))
            del layouts[oldest]
        _save(layouts)
    return {"layout": tree}


def _rename_in(node: dict[str, Any], old: str, new: str) -> None:
    if node.get("t") == "leaf":
        if node.get("s") == old:
            node["s"] = new
    else:
        for kid in node.get("c", []):
            _rename_in(kid, old, new)


def rename(old: str, new: str) -> None:
    """A session was renamed: move its own layout and every tile showing it."""
    with _lock:
        layouts = _load()
        if not layouts:
            return
        if old in layouts:
            layouts[new] = layouts.pop(old)
        for entry in layouts.values():
            _rename_in(entry["tree"], old, new)
        try:
            write_state_file(_path(), json.dumps(layouts).encode())
        except OSError:
            pass
