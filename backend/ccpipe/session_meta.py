"""When each session was first created and last opened.

tmux knows neither across a reboot: a sticky session is recreated by the
lifespan hook, so its ``session_created`` becomes the boot time, and
``session_last_attached`` starts out empty. The session picker shows both,
so ccpipe keeps its own record.

Storage: ``$XDG_STATE_HOME/ccpipe/session_meta.json`` (override with
``CCPIPE_SESSION_META_FILE``), 0600, atomic replace — the same shape as
sticky.py. Schema::

    {"<session>": {"created": <unix>, "opened": <unix>}, ...}

* ``created`` is set when the session is created through the API, and
  otherwise backfilled from tmux the first time the session is listed.
  It survives the reboot recreation of a sticky session.
* ``opened`` is bumped when a main terminal connects (not a tile beside
  another session), at most once a minute per session so the phone's
  periodic reconnects don't rewrite the file every few minutes.
* A name that is neither live nor sticky is dropped at list time, so a
  later session reusing it starts afresh.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from .paths import state_dir

log = logging.getLogger(__name__)

META_FILE_ENV = "CCPIPE_SESSION_META_FILE"
OPENED_WRITE_INTERVAL_S = 60

_lock = threading.Lock()
_opened_written: dict[str, float] = {}

Meta = dict[str, dict[str, int]]


def path() -> Path:
    override = os.environ.get(META_FILE_ENV)
    return Path(override) if override else state_dir() / "session_meta.json"


def load() -> Meta:
    p = path()
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring malformed session meta at %s: %s", p, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: Meta = {}
    for name, info in data.items():
        if not isinstance(name, str) or not isinstance(info, dict):
            continue
        entry = {k: int(v) for k, v in info.items()
                 if k in ("created", "opened") and isinstance(v, (int, float)) and v > 0}
        if entry:
            out[name] = entry
    return out


def _save(data: Meta) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, indent=2, sort_keys=True).encode() + b"\n")
    finally:
        os.close(fd)
    os.replace(tmp, p)


def note_created(name: str, when: int | None = None) -> None:
    """A brand-new session: start its record afresh."""
    with _lock:
        data = load()
        data[name] = {"created": int(when or time.time())}
        _opened_written.pop(name, None)
        _save(data)


def note_opened(name: str, when: float | None = None) -> None:
    now = when or time.time()
    last = _opened_written.get(name)
    if last is not None and now - last < OPENED_WRITE_INTERVAL_S:
        return
    with _lock:
        _opened_written[name] = now
        data = load()
        data.setdefault(name, {})["opened"] = int(now)
        try:
            _save(data)
        except OSError as exc:
            log.warning("could not record session open: %s", exc)


def rename(old: str, new: str) -> None:
    with _lock:
        data = load()
        if old in data:
            data[new] = data.pop(old)
            _save(data)
        if old in _opened_written:
            _opened_written[new] = _opened_written.pop(old)


def forget(name: str) -> None:
    with _lock:
        data = load()
        if data.pop(name, None) is not None:
            _save(data)
        _opened_written.pop(name, None)


def reconcile(live: dict[str, int], keep: set[str]) -> Meta:
    """Merge with the live sessions (name → tmux created time): backfill a
    missing ``created``, keep the older of the two, and drop entries that
    are neither live nor in ``keep`` (the sticky names). Writes only when
    something changed. Returns the merged map."""
    with _lock:
        data = load()
        changed = False
        for name, tmux_created in live.items():
            entry = data.setdefault(name, {})
            if tmux_created > 0 and (entry.get("created") is None or tmux_created < entry["created"]):
                entry["created"] = tmux_created
                changed = True
        for name in [n for n in data if n not in live and n not in keep]:
            del data[name]
            changed = True
        if changed:
            try:
                _save(data)
            except OSError as exc:
                log.warning("could not save session meta: %s", exc)
        return data
