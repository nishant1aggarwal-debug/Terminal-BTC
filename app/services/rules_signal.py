from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


def _trend_up(s: dict[str, Any]) -> bool:
    return s["ema_20"] > s["ema_50"] > s["ema_200"] and s["macd_hist"] > 0


def _trend_down(s: dict[str, Any]) -> bool:
    return s["ema_20"] < s["ema_50"] < s["ema_200"] and s["macd_hist"] < 0


def _confidence_from_macd(macd_hist: float, atr: float) -> float:
    if atr <= 0:
        return 0.5
    strength = min(abs(macd_hist) / atr, 1.0)
    return round(0.5 + 0.4 * strength, 3)


def generate_decision(
    snapshot: dict[str, Any],
    tv_alert: dict[str, Any] | None = None,
    position: dict[str, Any] | None = None,
) -> RulesDecision:
    """Deterministic EMA/RSI/MACD strategy. No external API, no cost.

    Rules:
      * Long  : trend up AND 50 < RSI < 70 AND spread_bps <= 5
      * Short : trend down AND 30 < RSI < 50 AND spread_bps <= 5
      * Exit  : opposite regime while a position is open
      * SL    : 1.5 * ATR   TP : 2.5 * ATR
      * Size  : 5% of quote equity, capped by risk.py downstream
    """
    price = float(snapshot["last_close"])
    rsi = float(snapshot["rsi_14"])
    atr = float(snapshot["atr_14"])
    spread = float(snapshot.get("spread_bps", 0.0))
    macd_hist = float(snapshot["macd_hist"])

    pos_qty = float((position or {}).get("qty", 0.0))

    # Wide spreads: refuse to trade.
    if spread > 5.0:
        return RulesDecision(
            action="hold",
            size_pct=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            confidence=0.2,
            reasoning=f"hold: spread {spread:.1f} bps > 5",
        )

    up = _trend_up(snapshot)
    down = _trend_down(snapshot)

    # If we're already long and trend flipped down, flatten (sell).
    if pos_qty > 0 and down:
        return RulesDecision(
            action="sell",
            size_pct=0.05,
            stop_loss=price + 1.5 * atr,
            take_profit=price - 2.5 * atr,
            confidence=_confidence_from_macd(macd_hist, atr),
            reasoning="exit long: trend flipped down",
        )

    # If we're already short and trend flipped up, flatten (buy to close).
    if pos_qty < 0 and up:
        return RulesDecision(
            action="buy",
            size_pct=0.05,
            stop_loss=price - 1.5 * atr,
            take_profit=price + 2.5 * atr,
            confidence=_confidence_from_macd(macd_hist, atr),
            reasoning="exit short: trend flipped up",
        )

    # Fresh entries only from flat.
    if abs(pos_qty) < 1e-9:
        if up and 50 < rsi < 70:
            return RulesDecision(
                action="buy",
                size_pct=0.05,
                stop_loss=price - 1.5 * atr,
                take_profit=price + 2.5 * atr,
                confidence=_confidence_from_macd(macd_hist, atr),
                reasoning=f"long: trend up, rsi {rsi:.1f}, macd_hist {macd_hist:.2f}",
            )
        if down and 30 < rsi < 50:
            return RulesDecision(
                action="sell",
                size_pct=0.05,
                stop_loss=price + 1.5 * atr,
                take_profit=price - 2.5 * atr,
                confidence=_confidence_from_macd(macd_hist, atr),
                reasoning=f"short: trend down, rsi {rsi:.1f}, macd_hist {macd_hist:.2f}",
            )

    return RulesDecision(
        action="hold",
        size_pct=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        confidence=0.4,
        reasoning="hold: no clean setup",
    )
