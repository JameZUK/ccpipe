"""/api/tts/voices accepts both Kokoro voice-list formats (no network)."""
from __future__ import annotations

import asyncio

import pytest

from ccpipe.routes import tts as tts_routes


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_client(data):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _Resp(data)

    return C


@pytest.mark.parametrize("data", [
    {"voices": ["af_bella", "bf_emma"]},                                  # older Kokoro-FastAPI
    {"voices": [{"id": "af_bella", "name": "af_bella", "overall_grade": "A-"},
                {"id": "bf_emma", "name": "bf_emma"}, {"name": "bf_emma"}, {"x": 1}]},  # newer
    ["af_bella", "bf_emma"],                                              # bare list
])
def test_voice_names_from_either_format(monkeypatch, data):
    monkeypatch.setattr(tts_routes.httpx, "AsyncClient", _fake_client(data))
    assert asyncio.run(tts_routes.tts_voices()) == {"voices": ["af_bella", "bf_emma"]}
