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

import time
from functools import lru_cache, wraps
from typing import Any, Callable, TypeVar

import ccxt

from app.config import get_settings
from app.logging_setup import get_logger

T = TypeVar("T")

_RETRY_ATTEMPTS = 3
_RETRY_BASE_MS = 500
_RETRYABLE = (ccxt.RateLimitExceeded, ccxt.DDoSProtection, ccxt.NetworkError)


def _with_retry(fn: Callable[..., T]) -> Callable[..., T]:
    """Exponential-backoff retry wrapper for public ccxt calls.

    Retries up to ``_RETRY_ATTEMPTS`` times on rate-limit/DDoS/network errors.
    Backoff schedule: 500ms, 1000ms, 2000ms (cumulative ~3.5s worst case).
    Any other exception propagates immediately — we only smooth over transient
    issues, not mask bugs.
    """
    @wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> T:
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                return fn(*args, **kwargs)
            except _RETRYABLE as exc:
                last_exc = exc
                wait_ms = _RETRY_BASE_MS * (2 ** attempt)
                log.warning(
                    "exchange_retry",
                    fn=fn.__name__,
                    attempt=attempt + 1,
                    error_type=type(exc).__name__,
                    wait_ms=wait_ms,
                )
                time.sleep(wait_ms / 1000.0)
        # Exhausted retries — surface the last exception.
        raise last_exc  # type: ignore[misc]
    return inner

log = get_logger(__name__)

_SUPPORTED = {
    # Datacenter-friendly exchanges (work from Render free tier)
    "kraken",      # ~14 USDT pairs — limited but rock-solid
    "mexc",        # ~600 USDT futures — biggest alt universe, permissive geo
    "bitget",      # ~500 USDT futures — wide alt coverage
    "gateio",      # ~400 USDT futures — wide alt coverage
    "okx",         # ~200 USDT futures — sometimes US-blocked
    "htx",         # Huobi/HTX — wide universe
    # Geo-blocked from US datacenters (Render free tier blocks them)
    "bybit",       # ~400 pairs but BLOCKED from AWS US
    "binance",     # ~400 pairs but BLOCKED from AWS US
}

# Pairs that Kraken doesn't list in USDT quote (they have USD versions instead).
# Skipping these on Kraken avoids symbol fetches returning empty data. If the
# user has DATA_SOURCE=bybit/binance, these are fine and get included.
#
# Verified by smoke-testing fetch_ticker against Kraken's public API. Kraken
# heavily prefers USD quote for older pairs; newer listings + meme tokens
# often skip USDT entirely. The tick-level filter still catches anything we
# missed — this just trims the obvious cases at scan time.
_KRAKEN_MISSING_USDT = {
    # Long-established pairs where Kraken only offers USD quote
    "TRX/USDT", "MATIC/USDT", "NEAR/USDT", "APT/USDT",
    "TON/USDT", "HBAR/USDT", "ETC/USDT", "BNB/USDT",
    # DeFi / L1s / L2s that returned empty live (Kraken only has USD quote)
    "FIL/USDT", "ARB/USDT", "OP/USDT", "SUI/USDT", "INJ/USDT",
    "UNI/USDT", "AAVE/USDT", "MKR/USDT", "CRV/USDT", "LDO/USDT",
    "XLM/USDT", "SAND/USDT", "AXS/USDT",
    # Newer / retail / meme pairs Kraken hasn't USDT-quoted (at time of writing)
    "SEI/USDT", "TIA/USDT", "JUP/USDT", "PYTH/USDT", "JTO/USDT",
    "PEPE/USDT", "WIF/USDT", "BONK/USDT", "FLOKI/USDT", "ORDI/USDT",
    "IMX/USDT", "RNDR/USDT", "FET/USDT", "THETA/USDT", "COMP/USDT",
    "SNX/USDT", "ICP/USDT", "KSM/USDT", "EGLD/USDT", "APE/USDT",
    "CHZ/USDT", "GALA/USDT", "RUNE/USDT", "GRT/USDT",
}

# MEXC lists these in load_markets() but actual fetch_ticker / fetch_ohlcv calls
# fail with "does not have market symbol". Caught from production logs — exclude
# them at discovery time so they don't waste HTTP retries on every tick + saturate
# the event loop while /healthz tries to respond.
_MEXC_PHANTOM_SYMBOLS = {
    "XAUT/USDT", "SILVER/USDT", "USOIL/USDT", "UKOIL/USDT", "TONCOIN/USDT",
}


def filter_supported(symbols: list[str]) -> list[str]:
    """Drop symbols the current data source doesn't list.

    Called once from scheduler/backtest so we never waste requests on pairs
    that will always 404.
    """
    if get_settings().data_source == "kraken":
        return [s for s in symbols if s not in _KRAKEN_MISSING_USDT]
    return list(symbols)


def _build(source: str) -> ccxt.Exchange:
    if source not in _SUPPORTED:
        raise ValueError(f"unsupported DATA_SOURCE={source!r}; supported: {sorted(_SUPPORTED)}")
    klass = getattr(ccxt, source)
    # For exchanges with futures/swap markets, prefer the swap (perp) market type
    # so fetch_markets returns USDT perps, not just spot. Most users want
    # leveraged trading, not spot.
    options: dict[str, Any] = {"enableRateLimit": True}
    if get_settings().trade_market == "futures" and source in {"mexc", "bitget", "gateio", "okx", "bybit", "binance"}:
        options["options"] = {"defaultType": "swap"}
    elif source == "htx" and get_settings().trade_market == "futures":
        options["options"] = {"defaultType": "future"}
    client = klass(options)
    log.info("data_source_init", source=source, default_type=options.get("options", {}).get("defaultType"))
    return client


def discover_universe(
    top_n: int = 50, min_quote_volume_usdt: float = 0.0,
) -> list[str]:
    """Auto-discover the top-N most-traded USDT pairs on the active exchange.

    Use this to escape the hardcoded TRADE_SYMBOLS list — when DATA_SOURCE points
    at a wide-universe exchange (mexc, bitget, gateio), this returns 50-200+
    actively-trading pairs instead of the 14 we hardcode for Kraken.

    Filters:
      * Quote currency = USDT
      * For futures: only swap/perp contracts (not dated futures)
      * Active markets only (no delisted)
      * 24h quote volume >= ``min_quote_volume_usdt`` (drops illiquid junk)
      * Sorted by 24h quote volume desc, top_n returned
    """
    client = get_client()
    settings = get_settings()
    try:
        markets = client.load_markets()
    except Exception as exc:
        log.warning("discover_universe_load_markets_failed", error=str(exc))
        return []

    # Source-specific phantom-symbol filter — listed by the exchange but with
    # no actual fetch_ticker / fetch_ohlcv backing. Excluding them at discovery
    # time prevents wasted HTTP retries on every subsequent tick.
    phantom: set[str] = set()
    if settings.data_source == "mexc":
        phantom = _MEXC_PHANTOM_SYMBOLS

    candidates: list[str] = []
    for sym, m in markets.items():
        if not m.get("active", True):
            continue
        if (m.get("quote") or "").upper() != "USDT":
            continue
        if settings.trade_market == "futures":
            # Want perpetual swaps, not dated futures or spot
            if not m.get("swap", False):
                continue
            if m.get("expiry") is not None:
                continue
        else:
            # Spot only
            if not m.get("spot", False):
                continue
        # Normalize to "BASE/USDT" form (drop any contract suffix like ":USDT")
        base = (m.get("base") or "").upper()
        if not base:
            continue
        normalized = f"{base}/USDT"
        if normalized in phantom:
            continue
        candidates.append(f"{base}/USDT")

    # De-dup while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        deduped.append(c)

    if not deduped:
        return []

    # Rank by 24h quote volume — one batched fetch_tickers call.
    try:
        tickers = client.fetch_tickers(deduped)
    except Exception as exc:
        log.warning("discover_universe_fetch_tickers_failed", error=str(exc))
        # Fall back to alphabetical top-N if ranking fails — still better than 14 pairs.
        return deduped[:top_n]

    def _vol(sym: str) -> float:
        t = tickers.get(sym) or {}
        return float(t.get("quoteVolume") or t.get("baseVolume") or 0)

    ranked = sorted(deduped, key=_vol, reverse=True)
    # Minimum-volume floor — drops symbols whose 24h quote volume can't
    # support our typical position size without prohibitive slippage. Even
    # if top_n could fit more pairs, we'd rather trade fewer good ones than
    # 200 zombies. ranked is volume-sorted desc, so as soon as one falls
    # below the floor every subsequent one will too — we can break.
    if min_quote_volume_usdt > 0:
        kept: list[str] = []
        for sym in ranked:
            if _vol(sym) < min_quote_volume_usdt:
                break
            kept.append(sym)
        ranked = kept
    out = ranked[:top_n]
    log.info("discover_universe_done", source=settings.data_source, total=len(deduped),
             eligible=len(ranked), returned=len(out), top=out[:5])
    return out


@lru_cache(maxsize=1)
def get_client() -> ccxt.Exchange:
    return _build(get_settings().data_source)


def source_name() -> str:
    return get_settings().data_source


@_with_retry
def fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 200) -> list[list[float]]:
    return get_client().fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)


@_with_retry
def fetch_order_book(symbol: str, limit: int = 5) -> dict[str, Any]:
    return get_client().fetch_order_book(symbol, limit=limit)


# Symbols that have been observed to fail fetch_ticker — we skip them for
# the rest of this process lifetime so the dashboard's /api/markets poll
# doesn't spend ~3 s per dead symbol retrying. Re-populated from scratch
# every process boot.
_BAD_TICKER_SYMBOLS: set[str] = set()


@_with_retry
def fetch_tickers(symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Batch-fetch last price / 24h change / volume for many symbols in one call.

    Exchanges that don't support batched tickers (Kraken sometimes) fall
    back to per-symbol fetch_ticker(). Returns {symbol: ticker_dict}.

    Known-bad symbols (those that already failed fetch_ticker once in this
    process) are skipped silently. Without this, /api/markets ate ~3 s per
    phantom symbol × ~3 phantoms = ~10 s of event-loop block per poll,
    timing out Render's /healthz.
    """
    client = get_client()
    requested = list(symbols or [])
    eligible = [s for s in requested if s not in _BAD_TICKER_SYMBOLS]
    try:
        return client.fetch_tickers(eligible) if eligible else {}
    except Exception as exc:
        log.warning("fetch_tickers_batch_failed_falling_back", error=str(exc))
        out: dict[str, dict[str, Any]] = {}
        for sym in eligible:
            try:
                out[sym] = client.fetch_ticker(sym)
            except Exception as sub_exc:
                log.warning("fetch_ticker_failed", symbol=sym, error=str(sub_exc))
                _BAD_TICKER_SYMBOLS.add(sym)  # never retry this one again
        return out
