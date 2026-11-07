"""What needs you, and push notifications (attention.py, notify.py).

  GET  /api/attention           everything that needs you now; the session
                                list polls it, and it counts as "a page is
                                on screen on this device" (?device=&visible=1)
  POST /api/attention/dismiss   clear finished turns (one session, or all)
  GET  /api/push                this server's push key, the subscribed
                                devices and the server-wide setting
  POST /api/push/subscribe      turn notifications on for a device
  POST /api/push/prefs          what a device is told about
  POST /api/push/unsubscribe    turn them off (by endpoint, or by id from
                                another device's settings)
  POST /api/push/test           send this device a test notification
  POST /api/push/status         whether the server still has this device
                                (by its endpoint), and its choices
  POST /api/push/config         the server-wide wait (notify.push_wait_s)
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import attention, notify
from .. import config as app_config
from ..auth import AuthDep, CsrfDep, SameOriginDep
from ..tmux import _SAFE_NAME_RE

router = APIRouter()

_DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _device(value: str | None) -> str:
    return value if value and _DEVICE_RE.fullmatch(value) else ""


@router.get("/api/attention", dependencies=[AuthDep, SameOriginDep])
async def get_attention(device: str | None = None, visible: int = 0) -> dict[str, Any]:
    d = _device(device)
    if d:
        notify.note_page(d, bool(visible))
    return {"items": [i.public() for i in attention.items()]}


class DismissBody(BaseModel):
    # A session's finished turn, or every one (None).
    session: str | None = None


@router.post("/api/attention/dismiss", dependencies=[AuthDep, CsrfDep])
async def dismiss(body: DismissBody) -> dict[str, Any]:
    if body.session is not None and not _SAFE_NAME_RE.fullmatch(body.session):
        raise HTTPException(status_code=400, detail="bad session name")
    attention.dismiss(body.session)
    return {"items": [i.public() for i in attention.items()]}


@router.get("/api/push", dependencies=[AuthDep, SameOriginDep])
async def push_state() -> dict[str, Any]:
    return {"key": notify.vapid().public_b64,
            "devices": notify.subscriptions(),
            "push_wait_s": app_config.load().notify.push_wait_s}


class Keys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class Prefs(BaseModel):
    ask: bool | None = None
    done: bool | None = None
    details: bool | None = None


class SubscribeBody(BaseModel):
    endpoint: str = Field(max_length=2048)
    keys: Keys
    device: str = Field(default="", max_length=64)
    label: str = Field(default="", max_length=80)
    prefs: Prefs | None = None
    # The endpoint this one replaces (the service worker, when the browser
    # renews the subscription).
    renew: str = Field(default="", max_length=2048)


def _origin(request: Request) -> str:
    # SameOrigin/CSRF checks have already tied this to our own page.
    origin = request.headers.get("origin") or ""
    if origin:
        return origin[:200]
    return f"{request.url.scheme}://{request.url.netloc}"[:200]


@router.post("/api/push/subscribe", dependencies=[AuthDep, CsrfDep])
async def subscribe(body: SubscribeBody, request: Request) -> dict[str, Any]:
    label = re.sub(r"[\x00-\x1f]", " ", body.label).strip()
    try:
        return notify.subscribe(body.endpoint, body.keys.p256dh, body.keys.auth,
                                device=_device(body.device), label=label,
                                origin=_origin(request),
                                prefs=body.prefs.model_dump(exclude_none=True) if body.prefs else None,
                                renew=body.renew)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


class EndpointBody(BaseModel):
    endpoint: str = Field(default="", max_length=2048)
    id: str = Field(default="", max_length=32)
    prefs: Prefs | None = None


@router.post("/api/push/prefs", dependencies=[AuthDep, CsrfDep])
async def prefs(body: EndpointBody) -> dict[str, Any]:
    out = notify.set_prefs(body.endpoint, body.prefs.model_dump(exclude_none=True) if body.prefs else {})
    if out is None:
        raise HTTPException(status_code=404, detail="this device isn't subscribed")
    return out


@router.post("/api/push/unsubscribe", dependencies=[AuthDep, CsrfDep])
async def unsubscribe(body: EndpointBody) -> dict[str, Any]:
    if not body.endpoint and not body.id:
        raise HTTPException(status_code=400, detail="which device?")
    return {"removed": notify.unsubscribe(body.endpoint or None, body.id or None)}


@router.post("/api/push/status", dependencies=[AuthDep, CsrfDep])
async def status(body: EndpointBody) -> dict[str, Any]:
    # A POST so the endpoint (a capability URL) stays out of access logs.
    return {"device": notify.find(body.endpoint) if body.endpoint else None}


@router.post("/api/push/test", dependencies=[AuthDep, CsrfDep])
async def test(body: EndpointBody) -> dict[str, Any]:
    r = await notify.send_test(body.endpoint)
    return {"ok": r.ok, "status": r.status, "detail": r.detail}


class ConfigBody(BaseModel):
    push_wait_s: int = Field(ge=0, le=app_config.PUSH_WAIT_MAX_S)


@router.post("/api/push/config", dependencies=[AuthDep, CsrfDep])
async def set_config(body: ConfigBody) -> dict[str, Any]:
    cfg = app_config.load_for_edit()
    cfg.notify.push_wait_s = body.push_wait_s
    app_config.save(cfg)
    return {"push_wait_s": cfg.notify.push_wait_s}
