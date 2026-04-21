"""Pluggable public market-data source.

All exchanges are accessed via ccxt's unified API, so callers always pass
normalized symbols like ``"BTC/USDT"`` and get unified ccxt responses back.

Switch between backends with DATA_SOURCE in .env:
  * ``bybit``   - default; globally accessible public endpoints
  * ``binance`` - largest liquidity (geo-blocked in some regions)
  * ``kraken``  - good fallback in restricted regions

Nothing here needs API keys — market data is public.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import ccxt

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)

_SUPPORTED = {"bybit", "binance", "kraken"}


def _build(source: str) -> ccxt.Exchange:
    if source not in _SUPPORTED:
        raise ValueError(f"unsupported DATA_SOURCE={source!r}; supported: {sorted(_SUPPORTED)}")
    klass = getattr(ccxt, source)
    client = klass({"enableRateLimit": True})
    log.info("data_source_init", source=source)
    return client


@lru_cache(maxsize=1)
def get_client() -> ccxt.Exchange:
    return _build(get_settings().data_source)


def source_name() -> str:
    return get_settings().data_source


def fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 200) -> list[list[float]]:
    return get_client().fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)


def fetch_order_book(symbol: str, limit: int = 5) -> dict[str, Any]:
    return get_client().fetch_order_book(symbol, limit=limit)
