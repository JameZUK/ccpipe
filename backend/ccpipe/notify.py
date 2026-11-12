"""Push notifications: tell a device what needs you (attention.py) when no
ccpipe page is on its screen, through its browser's push service
(webpush.py).

Each device that turns them on (Settings → Notifications) is a
subscription, with its own choices:

  ask      questions and permission prompts (on by default)
  done     finished turns (off by default)
  details  the question, or the end of Claude's reply, in the notification
           (on by default); off: only which session, for a lock screen
  open     also while a ccpipe page is on this device's screen, at once
           (off by default): for an iPad, where any sound from the page
           stops the music, the notification's own sound is the chime

Which devices get a new item:

  - not one with a ccpipe page on screen: the page shows it (unless it
    asked for ``open``: then at once, as it's the one you're using);
  - with no page on screen anywhere, straight away;
  - with a page on screen on another device, after ``notify.push_wait_s``
    (Settings), and only if it still needs you then: you may well answer
    it on the device you're using.

Stored in push.json (0600, beside the credentials): this server's VAPID key
and the subscriptions.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import secrets
import threading
import time
from typing import Any

from . import attention, statefile, webpush
from . import config as app_config
from .auth import state_file, write_state_file

log = logging.getLogger(__name__)

MAX_SUBS = 50
LANDING_FRESH_S = 8.0       # the session list checks in every 3 s while on screen
TTL_S = {"ask": 6 * 3600, "done": 3600, "test": 300}
URGENCY = {"ask": "high", "done": "normal", "test": "normal"}
# A runaway (a loop of tiny turns) mustn't buzz a phone all day.
BURST = 20                  # pushes per device …
BURST_WINDOW_S = 600.0      # … in this long
DONE_GAP_S = 60.0           # finished turns of one session, per device
# A subscription is dropped when its push service says it's gone (404 /
# 410), or when it has refused every push for this long (a 400/401/403
# each time; network errors, 429 and 5xx don't count against it).
FAILING_DROP_S = 7 * 86400
# A device with ccpipe on screen when something new came up is looked at
# again every RECHECK_S while it still needs you (up to HOLD_S): put away
# without answering, it's pushed to then.
RECHECK_S = 5.0
HOLD_S = 30 * 60.0
DEFAULT_PREFS = {"ask": True, "done": False, "details": True, "open": False}
# The VAPID "sub" (a contact for the push service) when the page's origin
# isn't https.
FALLBACK_SUBJECT = "https://github.com/JameZUK/ccpipe"

_lock = threading.Lock()


def _path():
    return state_file("push.json")


def _load() -> dict[str, Any]:
    # A read that fails blocks _save (statefile.py): the key and every
    # device's subscription are in here, and mustn't be replaced by an
    # empty file because of one bad read.
    data = statefile.read_json(_path())
    if not isinstance(data, dict):
        data = {}
    subs = data.get("subs")
    data["subs"] = [s for s in subs if isinstance(s, dict)] if isinstance(subs, list) else []
    return data


def _save(data: dict[str, Any]) -> None:
    write_state_file(_path(), json.dumps(data, indent=1).encode())


_vapid: webpush.Vapid | None = None


def vapid() -> webpush.Vapid:
    """This server's key, made the first time it's needed."""
    global _vapid
    if _vapid is not None:
        return _vapid
    with _lock:
        data = _load()
        try:
            _vapid = webpush.Vapid.from_pem(data["vapid"])
        except (KeyError, TypeError, ValueError):
            # Saved before it's used: a key that isn't on disk would be
            # replaced at the next restart, and every device with it.
            made = webpush.Vapid.generate()
            data["vapid"] = made.pem()
            _save(data)
            _vapid = made
            log.info("push: made a new VAPID key")
    return _vapid


def _prefs(p: Any, base: dict[str, bool] | None = None) -> dict[str, bool]:
    out = dict(base or DEFAULT_PREFS)
    if isinstance(p, dict):
        for k in DEFAULT_PREFS:
            if isinstance(p.get(k), bool):
                out[k] = p[k]
    return out


def _public(s: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlsplit
    return {"id": s.get("id"), "label": s.get("label", ""), "device": s.get("device", ""),
            "host": urlsplit(s.get("endpoint", "")).hostname or "",
            "created": s.get("created", 0), "ok_at": s.get("ok_at", 0),
            "prefs": _prefs(s.get("prefs"))}


def subscriptions() -> list[dict[str, Any]]:
    with _lock:
        return [_public(s) for s in _load()["subs"]]


def find(endpoint: str) -> dict[str, Any] | None:
    with _lock:
        s = next((s for s in _load()["subs"] if s.get("endpoint") == endpoint), None)
        return _public(s) if s else None


def subscribe(endpoint: str, p256dh: str, auth: str, *, device: str, label: str,
              origin: str, prefs: Any, renew: str = "") -> dict[str, Any]:
    """Add a device, or renew it: the same endpoint, the same device with a
    new one, or (``renew``) the endpoint the browser has just replaced —
    the service worker doesn't know the device, so it keeps the old one's
    name and settings. Raises ValueError for one that isn't a push
    service's."""
    if not webpush.endpoint_allowed(endpoint):
        raise ValueError("that isn't a push service this server sends to")
    try:
        if len(webpush.unb64u(p256dh)) != 65 or len(webpush.unb64u(auth)) != 16:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("bad subscription keys") from None
    with _lock:
        data = _load()
        old = next((s for s in data["subs"] if s.get("endpoint") in (endpoint, renew or None)
                    or (device and s.get("device") == device)), None)
        subs = [s for s in data["subs"] if s is not old]
        if len(subs) >= MAX_SUBS:
            raise ValueError("too many devices have notifications on")
        sub = {"id": (old or {}).get("id") or secrets.token_hex(8),
               "endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth},
               "device": device or (old or {}).get("device", ""),
               "label": label or (old or {}).get("label", ""), "origin": origin,
               "prefs": _prefs(prefs, (old or {}).get("prefs")),
               "created": (old or {}).get("created") or int(time.time()),
               "ok_at": (old or {}).get("ok_at", 0), "failing_since": 0}
        subs.append(sub)
        data["subs"] = subs
        _save(data)
        return _public(sub)


def set_prefs(endpoint: str, prefs: Any) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        sub = next((s for s in data["subs"] if s.get("endpoint") == endpoint), None)
        if sub is None:
            return None
        sub["prefs"] = _prefs(prefs, sub.get("prefs"))
        _save(data)
        return _public(sub)


def unsubscribe_all() -> int:
    """Forget every device ("sign out everywhere": a lost device mustn't
    go on receiving Claude's questions). Returns how many there were."""
    with _lock:
        data = _load()
        n = len(data["subs"])
        if n:
            data["subs"] = []
            _save(data)
        return n


def unsubscribe(endpoint: str | None = None, sub_id: str | None = None) -> bool:
    with _lock:
        data = _load()
        keep = [s for s in data["subs"]
                if not ((endpoint and s.get("endpoint") == endpoint)
                        or (sub_id and s.get("id") == sub_id))]
        if len(keep) == len(data["subs"]):
            return False
        data["subs"] = keep
        _save(data)
        return True


def _record(sub_id: str, result: webpush.Result) -> None:
    with _lock:
        data = _load()
        sub = next((s for s in data["subs"] if s.get("id") == sub_id), None)
        if sub is None:
            return
        now = int(time.time())
        if result.ok:
            sub["ok_at"], sub["failing_since"] = now, 0
        elif result.status in (400, 401, 403):
            sub["failing_since"] = sub.get("failing_since") or now
        failing = bool(sub.get("failing_since")) and now - sub["failing_since"] > FAILING_DROP_S
        if result.gone or failing:
            data["subs"] = [s for s in data["subs"] if s is not sub]
            log.info("push: dropped %s (%s): %s %s", sub.get("label") or sub_id,
                     "gone" if result.gone else "refused for a week", result.status, result.detail)
        elif not result.ok:
            sub["last_error"] = f"{result.status} {result.detail}".strip()[:200]
        _save(data)


# ─── Who's looking ────────────────────────────────────────────────────────

_landing: dict[str, float] = {}     # device → when its session list last checked in


def note_page(device: str, visible: bool) -> None:
    """The session list (no WebSocket) checks in while it's on screen."""
    if visible:
        _landing[device] = time.monotonic()
        if len(_landing) > 200:
            _landing.pop(next(iter(_landing)))
    else:
        _landing.pop(device, None)


def _landing_visible(device: str | None = None) -> bool:
    now = time.monotonic()
    if device is not None:
        return now - _landing.get(device, float("-inf")) < LANDING_FRESH_S
    return any(now - t < LANDING_FRESH_S for t in _landing.values())


def device_visible(device: str) -> bool:
    from . import ws
    return bool(device) and (ws.device_visible(device) or _landing_visible(device))


def anyone_visible() -> bool:
    from . import ws
    return ws.anyone_visible() or _landing_visible()


# ─── Sending ──────────────────────────────────────────────────────────────

def _clip_bytes(text: str, limit: int) -> str:
    raw = text.encode()
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode(errors="ignore").rstrip() + "…"


def _payload(item: attention.Item | None, details: bool, level: str) -> dict[str, Any]:
    if item is None:
        return {"title": "ccpipe", "body": "Notifications are on for this device.",
                "tag": "ccpipe-test", "session": "", "level": "test"}
    if item.level == "done":
        title = f"Claude finished · {item.session}"
        body = item.body if details and item.body else "Its turn is over."
    else:
        title = f"{item.title} · {item.session}"
        body = item.body if details and item.body else (
            "Open ccpipe to answer." if item.kind == "question" else "Open ccpipe to allow or deny it.")
    # Well inside one push message (webpush.MAX_PLAINTEXT, in bytes).
    return {"title": _clip_bytes(title, 300), "body": _clip_bytes(body, 1500),
            "tag": f"ccpipe-{level}-{item.session}",
            "session": item.session, "level": level, "id": item.id,
            "count": len(attention.items())}


def _subject(sub: dict[str, Any]) -> str:
    origin = sub.get("origin") or ""
    return origin if isinstance(origin, str) and origin.startswith("https://") else FALLBACK_SUBJECT


def _topic(item: attention.Item | None, level: str) -> str:
    """Replaces a notification still waiting at the push service. Letters
    and digits only, ≤ 32: RFC 8030 allows "-" and "_" too, but Apple
    refused "test-…" (400 BadWebPushTopic, the user's iPad, 2026-11-07)
    and Mozilla's autopush documents [A-Za-z0-9] only."""
    name = item.session if item else "test"
    return f"{level}{hashlib.sha256(name.encode()).hexdigest()[:24]}"


async def _send(sub: dict[str, Any], item: attention.Item | None, level: str) -> webpush.Result:
    prefs = _prefs(sub.get("prefs"))
    result = await webpush.send(vapid(), _subject(sub), sub, _payload(item, prefs["details"], level),
                                ttl=TTL_S[level], urgency=URGENCY[level], topic=_topic(item, level))
    await asyncio.to_thread(_record, sub["id"], result)
    if not result.ok:
        log.info("push to %s: %s %s", sub.get("label") or sub["id"], result.status, result.detail)
    return result


async def send_test(endpoint: str) -> webpush.Result:
    with _lock:
        sub = next((s for s in _load()["subs"] if s.get("endpoint") == endpoint), None)
    if sub is None:
        return webpush.Result(0, detail="this device isn't subscribed")
    return await _send(sub, None, "test")


_recent: dict[str, collections.deque[float]] = {}
_done_at: dict[tuple[str, str], float] = {}


def _allowed(sub_id: str, item: attention.Item) -> bool:
    now = time.monotonic()
    q = _recent.setdefault(sub_id, collections.deque())
    while q and now - q[0] > BURST_WINDOW_S:
        q.popleft()
    if len(q) >= BURST:
        return False
    if item.level == "done":
        key = (sub_id, item.session)
        if now - _done_at.get(key, float("-inf")) < DONE_GAP_S:
            return False
        _done_at[key] = now
    q.append(now)
    return True


def _targets(item: attention.Item) -> list[dict[str, Any]]:
    with _lock:
        subs = _load()["subs"]
    return [s for s in subs if (p := _prefs(s.get("prefs"))).get(item.level)
            and (p["open"] or not device_visible(s.get("device", "")))]


def _open_here(s: dict[str, Any]) -> bool:
    """On screen on a device that wants it even so."""
    return _prefs(s.get("prefs"))["open"] and device_visible(s.get("device", ""))


def _still(item: attention.Item) -> bool:
    return attention.get(item.session, item.level) is item


def _wanted(item: attention.Item) -> list[dict[str, Any]]:
    with _lock:
        subs = _load()["subs"]
    return [s for s in subs if _prefs(s.get("prefs")).get(item.level)]


async def _push(item: attention.Item, targets: list[dict[str, Any]], done: set[str]) -> None:
    done.update(s["id"] for s in targets)
    targets = [s for s in targets if _allowed(s["id"], item)]
    if targets:
        log.info("push: %s %s → %d device(s)", item.session, item.level, len(targets))
        await asyncio.gather(*(_send(s, item, item.level) for s in targets),
                             return_exceptions=True)


async def _deliver(item: attention.Item) -> None:
    if not _wanted(item):
        return
    done: set[str] = set()
    # On screen on a device that asked for them there too: it's the one
    # you're using, so now.
    await _push(item, [s for s in _targets(item) if _open_here(s)], done)
    # Another device to push to now, while ccpipe is on screen somewhere
    # (so on another device): give you the chance to answer it there first.
    wait = app_config.load().notify.push_wait_s
    if wait and [s for s in _targets(item) if s["id"] not in done] and anyone_visible():
        await asyncio.sleep(wait)
    until = time.monotonic() + HOLD_S
    while _still(item):
        await _push(item, [s for s in _targets(item) if s["id"] not in done], done)
        # Devices that had ccpipe on screen: again shortly, in case it's put
        # away with this still waiting.
        if not [s for s in _wanted(item) if s["id"] not in done] or time.monotonic() > until:
            return
        await asyncio.sleep(RECHECK_S)


_pending: dict[int, asyncio.Task[None]] = {}


def _delivered(task: asyncio.Task[None], item_id: int) -> None:
    _pending.pop(item_id, None)
    if not task.cancelled() and task.exception() is not None:
        # Otherwise pushes stop without a word (a damaged push.json, say).
        log.error("push: delivery failed", exc_info=task.exception())


def _on_attention(event: str, item: attention.Item) -> None:
    if event == "new":
        try:
            task = asyncio.get_running_loop().create_task(_deliver(item))
        except RuntimeError:
            return      # no loop: nothing to send with
        _pending[item.id] = task
        task.add_done_callback(lambda t, i=item.id: _delivered(t, i))
    elif event == "end":
        task = _pending.pop(item.id, None)
        if task is not None:
            task.cancel()


attention.subscribe(_on_attention)


async def stop() -> None:
    for task in list(_pending.values()):
        task.cancel()
    await webpush.close()
