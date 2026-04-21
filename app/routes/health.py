from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter

from app.config import get_settings
from app.db import get_session
from app.models import DailyPnL, KillSwitch, Position

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ready"}


@router.get("/status")
async def status() -> dict[str, Any]:
    settings = get_settings()
    with get_session() as s:
        ks = s.get(KillSwitch, 1)
        pnl = s.get(DailyPnL, date.today())
        pos = s.get(Position, settings.trade_symbol)
    return {
        "testnet": settings.binance_testnet,
        "live_trading": settings.live_trading,
        "real_orders_enabled": settings.real_orders_enabled,
        "symbol": settings.trade_symbol,
        "market": settings.trade_market,
        "timeframe": settings.trade_timeframe,
        "poll_interval_sec": settings.poll_interval_sec,
        "model": settings.claude_model,
        "kill_switch": {"enabled": bool(ks.enabled) if ks else False, "reason": ks.reason if ks else ""},
        "position": {"qty": pos.qty, "avg_entry": pos.avg_entry} if pos else {"qty": 0.0, "avg_entry": 0.0},
        "today": {
            "claude_usd_spent": pnl.claude_usd_spent if pnl else 0.0,
            "realized_usdt": pnl.realized_usdt if pnl else 0.0,
            "unrealized_usdt": pnl.unrealized_usdt if pnl else 0.0,
        },
    }
