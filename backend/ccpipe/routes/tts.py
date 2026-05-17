"""TTS endpoints — voice list + config + Kokoro proxy.

Speak / preview both open the upstream stream BEFORE constructing the
FastAPI ``StreamingResponse`` so a Kokoro 5xx surfaces as our own 502
with proper headers (a raise inside the generator would land after
headers were sent → the client gets a truncated MP3 with no error
indication).
"""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import config as app_config
from ..auth import AuthDep, CsrfDep

log = logging.getLogger(__name__)
router = APIRouter()


def _kokoro_url() -> str:
    return os.environ.get("CCPIPE_KOKORO_URL", "http://localhost:8880").rstrip("/")


class TtsConfigBody(BaseModel):
    voice: str | None = None
    speech_rate: float | None = Field(default=None, ge=0.5, le=2.0)
    enabled: bool | None = None
    scope: str | None = None  # one of ccpipe.config.VALID_SCOPES


class SpeakBody(BaseModel):
    text: str
    voice: str | None = None


@router.get("/api/tts/voices", dependencies=[AuthDep])
async def tts_voices() -> dict[str, list[str]]:
    """List Kokoro voice names. Returns an empty list if Kokoro is
    unreachable so the UI can render a graceful 'no voices available'."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{_kokoro_url()}/v1/audio/voices")
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("kokoro voices fetch failed: %s", exc)
        return {"voices": []}
    voices = data.get("voices", []) if isinstance(data, dict) else data
    if not isinstance(voices, list):
        return {"voices": []}
    return {"voices": [str(v) for v in voices if isinstance(v, str)]}


@router.get("/api/tts/config", dependencies=[AuthDep])
async def tts_config_get() -> dict[str, object]:
    return dict(app_config.load().to_dict()["tts"])


@router.post("/api/tts/config", dependencies=[AuthDep, CsrfDep])
async def tts_config_set(body: TtsConfigBody) -> dict[str, object]:
    cfg = app_config.load()
    if body.voice is not None:
        v = body.voice.strip()
        if v:
            if len(v) > 64:
                raise HTTPException(status_code=400, detail="voice too long")
            cfg.tts.voice = v
    if body.speech_rate is not None:
        cfg.tts.speech_rate = max(0.5, min(2.0, float(body.speech_rate)))
    if body.enabled is not None:
        cfg.tts.enabled = bool(body.enabled)
    if body.scope is not None:
        if body.scope in app_config.VALID_SCOPES:
            cfg.tts.scope = body.scope
        else:
            raise HTTPException(
                status_code=400,
                detail=f"invalid scope; one of {list(app_config.VALID_SCOPES)}",
            )
    app_config.save(cfg)
    return dict(cfg.to_dict()["tts"])


async def _open_kokoro_stream(payload: dict) -> tuple[httpx.AsyncClient, object, httpx.Response]:
    """Open a streaming POST to Kokoro and return (client, stream_cm,
    response) ready for the caller to iterate. Raises HTTPException on
    failure so a Kokoro 5xx becomes a 502 BEFORE any StreamingResponse
    headers reach the client."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=2.0, read=60.0,
                                                      write=10.0, pool=2.0))
    try:
        stream_cm = client.stream("POST", f"{_kokoro_url()}/v1/audio/speech",
                                    json=payload)
        resp = await stream_cm.__aenter__()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"kokoro unreachable: {exc}")
    if resp.status_code != 200:
        await stream_cm.__aexit__(None, None, None)
        await client.aclose()
        raise HTTPException(status_code=502, detail="kokoro error")
    return client, stream_cm, resp


def _stream_then_close(client: httpx.AsyncClient, stream_cm: object,
                        resp: httpx.Response) -> StreamingResponse:
    async def stream():
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            try: await stream_cm.__aexit__(None, None, None)
            except Exception: pass
            await client.aclose()
    return StreamingResponse(stream(), media_type="audio/mpeg")


@router.post("/api/tts/speak", dependencies=[AuthDep, CsrfDep])
async def tts_speak(body: SpeakBody) -> StreamingResponse:
    """Synthesize arbitrary text and stream MP3 back. Distinct from
    /api/tts/preview — this is the "repeat last response" endpoint
    used by the statusbar replay pill."""
    cfg = app_config.load()
    voice = (body.voice or cfg.tts.voice or "").strip()
    if not voice or len(voice) > 64:
        raise HTTPException(status_code=400, detail="invalid voice")
    text = body.text or ""
    if not text or len(text) > 4000:
        raise HTTPException(status_code=400, detail="text empty or too long")
    payload = {
        "model": "kokoro",
        "input": text,
        "voice": voice,
        "speed": cfg.tts.speech_rate,
        "response_format": "mp3",
    }
    client, stream_cm, resp = await _open_kokoro_stream(payload)
    return _stream_then_close(client, stream_cm, resp)


@router.get("/api/tts/preview", dependencies=[AuthDep])
async def tts_preview(request: Request, voice: str,
                       text: str = "Voice test, one two three.",
                       ) -> StreamingResponse:
    """Synthesize a short sample with the given voice and stream MP3
    back. Used by the settings modal's Test button.

    GETs with credentials can be triggered cross-origin via <img>,
    <audio>, etc., which would let a malicious page meter Kokoro work
    against the authenticated session. CsrfDep can't help (browsers
    don't send custom headers for such loads), so we rely on Fetch
    Metadata: Sec-Fetch-Site must be same-origin. We deliberately
    reject when it's absent to keep the gate strict."""
    sfs = request.headers.get("sec-fetch-site", "").lower()
    if sfs != "same-origin":
        raise HTTPException(status_code=403, detail="cross-origin preview blocked")
    if not voice or len(voice) > 64:
        raise HTTPException(status_code=400, detail="invalid voice")
    if len(text) > 200:
        raise HTTPException(status_code=400, detail="preview text too long")
    cfg = app_config.load()
    payload = {
        "model": "kokoro",
        "input": text,
        "voice": voice,
        "speed": cfg.tts.speech_rate,
        "response_format": "mp3",
    }
    client, stream_cm, resp = await _open_kokoro_stream(payload)
    return _stream_then_close(client, stream_cm, resp)
