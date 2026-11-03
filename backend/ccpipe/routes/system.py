"""Host resource use for the session picker and the session bar
(sysstats.py): CPU, memory and root-filesystem space with ten minutes of
history, plus — with ?session= — that session's CPU, memory, project size
and context-window fill. ``plan`` is the Claude subscription's usage
limits (plan_usage.py); POST /api/plan-usage/refresh asks for a fresh
check when the pop-up opens."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import plan_usage, sysstats, tmux
from ..auth import AuthDep, CsrfDep, SameOriginDep

router = APIRouter()


@router.get("/api/system", dependencies=[AuthDep, SameOriginDep])
async def system_stats(session: str | None = None) -> dict[str, Any]:
    if session is not None:
        try:
            session = tmux.safe_name(session)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    sysstats.touch()
    out = sysstats.snapshot(session)
    out["plan"] = plan_usage.snapshot()
    if session is not None and "session" in out:
        out["session"]["context"] = await plan_usage.session_context(session)
    return out


@router.post("/api/plan-usage/refresh", dependencies=[AuthDep, CsrfDep])
async def plan_usage_refresh() -> dict[str, Any]:
    return {"checking": plan_usage.request_refresh(), "plan": plan_usage.snapshot()}
