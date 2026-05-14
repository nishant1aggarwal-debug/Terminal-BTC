from __future__ import annotations

import asyncio
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
    # FORCE wide-universe defaults regardless of what Render env vars say.
    # Reason: Render's blueprint sync stamped DATA_SOURCE=kraken on the service
    # and won't auto-update it from render.yaml on subsequent syncs (Render's
    # behaviour, not ours). Overriding in-process means the user gets the wide
    # universe just by us pushing a commit — no manual Render env edits.
    # To opt out: set DATA_SOURCE_LOCK=true in Render (rare).
    import os
    if os.getenv("DATA_SOURCE_LOCK", "").lower() != "true":
        if settings.data_source == "kraken":
            log.info("forcing_wide_universe_data_source", was="kraken", now="mexc")
            settings.data_source = "mexc"
        if not settings.auto_discover_symbols:
            log.info("forcing_auto_discover_on")
            settings.auto_discover_symbols = True
        try:
            from app.exchange.data_source import get_client as _get_client
            _get_client.cache_clear()
        except Exception:
            pass
    # Bump the open-position cap to 15. Render's env var is stuck at 8 from the
    # earlier blueprint sync; this override is the no-touch way to raise it.
    if settings.max_open_positions < 15:
        log.info("forcing_max_open_positions", was=settings.max_open_positions, now=15)
        settings.max_open_positions = 15

    # Auto-discover the trade universe asynchronously. We DON'T do this inline
    # because it loads markets from MEXC (~600 pairs) and ranks by volume —
    # easily 5-15s, which exceeds Render's 5s healthcheck timeout and kills
    # the container before it can boot. Schedule it as a background task that
    # mutates settings.trade_symbols once it finishes; in the meantime the
    # scheduler ticks the hardcoded TRADE_SYMBOLS list.
    if settings.auto_discover_symbols:
        async def _discover_async():
            try:
                discovered = await asyncio.to_thread(
                    data_source.discover_universe,
                    settings.auto_discover_top_n,
                    settings.auto_discover_min_quote_volume_usdt,
                )
            except Exception as exc:
                log.warning("auto_discover_failed_keeping_hardcoded", error=str(exc))
                return
            if not discovered:
                log.warning("auto_discover_returned_empty")
                return
            settings.trade_symbols = ",".join(discovered)
            existing = set(settings.allowed_symbols)
            existing.update(discovered)
            settings.symbol_allowlist = ",".join(sorted(existing))
            log.info("auto_discover_universe_loaded", count=len(discovered),
                     top=discovered[:5])
        asyncio.create_task(_discover_async())

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
