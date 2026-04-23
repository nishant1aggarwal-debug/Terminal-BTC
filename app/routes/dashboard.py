"""Read-only dashboard API backing the /ui frontend.

Pulls aggregated state out of SQLite for the browser to render. No writes, no
exchange calls beyond reading the last-known `last_close` stored in Decision
snapshots — so the dashboard is cheap to hit and safe to poll.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query
from sqlmodel import desc, select

from app.config import get_settings
from app.db import get_session
from app.exchange import data_source
from app.services import backtest as backtest_svc
from app.services import notifications as notifications_svc
from app.services import scheduler as scheduler_svc
from app.models import (
    BacktestReport,
    ClosedTrade,
    DailyPnL,
    Decision,
    KillSwitch,
    MacroIndicator,
    Position,
    Trade,
)

router = APIRouter(prefix="/api", tags=["dashboard"])


def _latest_mark_prices() -> dict[str, float]:
    """Most recent `last_close` seen per symbol, pulled from stored Decision snapshots."""
    marks: dict[str, float] = {}
    with get_session() as s:
        # Newest first. We only keep the first (most recent) hit per symbol.
        rows = s.exec(select(Decision).order_by(desc(Decision.ts)).limit(5_000)).all()
    for row in rows:
        if row.symbol in marks:
            continue
        try:
            snap = json.loads(row.snapshot_json)
            lc = float(snap.get("last_close"))
            if lc > 0:
                marks[row.symbol] = lc
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return marks


def _unrealized(position: Position, mark: float | None) -> float:
    if mark is None or position.qty == 0:
        return 0.0
    return (mark - position.avg_entry) * position.qty


@router.get("/overview")
async def overview() -> dict[str, Any]:
    settings = get_settings()
    with get_session() as s:
        ks = s.get(KillSwitch, 1)
        positions = s.exec(select(Position)).all()
        trades = s.exec(select(Trade).order_by(desc(Trade.ts)).limit(1)).all()
        decisions = s.exec(select(Decision).order_by(desc(Decision.ts)).limit(1)).all()
        pnl_rows = s.exec(select(DailyPnL)).all()
        fg = s.get(MacroIndicator, "fear_greed")

    marks = _latest_mark_prices()
    open_positions = [p for p in positions if abs(p.qty) > 1e-9]
    open_notional = sum(abs(p.qty * p.avg_entry) for p in open_positions)
    unrealized = sum(_unrealized(p, marks.get(p.symbol)) for p in open_positions)
    realized_total = sum(r.realized_usdt for r in pnl_rows)
    today_row = next((r for r in pnl_rows if r.day == date.today()), None)

    equity = settings.paper_starting_equity_usdt + realized_total + unrealized
    pnl_total = equity - settings.paper_starting_equity_usdt
    pnl_pct = (pnl_total / settings.paper_starting_equity_usdt * 100.0) if settings.paper_starting_equity_usdt else 0.0

    last_trade_ts = trades[0].ts.isoformat() if trades else None
    last_decision_ts = decisions[0].ts.isoformat() if decisions else None

    return {
        "paper_mode": settings.paper_mode,
        "real_orders_enabled": settings.real_orders_enabled,
        "signal_mode": settings.signal_mode,
        "data_source": settings.data_source,
        "timeframe": settings.trade_timeframe,
        "market": settings.trade_market,
        "margin_mode": settings.margin_mode if settings.trade_market == "futures" else None,
        "leverage": settings.leverage if settings.trade_market == "futures" else None,
        "risk_per_trade_pct": settings.risk_per_trade_pct,
        "max_position_usdt": settings.max_position_usdt,
        "max_open_positions": settings.max_open_positions,
        "symbols": settings.symbols,
        "kill_switch": {
            "enabled": bool(ks.enabled) if ks else False,
            "reason": ks.reason if ks else "",
        },
        "starting_equity_usdt": settings.paper_starting_equity_usdt,
        "equity_usdt": equity,
        "cash_usdt": max(0.0, settings.paper_starting_equity_usdt + realized_total - open_notional),
        "open_notional_usdt": open_notional,
        "realized_pnl_usdt": realized_total,
        "unrealized_pnl_usdt": unrealized,
        "total_pnl_usdt": pnl_total,
        "total_pnl_pct": pnl_pct,
        "today_realized_usdt": today_row.realized_usdt if today_row else 0.0,
        "today_unrealized_usdt": unrealized,
        "open_positions_count": len(open_positions),
        "last_trade_ts": last_trade_ts,
        "last_decision_ts": last_decision_ts,
        "fear_greed": {
            "value": fg.value,
            "classification": fg.classification,
            "fetched_at": fg.fetched_at.isoformat(),
        } if fg else None,
        "scheduler": scheduler_svc.heartbeat(),
        "backtest": backtest_svc.state(),
        "notifications_unread": notifications_svc.unread_count(),
    }


_MARKETS_CACHE: dict[str, Any] = {"ts": 0.0, "data": []}
_MARKETS_TTL_SEC = 20


@router.get("/markets")
async def markets() -> list[dict[str, Any]]:
    """Current price + indicators for every TRADE_SYMBOL.

    Merges (1) the exchange's ticker (price, 24h change, volume) with (2) the
    most recent Decision's snapshot (RSI, MACD hist, ADX, EMA-stack trend,
    composite score bias) so each card shows at-a-glance whether the rules
    engine likes or dislikes the coin right now.

    Cached 20s — indicator data updates at each scheduler tick anyway.
    """
    import time
    now = time.time()
    if now - _MARKETS_CACHE["ts"] < _MARKETS_TTL_SEC and _MARKETS_CACHE["data"]:
        return _MARKETS_CACHE["data"]

    settings = get_settings()
    symbols = data_source.filter_supported(settings.symbols)
    try:
        tickers = data_source.fetch_tickers(symbols)
    except Exception as exc:
        return [{"symbol": s, "error": str(exc)} for s in symbols]

    # Latest decision snapshot per symbol — one pass over the most recent 5k rows.
    latest_snaps: dict[str, dict[str, Any]] = {}
    latest_actions: dict[str, dict[str, Any]] = {}
    with get_session() as s:
        decisions = s.exec(select(Decision).order_by(desc(Decision.ts)).limit(5_000)).all()
    for d in decisions:
        if d.symbol in latest_snaps:
            continue
        try:
            latest_snaps[d.symbol] = json.loads(d.snapshot_json)
            latest_actions[d.symbol] = {
                "action": d.action,
                "confidence": d.confidence,
                "reasoning": d.reasoning,
            }
        except (ValueError, TypeError, json.JSONDecodeError):
            continue

    out: list[dict[str, Any]] = []
    for sym in symbols:
        t = tickers.get(sym) or {}
        snap = latest_snaps.get(sym) or {}
        last_action = latest_actions.get(sym) or {}

        # Derive a compact "bias" label: long / short / watch based on the
        # latest decision and whether EMA stack is bullish/bearish.
        ema20 = snap.get("ema_20") or 0
        ema50 = snap.get("ema_50") or 0
        ema200 = snap.get("ema_200") or 0
        trend = "up" if ema20 > ema50 > ema200 else "down" if ema20 < ema50 < ema200 else "flat"

        bias = "watch"
        action = last_action.get("action")
        if action == "buy":
            bias = "long"
        elif action == "sell":
            bias = "short"

        out.append({
            "symbol": sym,
            "last": t.get("last") or t.get("close"),
            "bid": t.get("bid"),
            "ask": t.get("ask"),
            "high": t.get("high"),
            "low": t.get("low"),
            "change_pct": t.get("percentage"),
            "volume_24h": t.get("quoteVolume") or t.get("baseVolume"),
            "source": settings.data_source,
            # Rules-engine readouts from the last decision:
            "rsi": snap.get("rsi_14"),
            "macd_hist": snap.get("macd_hist"),
            "adx": snap.get("adx_14"),
            "trend": trend,
            "bias": bias,
            "last_action": action,
            "last_confidence": last_action.get("confidence"),
        })
    _MARKETS_CACHE["ts"] = now
    _MARKETS_CACHE["data"] = out
    return out


@router.get("/notifications")
async def notifications_list(
    limit: int = Query(50, ge=1, le=500),
    unread_only: bool = Query(False),
) -> dict[str, Any]:
    return {
        "unread": notifications_svc.unread_count(),
        "items": notifications_svc.list_recent(limit=limit, unread_only=unread_only),
    }


@router.get("/positions")
async def positions() -> list[dict[str, Any]]:
    with get_session() as s:
        rows = s.exec(select(Position)).all()
    marks = _latest_mark_prices()
    out: list[dict[str, Any]] = []
    for p in rows:
        if abs(p.qty) <= 1e-9:
            continue
        mark = marks.get(p.symbol)
        upnl = _unrealized(p, mark)
        notional = abs(p.qty * p.avg_entry)
        pct = (upnl / notional * 100.0) if notional else 0.0
        out.append({
            "symbol": p.symbol,
            "qty": p.qty,
            "side": "long" if p.qty > 0 else "short",
            "avg_entry": p.avg_entry,
            "mark_price": mark,
            "notional_usdt": notional,
            "unrealized_pnl_usdt": upnl,
            "unrealized_pnl_pct": pct,
            "updated_at": p.updated_at.isoformat(),
        })
    out.sort(key=lambda r: abs(r["unrealized_pnl_usdt"]), reverse=True)
    return out


@router.get("/trades")
async def trades(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    with get_session() as s:
        rows = s.exec(select(Trade).order_by(desc(Trade.ts)).limit(limit)).all()
    return [
        {
            "id": t.id,
            "ts": t.ts.isoformat(),
            "symbol": t.symbol,
            "side": t.side,
            "type": t.type,
            "amount": t.amount,
            "filled_amount": t.filled_amount,
            "price": t.price,
            "avg_price": t.avg_price,
            "status": t.status,
            "decision_id": t.decision_id,
        }
        for t in rows
    ]


@router.get("/decisions")
async def decisions(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    with get_session() as s:
        rows = s.exec(select(Decision).order_by(desc(Decision.ts)).limit(limit)).all()
    out: list[dict[str, Any]] = []
    for d in rows:
        last_close = None
        try:
            last_close = float(json.loads(d.snapshot_json).get("last_close"))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        out.append({
            "id": d.id,
            "ts": d.ts.isoformat(),
            "source": d.source,
            "symbol": d.symbol,
            "action": d.action,
            "confidence": d.confidence,
            "size_pct": d.size_pct,
            "stop_loss": d.stop_loss,
            "take_profit": d.take_profit,
            "reasoning": d.reasoning,
            "last_close": last_close,
        })
    return out


@router.get("/stats")
async def stats() -> dict[str, Any]:
    """Win rate, profit factor, drawdown, and per-symbol breakdown.

    Everything is derived from ClosedTrade rows (round-trips) — so stats
    only include trades where entry and exit are both realized.
    """
    settings = get_settings()
    with get_session() as s:
        closed = s.exec(select(ClosedTrade).order_by(ClosedTrade.closed_at)).all()

    n = len(closed)
    wins = [t for t in closed if t.net_pnl_usdt > 0]
    losses = [t for t in closed if t.net_pnl_usdt < 0]
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    gross_wins = sum(t.net_pnl_usdt for t in wins)
    gross_losses = -sum(t.net_pnl_usdt for t in losses)  # positive magnitude
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (float("inf") if gross_wins > 0 else 0.0)
    avg_win = (gross_wins / len(wins)) if wins else 0.0
    avg_loss = (-gross_losses / len(losses)) if losses else 0.0
    expectancy = (sum(t.net_pnl_usdt for t in closed) / n) if n else 0.0
    avg_hold_min = (sum(t.hold_seconds for t in closed) / n / 60.0) if n else 0.0
    total_commission = sum(t.commission_usdt for t in closed)
    total_funding = sum(t.funding_usdt for t in closed)

    # Max drawdown on cumulative equity curve.
    peak = settings.paper_starting_equity_usdt
    eq = peak
    max_dd = 0.0
    for t in closed:
        eq += t.net_pnl_usdt
        peak = max(peak, eq)
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / peak * 100.0) if peak else 0.0

    # Per-symbol breakdown.
    by_symbol: dict[str, dict[str, Any]] = {}
    for t in closed:
        row = by_symbol.setdefault(
            t.symbol,
            {"symbol": t.symbol, "trades": 0, "wins": 0, "losses": 0, "pnl_usdt": 0.0},
        )
        row["trades"] += 1
        row["pnl_usdt"] += t.net_pnl_usdt
        if t.net_pnl_usdt > 0:
            row["wins"] += 1
        elif t.net_pnl_usdt < 0:
            row["losses"] += 1
    for row in by_symbol.values():
        row["win_rate_pct"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
    symbol_rows = sorted(by_symbol.values(), key=lambda r: r["pnl_usdt"], reverse=True)

    # Promote-to-live gate: only meaningful past 20 trades.
    ready_for_live = n >= 20 and win_rate >= 70.0 and profit_factor >= 1.5

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor if profit_factor != float("inf") else None,
        "profit_factor_inf": profit_factor == float("inf"),
        "avg_win_usdt": avg_win,
        "avg_loss_usdt": avg_loss,
        "expectancy_usdt": expectancy,
        "avg_hold_minutes": avg_hold_min,
        "total_net_pnl_usdt": sum(t.net_pnl_usdt for t in closed),
        "total_commission_usdt": total_commission,
        "total_funding_usdt": total_funding,
        "max_drawdown_usdt": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "by_symbol": symbol_rows,
        "ready_for_live": ready_for_live,
        "min_trades_for_promotion": 20,
        "min_win_rate_for_promotion_pct": 70.0,
        "min_profit_factor_for_promotion": 1.5,
    }


@router.get("/closed-trades")
async def closed_trades(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    with get_session() as s:
        rows = s.exec(select(ClosedTrade).order_by(desc(ClosedTrade.closed_at)).limit(limit)).all()
    return [
        {
            "id": t.id,
            "closed_at": t.closed_at.isoformat(),
            "entry_ts": t.entry_ts.isoformat(),
            "symbol": t.symbol,
            "side": t.side,
            "qty": t.qty,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "gross_pnl_usdt": t.gross_pnl_usdt,
            "commission_usdt": t.commission_usdt,
            "funding_usdt": t.funding_usdt,
            "net_pnl_usdt": t.net_pnl_usdt,
            "pnl_pct": t.pnl_pct,
            "hold_seconds": t.hold_seconds,
            "entry_confidence": t.entry_confidence,
        }
        for t in rows
    ]


@router.get("/signals/latest")
async def latest_signals(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    """Recent actionable (non-hold) signals, formatted for manual copy to your exchange.

    Each row is a ready-to-place order: symbol, side, entry, SL, TP, size %, confidence.
    """
    with get_session() as s:
        rows = s.exec(
            select(Decision)
            .where(Decision.action != "hold")
            .order_by(desc(Decision.ts))
            .limit(limit)
        ).all()
    out: list[dict[str, Any]] = []
    for d in rows:
        last_close = None
        try:
            last_close = float(json.loads(d.snapshot_json).get("last_close"))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        out.append({
            "id": d.id,
            "ts": d.ts.isoformat(),
            "symbol": d.symbol,
            "side": "BUY" if d.action == "buy" else "SELL",
            "entry_price": last_close,
            "stop_loss": d.stop_loss,
            "take_profit": d.take_profit,
            "size_pct_of_equity": d.size_pct * 100.0,
            "confidence": d.confidence,
            "reasoning": d.reasoning,
            "rr_ratio": (
                abs((d.take_profit - last_close) / (last_close - d.stop_loss))
                if last_close and d.stop_loss and d.take_profit and last_close != d.stop_loss
                else None
            ),
        })
    return out


@router.get("/macro")
async def macro_state() -> dict[str, Any]:
    """Latest macro indicators (Fear & Greed for now)."""
    with get_session() as s:
        rows = s.exec(select(MacroIndicator)).all()
    out: dict[str, Any] = {}
    for r in rows:
        out[r.name] = {
            "value": r.value,
            "classification": r.classification,
            "source": r.source,
            "fetched_at": r.fetched_at.isoformat(),
        }
    return out


@router.get("/backtest")
async def backtest_latest() -> dict[str, Any]:
    """Latest BacktestReport per symbol + aggregate summary."""
    with get_session() as s:
        rows = s.exec(select(BacktestReport).order_by(desc(BacktestReport.generated_at))).all()
    latest_by_symbol: dict[str, BacktestReport] = {}
    for r in rows:
        if r.symbol not in latest_by_symbol:
            latest_by_symbol[r.symbol] = r
    reports = list(latest_by_symbol.values())

    total_trades = sum(r.trades for r in reports)
    total_wins = sum(r.wins for r in reports)
    total_losses = sum(r.losses for r in reports)
    overall_wr = (total_wins / total_trades * 100.0) if total_trades else 0.0
    avg_pnl_pct = (sum(r.net_pnl_pct for r in reports) / len(reports)) if reports else 0.0
    latest_generated = max((r.generated_at for r in reports), default=None)

    return {
        "generated_at": latest_generated.isoformat() if latest_generated else None,
        "symbols_covered": len(reports),
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "overall_win_rate_pct": overall_wr,
        "avg_pnl_pct": avg_pnl_pct,
        "state": backtest_svc.state(),
        "reports": [
            {
                "symbol": r.symbol,
                "timeframe": r.timeframe,
                "generated_at": r.generated_at.isoformat(),
                "candles": r.candles,
                "trades": r.trades,
                "wins": r.wins,
                "losses": r.losses,
                "win_rate_pct": r.win_rate_pct,
                "profit_factor": r.profit_factor,
                "net_pnl_pct": r.net_pnl_pct,
                "max_drawdown_pct": r.max_drawdown_pct,
                "avg_hold_minutes": r.avg_hold_minutes,
                "period_start": r.period_start.isoformat() if r.period_start else None,
                "period_end": r.period_end.isoformat() if r.period_end else None,
            }
            for r in sorted(reports, key=lambda r: r.net_pnl_pct, reverse=True)
        ],
    }


@router.get("/equity")
async def equity(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Equity curve built from realized ClosedTrade rows (net of commissions)."""
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with get_session() as s:
        rows = s.exec(
            select(ClosedTrade).where(ClosedTrade.closed_at >= cutoff).order_by(ClosedTrade.closed_at)
        ).all()

    equity_val = settings.paper_starting_equity_usdt
    points: list[dict[str, Any]] = [{
        "ts": cutoff.isoformat(),
        "equity": equity_val,
        "event": "start",
    }]
    for t in rows:
        equity_val += t.net_pnl_usdt
        points.append({
            "ts": t.closed_at.isoformat(),
            "equity": equity_val,
            "event": f"close {t.symbol} {t.side} {'+' if t.net_pnl_usdt >= 0 else ''}{t.net_pnl_usdt:.2f}",
        })

    return {
        "starting_equity_usdt": settings.paper_starting_equity_usdt,
        "points": points,
    }
