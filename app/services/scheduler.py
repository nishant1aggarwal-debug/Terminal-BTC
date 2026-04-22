from __future__ import annotations

import asyncio
import json
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db import get_session
from app.exchange import data_source
from app.logging_setup import get_logger
from app.models import Decision, Position
from app.services import backtest, executor, funding, macro, signal
from app.services.market_data import get_snapshot

log = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None
_tv_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)

# Heartbeat state the dashboard reads via /api/overview.
_last_tick_started_at: str | None = None
_last_tick_finished_at: str | None = None
_last_tick_summary: dict[str, Any] | None = None


def heartbeat() -> dict[str, Any]:
    from datetime import datetime, timezone
    settings = get_settings()
    next_at = None
    if _scheduler is not None:
        job = _scheduler.get_job("tick")
        if job and job.next_run_time:
            next_at = job.next_run_time.astimezone(timezone.utc).isoformat()
    return {
        "scheduler_running": _scheduler is not None,
        "poll_interval_sec": settings.poll_interval_sec,
        "last_tick_started_at": _last_tick_started_at,
        "last_tick_finished_at": _last_tick_finished_at,
        "last_tick_summary": _last_tick_summary,
        "next_tick_at": next_at,
    }


def tv_queue() -> asyncio.Queue[dict[str, Any]]:
    return _tv_queue


def _current_position(symbol: str) -> dict[str, Any]:
    with get_session() as s:
        pos = s.get(Position, symbol)
        if pos is None:
            return {"qty": 0.0, "avg_entry": 0.0}
        return {"qty": pos.qty, "avg_entry": pos.avg_entry}


async def _tick_symbol(
    symbol: str, tf: str, tv_alert: dict[str, Any] | None, source: str
) -> dict[str, Any]:
    try:
        snap = await asyncio.to_thread(get_snapshot, symbol, tf)
    except Exception as exc:
        log.error("snapshot_failed", symbol=symbol, error=str(exc))
        return {"symbol": symbol, "status": "skipped", "reason": f"snapshot: {exc}"}

    position = _current_position(symbol)

    try:
        decision = await asyncio.to_thread(signal.generate, snap.to_dict(), tv_alert, position)
    except Exception as exc:
        log.error("signal_generation_failed", symbol=symbol, error=str(exc))
        return {"symbol": symbol, "status": "skipped", "reason": f"signal_error: {exc}"}

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
            reasoning=f"[{decision.backend}] {decision.reasoning}",
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
        decision.confidence,
    )
    log.info(
        "tick_complete",
        decision_id=decision_id,
        symbol=symbol,
        action=decision.action,
        status=result.status,
        message=result.message,
        source=source,
    )
    return {
        "symbol": symbol,
        "decision_id": decision_id,
        "action": decision.action,
        "status": result.status,
        "message": result.message,
    }


async def tick(
    tv_alert: dict[str, Any] | None = None, source: str = "scheduler"
) -> dict[str, Any]:
    """Run one decision cycle across all TRADE_SYMBOLS.

    A webhook-triggered tick scopes to the alert's symbol (if it's on the allowlist);
    a scheduler tick sweeps every configured symbol serially.
    """
    global _last_tick_started_at, _last_tick_finished_at, _last_tick_summary
    from datetime import datetime, timezone
    _last_tick_started_at = datetime.now(timezone.utc).isoformat()

    settings = get_settings()
    tf = settings.trade_timeframe

    if tv_alert and tv_alert.get("symbol"):
        symbols_to_run = [tv_alert["symbol"]] if tv_alert["symbol"] in settings.allowed_symbols else []
        if not symbols_to_run:
            log.warning("tv_alert_symbol_not_allowed", symbol=tv_alert.get("symbol"))
            _last_tick_finished_at = datetime.now(timezone.utc).isoformat()
            return {"status": "skipped", "reason": "symbol_not_allowed", "results": []}
    else:
        symbols_to_run = data_source.filter_supported(settings.symbols)

    # Parallel fan-out: fetch snapshots and run decisions concurrently.
    # Previously sequential (16 × ~1s/symbol ≈ 16-20s) — hit Render's 30s
    # request timeout on free tier. gather() brings it down to ~3-4s.
    coros = []
    for sym in symbols_to_run:
        alert_for_sym = tv_alert if tv_alert and tv_alert.get("symbol") == sym else None
        coros.append(_tick_symbol(sym, tf, alert_for_sym, source))
    results = list(await asyncio.gather(*coros, return_exceptions=False))

    _last_tick_finished_at = datetime.now(timezone.utc).isoformat()
    # Summarize by action so the dashboard can show "2 buys, 1 sell, 13 holds".
    counts = {"buy": 0, "sell": 0, "hold": 0, "skipped": 0}
    for r in results:
        key = r.get("action") or ("skipped" if r.get("status") == "skipped" else "hold")
        counts[key] = counts.get(key, 0) + 1
    _last_tick_summary = {"counts": counts, "total": len(results), "source": source}

    return {"status": "ok", "count": len(results), "results": results}


async def _tv_consumer() -> None:
    while True:
        alert = await _tv_queue.get()
        try:
            await tick(tv_alert=alert, source="tv")
        except Exception as exc:
            log.error("tv_consumer_error", error=str(exc))
        finally:
            _tv_queue.task_done()


async def _funding_job() -> None:
    """Fires every hour; only charges when UTC hour is 0/8/16."""
    if funding.should_charge_now():
        await asyncio.to_thread(funding.charge_funding)


async def _macro_job() -> None:
    """Refresh Fear & Greed index from alternative.me."""
    await asyncio.to_thread(macro.refresh_fear_greed)


async def _backtest_job() -> None:
    """Replay the rules engine against historical candles for every TRADE_SYMBOL."""
    await asyncio.to_thread(backtest.run_all)


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    settings = get_settings()
    sched = AsyncIOScheduler()
    sched.add_job(tick, "interval", seconds=settings.poll_interval_sec, id="tick", max_instances=1)
    # Check funding every minute — job no-ops unless we're at an 8h boundary.
    sched.add_job(_funding_job, "interval", seconds=60, id="funding", max_instances=1)
    # Macro indicators (F&G) every MACRO_POLL_MIN minutes.
    sched.add_job(
        _macro_job, "interval", minutes=settings.macro_poll_min, id="macro", max_instances=1,
    )
    # Nightly backtest at BACKTEST_HOUR_UTC.
    sched.add_job(
        _backtest_job, "cron", hour=settings.backtest_hour_utc, minute=0,
        id="backtest", max_instances=1,
    )
    sched.start()
    # Warm the F&G cache on startup so the first ticks see it.
    asyncio.get_event_loop().create_task(_macro_job())
    asyncio.get_event_loop().create_task(_tv_consumer())
    _scheduler = sched
    log.info(
        "scheduler_started",
        poll_sec=settings.poll_interval_sec,
        symbols=settings.symbols,
        data_source=settings.data_source,
    )


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler_stopped")
