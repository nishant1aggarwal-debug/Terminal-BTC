from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import risk
from app.services.scheduler import tick

router = APIRouter(prefix="/control", tags=["control"])


class KillBody(BaseModel):
    reason: str = ""


@router.post("/kill")
async def kill(body: KillBody) -> dict[str, Any]:
    risk.set_kill_switch(True, body.reason or "manual")
    return {"ok": True, "kill_switch": True, "reason": body.reason}


@router.post("/resume")
async def resume() -> dict[str, Any]:
    risk.set_kill_switch(False, "")
    return {"ok": True, "kill_switch": False}


@router.post("/tick-now")
async def tick_now() -> dict[str, Any]:
    return await tick(source="scheduler")
