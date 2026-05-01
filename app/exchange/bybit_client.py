"""Bybit USDⓈ-M futures execution client.

Mirrors ``binance_client.py`` so ``executor.execute()`` can route real orders
through whichever venue the user picks via ``TRADE_EXCHANGE``. Paper mode
ignores this entirely — only the ``real_orders_enabled`` path comes here.

Bybit specifics handled:
  * One-way mode (``positionIdx=0``) — ccxt's default for Bybit perps.
  * Per-pair leverage set on first order via ``set_leverage`` (idempotent).
  * Testnet vs mainnet via ``BYBIT_TESTNET`` flag (mirrors Binance pattern).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import ccxt

from app.config import Settings, get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)

_LEVERAGE_SET: set[str] = set()  # pairs we've already called set_leverage on


def _build_authed(settings: Settings) -> ccxt.Exchange:
    klass = ccxt.bybit
    client = klass(
        {
            "apiKey": settings.bybit_api_key,
            "secret": settings.bybit_api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap" if settings.trade_market == "futures" else "spot",
                "defaultSubType": "linear",  # USDⓈ-M perps
            },
        }
    )
    if settings.bybit_testnet:
        client.set_sandbox_mode(True)
    log.info(
        "bybit_authed_client_init",
        testnet=settings.bybit_testnet,
        market=settings.trade_market,
    )
    return client


@lru_cache(maxsize=1)
def get_authed_client() -> ccxt.Exchange:
    return _build_authed(get_settings())


def _ensure_leverage(symbol: str) -> None:
    """First time we touch a symbol, set its leverage to the configured value.

    Bybit needs ``set_leverage`` per-pair before the first order. We cache the
    set so we don't call it on every order (rate-limit-friendly).
    """
    if symbol in _LEVERAGE_SET:
        return
    settings = get_settings()
    if settings.trade_market != "futures":
        _LEVERAGE_SET.add(symbol)
        return
    try:
        get_authed_client().set_leverage(int(settings.leverage), symbol)
        log.info("bybit_leverage_set", symbol=symbol, leverage=settings.leverage)
    except Exception as exc:
        # Some pairs may not allow the requested leverage; log and continue.
        log.warning("bybit_set_leverage_failed", symbol=symbol, error=str(exc))
    _LEVERAGE_SET.add(symbol)


def fetch_balance() -> dict[str, Any]:
    return get_authed_client().fetch_balance()


def fetch_positions(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    client = get_authed_client()
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
    _ensure_leverage(symbol)
    params: dict[str, Any] = {"positionIdx": 0}  # one-way mode
    if client_order_id:
        # Bybit uses a different param name than Binance.
        params["clientOrderId"] = client_order_id
    return get_authed_client().create_order(symbol, order_type, side, amount, price, params)


def quote_equity_usdt() -> float:
    """Live USDT balance (futures wallet when TRADE_MARKET=futures)."""
    try:
        bal = fetch_balance()
    except Exception as exc:
        log.warning("bybit_fetch_balance_failed", error=str(exc))
        return 0.0
    free = (bal.get("free") or {}).get("USDT")
    if free is not None:
        return float(free)
    total = (bal.get("total") or {}).get("USDT", 0.0)
    return float(total)
