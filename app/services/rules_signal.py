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

Self-learning: each indicator's raw contribution is multiplied by a
per-indicator weight read from the override map (``weight_ema``,
``weight_macd``, etc.). Default weight is 1.0; auditor lowers the weight
on indicators that misfire in the recent window.

SL = 1.5 * ATR, TP = 2.5 * ATR (same as before). Size is 5% of equity, which
risk.py clamps to MAX_POSITION_USDT downstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


_INDICATOR_NAMES = ("ema", "macd", "rsi", "stoch_rsi", "bb")


def _apply_overrides(decision: "RulesDecision", symbol: str, threshold: float) -> "RulesDecision":
    """Consult the strategy auditor's active overrides for this symbol and adjust.

    Honoured override types:
      * disable_long / disable_short — flip the action to hold.
      * size_multiplier — scale the size_pct (clamped 0.25..1.75).
      * confidence_adj — shift the firing threshold up/down for this symbol.
        A positive value means "tighter" — if the resulting score no longer
        clears the adjusted threshold, we demote to hold.

    (weight_* overrides are applied BEFORE scoring, inside generate_decision,
    so this function doesn't see them.)
    """
    # Lazy import avoids auditor→rules_signal circular.
    from app.services import auditor, regime as _regime

    try:
        overrides = auditor.override_map(symbol, regime=_regime.current_regime())
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
    # Self-learning telemetry — auditor reads these off the persisted Decision
    # row to compute per-indicator + per-regime win rates and write adaptive
    # overrides.
    contributions: dict[str, float] = field(default_factory=dict)
    dominant_indicator: str | None = None
    regime: str | None = None


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


def _fetch_weights(symbol: str) -> dict[str, float]:
    """Pull per-indicator weight overrides for this symbol (regime-filtered).

    Defaults to 1.0 for every indicator. Auditor writes weight_<name>=0.5 to
    halve an indicator's vote when it's been misfiring lately.
    """
    from app.services import auditor, regime as _regime
    try:
        overrides = auditor.override_map(symbol, regime=_regime.current_regime())
    except Exception:
        return {name: 1.0 for name in _INDICATOR_NAMES}
    weights: dict[str, float] = {}
    for name in _INDICATOR_NAMES:
        raw = overrides.get(f"weight_{name}")
        if raw is None:
            weights[name] = 1.0
            continue
        try:
            weights[name] = max(0.0, min(2.0, float(raw)))
        except ValueError:
            weights[name] = 1.0
    return weights


def _compute_contributions(
    snapshot: dict[str, Any], weights: dict[str, float]
) -> dict[str, float]:
    """Per-indicator signed contribution AFTER weight multiplier."""
    return {
        "ema": _score_ema(snapshot) * weights.get("ema", 1.0),
        "macd": _score_macd(snapshot) * weights.get("macd", 1.0),
        "rsi": _score_rsi(snapshot) * weights.get("rsi", 1.0),
        "stoch_rsi": _score_stoch_rsi(snapshot) * weights.get("stoch_rsi", 1.0),
        "bb": _score_bb(snapshot) * weights.get("bb", 1.0),
    }


def _dominant_indicator(contribs: dict[str, float]) -> str | None:
    """Largest absolute contributor — the one the auditor will blame/credit."""
    if not contribs:
        return None
    name, val = max(contribs.items(), key=lambda kv: abs(kv[1]))
    return name if abs(val) > 1e-9 else None


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
    symbol = str(snapshot.get("symbol", ""))

    pos_qty = float((position or {}).get("qty", 0.0))

    # Resolve current market regime now so it lands on every Decision row
    # (including holds, when sampled).
    from app.services import regime as _regime
    current_regime = _regime.current_regime()

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
            regime=current_regime,
        )
    if atr <= 0:
        return RulesDecision(
            action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
            confidence=0.0, reasoning="hold: no ATR",
            regime=current_regime,
        )
    # ADX trend-strength gate — block new entries in choppy markets.
    # Exits still fire (see below), to get us out of a trade that's losing momentum.
    if adx < 20 and abs(pos_qty) < 1e-9:
        return RulesDecision(
            action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
            confidence=0.30,
            reasoning=f"hold: ADX {adx:.1f} < 20 (choppy)",
            regime=current_regime,
        )

    # Per-indicator weights (regime-filtered overrides).
    weights = _fetch_weights(symbol)
    contribs = _compute_contributions(snapshot, weights)
    dominant = _dominant_indicator(contribs)

    # Composite score in [-1, +1]. Positive = bullish, negative = bearish.
    score = sum(contribs.values())
    score = max(-1.0, min(1.0, score))
    conf = round(min(abs(score) + 0.05, 1.0), 3)  # small floor so it's never 0

    # Build a compact reasoning string listing the active contributors.
    # Also surface non-unit weights so the user sees the auditor's tweaks.
    contrib_strs: list[str] = []
    for name, val in contribs.items():
        if abs(val) < 0.01:
            continue
        w = weights.get(name, 1.0)
        tag = f"{name.upper()} {val:+.2f}"
        if abs(w - 1.0) > 1e-6:
            tag += f"×{w:.2f}"
        contrib_strs.append(tag)
    reason_tail = f"score={score:+.2f} [{' · '.join(contrib_strs) or 'flat'}] ADX={adx:.0f}"

    threshold = settings.min_signal_confidence
    # ADD signals fire at the SAME threshold as fresh entries. If the same
    # composite score that opens a fresh position is still firing on the next
    # bar, the user wants that as an ADD #N (averaging in) rather than a
    # silent hold. The executor's MAX_PYRAMID_ADDS=2 cap is the runaway guard.
    add_threshold = threshold

    def _make(action: str, sl: float, tp: float, why: str) -> RulesDecision:
        return RulesDecision(
            action=action, size_pct=0.05,
            stop_loss=sl, take_profit=tp,
            confidence=conf, reasoning=f"{why}: {reason_tail}",
            contributions=contribs, dominant_indicator=dominant,
            regime=current_regime,
        )

    # Exit on flipped regime while holding a position.
    if pos_qty > 0 and score < -0.20:
        d = _make("sell", price + 1.5 * atr, price - 2.5 * atr, "exit long")
        return _apply_overrides(d, symbol, threshold)
    if pos_qty < 0 and score > 0.20:
        d = _make("buy", price - 1.5 * atr, price + 2.5 * atr, "exit short")
        return _apply_overrides(d, symbol, threshold)

    # Reinforcement ADD: when an existing position is open in the same direction
    # and the composite score is even stronger than the original entry bar, fire
    # a same-direction signal so the executor pyramids in (capped at 2 adds in
    # executor.MAX_PYRAMID_ADDS). The notification gets an "ADD #N to LONG …"
    # title via the scheduler so the user knows to average into the existing
    # position rather than open a new one.
    if pos_qty > 0 and score >= add_threshold:
        d = _make("buy", price - 1.5 * atr, price + 2.5 * atr, "add long")
        return _apply_overrides(d, symbol, threshold)
    if pos_qty < 0 and score <= -add_threshold:
        d = _make("sell", price + 1.5 * atr, price - 2.5 * atr, "add short")
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
        # Regime gate: refuse NEW entries when BTC is in a chop regime.
        # Exits/adds on existing positions are handled above (pos_qty != 0),
        # so this only blocks fresh exposure — trading alts in a sideways
        # tape is a coin-flip minus fees, and that fee-bleed was a large
        # chunk of the realized losses. Trend regimes (bull/bear) still trade.
        if settings.block_entries_in_chop and current_regime == "chop":
            return RulesDecision(
                action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
                confidence=conf,
                reasoning=f"hold: chop regime — no fresh entries ({reason_tail})",
                contributions=contribs, dominant_indicator=dominant,
                regime=current_regime,
            )
        if score >= threshold:
            if htf_down is True:
                return RulesDecision(
                    action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
                    confidence=conf,
                    reasoning=f"hold: 15m long but 1h trend down — no HTF confirmation ({reason_tail})",
                    contributions=contribs, dominant_indicator=dominant,
                    regime=current_regime,
                )
            d = _make("buy", price - 1.5 * atr, price + 2.5 * atr, "long")
            return _apply_overrides(d, symbol, threshold)
        if score <= -threshold:
            if htf_up is True:
                return RulesDecision(
                    action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
                    confidence=conf,
                    reasoning=f"hold: 15m short but 1h trend up — no HTF confirmation ({reason_tail})",
                    contributions=contribs, dominant_indicator=dominant,
                    regime=current_regime,
                )
            d = _make("sell", price + 1.5 * atr, price - 2.5 * atr, "short")
            return _apply_overrides(d, symbol, threshold)

    return RulesDecision(
        action="hold", size_pct=0.0, stop_loss=0.0, take_profit=0.0,
        confidence=conf, reasoning=f"hold: {reason_tail}",
        contributions=contribs, dominant_indicator=dominant,
        regime=current_regime,
    )
