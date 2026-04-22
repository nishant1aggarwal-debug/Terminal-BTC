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


def fetch_tickers(symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Batch-fetch last price / 24h change / volume for many symbols in one call.

    Exchanges that don't support batched tickers (Kraken sometimes) fall
    back to per-symbol fetch_ticker(). Returns {symbol: ticker_dict}.
    """
    client = get_client()
    try:
        return client.fetch_tickers(symbols)
    except Exception as exc:
        log.warning("fetch_tickers_batch_failed_falling_back", error=str(exc))
        out: dict[str, dict[str, Any]] = {}
        for sym in symbols or []:
            try:
                out[sym] = client.fetch_ticker(sym)
            except Exception as sub_exc:
                log.warning("fetch_ticker_failed", symbol=sym, error=str(sub_exc))
        return out
