from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

from app.exchange import data_source
from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Snapshot:
    symbol: str
    timeframe: str
    last_close: float
    rsi_14: float
    macd: float
    macd_signal: float
    macd_hist: float
    ema_20: float
    ema_50: float
    ema_200: float
    atr_14: float
    bid: float
    ask: float
    spread_bps: float
    recent_candles: list[dict[str, float]]  # last 10 OHLCV rows

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ohlcv_to_df(ohlcv: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def get_snapshot(symbol: str, timeframe: str = "15m", limit: int = 200) -> Snapshot:
    ohlcv = data_source.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = _ohlcv_to_df(ohlcv)

    close = df["close"]
    high = df["high"]
    low = df["low"]

    rsi = RSIIndicator(close=close, window=14).rsi()
    macd = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
    ema20 = EMAIndicator(close=close, window=20).ema_indicator()
    ema50 = EMAIndicator(close=close, window=50).ema_indicator()
    ema200 = EMAIndicator(close=close, window=200).ema_indicator()
    atr = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()

    book = data_source.fetch_order_book(symbol, limit=5)
    bid = float(book["bids"][0][0]) if book.get("bids") else float("nan")
    ask = float(book["asks"][0][0]) if book.get("asks") else float("nan")
    mid = (bid + ask) / 2 if bid and ask else float(close.iloc[-1])
    spread_bps = ((ask - bid) / mid) * 10_000 if mid else 0.0

    recent = (
        df.tail(10)[["ts", "open", "high", "low", "close", "volume"]]
        .assign(ts=lambda x: x["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        .to_dict(orient="records")
    )

    snap = Snapshot(
        symbol=symbol,
        timeframe=timeframe,
        last_close=float(close.iloc[-1]),
        rsi_14=float(rsi.iloc[-1]),
        macd=float(macd.macd().iloc[-1]),
        macd_signal=float(macd.macd_signal().iloc[-1]),
        macd_hist=float(macd.macd_diff().iloc[-1]),
        ema_20=float(ema20.iloc[-1]),
        ema_50=float(ema50.iloc[-1]),
        ema_200=float(ema200.iloc[-1]),
        atr_14=float(atr.iloc[-1]),
        bid=bid,
        ask=ask,
        spread_bps=float(spread_bps),
        recent_candles=recent,
    )
    log.info("market_snapshot", symbol=symbol, tf=timeframe, last_close=snap.last_close, rsi=snap.rsi_14)
    return snap
