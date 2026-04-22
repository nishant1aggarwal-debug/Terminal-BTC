from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import backtest, macro, risk
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
    """Kick off a tick in the background and return immediately.

    Doing the full 16-symbol sweep synchronously can exceed cloud hosts' HTTP
    timeouts (Render free tier = 30s). The client polls /api/overview for the
    heartbeat to see when it finishes.
    """
    import asyncio
    asyncio.create_task(tick(source="scheduler"))
    return {"ok": True, "message": "tick started in background"}


@router.post("/macro-refresh")
async def macro_refresh() -> dict[str, Any]:
    row = macro.refresh_fear_greed()
    if row is None:
        return {"ok": False, "error": "fetch_failed"}
    return {
        "ok": True,
        "name": row.name,
        "value": row.value,
        "classification": row.classification,
        "fetched_at": row.fetched_at.isoformat(),
    }


@router.post("/backtest-now")
async def backtest_now() -> dict[str, Any]:
    """Run the backtester on every TRADE_SYMBOL right now and return a summary."""
    reports = backtest.run_all()
    return {
        "ok": True,
        "count": len(reports),
        "reports": [
            {
                "symbol": r.symbol,
                "trades": r.trades,
                "win_rate_pct": r.win_rate_pct,
                "profit_factor": r.profit_factor,
                "net_pnl_pct": r.net_pnl_pct,
                "max_drawdown_pct": r.max_drawdown_pct,
            }
            for r in reports
        ],
    }
