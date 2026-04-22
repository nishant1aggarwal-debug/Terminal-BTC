from __future__ import annotations

from app.services import rules_signal


def _snap(**overrides):
    base = {
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "last_close": 60_000.0,
        "rsi_14": 55.0,
        "macd": 10.0,
        "macd_signal": 5.0,
        "macd_hist": 300.0,  # == ATR so MACD score contributes full +0.20
        "ema_20": 60_100.0,
        "ema_50": 59_800.0,
        "ema_200": 59_000.0,
        "atr_14": 300.0,
        # Defaults compatible with the multi-indicator engine (ADX>20, neutral BB/StochRSI).
        "bb_upper": 60_500.0,
        "bb_middle": 60_000.0,
        "bb_lower": 59_500.0,
        "bb_pct": 0.6,
        "stoch_rsi_k": 60.0,
        "stoch_rsi_d": 55.0,
        "adx_14": 28.0,
        "bid": 59_999.0,
        "ask": 60_001.0,
        "spread_bps": 1.5,
        "recent_candles": [],
    }
    base.update(overrides)
    return base


def test_long_on_trend_up():
    d = rules_signal.generate_decision(_snap())
    assert d.action == "buy"
    assert 0 < d.size_pct <= 0.25
    assert d.stop_loss < _snap()["last_close"] < d.take_profit


def test_short_on_trend_down():
    d = rules_signal.generate_decision(
        _snap(
            rsi_14=40.0,
            macd_hist=-300.0,
            ema_20=59_000.0,
            ema_50=59_500.0,
            ema_200=60_000.0,
            bb_pct=0.2,
            stoch_rsi_k=40.0,
            stoch_rsi_d=45.0,
        )
    )
    assert d.action == "sell"
    assert d.take_profit < _snap()["last_close"] < d.stop_loss


def test_hold_on_wide_spread():
    d = rules_signal.generate_decision(_snap(spread_bps=20.0))
    assert d.action == "hold"
    assert "spread" in d.reasoning


def test_hold_on_no_trend():
    d = rules_signal.generate_decision(
        _snap(
            rsi_14=50.0,
            macd_hist=0.1,
            ema_20=60_000.0,
            ema_50=60_000.0,
            ema_200=60_000.0,
        )
    )
    assert d.action == "hold"


def test_exit_long_when_trend_flips_down():
    d = rules_signal.generate_decision(
        _snap(
            rsi_14=40.0,
            macd_hist=-300.0,
            ema_20=59_000.0,
            ema_50=59_500.0,
            ema_200=60_000.0,
            bb_pct=0.2,
            stoch_rsi_k=40.0,
            stoch_rsi_d=45.0,
        ),
        position={"qty": 0.001, "avg_entry": 60_000.0},
    )
    assert d.action == "sell"
