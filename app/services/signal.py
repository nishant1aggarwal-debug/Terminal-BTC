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
from app.services import rules_signal

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
    return Decision(
        action=rd.action,
        size_pct=rd.size_pct,
        stop_loss=rd.stop_loss,
        take_profit=rd.take_profit,
        confidence=rd.confidence,
        reasoning=rd.reasoning,
        backend="rules",
    )
