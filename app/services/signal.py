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
from app.services import macro, news, regime, rules_signal

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

    # Regime weighting: real trading systems take BOTH sides, just sized
    # smaller when they're against the dominant trend. We never block — the
    # per-symbol composite score has already proved the setup is high
    # conviction. The 4h regime just dials position size up or down.
    #   BULL  — longs ×1.0, shorts ×0.6 (counter-trend allowed but smaller)
    #   BEAR  — shorts ×1.0, longs ×0.6
    #   CHOP  — both ×0.5 (regime uncertainty → smaller bets either side)
    current = regime.current_regime()
    if rd.action == "buy":
        if current == "bear":
            rd.size_pct *= 0.6
            rd.reasoning = f"{rd.reasoning} | regime=bear · counter-trend long, size ×0.6"
        elif current == "chop":
            rd.size_pct *= 0.5
            rd.reasoning = f"{rd.reasoning} | regime=chop · long size ×0.5"
    elif rd.action == "sell":
        if current == "bull":
            rd.size_pct *= 0.6
            rd.reasoning = f"{rd.reasoning} | regime=bull · counter-trend short, size ×0.6"
        elif current == "chop":
            rd.size_pct *= 0.5
            rd.reasoning = f"{rd.reasoning} | regime=chop · short size ×0.5"

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

    # News sentiment — recency-weighted CryptoPanic score in [-1, +1] for the
    # symbol's currency. Positive sentiment boosts long confidence (and
    # dampens shorts), negative does the inverse. Capped per news_sentiment_max_adj.
    if rd.action in {"buy", "sell"}:
        try:
            sym = str(snapshot.get("symbol", ""))
            settings_local = get_settings()
            sent = news.symbol_sentiment(sym, hours=settings_local.news_sentiment_window_hours)
        except Exception:
            sent = 0.0
        if abs(sent) > 0.05:
            cap = get_settings().news_sentiment_max_adj
            adj = max(-cap, min(cap, sent * cap))  # scale [-1,1] → [-cap, +cap]
            if rd.action == "buy":
                rd.confidence = round(max(0.0, min(1.0, rd.confidence + adj)), 3)
            else:  # sell
                rd.confidence = round(max(0.0, min(1.0, rd.confidence - adj)), 3)
            tag = "boosted" if adj > 0 else "dampened"
            rd.reasoning = (
                f"{rd.reasoning} | news={sent:+.2f} → conf {tag} {adj:+.2f}"
            )

    # Smart position sizing: base 3% of equity, scaled UP by confidence above
    # threshold and by ADX strength. High-conviction + strong-trend setups bet
    # bigger; weak setups that barely clear the threshold bet tiny. Cap at 6%.
    # Drawdown circuit breaker: if paper equity is too far below peak,
    # multiply the sized fraction by dd_size_mult (default 0.5) until recovered.
    if rd.action in {"buy", "sell"}:
        settings = get_settings()
        from app.services import risk

        # Prefer Kelly sizing once we have 50+ closed trades — empirically
        # grounded in actual win rate and win/loss ratio. Before that,
        # fall back to confidence+ADX heuristic (no real history to fit).
        kelly = risk.kelly_fraction(min_trades=50)
        if kelly is not None:
            sized = kelly["fraction"]
            rd.reasoning = (
                f"{rd.reasoning} | Kelly {kelly['half_kelly'] * 100:.2f}% "
                f"(p={kelly['win_rate']:.2f}, b={kelly['avg_win_loss_ratio']:.2f}, "
                f"n={kelly['sample_size']})"
            )
        else:
            conf_boost = 1.0 + max(0.0, rd.confidence - settings.min_signal_confidence) * 2.0
            adx = float(snapshot.get("adx_14", 20.0))
            adx_boost = min(1.5, 1.0 + max(0.0, adx - 20.0) * 0.02)
            sized = 0.03 * conf_boost * adx_boost

        dd = risk.drawdown_state()
        if dd["multiplier_active"]:
            sized *= dd["multiplier"]
            rd.reasoning = (
                f"{rd.reasoning} | DD {dd['dd_pct'] * 100:.1f}% · size ×{dd['multiplier']}"
            )

        # Honour the chop-regime half-size already baked in earlier.
        if rd.size_pct > 0:
            sized = min(sized, rd.size_pct) if rd.size_pct < 0.06 else sized

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
