from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import init_db
from app.logging_setup import configure_logging, get_logger
from app.routes import control, health, webhook
from app.services import scheduler

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    scheduler.start()
    log.info(
        "app_started",
        testnet=settings.binance_testnet,
        live_trading=settings.live_trading,
        symbol=settings.trade_symbol,
    )
    try:
        yield
    finally:
        scheduler.stop()
        log.info("app_stopped")


app = FastAPI(title="Terminal-BTC", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(control.router)
app.include_router(webhook.router)
