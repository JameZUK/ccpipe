"""Static frontend file routes.

The frontend dist directory is configurable via CCPIPE_FRONTEND_DIST so
a development checkout can point at the in-tree ``frontend/dist`` while
a packaged install points at e.g. ``/opt/ccpipe/frontend``. Routes are
registered unconditionally so the operator sees a useful 503 (with
the actual missing path) rather than a confusing 404 when the Vite
build hasn't run yet.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)
router = APIRouter()


def _frontend_dist() -> Path:
    return Path(os.environ.get("CCPIPE_FRONTEND_DIST") or "/app/frontend")


# The /assets mount needs to happen on the *app* (not on a router) so
# that StaticFiles can lay down its own subroutes. Expose a function the
# main module calls during startup.
def mount_static(app: FastAPI) -> None:
    dist = _frontend_dist()
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
    else:
        log.warning("CCPIPE_FRONTEND_DIST=%s does not exist; static routes will 503 "
                    "until the frontend is built (cd frontend && npm run build)",
                    dist)


def _serve_file(relative: str) -> FileResponse:
    target = _frontend_dist() / relative
    if not target.exists():
        raise HTTPException(
            status_code=503,
            detail=f"frontend not built; missing {target}. "
                   f"Run: cd frontend && npm run build",
        )
    # HTML (and any other non-hashed top-level asset) MUST be revalidated
    # on every load — without this, browsers heuristically cache index.html
    # for hours and keep referencing stale hashed asset URLs even after a
    # rebuild + service restart. Hashed bundles under /assets/* are
    # immutable by Vite's design and stay cacheable.
    return FileResponse(target, headers={"Cache-Control": "no-store"})


@router.get("/")
async def index() -> FileResponse: return _serve_file("index.html")


@router.get("/manifest.webmanifest")
async def manifest() -> FileResponse: return _serve_file("manifest.webmanifest")


@router.get("/sw.js")
async def service_worker() -> FileResponse: return _serve_file("sw.js")


# PWA icon set. Each variant is served from its own route so the
# top-level URLs stay flat (browsers expect /icon-192.png, not
# /assets/icon-192.png) and the TrustedHostMiddleware + CSP layer
# still apply. The SVG is the canonical mark used by desktop browser
# tabs; the PNG raster pack is what mobile launchers actually pick
# up — Firefox Android in particular won't fall back from a 404'd
# manifest icon to the SVG, so the PNGs must be reachable or the
# home-screen shortcut ends up with a generated letter glyph.
@router.get("/icon.svg")
async def icon_svg() -> FileResponse: return _serve_file("icon.svg")


@router.get("/icon-maskable.svg")
async def icon_maskable_svg() -> FileResponse: return _serve_file("icon-maskable.svg")


@router.get("/icon-192.png")
async def icon_192_png() -> FileResponse: return _serve_file("icon-192.png")


@router.get("/icon-512.png")
async def icon_512_png() -> FileResponse: return _serve_file("icon-512.png")


@router.get("/icon-maskable-192.png")
async def icon_maskable_192_png() -> FileResponse:
    return _serve_file("icon-maskable-192.png")


@router.get("/icon-maskable-512.png")
async def icon_maskable_512_png() -> FileResponse:
    return _serve_file("icon-maskable-512.png")


@router.get("/apple-touch-icon.png")
async def apple_touch_icon() -> FileResponse:
    return _serve_file("apple-touch-icon.png")


@router.get("/mic-worklet.js")
async def mic_worklet() -> FileResponse: return _serve_file("mic-worklet.js")
