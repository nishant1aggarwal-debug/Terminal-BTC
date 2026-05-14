from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db import get_session
from app.exchange import data_source
from app.logging_setup import get_logger
from app.models import Decision, Position
from app.services import auditor, backtest, email_digest, executor, funding, macro, news, notifications, optimizer, regime, signal, targets
from app.services.market_data import get_snapshot

log = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None
_tv_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)

# Heartbeat state the dashboard reads via /api/overview.
_last_tick_started_at: str | None = None
_last_tick_finished_at: str | None = None
_last_tick_summary: dict[str, Any] | None = None


def heartbeat() -> dict[str, Any]:
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

    # Higher-timeframe confirmation — cuts whipsaw entries. 15m signals only
    # fire when 1h (or whatever HTF_TIMEFRAME is) agrees on direction.
    settings = get_settings()
    snap_dict = snap.to_dict()
    if settings.htf_confirmation:
        try:
            htf_snap = await asyncio.to_thread(
                get_snapshot, symbol, settings.htf_timeframe,
            )
            snap_dict["htf_trend_up"] = (
                htf_snap.ema_20 > htf_snap.ema_50 and htf_snap.macd_hist > 0
            )
            snap_dict["htf_trend_down"] = (
                htf_snap.ema_20 < htf_snap.ema_50 and htf_snap.macd_hist < 0
            )
            snap_dict["htf_timeframe"] = settings.htf_timeframe
        except Exception as exc:
            log.warning("htf_snapshot_failed", symbol=symbol, error=str(exc))
            snap_dict["htf_trend_up"] = None
            snap_dict["htf_trend_down"] = None

    # STEP 1 — target monitor runs BEFORE signal generation. If the latest bar
    # crossed SL/TP1/TP2/trailing for an open position, exit the appropriate
    # tranche first. This way the signal engine sees the post-exit state (flat
    # or reduced), not a stale pre-exit one.
    try:
        await asyncio.to_thread(targets.check_and_exit, symbol, snap.last_close)
    except Exception as exc:
        log.warning("target_check_failed", symbol=symbol, error=str(exc))

    position = _current_position(symbol)

    try:
        decision = await asyncio.to_thread(signal.generate, snap_dict, tv_alert, position)
    except Exception as exc:
        log.error("signal_generation_failed", symbol=symbol, error=str(exc))
        return {"symbol": symbol, "status": "skipped", "reason": f"signal_error: {exc}"}

    # Fire a SIGNAL notification whenever a fresh buy/sell clears the threshold.
    # This is separate from OPEN — it's the heads-up the user sees BEFORE the
    # executor processes risk/sizing, so they can copy the call to their real
    # exchange even if we veto our own paper fill.
    if decision.action in {"buy", "sell"} and source == "scheduler":
        atr = float(snap.atr_14) if snap.atr_14 else 0.0
        # Use the same SL/TP1/TP2 calculator as the executor + target monitor
        # so the levels in the user notification are exactly what gets planned
        # on the position when the order opens.
        sl_calc, tp1, tp2 = targets.compute_levels(snap.last_close, atr, decision.action)
        # Overwrite the decision's SL too — rules_signal still uses the old
        # 1.5×ATR-only computation; align it with the unified helper.
        decision.stop_loss = sl_calc
        # Whether the higher-timeframe trend agrees with this entry direction
        # — used by the style classifier to bump SWING → LONG when the 1h
        # backdrop says the move has legs.
        htf_agrees = None
        if decision.action == "buy" and snap_dict.get("htf_trend_up") is True:
            htf_agrees = True
        elif decision.action == "sell" and snap_dict.get("htf_trend_down") is True:
            htf_agrees = True

        # Detect whether this signal is a fresh entry or an ADD to an
        # existing same-direction position. The user wants explicit ADD
        # alerts ("buy more on SIREN, average down") instead of identical
        # LONG-LONG-LONG spam they can't distinguish from fresh entries.
        current_qty = float(position.get("qty") or 0.0)
        current_avg = float(position.get("avg_entry") or 0.0)
        is_same_direction = (
            (decision.action == "buy" and current_qty > 1e-9)
            or (decision.action == "sell" and current_qty < -1e-9)
        )
        is_add = is_same_direction
        # Look up the actual add count from the persisted Position so the
        # alert title says "ADD #1", "ADD #2", etc. matching what the
        # executor will record. +1 because we're about to add another.
        add_index = 0
        if is_add:
            from app.models import Position as _Position
            with get_session() as _s:
                _pos = _s.get(_Position, symbol)
                add_index = (_pos.adds if _pos else 0) + 1

        notifications.signal_fired(
            symbol=symbol,
            action=decision.action,
            confidence=decision.confidence or 0.0,
            entry=snap.last_close,
            sl=decision.stop_loss,
            tp1=tp1,
            tp2=tp2,
            reasoning=decision.reasoning,
            htf_agrees=htf_agrees,
            is_add=is_add,
            add_index=add_index,
            current_qty=current_qty,
            current_avg_entry=current_avg,
        )

    # Persist Decision rows for actionable signals (buy/sell) ALWAYS — these
    # power the auditor and the user's per-symbol audit trail. For 'hold'
    # decisions we keep only every Nth one (rotating sample) instead of
    # every tick × every symbol — that was dumping ~3000 rows/day of
    # mostly-uninteresting holds and was the primary driver of the Neon
    # egress quota burn. We still keep enough holds so /api/markets and the
    # auditor can read recent indicator snapshots.
    # Persistent SQLite on Render Starter — no egress quota to worry about, so
    # we write every Decision row (including holds). The auditor + dashboard
    # both benefit from the complete history.
    should_persist = True

    decision_id: int | None = None
    if should_persist:
        contribs_json = (
            json.dumps(decision.contributions, default=str)
            if decision.contributions else None
        )
        with get_session() as s:
            row = Decision(
                source=source,
                symbol=symbol,
                timeframe=tf,
                snapshot_json=json.dumps(snap_dict, default=str),
                action=decision.action,
                size_pct=decision.size_pct,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                confidence=decision.confidence,
                reasoning=f"[{decision.backend}] {decision.reasoning}",
                claude_usd_cost=decision.usd_cost,
                indicator_contributions_json=contribs_json,
                regime=decision.regime,
                dominant_indicator=decision.dominant_indicator,
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            decision_id = row.id

    # When we skipped persistence (hold + non-sampling tick), the executor
    # can short-circuit too — there's nothing for it to do on a hold anyway.
    if decision_id is None:
        return {
            "symbol": symbol,
            "action": "hold",
            "status": "hold",
            "message": "hold (not persisted)",
        }

    result = await asyncio.to_thread(
        executor.execute,
        decision_id,
        symbol,
        decision.action,
        decision.size_pct,
        snap.last_close,
        decision.confidence,
        snap.atr_14,
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

    # Bounded parallelism. Starter has 0.5 vCPU + 512 MB. Each in-flight
    # symbol holds a pandas DataFrame for OHLCV + HTF + 9 indicator columns —
    # ~30 MB resident at peak. 3 concurrent + 5-min interval is the stable
    # combo on Starter: ticks finish in ~25 s for a 40-symbol universe, well
    # inside the 5 s /healthz window between batches. Higher parallelism
    # (4-6) caused /healthz timeouts and OOM kills.
    sem = asyncio.Semaphore(3)

    async def _bounded(sym, alert):
        async with sem:
            return await _tick_symbol(sym, tf, alert, source)

    coros = []
    for sym in symbols_to_run:
        alert_for_sym = tv_alert if tv_alert and tv_alert.get("symbol") == sym else None
        coros.append(_bounded(sym, alert_for_sym))
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


async def _regime_job() -> None:
    """Refresh BTC 4h regime classification (bull/bear/chop)."""
    await asyncio.to_thread(regime.refresh_regime)


async def _news_job() -> None:
    """Pull latest CryptoPanic posts and store NewsEvent rows."""
    await asyncio.to_thread(news.refresh_news)


async def _digest_job() -> None:
    """Send daily P&L summary email at DIGEST_HOUR_UTC:DIGEST_MINUTE_UTC."""
    await asyncio.to_thread(email_digest.send_digest)


async def _keep_warm_job() -> None:
    """Self-ping the public /healthz every 10 min so Render's free-tier idle
    eviction doesn't sleep the container between user visits. External traffic
    is what resets Render's 15-min idle timer — internal CPU activity doesn't
    count. Disabled when KEEP_WARM_URL is empty (e.g. local dev).
    """
    import os, urllib.request
    url = os.getenv("KEEP_WARM_URL", "https://terminal-btc.onrender.com/healthz")
    if not url:
        return
    try:
        await asyncio.to_thread(urllib.request.urlopen, url, None, 10)
    except Exception as exc:
        log.warning("keep_warm_failed", error=str(exc))


async def _backtest_job() -> None:
    """Replay the rules engine against historical candles for every TRADE_SYMBOL."""
    await asyncio.to_thread(backtest.run_all)


async def _auditor_job() -> None:
    """Self-improving loop: review recent trades + backtests, write overrides."""
    await asyncio.to_thread(auditor.run_audit)


async def _optimizer_job() -> None:
    """Weekly walk-forward parameter optimizer — finds the best
    confidence_adj / size_multiplier per symbol by replaying the rules engine
    on different parameter grids and picking the winner by OOS PF×sqrt(trades).
    """
    settings = get_settings()
    await asyncio.to_thread(
        optimizer.run_optimizer,
        settings.optimizer_max_symbols,
        settings.optimizer_override_hours,
    )


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
    # BTC 4h regime — same cadence as macro since it depends on F&G + EMAs.
    sched.add_job(
        _regime_job, "interval", minutes=settings.macro_poll_min, id="regime", max_instances=1,
    )
    # CryptoPanic news — every NEWS_POLL_MIN minutes.
    sched.add_job(
        _news_job, "interval", minutes=settings.news_poll_min, id="news", max_instances=1,
    )
    # Nightly backtest at BACKTEST_HOUR_UTC.
    sched.add_job(
        _backtest_job, "cron", hour=settings.backtest_hour_utc, minute=0,
        id="backtest", max_instances=1,
    )
    # Nightly auditor — runs after the backtest so it has fresh numbers.
    sched.add_job(
        _auditor_job, "cron", hour=settings.auditor_hour_utc, minute=0,
        id="auditor", max_instances=1,
    )
    # Weekly walk-forward parameter optimizer. Runs Sundays after the
    # auditor (so the auditor's regime/indicator overrides are already in
    # place when the optimizer chooses its own confidence_adj/size_multiplier).
    sched.add_job(
        _optimizer_job, "cron",
        day_of_week=settings.optimizer_day_of_week,
        hour=settings.optimizer_hour_utc, minute=15,
        id="optimizer", max_instances=1,
    )
    # Daily P&L digest email at DIGEST_HOUR_UTC:DIGEST_MINUTE_UTC
    # (no-op if SMTP env vars unset).
    sched.add_job(
        _digest_job, "cron",
        hour=settings.digest_hour_utc, minute=settings.digest_minute_utc,
        id="digest", max_instances=1,
    )
    # Self-ping every 10 minutes so Render's idle eviction doesn't sleep the
    # container — fixes the 503 on /control/audit-now and friends after long
    # quiet periods.
    sched.add_job(
        _keep_warm_job, "interval", minutes=10, id="keep_warm", max_instances=1,
    )
    sched.start()
    # Warm caches on startup so the first ticks see real values.
    loop = asyncio.get_event_loop()
    loop.create_task(_macro_job())
    loop.create_task(_regime_job())
    loop.create_task(_news_job())
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
