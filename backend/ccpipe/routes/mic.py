"""Voice-input config endpoints.

Surfaces ``MicConfig`` (see ``ccpipe.config``) over HTTP so the Settings
UI can read + write the four knobs. Persisted to the same
``~/.local/state/ccpipe/config.json`` as the TTS settings.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import config as app_config
from ..auth import AuthDep, CsrfDep

log = logging.getLogger(__name__)
router = APIRouter()


class MicConfigBody(BaseModel):
    auto_stop_enabled: bool | None = None
    # Bounds chosen to keep the UI from saving a value that breaks the
    # system: silence below 200 ms would trip on intra-word pauses;
    # above 15 s the mic would feel stuck. drain_pad over 10 s is
    # ridiculous. max_recording 5..600 s covers anything reasonable.
    silence_ms: int | None = Field(default=None, ge=200, le=15000)
    drain_pad_ms: int | None = Field(default=None, ge=0, le=10000)
    max_recording_seconds: int | None = Field(default=None, ge=5, le=600)


@router.get("/api/mic/config", dependencies=[AuthDep])
async def mic_config_get() -> dict[str, object]:
    return dict(app_config.load().to_dict()["mic"])


@router.post("/api/mic/config", dependencies=[AuthDep, CsrfDep])
async def mic_config_set(body: MicConfigBody) -> dict[str, object]:
    cfg = app_config.load()
    if body.auto_stop_enabled is not None:
        cfg.mic.auto_stop_enabled = bool(body.auto_stop_enabled)
    if body.silence_ms is not None:
        cfg.mic.silence_ms = max(200, min(15000, int(body.silence_ms)))
    if body.drain_pad_ms is not None:
        cfg.mic.drain_pad_ms = max(0, min(10000, int(body.drain_pad_ms)))
    if body.max_recording_seconds is not None:
        cfg.mic.max_recording_seconds = max(
            5, min(600, int(body.max_recording_seconds))
        )
    app_config.save(cfg)
    return dict(cfg.to_dict()["mic"])
