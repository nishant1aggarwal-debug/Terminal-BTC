from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter
from sqlmodel import select

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
        positions = s.exec(select(Position)).all()

    positions_out = {
        p.symbol: {"qty": p.qty, "avg_entry": p.avg_entry}
        for p in positions
        if abs(p.qty) > 1e-9
    }

    return {
        "paper_mode": settings.paper_mode,
        "signal_mode": settings.signal_mode,
        "data_source": settings.data_source,
        "testnet": settings.binance_testnet,
        "live_trading": settings.live_trading,
        "real_orders_enabled": settings.real_orders_enabled,
        "symbols": settings.symbols,
        "market": settings.trade_market,
        "timeframe": settings.trade_timeframe,
        "poll_interval_sec": settings.poll_interval_sec,
        "claude_model": settings.claude_model if settings.anthropic_api_key else None,
        "paper_starting_equity_usdt": settings.paper_starting_equity_usdt if settings.paper_mode else None,
        "kill_switch": {
            "enabled": bool(ks.enabled) if ks else False,
            "reason": ks.reason if ks else "",
        },
        "open_positions": positions_out,
        "today": {
            "claude_usd_spent": pnl.claude_usd_spent if pnl else 0.0,
            "realized_usdt": pnl.realized_usdt if pnl else 0.0,
            "unrealized_usdt": pnl.unrealized_usdt if pnl else 0.0,
        },
    }
