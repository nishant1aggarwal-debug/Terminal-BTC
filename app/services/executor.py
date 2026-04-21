from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlmodel import select

from app.config import get_settings
from app.db import get_session
from app.exchange import binance_client
from app.logging_setup import get_logger
from app.models import Position, Trade
from app.services import risk

log = get_logger(__name__)


@dataclass
class ExecutionResult:
    ok: bool
    status: str  # "filled" | "dry" | "rejected" | "pending"
    trade_id: int | None
    message: str


def _client_order_id(decision_id: int) -> str:
    return "tb-" + hashlib.sha1(str(decision_id).encode()).hexdigest()[:20]


def _paper_equity_usdt() -> float:
    """Paper equity = starting equity + realized PnL - cost of open position.

    Simple model: we subtract the notional of any currently-open position from the
    starting cash, so size calcs don't double-spend. Unrealized PnL isn't reinvested.
    """
    from app.models import DailyPnL

    settings = get_settings()
    with get_session() as s:
        pos = s.get(Position, settings.trade_symbol)
        open_notional = abs(pos.qty * pos.avg_entry) if pos else 0.0
        # Sum realized PnL across all daily rows.
        rows = s.exec(select(DailyPnL)).all()
        realized = sum(r.realized_usdt for r in rows)
    return max(0.0, settings.paper_starting_equity_usdt + realized - open_notional)


def _update_position(symbol: str, side: str, amount: float, price: float) -> None:
    with get_session() as s:
        pos = s.get(Position, symbol)
        if pos is None:
            pos = Position(symbol=symbol, qty=0.0, avg_entry=0.0)
        signed = amount if side == "buy" else -amount
        new_qty = pos.qty + signed
        if pos.qty == 0 or (pos.qty > 0) != (new_qty > 0):
            pos.avg_entry = price
        elif signed * pos.qty > 0:  # adding to same side
            total_cost = pos.avg_entry * abs(pos.qty) + price * amount
            pos.avg_entry = total_cost / (abs(pos.qty) + amount)
        pos.qty = new_qty
        s.add(pos)
        s.commit()


def execute(
    decision_id: int,
    symbol: str,
    action: str,
    size_pct: float,
    last_price: float,
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
            t.avg_price = last_price
            s.add(t)
            s.commit()
        _update_position(symbol, action, amount, last_price)
        log.info(
            "executor_dry_run",
            decision_id=decision_id,
            trade_id=trade_id,
            symbol=symbol,
            side=action,
            amount=amount,
            price=last_price,
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

    with get_session() as s:
        t = s.get(Trade, trade_id)
        t.status = "filled"
        t.filled_amount = filled
        t.avg_price = avg_price
        t.exchange_order_id = str(resp.get("id") or "")
        t.raw_response = json.dumps(resp, default=str)[:10_000]
        s.add(t)
        s.commit()

    _update_position(symbol, action, filled, avg_price)
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
