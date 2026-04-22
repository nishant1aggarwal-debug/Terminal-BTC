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
from app.models import DailyPnL, Decision, KillSwitch, Position, Trade

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


@router.get("/equity")
async def equity(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Equity curve built from filled/dry trades + daily realized PnL.

    Each filled trade becomes a point: equity at that moment = starting + realized_at_that_time
    + (position mark-to-entry unrealized is skipped for simplicity; curve = cash + realized).
    """
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with get_session() as s:
        rows = s.exec(
            select(Trade).where(Trade.ts >= cutoff).order_by(Trade.ts)
        ).all()

    points: list[dict[str, Any]] = [{
        "ts": cutoff.isoformat(),
        "equity": settings.paper_starting_equity_usdt,
        "event": "start",
    }]
    # We don't track per-trade PnL in the DB yet, so the curve is flat on entries and
    # steps at exits. Close detection: a trade that reduces |qty| toward zero.
    running_qty: dict[str, float] = {}
    running_entry: dict[str, float] = {}
    realized = 0.0
    for t in rows:
        px = t.avg_price or t.price or 0.0
        amt = t.filled_amount or t.amount
        if not px or not amt or t.status not in {"filled", "dry"}:
            continue
        q = running_qty.get(t.symbol, 0.0)
        e = running_entry.get(t.symbol, 0.0)
        signed = amt if t.side == "buy" else -amt
        new_q = q + signed
        # Closing or reducing a position in the opposite direction realizes PnL.
        if q != 0 and (q > 0) != (signed > 0):
            closed = min(abs(q), abs(signed))
            if q > 0:
                realized += (px - e) * closed
            else:
                realized += (e - px) * closed
        if q == 0 or (q > 0) != (new_q > 0):
            running_entry[t.symbol] = px
        elif signed * q > 0:
            running_entry[t.symbol] = (e * abs(q) + px * amt) / (abs(q) + amt)
        running_qty[t.symbol] = new_q
        points.append({
            "ts": t.ts.isoformat(),
            "equity": settings.paper_starting_equity_usdt + realized,
            "event": f"{t.side} {t.symbol}",
        })

    return {
        "starting_equity_usdt": settings.paper_starting_equity_usdt,
        "points": points,
    }
