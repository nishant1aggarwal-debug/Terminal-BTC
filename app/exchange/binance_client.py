from __future__ import annotations

from functools import lru_cache
from typing import Any

import ccxt

from app.config import Settings, get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


def _build_client(settings: Settings) -> ccxt.Exchange:
    exchange_id = "binanceusdm" if settings.trade_market == "futures" else "binance"
    klass = getattr(ccxt, exchange_id)
    client = klass(
        {
            "apiKey": settings.binance_api_key,
            "secret": settings.binance_api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future" if settings.trade_market == "futures" else "spot"},
        }
    )
    if settings.binance_testnet:
        client.set_sandbox_mode(True)
    log.info(
        "binance_client_init",
        exchange=exchange_id,
        testnet=settings.binance_testnet,
        market=settings.trade_market,
    )
    return client


@lru_cache(maxsize=1)
def get_client() -> ccxt.Exchange:
    return _build_client(get_settings())


def fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 100) -> list[list[float]]:
    return get_client().fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)


def fetch_order_book(symbol: str, limit: int = 10) -> dict[str, Any]:
    return get_client().fetch_order_book(symbol, limit=limit)


def fetch_balance() -> dict[str, Any]:
    return get_client().fetch_balance()


def fetch_positions(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    client = get_client()
    if not hasattr(client, "fetch_positions"):
        return []
    try:
        return client.fetch_positions(symbols)
    except ccxt.NotSupported:
        return []


def create_order(
    symbol: str,
    side: str,
    amount: float,
    order_type: str = "market",
    price: float | None = None,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if client_order_id:
        # Binance accepts both keys depending on product; ccxt normalizes.
        params["clientOrderId"] = client_order_id
        params["newClientOrderId"] = client_order_id
    return get_client().create_order(symbol, order_type, side, amount, price, params)


def quote_equity_usdt() -> float:
    """Available quote-currency balance (USDT)."""
    try:
        bal = fetch_balance()
    except Exception as exc:  # network or auth
        log.warning("fetch_balance_failed", error=str(exc))
        return 0.0
    free = (bal.get("free") or {}).get("USDT")
    if free is not None:
        return float(free)
    total = (bal.get("total") or {}).get("USDT", 0.0)
    return float(total)
