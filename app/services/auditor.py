"""Strategy auditor — the self-improving loop.

Runs nightly. Reads the last N closed trades per symbol, recent backtest
reports, and macro state, and writes ``StrategyOverride`` rows that the
rules engine applies on every tick. Two modes:

  * **Local (always on):** deterministic rules. If a symbol's recent win
    rate is below the loss threshold (default 30%), disable the losing
    side for ``AUDITOR_OVERRIDE_HOURS`` (24h). If it's above the winning
    threshold (65%), bump its size multiplier to 1.25. No LLM, no cost.

  * **Claude-augmented (optional):** when ``ANTHROPIC_API_KEY`` is set and
    ``AUDITOR_USE_CLAUDE=true``, also ask Claude for pattern-based
    suggestions beyond what local rules can spot ("shorts fail when F&G
    is between 40-50 for SOL"). Tool-use gives structured output; cost
    capped via ``DAILY_CLAUDE_USD_CAP``.

Safety: every override auto-expires after ``AUDITOR_OVERRIDE_HOURS``, and
we cap the total new overrides per run at ``AUDITOR_MAX_OVERRIDES_PER_RUN``.
A bad audit can't wreck the system — the worst case is 12 symbol-side
pairs get paused for 24h.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import desc, select

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import (
    AuditReport,
    BacktestReport,
    ClosedTrade,
    MacroIndicator,
    StrategyOverride,
)
from app.services import notifications

log = get_logger(__name__)


@dataclass
class ProposedOverride:
    symbol: str
    param_key: str
    param_value: str
    reason: str
    regime: str | None = None  # bull | bear | chop | None=applies in any regime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _per_symbol_stats(trades: list[ClosedTrade]) -> dict[str, dict[str, Any]]:
    """Aggregate recent trades by symbol+side."""
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        key = f"{t.symbol}|{t.side}"
        row = out.setdefault(
            key,
            {"symbol": t.symbol, "side": t.side, "trades": 0, "wins": 0,
             "pnl_usdt": 0.0},
        )
        row["trades"] += 1
        row["pnl_usdt"] += t.net_pnl_usdt
        if t.net_pnl_usdt > 0:
            row["wins"] += 1
    for row in out.values():
        row["win_rate_pct"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
    return out


def _per_indicator_stats(trades: list[ClosedTrade]) -> dict[str, dict[str, Any]]:
    """Aggregate by (symbol, dominant_indicator).

    Key format: ``"BTC/USDT|ema"``. Trades without a dominant indicator are
    skipped (older trades won't have it). Used to identify indicators that
    misfire on a particular symbol — auditor halves their weight when win
    rate is bad.
    """
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        ind = getattr(t, "entry_dominant_indicator", None)
        if not ind:
            continue
        key = f"{t.symbol}|{ind}"
        row = out.setdefault(
            key,
            {"symbol": t.symbol, "indicator": ind, "trades": 0, "wins": 0,
             "pnl_usdt": 0.0},
        )
        row["trades"] += 1
        row["pnl_usdt"] += t.net_pnl_usdt
        if t.net_pnl_usdt > 0:
            row["wins"] += 1
    for row in out.values():
        row["win_rate_pct"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
    return out


def _per_regime_stats(trades: list[ClosedTrade]) -> dict[str, dict[str, Any]]:
    """Aggregate by (symbol, regime, side).

    Key format: ``"BTC/USDT|bull|long"``. Powers regime-scoped overrides
    ("disable_short on ADA in bull regimes" without nuking bear shorts that
    actually work).
    """
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        regime = getattr(t, "entry_regime", None)
        if not regime:
            continue
        key = f"{t.symbol}|{regime}|{t.side}"
        row = out.setdefault(
            key,
            {"symbol": t.symbol, "regime": regime, "side": t.side,
             "trades": 0, "wins": 0, "pnl_usdt": 0.0},
        )
        row["trades"] += 1
        row["pnl_usdt"] += t.net_pnl_usdt
        if t.net_pnl_usdt > 0:
            row["wins"] += 1
    for row in out.values():
        row["win_rate_pct"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
    return out


def _local_proposals(
    stats_by_symbol: dict[str, dict[str, Any]],
    backtest_by_symbol: dict[str, BacktestReport],
    indicator_stats: dict[str, dict[str, Any]] | None = None,
    regime_stats: dict[str, dict[str, Any]] | None = None,
) -> list[ProposedOverride]:
    """Rule-based proposals. Deterministic, no LLM.

    Four learning surfaces:
      1. Per-(symbol, side) — global disable/boost on a direction.
      2. Per-(symbol, dominant_indicator) — halve an indicator's weight when
         it's been the largest contributor on a string of losers.
      3. Per-(symbol, regime, side) — regime-scoped disables ("disable_short
         on BTC in bull regimes" without touching bear-regime shorts).
      4. Backtest PF — symbol-wide threshold tightening / loosening.
    """
    settings = get_settings()
    proposals: list[ProposedOverride] = []

    for key, row in stats_by_symbol.items():
        if row["trades"] < settings.auditor_min_trades:
            continue
        wr = row["win_rate_pct"]
        symbol = row["symbol"]
        side = row["side"]  # "long" or "short"

        # Losing side → disable that direction for the override window.
        if wr < settings.auditor_loss_win_rate_pct:
            param_key = "disable_long" if side == "long" else "disable_short"
            proposals.append(ProposedOverride(
                symbol=symbol,
                param_key=param_key,
                param_value="true",
                reason=(
                    f"Last {row['trades']} {side}s won {wr:.0f}% "
                    f"(< {settings.auditor_loss_win_rate_pct:.0f}% threshold). "
                    f"Net {row['pnl_usdt']:+.2f} USDT. Pausing 24h."
                ),
            ))

        # Winning side → nudge size up (capped at 1.25).
        elif wr >= settings.auditor_win_win_rate_pct:
            proposals.append(ProposedOverride(
                symbol=symbol,
                param_key="size_multiplier",
                param_value="1.25",
                reason=(
                    f"Last {row['trades']} {side}s won {wr:.0f}% "
                    f"(> {settings.auditor_win_win_rate_pct:.0f}% threshold). "
                    f"Net {row['pnl_usdt']:+.2f} USDT. Size ×1.25 for 24h."
                ),
            ))

    # Per-indicator learning: when one indicator dominates a string of losers
    # on the same symbol, halve its weight. The minimum-trades bar is the
    # same as the symbol-side gate so we don't react to noise.
    for key, row in (indicator_stats or {}).items():
        if row["trades"] < settings.auditor_min_trades:
            continue
        wr = row["win_rate_pct"]
        if wr < settings.auditor_loss_win_rate_pct:
            proposals.append(ProposedOverride(
                symbol=row["symbol"],
                param_key=f"weight_{row['indicator']}",
                param_value="0.5",
                reason=(
                    f"Last {row['trades']} entries dominated by {row['indicator'].upper()} "
                    f"won {wr:.0f}% (< {settings.auditor_loss_win_rate_pct:.0f}%). "
                    f"Net {row['pnl_usdt']:+.2f} USDT. Halving its weight 24h."
                ),
            ))
        elif wr >= settings.auditor_win_win_rate_pct:
            proposals.append(ProposedOverride(
                symbol=row["symbol"],
                param_key=f"weight_{row['indicator']}",
                param_value="1.25",
                reason=(
                    f"Last {row['trades']} entries dominated by {row['indicator'].upper()} "
                    f"won {wr:.0f}%. Net {row['pnl_usdt']:+.2f} USDT. Boosting "
                    f"weight ×1.25 for 24h."
                ),
            ))

    # Per-regime learning: disable a direction only inside the regime where it
    # consistently loses. Need slightly more evidence (×2 of the regular gate)
    # because we're splitting the trade sample by regime — and disabling a
    # whole symbol-side-regime tuple is more aggressive than a global disable
    # would have been across the same trades.
    regime_min = max(settings.auditor_min_trades, 6)
    for key, row in (regime_stats or {}).items():
        if row["trades"] < regime_min:
            continue
        wr = row["win_rate_pct"]
        if wr < settings.auditor_loss_win_rate_pct:
            param_key = "disable_long" if row["side"] == "long" else "disable_short"
            proposals.append(ProposedOverride(
                symbol=row["symbol"],
                param_key=param_key,
                param_value="true",
                regime=row["regime"],
                reason=(
                    f"In {row['regime']} regime, last {row['trades']} {row['side']}s "
                    f"won {wr:.0f}% (< {settings.auditor_loss_win_rate_pct:.0f}%). "
                    f"Net {row['pnl_usdt']:+.2f} USDT. Disabling that side IN "
                    f"{row['regime'].upper()} only — other regimes unaffected."
                ),
            ))

    # Also consult the latest backtest: symbols with negative PF under 0.6
    # get their threshold tightened; symbols with PF > 1.5 get it loosened.
    for symbol, report in backtest_by_symbol.items():
        if report.trades < settings.auditor_min_trades:
            continue
        pf = report.profit_factor
        if pf is None:
            continue
        if pf < 0.6:
            proposals.append(ProposedOverride(
                symbol=symbol,
                param_key="confidence_adj",
                param_value="0.10",
                reason=f"Backtest PF {pf:.2f} < 0.6 over {report.trades} trades. Tightening threshold by 0.10.",
            ))
        elif pf > 1.5:
            proposals.append(ProposedOverride(
                symbol=symbol,
                param_key="confidence_adj",
                param_value="-0.05",
                reason=f"Backtest PF {pf:.2f} > 1.5 over {report.trades} trades. Loosening threshold by 0.05.",
            ))

    return proposals


def _claude_proposals(
    stats_by_symbol: dict[str, dict[str, Any]],
    backtest_by_symbol: dict[str, BacktestReport],
    fg_value: float | None,
    indicator_stats: dict[str, dict[str, Any]] | None = None,
    regime_stats: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[ProposedOverride], float, str]:
    """Claude-augmented proposals. Returns (proposals, usd_cost, summary)."""
    settings = get_settings()
    if not settings.anthropic_api_key or not settings.auditor_use_claude:
        return [], 0.0, ""
    try:
        import anthropic
    except ImportError:
        log.warning("auditor_claude_skip_no_sdk")
        return [], 0.0, ""

    # Compact prompt: hand Claude the stats + backtest summary, get structured tool-call output.
    bt_summary = [
        {
            "symbol": sym,
            "trades": r.trades,
            "win_rate_pct": round(r.win_rate_pct, 1),
            "profit_factor": r.profit_factor,
            "net_pnl_pct": round(r.net_pnl_pct, 2),
            "max_drawdown_pct": round(r.max_drawdown_pct, 2),
        }
        for sym, r in backtest_by_symbol.items()
    ]
    live_summary = [
        {**row, "win_rate_pct": round(row["win_rate_pct"], 1),
         "pnl_usdt": round(row["pnl_usdt"], 2)}
        for row in stats_by_symbol.values()
    ]
    indicator_summary = [
        {**row, "win_rate_pct": round(row["win_rate_pct"], 1),
         "pnl_usdt": round(row["pnl_usdt"], 2)}
        for row in (indicator_stats or {}).values()
    ]
    regime_summary = [
        {**row, "win_rate_pct": round(row["win_rate_pct"], 1),
         "pnl_usdt": round(row["pnl_usdt"], 2)}
        for row in (regime_stats or {}).values()
    ]

    tool = {
        "name": "propose_overrides",
        "description": "Propose strategy overrides per symbol based on performance review.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "1-2 sentence summary of what you saw and why."},
                "overrides": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "param_key": {
                                "type": "string",
                                "enum": [
                                    "disable_long", "disable_short",
                                    "size_multiplier", "confidence_adj",
                                    "weight_ema", "weight_macd", "weight_rsi",
                                    "weight_stoch_rsi", "weight_bb",
                                ],
                            },
                            "param_value": {
                                "type": "string",
                                "description": (
                                    "String value. For disable_*: 'true' or 'false'. "
                                    "For size_multiplier: float 0.5..1.5. "
                                    "For confidence_adj: float -0.10..+0.20. "
                                    "For weight_*: float 0.0..2.0 (1.0 = default)."
                                ),
                            },
                            "regime": {
                                "type": "string",
                                "enum": ["bull", "bear", "chop"],
                                "description": (
                                    "OPTIONAL: scope the override to one regime. "
                                    "Omit for an always-on override."
                                ),
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["symbol", "param_key", "param_value", "reason"],
                    },
                },
            },
            "required": ["summary", "overrides"],
        },
    }

    system = (
        "You are a quantitative trading strategy auditor. You review recent closed trades and "
        "backtest reports and propose narrow, evidence-based parameter overrides. Rules: "
        "1) Only propose overrides when you can point to at least 5 trades of evidence "
        "(6 for regime-scoped). "
        "2) Disable a side ONLY if win rate < 25% AND net PnL is negative. "
        "3) size_multiplier is between 0.5 and 1.5. 4) confidence_adj is between -0.10 "
        "and +0.20. 5) weight_* multipliers are 0.0..2.0 — drop the weight of an indicator "
        "that dominates losing entries on a given symbol; boost it on consistent winners. "
        "6) Use `regime` to scope an override to ONE regime (bull/bear/chop) when the "
        "evidence is regime-specific — e.g. 'shorts on BTC only fail in bull regimes'. "
        "7) Prefer narrow, symbol-specific changes over broad ones. "
        "8) Max 12 overrides per audit."
    )

    user_msg = {
        "backtest_reports": bt_summary,
        "live_trades_by_symbol_side": live_summary,
        "trades_by_dominant_indicator": indicator_summary,
        "trades_by_symbol_regime_side": regime_summary,
        "fear_greed": fg_value,
        "note": (
            "Propose overrides via the tool. If nothing is actionable, return an empty "
            "overrides list. Use the per-indicator and per-regime breakdowns to write "
            "narrower (and therefore safer) overrides than a blanket symbol-side disable."
        ),
    }

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(user_msg, default=str)}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "propose_overrides"},
        )
    except Exception as exc:
        log.error("auditor_claude_call_failed", error=str(exc))
        return [], 0.0, ""

    tool_input: dict[str, Any] = {}
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == "propose_overrides":
            tool_input = getattr(block, "input", {}) or {}
            break

    summary = tool_input.get("summary", "") or ""
    overrides = tool_input.get("overrides", []) or []
    out: list[ProposedOverride] = []
    for o in overrides[: settings.auditor_max_overrides_per_run]:
        try:
            raw_regime = o.get("regime")
            regime = str(raw_regime) if raw_regime in {"bull", "bear", "chop"} else None
            out.append(ProposedOverride(
                symbol=str(o["symbol"]),
                param_key=str(o["param_key"]),
                param_value=str(o["param_value"]),
                reason=str(o.get("reason", ""))[:500],
                regime=regime,
            ))
        except KeyError:
            continue

    # Cost estimate — rough, matching claude_signal.py convention.
    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) if usage else 0
    out_tok = getattr(usage, "output_tokens", 0) if usage else 0
    # Opus pricing roughly $15 / $75 per M tokens.
    usd_cost = (in_tok * 15.0 + out_tok * 75.0) / 1_000_000.0

    return out, usd_cost, summary


def _store_overrides(
    audit_id: int,
    source: str,
    proposals: list[ProposedOverride],
    window: timedelta,
    cap: int,
) -> int:
    """Insert proposed overrides, honouring the per-run cap. Returns count stored."""
    if not proposals:
        return 0
    now = _now()
    expires_at = now + window
    stored = 0
    with get_session() as s:
        for p in proposals[:cap]:
            # Deactivate any existing active override for the same
            # (symbol, param_key, regime) tuple so the newest proposal wins
            # cleanly. We DON'T deactivate other-regime overrides on the same
            # key — those are independent learning slots ("disable_long in
            # bear" doesn't conflict with "disable_long in bull").
            existing = s.exec(
                select(StrategyOverride)
                .where(StrategyOverride.symbol == p.symbol)
                .where(StrategyOverride.param_key == p.param_key)
                .where(StrategyOverride.expires_at > now)
            ).all()
            for e in existing:
                if e.regime == p.regime:
                    e.expires_at = now
                    s.add(e)
            s.add(StrategyOverride(
                symbol=p.symbol,
                param_key=p.param_key,
                param_value=p.param_value,
                reason=p.reason,
                created_at=now,
                expires_at=expires_at,
                source=source,
                audit_id=audit_id,
                regime=p.regime,
            ))
            stored += 1
        s.commit()
    return stored


def run_audit() -> dict[str, Any]:
    """Execute one full audit cycle. Safe to call from cron or on-demand."""
    settings = get_settings()
    window = timedelta(hours=settings.auditor_override_hours)
    cutoff = _now() - timedelta(days=7)  # trades in last week

    with get_session() as s:
        trades = s.exec(
            select(ClosedTrade)
            .where(ClosedTrade.closed_at >= cutoff)
            .order_by(desc(ClosedTrade.closed_at))
            .limit(settings.auditor_window_trades)
        ).all()
        reports = s.exec(
            select(BacktestReport).order_by(desc(BacktestReport.generated_at)).limit(200)
        ).all()
        fg = s.get(MacroIndicator, "fear_greed")

    # Most recent backtest per symbol.
    bt_latest: dict[str, BacktestReport] = {}
    for r in reports:
        if r.symbol not in bt_latest:
            bt_latest[r.symbol] = r

    stats = _per_symbol_stats(list(trades))
    indicator_stats = _per_indicator_stats(list(trades))
    regime_stats = _per_regime_stats(list(trades))

    # Open the audit row first so we can link overrides to it.
    with get_session() as s:
        ar = AuditReport(
            source="local", window_trades=len(trades), summary="running…",
            overrides_proposed=0, overrides_stored=0,
        )
        s.add(ar)
        s.commit()
        s.refresh(ar)
        audit_id = ar.id

    local = _local_proposals(stats, bt_latest, indicator_stats, regime_stats)
    stored_local = _store_overrides(audit_id, "local", local, window,
                                    settings.auditor_max_overrides_per_run)

    claude_props, claude_cost, claude_summary = _claude_proposals(
        stats, bt_latest, fg.value if fg else None,
        indicator_stats=indicator_stats, regime_stats=regime_stats,
    )
    stored_claude = _store_overrides(audit_id, "claude", claude_props, window,
                                     max(0, settings.auditor_max_overrides_per_run - stored_local))

    total_proposed = len(local) + len(claude_props)
    total_stored = stored_local + stored_claude
    summary = (
        f"Local: {len(local)} proposed, {stored_local} stored. "
        f"Claude: {len(claude_props)} proposed, {stored_claude} stored. "
        f"{claude_summary}"
    ).strip()

    with get_session() as s:
        ar = s.get(AuditReport, audit_id)
        ar.source = "claude" if claude_props else "local"
        ar.summary = summary[:1000]
        ar.overrides_proposed = total_proposed
        ar.overrides_stored = total_stored
        ar.claude_usd_cost = claude_cost
        s.add(ar)
        s.commit()

    notifications.fire(
        "INFO", "SYSTEM", f"Audit: {total_stored} overrides applied",
        message=summary, severity="info",
    )

    log.info(
        "audit_complete",
        trades=len(trades), proposed=total_proposed, stored=total_stored,
        claude_cost=claude_cost,
    )
    return {
        "audit_id": audit_id,
        "trades_reviewed": len(trades),
        "proposed": total_proposed,
        "stored": total_stored,
        "summary": summary,
        "claude_usd_cost": claude_cost,
    }


def active_overrides(
    symbol: str | None = None, regime: str | None = None
) -> list[dict[str, Any]]:
    """Return active (non-expired) overrides.

    ``regime`` filters: an override row matches when ``row.regime`` is NULL
    (applies to every regime) OR equals the supplied current regime. Pass
    ``None`` to see every regime's override (used by the dashboard).
    """
    now = _now()
    with get_session() as s:
        q = select(StrategyOverride).where(StrategyOverride.expires_at > now)
        if symbol is not None:
            q = q.where(StrategyOverride.symbol == symbol)
        rows = s.exec(q.order_by(desc(StrategyOverride.created_at))).all()
    if regime is not None:
        rows = [r for r in rows if r.regime is None or r.regime == regime]
    return [
        {
            "id": o.id,
            "symbol": o.symbol,
            "param_key": o.param_key,
            "param_value": o.param_value,
            "reason": o.reason,
            "created_at": o.created_at.isoformat(),
            "expires_at": o.expires_at.isoformat(),
            "source": o.source,
            "audit_id": o.audit_id,
            "regime": o.regime,
        }
        for o in rows
    ]


def override_map(symbol: str, regime: str | None = None) -> dict[str, str]:
    """Return {param_key: param_value} of all currently-active overrides for a symbol.

    Rules engine calls this on every decision to apply per-symbol tweaks.
    When ``regime`` is supplied, regime-scoped overrides only fire if the
    current regime matches (NULL regime means any regime).

    Conflict policy: most recent override wins per param_key. Since
    ``active_overrides`` already returns rows ordered by created_at desc, we
    overwrite from oldest to newest below so the final dict carries the
    freshest decision.
    """
    rows = active_overrides(symbol=symbol, regime=regime)
    out: dict[str, str] = {}
    for o in reversed(rows):  # oldest first → newest overwrites
        out[o["param_key"]] = o["param_value"]
    return out


def recent_audits(limit: int = 20) -> list[dict[str, Any]]:
    with get_session() as s:
        rows = s.exec(
            select(AuditReport).order_by(desc(AuditReport.generated_at)).limit(limit)
        ).all()
    return [
        {
            "id": a.id,
            "generated_at": a.generated_at.isoformat(),
            "source": a.source,
            "window_trades": a.window_trades,
            "summary": a.summary,
            "overrides_proposed": a.overrides_proposed,
            "overrides_stored": a.overrides_stored,
            "claude_usd_cost": a.claude_usd_cost,
        }
        for a in rows
    ]
