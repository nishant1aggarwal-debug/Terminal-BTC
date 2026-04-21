from __future__ import annotations

import asyncio
import json
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import Decision, Position
from app.services import claude_signal, executor
from app.services.market_data import get_snapshot

log = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None
_tv_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)


def tv_queue() -> asyncio.Queue[dict[str, Any]]:
    return _tv_queue


def _current_position(symbol: str) -> dict[str, Any]:
    with get_session() as s:
        pos = s.get(Position, symbol)
        if pos is None:
            return {"qty": 0.0, "avg_entry": 0.0}
        return {"qty": pos.qty, "avg_entry": pos.avg_entry}


async def tick(tv_alert: dict[str, Any] | None = None, source: str = "scheduler") -> dict[str, Any]:
    settings = get_settings()
    symbol = settings.trade_symbol
    tf = settings.trade_timeframe

    snap = await asyncio.to_thread(get_snapshot, symbol, tf)
    position = _current_position(symbol)

    try:
        decision = await asyncio.to_thread(
            claude_signal.generate_decision, snap.to_dict(), tv_alert, position
        )
    except claude_signal.ClaudeCostCapExceeded as exc:
        log.warning("tick_skipped_cost_cap", error=str(exc))
        return {"status": "skipped", "reason": "cost_cap"}

    with get_session() as s:
        row = Decision(
            source=source,
            symbol=symbol,
            timeframe=tf,
            snapshot_json=json.dumps(snap.to_dict(), default=str),
            action=decision.action,
            size_pct=decision.size_pct,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            claude_usd_cost=decision.usd_cost,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        decision_id = row.id

    result = await asyncio.to_thread(
        executor.execute,
        decision_id,
        symbol,
        decision.action,
        decision.size_pct,
        snap.last_close,
    )
    log.info(
        "tick_complete",
        decision_id=decision_id,
        action=decision.action,
        status=result.status,
        message=result.message,
        source=source,
    )
    return {
        "decision_id": decision_id,
        "action": decision.action,
        "status": result.status,
        "message": result.message,
    }


async def _tv_consumer() -> None:
    while True:
        alert = await _tv_queue.get()
        try:
            await tick(tv_alert=alert, source="tv")
        except Exception as exc:
            log.error("tv_consumer_error", error=str(exc))
        finally:
            _tv_queue.task_done()


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    settings = get_settings()
    sched = AsyncIOScheduler()
    sched.add_job(tick, "interval", seconds=settings.poll_interval_sec, id="tick", max_instances=1)
    sched.start()
    asyncio.get_event_loop().create_task(_tv_consumer())
    _scheduler = sched
    log.info("scheduler_started", poll_sec=settings.poll_interval_sec)


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler_stopped")
