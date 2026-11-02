"""Host resource use for the session picker and the session bar
(sysstats.py): CPU, memory and root-filesystem space with ten minutes of
history, plus — with ?session= — that session's CPU, memory and project
size."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import sysstats, tmux
from ..auth import AuthDep, SameOriginDep

router = APIRouter()


@router.get("/api/system", dependencies=[AuthDep, SameOriginDep])
async def system_stats(session: str | None = None) -> dict[str, Any]:
    if session is not None:
        try:
            session = tmux.safe_name(session)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    sysstats.touch()
    return sysstats.snapshot(session)
