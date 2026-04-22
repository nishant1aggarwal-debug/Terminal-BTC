"""Historical backtester for the rules engine.

Replays ``rules_signal`` against the last ``BACKTEST_CANDLES`` bars per symbol
and writes a ``BacktestReport`` row so the dashboard can show win-rate /
profit-factor / drawdown for each pair — all from out-of-sample data, before
you commit real money.

Simple event loop:
  * For each bar i, build a snapshot from bars[0..i] and feed it to
    rules_signal.generate_decision.
  * Maintain a virtual position; on entries, record entry. On exits
    (opposite-side signal, SL hit, or TP hit on the next bar's high/low),
    realize PnL net of ``FEE_*_BPS`` commissions.
  * Aggregate wins/losses/PnL/drawdown.

Not a tick-by-tick simulator — bar-close approximations. Good enough to compare
strategies and decide "is this worth risking money on?" but not a substitute for
forward paper trading.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from ta.momentum import RSIIndicator, StochRSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands

from app.config import get_settings
from app.db import get_session
from app.exchange import data_source
from app.logging_setup import get_logger
from app.models import BacktestReport
from app.services import rules_signal

log = get_logger(__name__)


@dataclass
class _VirtualTrade:
    side: str       # "long" | "short"
    entry_price: float
    entry_ts: datetime
    stop_loss: float
    take_profit: float


def _snapshot_at(df: pd.DataFrame, i: int, symbol: str, timeframe: str) -> dict[str, Any]:
    """Build a Snapshot-shaped dict from the slice bars[0..i] (inclusive)."""
    window = df.iloc[: i + 1]
    close = window["close"]
    high = window["high"]
    low = window["low"]
    rsi = RSIIndicator(close=close, window=14).rsi()
    macd = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
    ema20 = EMAIndicator(close=close, window=20).ema_indicator()
    ema50 = EMAIndicator(close=close, window=50).ema_indicator()
    ema200 = EMAIndicator(close=close, window=200).ema_indicator()
    atr = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
    bb = BollingerBands(close=close, window=20, window_dev=2)
    stoch_rsi = StochRSIIndicator(close=close, window=14, smooth1=3, smooth2=3)
    adx = ADXIndicator(high=high, low=low, close=close, window=14).adx()
    lc = float(close.iloc[-1])
    bb_up = float(bb.bollinger_hband().iloc[-1])
    bb_lo = float(bb.bollinger_lband().iloc[-1])
    bb_pct = (lc - bb_lo) / (bb_up - bb_lo) if bb_up > bb_lo else 0.5
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "last_close": lc,
        "rsi_14": float(rsi.iloc[-1]),
        "macd": float(macd.macd().iloc[-1]),
        "macd_signal": float(macd.macd_signal().iloc[-1]),
        "macd_hist": float(macd.macd_diff().iloc[-1]),
        "ema_20": float(ema20.iloc[-1]),
        "ema_50": float(ema50.iloc[-1]),
        "ema_200": float(ema200.iloc[-1]),
        "atr_14": float(atr.iloc[-1]),
        "bb_upper": bb_up,
        "bb_middle": float(bb.bollinger_mavg().iloc[-1]),
        "bb_lower": bb_lo,
        "bb_pct": float(bb_pct),
        "stoch_rsi_k": float(stoch_rsi.stochrsi_k().iloc[-1] * 100.0),
        "stoch_rsi_d": float(stoch_rsi.stochrsi_d().iloc[-1] * 100.0),
        "adx_14": float(adx.iloc[-1]),
        "bid": lc, "ask": lc,  # assume tight book in historical data
        "spread_bps": 1.0,
        "recent_candles": [],
    }


def _timeframe_minutes(tf: str) -> int:
    unit = tf[-1].lower()
    try:
        n = int(tf[:-1])
    except ValueError:
        return 15
    return n * {"m": 1, "h": 60, "d": 1440}.get(unit, 1)


def backtest_symbol(symbol: str, timeframe: str, candles: int) -> BacktestReport:
    """Replay the rules engine over `candles` bars of `symbol`."""
    ohlcv = data_source.fetch_ohlcv(symbol, timeframe=timeframe, limit=candles)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)

    settings = get_settings()
    fee_bps = settings.fee_futures_bps if settings.trade_market == "futures" else settings.fee_spot_bps
    fee_rate = fee_bps / 10_000.0

    open_trade: _VirtualTrade | None = None
    wins = 0
    losses = 0
    net_pnl = 0.0
    gross_wins = 0.0
    gross_losses = 0.0
    hold_minutes: list[float] = []
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    tf_min = _timeframe_minutes(timeframe)

    # 200 bars needed for EMA200 warm-up.
    for i in range(200, len(df) - 1):
        bar = df.iloc[i]
        next_bar = df.iloc[i + 1]
        close = float(bar["close"])
        # Intra-bar exit checks on NEXT bar (we'd have known SL/TP from entry).
        if open_trade:
            nh, nl = float(next_bar["high"]), float(next_bar["low"])
            hit_sl = (open_trade.side == "long" and nl <= open_trade.stop_loss) or \
                     (open_trade.side == "short" and nh >= open_trade.stop_loss)
            hit_tp = (open_trade.side == "long" and nh >= open_trade.take_profit) or \
                     (open_trade.side == "short" and nl <= open_trade.take_profit)
            if hit_sl or hit_tp:
                exit_price = open_trade.stop_loss if hit_sl else open_trade.take_profit
                if open_trade.side == "long":
                    gross = (exit_price - open_trade.entry_price) / open_trade.entry_price
                else:
                    gross = (open_trade.entry_price - exit_price) / open_trade.entry_price
                net = gross - 2 * fee_rate  # two taker fills per round-trip
                net_pnl += net
                cum += net
                peak = max(peak, cum)
                max_dd = max(max_dd, peak - cum)
                if net > 0:
                    wins += 1
                    gross_wins += net
                else:
                    losses += 1
                    gross_losses += -net
                hold_minutes.append(
                    (next_bar["ts"].to_pydatetime() - open_trade.entry_ts).total_seconds() / 60.0
                )
                open_trade = None
                continue

        snap = _snapshot_at(df, i, symbol, timeframe)
        pos = {"qty": 1.0 if open_trade and open_trade.side == "long" else
                       -1.0 if open_trade and open_trade.side == "short" else 0.0,
               "avg_entry": open_trade.entry_price if open_trade else 0.0}
        dec = rules_signal.generate_decision(snap, position=pos)

        if dec.action == "hold":
            continue

        # Opposite-signal exit.
        if open_trade:
            flipping = (open_trade.side == "long" and dec.action == "sell") or \
                       (open_trade.side == "short" and dec.action == "buy")
            if flipping:
                exit_price = close
                if open_trade.side == "long":
                    gross = (exit_price - open_trade.entry_price) / open_trade.entry_price
                else:
                    gross = (open_trade.entry_price - exit_price) / open_trade.entry_price
                net = gross - 2 * fee_rate
                net_pnl += net
                cum += net
                peak = max(peak, cum)
                max_dd = max(max_dd, peak - cum)
                if net > 0:
                    wins += 1
                    gross_wins += net
                else:
                    losses += 1
                    gross_losses += -net
                hold_minutes.append(
                    (bar["ts"].to_pydatetime() - open_trade.entry_ts).total_seconds() / 60.0
                )
                open_trade = None

        # Fresh entry (only from flat).
        if open_trade is None and dec.action in {"buy", "sell"}:
            open_trade = _VirtualTrade(
                side="long" if dec.action == "buy" else "short",
                entry_price=close,
                entry_ts=bar["ts"].to_pydatetime(),
                stop_loss=dec.stop_loss,
                take_profit=dec.take_profit,
            )

    trades = wins + losses
    win_rate = (wins / trades * 100.0) if trades else 0.0
    pf = (gross_wins / gross_losses) if gross_losses > 0 else None
    report = BacktestReport(
        symbol=symbol,
        timeframe=timeframe,
        generated_at=datetime.now(timezone.utc),
        candles=len(df),
        trades=trades,
        wins=wins,
        losses=losses,
        win_rate_pct=win_rate,
        profit_factor=pf,
        net_pnl_pct=net_pnl * 100.0,
        max_drawdown_pct=max_dd * 100.0,
        avg_hold_minutes=(sum(hold_minutes) / len(hold_minutes)) if hold_minutes else 0.0,
        params_json=json.dumps({"fee_bps": fee_bps, "ema": [20, 50, 200], "rsi": 14}),
        period_start=df["ts"].iloc[200].to_pydatetime() if len(df) > 200 else None,
        period_end=df["ts"].iloc[-1].to_pydatetime(),
    )
    with get_session() as s:
        s.add(report)
        s.commit()
    log.info(
        "backtest_done",
        symbol=symbol, trades=trades, win_rate=win_rate, pf=pf, net_pct=net_pnl * 100.0,
    )
    return report


def run_all() -> list[BacktestReport]:
    """Backtest every TRADE_SYMBOL; returns the fresh reports."""
    settings = get_settings()
    tf = settings.backtest_timeframe or settings.trade_timeframe
    candles = settings.backtest_candles
    reports: list[BacktestReport] = []
    for sym in data_source.filter_supported(settings.symbols):
        try:
            reports.append(backtest_symbol(sym, tf, candles))
        except Exception as exc:
            log.error("backtest_symbol_failed", symbol=sym, error=str(exc))
    return reports
