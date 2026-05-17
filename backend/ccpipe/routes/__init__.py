"""HTTP route packages.

Each submodule exports a single ``router`` APIRouter that's wired into
the FastAPI app via ``app.include_router`` in ``ccpipe.main``. Routes
are split by concern (auth, fs, tts, sessions, …) so each module fits
in a couple hundred LOC and can be unit-tested in isolation.

main.py keeps middleware, lifespan, the WebSocket endpoint, and the
static-file fallbacks because those are tightly coupled to the
application factory.
"""
