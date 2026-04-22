from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlmodel import select

from app.config import get_settings
from app.db import get_session
from app.exchange import binance_client
from app.logging_setup import get_logger
from app.models import ClosedTrade, DailyPnL, Position, Trade
from app.services import risk

log = get_logger(__name__)


def _fee_bps() -> float:
    s = get_settings()
    return s.fee_futures_bps if s.trade_market == "futures" else s.fee_spot_bps


def _apply_slippage(side: str, price: float) -> float:
    """Market orders get adverse fills: buys fill above mid, sells below."""
    bps = get_settings().slippage_bps
    adj = price * (bps / 10_000.0)
    return price + adj if side == "buy" else price - adj


def _commission(notional: float) -> float:
    return notional * (_fee_bps() / 10_000.0)


def _bump_daily_realized(amount: float) -> None:
    today = date.today()
    with get_session() as s:
        row = s.get(DailyPnL, today)
        if row is None:
            row = DailyPnL(day=today, realized_usdt=0.0)
        row.realized_usdt += amount
        row.updated_at = datetime.now(timezone.utc)
        s.add(row)
        s.commit()


@dataclass
class ExecutionResult:
    ok: bool
    status: str  # "filled" | "dry" | "rejected" | "pending"
    trade_id: int | None
    message: str


def _client_order_id(decision_id: int) -> str:
    return "tb-" + hashlib.sha1(str(decision_id).encode()).hexdigest()[:20]


def _paper_equity_usdt() -> float:
    """Paper equity = starting equity + realized PnL - cost of ALL open positions.

    Simple model: we subtract the aggregate notional of every open position from the
    starting cash, so size calcs across the multi-symbol loop don't double-spend.
    Unrealized PnL isn't reinvested.
    """
    from app.models import DailyPnL

    settings = get_settings()
    with get_session() as s:
        positions = s.exec(select(Position)).all()
        open_notional = sum(abs(p.qty * p.avg_entry) for p in positions)
        rows = s.exec(select(DailyPnL)).all()
        realized = sum(r.realized_usdt for r in rows)
    return max(0.0, settings.paper_starting_equity_usdt + realized - open_notional)


def _update_position(
    symbol: str,
    side: str,
    amount: float,
    fill_price: float,
    entry_commission: float,
    decision_id: int,
    confidence: float | None,
) -> float:
    """Apply a fill to the Position and emit a ClosedTrade when one closes.

    Returns the realized PnL (USDT, net of commissions) booked by this fill.
    Zero for opens / adds; non-zero only when qty crosses toward or through zero.
    """
    now = datetime.now(timezone.utc)
    realized_net = 0.0
    with get_session() as s:
        pos = s.get(Position, symbol)
        if pos is None:
            pos = Position(symbol=symbol, qty=0.0, avg_entry=0.0)
        signed = amount if side == "buy" else -amount
        new_qty = pos.qty + signed
        closing = pos.qty != 0 and (pos.qty > 0) != (signed > 0)

        if closing:
            closed_qty = min(abs(pos.qty), abs(signed))
            pos_side = "long" if pos.qty > 0 else "short"
            if pos.qty > 0:  # long, selling to close
                gross = (fill_price - pos.avg_entry) * closed_qty
            else:            # short, buying to close
                gross = (pos.avg_entry - fill_price) * closed_qty
            # Apportion the entry commission by the fraction of the position being closed.
            entry_comm_portion = (closed_qty / abs(pos.qty)) * _commission(abs(pos.qty) * pos.avg_entry)
            exit_comm_portion = (closed_qty / abs(signed)) * entry_commission
            commission_total = entry_comm_portion + exit_comm_portion
            net = gross - commission_total
            realized_net = net
            entry_notional = closed_qty * pos.avg_entry
            pnl_pct = (net / entry_notional * 100.0) if entry_notional else 0.0
            hold_sec = int((now - (pos.opened_at or now)).total_seconds())
            closed = ClosedTrade(
                closed_at=now,
                symbol=symbol,
                side=pos_side,
                qty=closed_qty,
                entry_price=pos.avg_entry,
                exit_price=fill_price,
                entry_ts=pos.opened_at or now,
                gross_pnl_usdt=gross,
                commission_usdt=commission_total,
                net_pnl_usdt=net,
                pnl_pct=pnl_pct,
                hold_seconds=hold_sec,
                entry_decision_id=pos.opened_decision_id,
                exit_decision_id=decision_id,
                entry_confidence=pos.opened_confidence,
            )
            s.add(closed)

        pos.qty = new_qty
        if abs(new_qty) < 1e-9:
            # Fully flat — clear entry metadata so the next open starts fresh.
            pos.qty = 0.0
            pos.avg_entry = 0.0
            pos.opened_at = None
            pos.opened_decision_id = None
            pos.opened_confidence = None
        elif closing and (pos.qty > 0) != (new_qty > 0):
            # Crossed through zero into the opposite side: remainder opens a fresh position.
            pos.avg_entry = fill_price
            pos.opened_at = now
            pos.opened_decision_id = decision_id
            pos.opened_confidence = confidence
        elif not closing and (pos.qty == 0 or signed * pos.qty > 0):
            # Fresh open or same-side add.
            if pos.opened_at is None:
                pos.opened_at = now
                pos.opened_decision_id = decision_id
                pos.opened_confidence = confidence
                pos.avg_entry = fill_price
            else:
                total_cost = pos.avg_entry * abs(pos.qty - signed) + fill_price * amount
                pos.avg_entry = total_cost / abs(new_qty)
        pos.updated_at = now
        s.add(pos)
        s.commit()

    if realized_net != 0.0:
        _bump_daily_realized(realized_net)
    return realized_net


def execute(
    decision_id: int,
    symbol: str,
    action: str,
    size_pct: float,
    last_price: float,
    confidence: float | None = None,
) -> ExecutionResult:
    settings = get_settings()

    if action == "hold":
        log.info("executor_hold", decision_id=decision_id, symbol=symbol)
        return ExecutionResult(ok=True, status="hold", trade_id=None, message="hold")

    coid = _client_order_id(decision_id)

    # Idempotency check FIRST — a replay of the same decision must never re-run risk
    # checks or reach the exchange. Return the prior trade as-is.
    with get_session() as s:
        existing = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
        if existing is not None:
            log.info("executor_idempotent_hit", decision_id=decision_id, trade_id=existing.id)
            return ExecutionResult(
                ok=True, status=existing.status, trade_id=existing.id, message="idempotent_replay"
            )

    if settings.paper_mode:
        equity = _paper_equity_usdt()
    else:
        equity = binance_client.quote_equity_usdt()
    notional = max(0.0, equity * size_pct)

    approval = risk.check(action=action, symbol=symbol, notional_usdt=notional)
    if not approval.ok:
        log.warning("executor_risk_veto", decision_id=decision_id, reason=approval.reason)
        return ExecutionResult(ok=False, status="rejected", trade_id=None, message=approval.reason)

    if last_price <= 0:
        return ExecutionResult(ok=False, status="rejected", trade_id=None, message="bad_price")
    amount = round(notional / last_price, 6)
    if amount <= 0:
        return ExecutionResult(ok=False, status="rejected", trade_id=None, message="zero_size")

    # Paper-fill model: buys lift the spread, sells give it away — same as a real
    # market order against a tight book. Commission lands on notional, just like
    # Binance's taker fee.
    fill_price = _apply_slippage(action, last_price)
    fill_notional = amount * fill_price
    commission = _commission(fill_notional)
    slippage_cost = abs(fill_price - last_price) * amount

    # Persist pending trade first so we never lose the record if the network call fails.
    with get_session() as s:
        trade = Trade(
            decision_id=decision_id,
            symbol=symbol,
            side=action,
            type="market",
            amount=amount,
            price=last_price,
            client_order_id=coid,
            status="pending",
            commission_usdt=commission,
            slippage_usdt=slippage_cost,
        )
        s.add(trade)
        s.commit()
        s.refresh(trade)
        trade_id = trade.id

    if not settings.real_orders_enabled:
        with get_session() as s:
            t = s.get(Trade, trade_id)
            t.status = "dry"
            t.filled_amount = amount
            t.avg_price = fill_price
            s.add(t)
            s.commit()
        _update_position(symbol, action, amount, fill_price, commission, decision_id, confidence)
        log.info(
            "executor_dry_run",
            decision_id=decision_id,
            trade_id=trade_id,
            symbol=symbol,
            side=action,
            amount=amount,
            fill_price=fill_price,
            commission=commission,
        )
        return ExecutionResult(ok=True, status="dry", trade_id=trade_id, message="dry_run")

    try:
        resp: dict[str, Any] = binance_client.create_order(
            symbol=symbol,
            side=action,
            amount=amount,
            order_type="market",
            client_order_id=coid,
        )
    except Exception as exc:
        log.error("executor_order_failed", decision_id=decision_id, error=str(exc))
        with get_session() as s:
            t = s.get(Trade, trade_id)
            t.status = "rejected"
            t.raw_response = str(exc)
            s.add(t)
            s.commit()
        return ExecutionResult(ok=False, status="rejected", trade_id=trade_id, message=str(exc))

    filled = float(resp.get("filled") or resp.get("amount") or amount)
    avg_price = float(resp.get("average") or resp.get("price") or last_price)
    live_commission = _commission(filled * avg_price)

    with get_session() as s:
        t = s.get(Trade, trade_id)
        t.status = "filled"
        t.filled_amount = filled
        t.avg_price = avg_price
        t.commission_usdt = live_commission
        t.exchange_order_id = str(resp.get("id") or "")
        t.raw_response = json.dumps(resp, default=str)[:10_000]
        s.add(t)
        s.commit()

    _update_position(symbol, action, filled, avg_price, live_commission, decision_id, confidence)
    log.info(
        "executor_filled",
        decision_id=decision_id,
        trade_id=trade_id,
        symbol=symbol,
        side=action,
        amount=filled,
        price=avg_price,
    )
    return ExecutionResult(ok=True, status="filled", trade_id=trade_id, message="filled")
