from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

import asyncio

from app.services import auditor, backtest, email_digest, macro, news, notifications, optimizer, push, regime, risk
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
    result = await asyncio.to_thread(macro.refresh_fear_greed)
    if result is None:
        return {"ok": False, "error": "fetch_failed"}
    return {"ok": True, **result}


@router.post("/regime-refresh")
async def regime_refresh() -> dict[str, Any]:
    result = await asyncio.to_thread(regime.refresh_regime)
    return {"ok": True, **(result or {})}


@router.post("/news-refresh")
async def news_refresh() -> dict[str, Any]:
    new_count = await asyncio.to_thread(news.refresh_news)
    return {"ok": True, "new_rows": new_count}


@router.post("/digest-now")
async def digest_now() -> dict[str, Any]:
    """Send the daily digest email immediately (for testing)."""
    return await asyncio.to_thread(email_digest.send_digest)


@router.post("/test-push")
async def test_push() -> dict[str, Any]:
    """Send a test notification to your phone via ntfy.sh — use this after
    setting NTFY_TOPIC to confirm the push channel is wired correctly.
    """
    return await asyncio.to_thread(push.test_push)


@router.post("/notifications/mark-read")
async def mark_read() -> dict[str, Any]:
    count = notifications.mark_all_read()
    return {"ok": True, "marked_read": count}


@router.post("/audit-now")
async def audit_now() -> dict[str, Any]:
    """Run the strategy auditor in the background. Returns immediately so the
    dashboard isn't blocked on Claude round-trip time.
    """
    async def _runner():
        try:
            await asyncio.to_thread(auditor.run_audit)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("auditor_background_failed: %s", exc)
    asyncio.create_task(_runner())
    return {"ok": True, "message": "audit started in background"}


@router.post("/optimize-now")
async def optimize_now() -> dict[str, Any]:
    """Run the walk-forward parameter optimizer in the background.

    Replays the rules engine across a small grid of (confidence_adj,
    size_multiplier) values for the top-N most-recently-backtested symbols
    and writes the winners as week-long StrategyOverrides.
    """
    from app.config import get_settings
    settings = get_settings()

    async def _runner():
        try:
            await asyncio.to_thread(
                optimizer.run_optimizer,
                settings.optimizer_max_symbols,
                settings.optimizer_override_hours,
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception(
                "optimizer_background_failed: %s", exc,
            )
    asyncio.create_task(_runner())
    return {"ok": True, "message": "optimizer started in background"}


@router.post("/backtest-now")
async def backtest_now() -> dict[str, Any]:
    """Kick off a full backtest in the background and return immediately.

    Running the 12-symbol × 500-candle replay synchronously blocked
    Render's single worker for ~90s — long enough to 502 out and to
    starve every other API call. Fire-and-forget lets the dashboard
    keep refreshing while the work runs in a thread pool.
    """
    async def _runner():
        try:
            await asyncio.to_thread(backtest.run_all)
        except Exception as exc:
            # Swallow so a single symbol failure doesn't take down the task.
            import logging
            logging.getLogger(__name__).exception("backtest_background_failed: %s", exc)
    asyncio.create_task(_runner())
    return {"ok": True, "message": "backtest started in background; poll /api/backtest"}
