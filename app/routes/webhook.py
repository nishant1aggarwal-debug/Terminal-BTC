from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import get_settings
from app.logging_setup import get_logger
from app.services.scheduler import tv_queue

log = get_logger(__name__)
router = APIRouter(prefix="/tv", tags=["tradingview"])


class TVAlert(BaseModel):
    secret: str = Field(..., description="Shared secret configured in TradingView alert body")
    symbol: str
    price: float | None = None
    alert: str | None = None
    tf: str | None = None
    ts: str | None = None
    extra: dict[str, Any] | None = None


@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def tv_webhook(payload: TVAlert, request: Request) -> dict[str, Any]:
    settings = get_settings()

    if not hmac.compare_digest(payload.secret, settings.tradingview_webhook_secret):
        log.warning("tv_webhook_bad_secret", client=request.client.host if request.client else None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad_secret")

    alert_body = payload.model_dump(exclude={"secret"})
    queue = tv_queue()
    try:
        queue.put_nowait(alert_body)
    except Exception:
        log.warning("tv_webhook_queue_full")
        raise HTTPException(status_code=503, detail="queue_full")

    log.info("tv_webhook_accepted", symbol=payload.symbol, alert=payload.alert)
    return {"ok": True, "queued": True}
