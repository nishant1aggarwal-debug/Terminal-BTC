from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db
from app.exchange import data_source
from app.logging_setup import configure_logging, get_logger
from app.routes import control, dashboard, health, webhook
from app.security import require_basic_auth
from app.services import scheduler

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    # Auto-discover the trade universe from the data source's market list when
    # AUTO_DISCOVER_SYMBOLS=true. Mutates settings.trade_symbols + symbol_allowlist
    # in-place so every downstream module (scheduler, risk, dashboard) sees the
    # widened universe.
    if settings.auto_discover_symbols:
        try:
            discovered = data_source.discover_universe(top_n=settings.auto_discover_top_n)
        except Exception as exc:
            log.warning("auto_discover_failed_keeping_hardcoded", error=str(exc))
            discovered = []
        if discovered:
            joined = ",".join(discovered)
            settings.trade_symbols = joined
            # Allowlist gets the union so TradingView webhooks for any
            # discovered symbol are accepted.
            existing = set(settings.allowed_symbols)
            existing.update(discovered)
            settings.symbol_allowlist = ",".join(sorted(existing))
            log.info("auto_discover_universe_loaded", count=len(discovered), top=discovered[:5])
    scheduler.start()
    log.info(
        "app_started",
        paper_mode=settings.paper_mode,
        live_trading=settings.live_trading,
        data_source=settings.data_source,
        symbol_count=len(settings.symbols),
        signal_mode=settings.signal_mode,
        auto_discover=settings.auto_discover_symbols,
    )
    try:
        yield
    finally:
        scheduler.stop()
        log.info("app_stopped")


app = FastAPI(title="Terminal-BTC", version="0.1.0", lifespan=lifespan)
# Health stays public so Render / Kubernetes probes work without credentials.
app.include_router(health.router)
# Every other surface is protected by basic auth when env is configured.
# Open mode (both env vars empty) = no-op dependency, zero overhead.
_auth = [Depends(require_basic_auth)]
app.include_router(control.router, dependencies=_auth)
app.include_router(webhook.router)  # webhook has its own shared-secret; don't double-gate
app.include_router(dashboard.router, dependencies=_auth)

_static_dir = Path(__file__).parent / "static"
app.mount("/ui", StaticFiles(directory=_static_dir, html=True), name="ui")


@app.middleware("http")
async def _ui_basic_auth(request, call_next):
    """StaticFiles mount bypasses router dependencies, so protect it here.

    Only the /ui path needs manual gating — everything else runs through the
    router-level dependency. Health and the TV webhook stay open.
    """
    if request.url.path.startswith("/ui"):
        try:
            await require_basic_auth(request)
        except Exception as exc:
            from fastapi.responses import Response
            return Response(
                content=str(exc.detail if hasattr(exc, "detail") else "unauthorized"),
                status_code=exc.status_code if hasattr(exc, "status_code") else 401,
                headers=exc.headers if hasattr(exc, "headers") else {
                    "WWW-Authenticate": 'Basic realm="terminal-btc"',
                },
            )
    return await call_next(request)


@app.get("/", include_in_schema=False, dependencies=_auth)
async def _root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")
