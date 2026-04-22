"""Multi-indicator rules engine — Bybit-futures flavor.

Instead of a single gate, every standard TradingView indicator we compute in
``market_data.get_snapshot`` contributes a signed score toward a composite
confidence value. The action fires when |score| crosses
``MIN_SIGNAL_CONFIDENCE`` (default 0.55).

Indicators and their contribution (all in [-1, +1], summed then clamped):
  * EMA regime  — +/-0.30 when EMA20>EMA50>EMA200 (bull) or inverted (bear);
                   0 otherwise.
  * MACD histogram — sign + magnitude vs ATR, up to +/-0.20.
  * RSI zone    — +0.15 if 45<RSI<70 (bullish pullback), -0.15 if 30<RSI<55
                   (bearish rally). Outside those zones: 0.
  * StochRSI    — +0.15 on bullish %K cross from oversold (<30), -0.15 on
                   bearish cross from overbought (>70).
  * Bollinger % — +0.10 when price rides the upper band in an uptrend (bb_pct
                   > 0.7), -0.10 when bb_pct < 0.3 in a downtrend.
  * ADX filter  — hard requirement: ADX >= 20 for any entry to fire. Below
                   that, chop — we return hold.
  * Fear & Greed — applied in signal.py after the fact (0.75..1.15 multiplier).

SL = 1.5 * ATR, TP = 2.5 * ATR (same as before). Size is 5% of equity, which
risk.py clamps to MAX_POSITION_USDT downstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class RulesDecision:
    action: str  # "buy" | "sell" | "hold"
    size_pct: float
    stop_loss: float
    take_profit: float
    confidence: float
    reasoning: str
    usd_cost: float = 0.0
    raw_usage: dict[str, int] | None = None


def _score_ema(s: dict[str, Any]) -> float:
    if s["ema_20"] > s["ema_50"] > s["ema_200"]:
        return 0.30
    if s["ema_20"] < s["ema_50"] < s["ema_200"]:
        return -0.30
    return 0.0


def _score_macd(s: dict[str, Any]) -> float:
    atr = s.get("atr_14") or 0.0
    if atr <= 0:
        return 0.0
    strength = min(abs(s["macd_hist"]) / atr, 1.0)
    signed = 0.20 * strength
    return signed if s["macd_hist"] > 0 else -signed


def _score_rsi(s: dict[str, Any]) -> float:
    r = s["rsi_14"]
    if 45 < r < 70:
        return 0.15
    if 30 < r < 55:
        return -0.15
    return 0.0


def _score_stoch_rsi(s: dict[str, Any]) -> float:
    k = s.get("stoch_rsi_k", 50.0)
    d = s.get("stoch_rsi_d", 50.0)
    # Bullish cross from oversold: K crosses above D while both < 30.
    if k > d and k < 30:
        return 0.15
    # Bearish cross from overbought: K crosses below D while both > 70.
    if k < d and k > 70:
        return -0.15
    # Weaker bias: just directional momentum.
    if k > d:
        return 0.05
    if k < d:
        return -0.05
    return 0.0


def _score_bb(s: dict[str, Any]) -> float:
    pct = s.get("bb_pct", 0.5)
    ema_up = s["ema_20"] > s["ema_50"]
    if pct > 0.70 and ema_up:
        return 0.10
    if pct < 0.30 and not ema_up:
        return -0.10
    return 0.0


def generate_decision(
    snapshot: dict[str, Any],
    tv_alert: dict[str, Any] | None = None,
    position: dict[str, Any] | None = None,
) -> RulesDecision:
    settings = get_settings()
    price = float(snapshot["last_close"])
    atr = float(snapshot["atr_14"])
    spread = float(snapshot.get("spread_bps", 0.0))
    adx = float(snapshot.get("adx_14", 0.0))

    pos_qty = float((position or {}).get("qty", 0.0))

    # Hard filters.
    if spread > 5.0:
        return RulesDecision(
            action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
            confidence=0.20,
            reasoning=f"hold: wide spread {spread:.1f}bps",
        )
    if atr <= 0:
        return RulesDecision(
            action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
            confidence=0.0, reasoning="hold: no ATR",
        )
    # ADX trend-strength gate — block new entries in choppy markets.
    # Exits still fire (see below), to get us out of a trade that's losing momentum.
    if adx < 20 and abs(pos_qty) < 1e-9:
        return RulesDecision(
            action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
            confidence=0.30,
            reasoning=f"hold: ADX {adx:.1f} < 20 (choppy)",
        )

    # Composite score in [-1, +1]. Positive = bullish, negative = bearish.
    score = (
        _score_ema(snapshot)
        + _score_macd(snapshot)
        + _score_rsi(snapshot)
        + _score_stoch_rsi(snapshot)
        + _score_bb(snapshot)
    )
    score = max(-1.0, min(1.0, score))
    conf = round(min(abs(score) + 0.05, 1.0), 3)  # small floor so it's never 0

    # Build a compact reasoning string listing the active contributors.
    contribs = []
    ema_s = _score_ema(snapshot)
    if ema_s: contribs.append(f"EMA {ema_s:+.2f}")
    macd_s = _score_macd(snapshot)
    if abs(macd_s) > 0.01: contribs.append(f"MACD {macd_s:+.2f}")
    rsi_s = _score_rsi(snapshot)
    if rsi_s: contribs.append(f"RSI {rsi_s:+.2f}")
    stoch_s = _score_stoch_rsi(snapshot)
    if abs(stoch_s) > 0.01: contribs.append(f"StochRSI {stoch_s:+.2f}")
    bb_s = _score_bb(snapshot)
    if bb_s: contribs.append(f"BB {bb_s:+.2f}")
    reason_tail = f"score={score:+.2f} [{' · '.join(contribs) or 'flat'}] ADX={adx:.0f}"

    # Exit on flipped regime while holding a position.
    if pos_qty > 0 and score < -0.20:
        return RulesDecision(
            action="sell", size_pct=0.05,
            stop_loss=price + 1.5 * atr, take_profit=price - 2.5 * atr,
            confidence=conf, reasoning=f"exit long: {reason_tail}",
        )
    if pos_qty < 0 and score > 0.20:
        return RulesDecision(
            action="buy", size_pct=0.05,
            stop_loss=price - 1.5 * atr, take_profit=price + 2.5 * atr,
            confidence=conf, reasoning=f"exit short: {reason_tail}",
        )

    # Fresh entries only from flat, and only when score crosses the bar.
    if abs(pos_qty) < 1e-9:
        threshold = settings.min_signal_confidence
        if score >= threshold:
            return RulesDecision(
                action="buy", size_pct=0.05,
                stop_loss=price - 1.5 * atr, take_profit=price + 2.5 * atr,
                confidence=conf, reasoning=f"long: {reason_tail}",
            )
        if score <= -threshold:
            return RulesDecision(
                action="sell", size_pct=0.05,
                stop_loss=price + 1.5 * atr, take_profit=price - 2.5 * atr,
                confidence=conf, reasoning=f"short: {reason_tail}",
            )

    return RulesDecision(
        action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
        confidence=conf, reasoning=f"hold: {reason_tail}",
    )
