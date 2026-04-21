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
        paper_mode=settings.paper_mode,
        live_trading=settings.live_trading,
        data_source=settings.data_source,
        symbols=settings.symbols,
        signal_mode=settings.signal_mode,
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
