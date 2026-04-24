"""Signal dispatcher. Picks the right generator based on SIGNAL_MODE.

Kept thin on purpose: routes to either the local rules engine (default, no
external API) or Claude (requires ANTHROPIC_API_KEY). Callers get a uniform
`Decision`-shaped result regardless of backend.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger
from app.services import macro, rules_signal

log = get_logger(__name__)


@dataclass
class Decision:
    action: str
    size_pct: float
    stop_loss: float
    take_profit: float
    confidence: float
    reasoning: str
    usd_cost: float = 0.0
    raw_usage: dict[str, int] | None = None
    backend: str = "rules"


def generate(
    snapshot: dict[str, Any],
    tv_alert: dict[str, Any] | None = None,
    position: dict[str, Any] | None = None,
) -> Decision:
    settings = get_settings()
    mode = settings.signal_mode

    if mode == "hold":
        return Decision(
            action="hold",
            size_pct=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            confidence=0.0,
            reasoning="signal_mode=hold",
            backend="hold",
        )

    if mode == "claude":
        if not settings.anthropic_api_key:
            log.warning("signal_mode_claude_missing_key_falling_back_to_rules")
        else:
            from app.services import claude_signal  # lazy: avoid importing anthropic when unused

            cd = claude_signal.generate_decision(snapshot, tv_alert, position)
            return Decision(
                action=cd.action,
                size_pct=cd.size_pct,
                stop_loss=cd.stop_loss,
                take_profit=cd.take_profit,
                confidence=cd.confidence,
                reasoning=cd.reasoning,
                usd_cost=cd.usd_cost,
                raw_usage=cd.raw_usage,
                backend="claude",
            )

    rd = rules_signal.generate_decision(snapshot, tv_alert, position)
    # Nudge confidence based on Fear & Greed extremes (no-op if F&G missing).
    fg = macro.get_latest("fear_greed")
    if fg is not None and rd.action != "hold":
        mult = macro.confidence_adjustment(rd.action, fg.value)
        if mult != 1.0:
            rd.confidence = round(max(0.0, min(1.0, rd.confidence * mult)), 3)
            tag = "dampened" if mult < 1.0 else "boosted"
            rd.reasoning = (
                f"{rd.reasoning} | F&G={fg.value:.0f} ({fg.classification}) — conf {tag} x{mult:.2f}"
            )

    # Smart position sizing: base 3% of equity, scaled UP by confidence above
    # threshold and by ADX strength. High-conviction + strong-trend setups bet
    # bigger; weak setups that barely clear the threshold bet tiny. Cap at 6%.
    # Drawdown circuit breaker: if paper equity is too far below peak,
    # multiply the sized fraction by dd_size_mult (default 0.5) until recovered.
    if rd.action in {"buy", "sell"}:
        settings = get_settings()
        conf_boost = 1.0 + max(0.0, rd.confidence - settings.min_signal_confidence) * 2.0
        adx = float(snapshot.get("adx_14", 20.0))
        adx_boost = min(1.5, 1.0 + max(0.0, adx - 20.0) * 0.02)
        sized = 0.03 * conf_boost * adx_boost

        from app.services import risk
        dd = risk.drawdown_state()
        if dd["multiplier_active"]:
            sized *= dd["multiplier"]
            rd.reasoning = (
                f"{rd.reasoning} | DD {dd['dd_pct'] * 100:.1f}% · size ×{dd['multiplier']}"
            )

        rd.size_pct = min(0.06, round(sized, 4))
        rd.reasoning = f"{rd.reasoning} | size {rd.size_pct * 100:.2f}%"

    return Decision(
        action=rd.action,
        size_pct=rd.size_pct,
        stop_loss=rd.stop_loss,
        take_profit=rd.take_profit,
        confidence=rd.confidence,
        reasoning=rd.reasoning,
        backend="rules",
    )
