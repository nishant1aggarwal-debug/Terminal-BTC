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


def _apply_overrides(decision: "RulesDecision", symbol: str, threshold: float) -> "RulesDecision":
    """Consult the strategy auditor's active overrides for this symbol and adjust.

    Four override types we honour:
      * disable_long / disable_short — flip the action to hold.
      * size_multiplier — scale the size_pct (clamped 0.25..1.75).
      * confidence_adj — shift the firing threshold up/down for this symbol.
        A positive value means "tighter" — if the resulting score no longer
        clears the adjusted threshold, we demote to hold.
    """
    # Lazy import avoids auditor→rules_signal circular.
    from app.services import auditor

    try:
        overrides = auditor.override_map(symbol)
    except Exception as exc:
        log.warning("override_lookup_failed", symbol=symbol, error=str(exc))
        return decision

    if not overrides:
        return decision

    notes: list[str] = []

    # Directional disables — flip to hold.
    if decision.action == "buy" and overrides.get("disable_long", "").lower() == "true":
        notes.append("override:disable_long")
        decision.action = "hold"
        decision.size_pct = 0.0
    elif decision.action == "sell" and overrides.get("disable_short", "").lower() == "true":
        notes.append("override:disable_short")
        decision.action = "hold"
        decision.size_pct = 0.0

    # Confidence adjustment — tighten or loosen the threshold on this symbol.
    conf_adj_raw = overrides.get("confidence_adj")
    if conf_adj_raw:
        try:
            conf_adj = max(-0.20, min(0.20, float(conf_adj_raw)))
        except ValueError:
            conf_adj = 0.0
        if decision.action in {"buy", "sell"}:
            adjusted_threshold = threshold + conf_adj
            if decision.confidence < adjusted_threshold:
                notes.append(f"override:conf_adj+{conf_adj:+.2f} demoted (conf {decision.confidence:.2f} < {adjusted_threshold:.2f})")
                decision.action = "hold"
                decision.size_pct = 0.0
            else:
                notes.append(f"override:conf_adj{conf_adj:+.2f}")

    # Size multiplier — scale the bet.
    if decision.action in {"buy", "sell"}:
        mult_raw = overrides.get("size_multiplier")
        if mult_raw:
            try:
                mult = max(0.25, min(1.75, float(mult_raw)))
            except ValueError:
                mult = 1.0
            if mult != 1.0:
                decision.size_pct = round(decision.size_pct * mult, 4)
                notes.append(f"override:size×{mult}")

    if notes:
        decision.reasoning = f"{decision.reasoning} | {' · '.join(notes)}"
    return decision


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

    # Spread filter scales with the profit target. With min_tp2_pct = 5%, a
    # round-trip spread cost up to ~4% of the target is acceptable — that lets
    # us trade MEXC mid-cap alts which often run 10-30 bps spreads. Tighter
    # targets (lower min_tp2_pct) auto-tighten the filter. Cap at 40 bps so we
    # never trade a truly illiquid pair.
    max_spread_bps = min(40.0, settings.min_tp2_pct * 100 * 8.0)  # 5% TP2 → 40 bps cap
    if spread > max_spread_bps:
        return RulesDecision(
            action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
            confidence=0.20,
            reasoning=f"hold: wide spread {spread:.1f}bps > {max_spread_bps:.0f}bps cap",
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

    threshold = settings.min_signal_confidence
    symbol = str(snapshot.get("symbol", ""))
    # ADD signals fire at the SAME threshold as fresh entries. If the same
    # composite score that opens a fresh position is still firing on the next
    # bar, the user wants that as an ADD #N (averaging in) rather than a
    # silent hold. The executor's MAX_PYRAMID_ADDS=2 cap is the runaway guard.
    add_threshold = threshold

    # Exit on flipped regime while holding a position.
    if pos_qty > 0 and score < -0.20:
        d = RulesDecision(
            action="sell", size_pct=0.05,
            stop_loss=price + 1.5 * atr, take_profit=price - 2.5 * atr,
            confidence=conf, reasoning=f"exit long: {reason_tail}",
        )
        return _apply_overrides(d, symbol, threshold)
    if pos_qty < 0 and score > 0.20:
        d = RulesDecision(
            action="buy", size_pct=0.05,
            stop_loss=price - 1.5 * atr, take_profit=price + 2.5 * atr,
            confidence=conf, reasoning=f"exit short: {reason_tail}",
        )
        return _apply_overrides(d, symbol, threshold)

    # Reinforcement ADD: when an existing position is open in the same direction
    # and the composite score is even stronger than the original entry bar, fire
    # a same-direction signal so the executor pyramids in (capped at 2 adds in
    # executor.MAX_PYRAMID_ADDS). The notification gets an "ADD #N to LONG …"
    # title via the scheduler so the user knows to average into the existing
    # position rather than open a new one.
    if pos_qty > 0 and score >= add_threshold:
        d = RulesDecision(
            action="buy", size_pct=0.05,
            stop_loss=price - 1.5 * atr, take_profit=price + 2.5 * atr,
            confidence=conf, reasoning=f"add long: {reason_tail}",
        )
        return _apply_overrides(d, symbol, threshold)
    if pos_qty < 0 and score <= -add_threshold:
        d = RulesDecision(
            action="sell", size_pct=0.05,
            stop_loss=price + 1.5 * atr, take_profit=price - 2.5 * atr,
            confidence=conf, reasoning=f"add short: {reason_tail}",
        )
        return _apply_overrides(d, symbol, threshold)

    # Higher-timeframe confirmation. If the scheduler fetched the 1h snapshot
    # and it disagrees with the 15m direction, we refuse the entry. This is
    # the single biggest filter against whipsaws — only trade WITH the higher
    # trend. (If HTF fields are absent — e.g. fetch failed — we fall back to
    # 15m-only, not block everything.)
    htf_up = snapshot.get("htf_trend_up")
    htf_down = snapshot.get("htf_trend_down")

    # Fresh entries only from flat, and only when score crosses the bar.
    if abs(pos_qty) < 1e-9:
        if score >= threshold:
            if htf_down is True:
                return RulesDecision(
                    action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
                    confidence=conf,
                    reasoning=f"hold: 15m long but 1h trend down — no HTF confirmation ({reason_tail})",
                )
            d = RulesDecision(
                action="buy", size_pct=0.05,
                stop_loss=price - 1.5 * atr, take_profit=price + 2.5 * atr,
                confidence=conf, reasoning=f"long: {reason_tail}",
            )
            return _apply_overrides(d, symbol, threshold)
        if score <= -threshold:
            if htf_up is True:
                return RulesDecision(
                    action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
                    confidence=conf,
                    reasoning=f"hold: 15m short but 1h trend up — no HTF confirmation ({reason_tail})",
                )
            d = RulesDecision(
                action="sell", size_pct=0.05,
                stop_loss=price + 1.5 * atr, take_profit=price - 2.5 * atr,
                confidence=conf, reasoning=f"short: {reason_tail}",
            )
            return _apply_overrides(d, symbol, threshold)

    return RulesDecision(
        action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
        confidence=conf, reasoning=f"hold: {reason_tail}",
    )
