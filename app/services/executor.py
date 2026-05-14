from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlmodel import select

from app.config import get_settings
from app.db import get_session
from app.exchange import binance_client, bybit_client
from app.logging_setup import get_logger
from app.models import ClosedTrade, DailyPnL, Decision, Position, Trade
from app.services import notifications, risk, targets


def _live_client():
    """Pick the right execution client based on TRADE_EXCHANGE config."""
    settings = get_settings()
    return bybit_client if settings.trade_exchange == "bybit" else binance_client

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


def _as_utc(dt):
    """Normalize a datetime to tz-aware UTC.

    Postgres returns naive datetimes for plain ``DateTime`` columns; SQLite
    behaves the same. Subtracting a naive value from ``datetime.now(timezone.utc)``
    raises TypeError, so we attach UTC tzinfo when missing.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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


MAX_PYRAMID_ADDS = 2  # entries beyond initial open (so 3 total tranches max)


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
    event: dict | None = None
    with get_session() as s:
        pos = s.get(Position, symbol)
        if pos is None:
            pos = Position(symbol=symbol, qty=0.0, avg_entry=0.0)
        signed = amount if side == "buy" else -amount
        new_qty = pos.qty + signed
        closing = pos.qty != 0 and (pos.qty > 0) != (signed > 0)

        was_open = abs(pos.qty) > 1e-9
        was_fresh_open = not was_open

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
            opened_at_aware = _as_utc(pos.opened_at) or now
            hold_sec = int((now - opened_at_aware).total_seconds())
            # Pull the entry decision's self-learning telemetry so the auditor
            # can attribute this realized PnL to the right indicator + regime
            # at the time the trade was opened (NOT closed — closing context is
            # noise for per-indicator learning).
            entry_contribs_json: str | None = None
            entry_regime: str | None = None
            entry_dom: str | None = None
            if pos.opened_decision_id is not None:
                entry_dec = s.get(Decision, pos.opened_decision_id)
                if entry_dec is not None:
                    entry_contribs_json = entry_dec.indicator_contributions_json
                    entry_regime = entry_dec.regime
                    entry_dom = entry_dec.dominant_indicator
            closed = ClosedTrade(
                closed_at=now,
                symbol=symbol,
                side=pos_side,
                qty=closed_qty,
                entry_price=pos.avg_entry,
                exit_price=fill_price,
                entry_ts=opened_at_aware,
                gross_pnl_usdt=gross,
                commission_usdt=commission_total,
                net_pnl_usdt=net,
                pnl_pct=pnl_pct,
                hold_seconds=hold_sec,
                entry_decision_id=pos.opened_decision_id,
                exit_decision_id=decision_id,
                entry_confidence=pos.opened_confidence,
                entry_contributions_json=entry_contribs_json,
                entry_regime=entry_regime,
                entry_dominant_indicator=entry_dom,
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
            pos.sl_price = None
            pos.tp1_price = None
            pos.tp2_price = None
            pos.tp1_hit = False
            pos.tp2_hit = False
            pos.trailing_high_water = None
            pos.initial_qty = 0.0
            pos.adds = 0
            event = {"kind": "CLOSE", "side": side, "qty": amount, "price": fill_price,
                     "pnl": realized_net}
        elif closing and (pos.qty > 0) != (new_qty > 0):
            # Crossed through zero into the opposite side: remainder opens a fresh position.
            pos.avg_entry = fill_price
            pos.opened_at = now
            pos.opened_decision_id = decision_id
            pos.opened_confidence = confidence
            pos.initial_qty = abs(new_qty)
            pos.adds = 0
            pos.tp1_hit = False
            pos.tp2_hit = False
            event = {"kind": "FLIP_OPEN", "side": side, "qty": abs(new_qty),
                     "price": fill_price, "confidence": confidence}
        elif not closing and (pos.qty == 0 or signed * pos.qty > 0):
            # Fresh open or same-side add.
            if pos.opened_at is None:
                pos.opened_at = now
                pos.opened_decision_id = decision_id
                pos.opened_confidence = confidence
                pos.avg_entry = fill_price
                pos.initial_qty = amount
                pos.adds = 0
                event = {"kind": "OPEN", "side": side, "qty": amount,
                         "price": fill_price, "confidence": confidence}
            else:
                total_cost = pos.avg_entry * abs(pos.qty - signed) + fill_price * amount
                pos.avg_entry = total_cost / abs(new_qty)
                pos.adds = (pos.adds or 0) + 1
                event = {"kind": "ADD", "side": side, "qty": amount,
                         "price": fill_price, "add_index": pos.adds}
        pos.updated_at = now
        s.add(pos)
        s.commit()

    if realized_net != 0.0:
        _bump_daily_realized(realized_net)

    # Fire notifications OUTSIDE the session scope to avoid nested transactions.
    if event is not None:
        if event["kind"] == "OPEN" or event["kind"] == "FLIP_OPEN":
            notifications.position_opened(
                symbol, event["side"], event["qty"], event["price"],
            )
        elif event["kind"] == "ADD":
            notifications.position_added(
                symbol, event["side"], event["qty"], event["price"], event["add_index"],
            )
        elif event["kind"] == "CLOSE":
            notifications.position_closed(
                symbol, event["side"], event["qty"], event["price"], event["pnl"],
            )

    return realized_net


def execute(
    decision_id: int,
    symbol: str,
    action: str,
    size_pct: float,
    last_price: float,
    confidence: float | None = None,
    atr: float | None = None,
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

    # Pyramid-add guard: if a same-direction signal fires while we already hold
    # a position, scale the size down and cap total adds. A 3rd add on BTC
    # while long is just FOMO — we refuse.
    with get_session() as s:
        pos = s.get(Position, symbol)
    if pos is not None and abs(pos.qty) > 1e-9:
        same_direction = (pos.qty > 0 and action == "buy") or (pos.qty < 0 and action == "sell")
        if same_direction:
            if (pos.adds or 0) >= MAX_PYRAMID_ADDS:
                notifications.risk_veto(symbol, action, f"max pyramid adds ({MAX_PYRAMID_ADDS}) reached")
                return ExecutionResult(ok=False, status="rejected", trade_id=None,
                                       message="max_pyramid_adds")
            # Each add half the size of the previous — 1.0, 0.5, 0.25 of initial.
            size_pct = size_pct * (0.5 ** ((pos.adds or 0) + 1))

    if settings.paper_mode:
        equity = _paper_equity_usdt()
    else:
        equity = _live_client().quote_equity_usdt()

    # size_pct is the MARGIN allocation as a fraction of equity. For futures
    # we multiply by leverage to get true notional — this is what makes a
    # "5% spot move on 10x" actually deliver ~50% return on the margin
    # committed, instead of just ~5% on cash.
    margin = max(0.0, equity * size_pct)
    leverage_mult = settings.leverage if settings.trade_market == "futures" else 1.0
    notional = margin * leverage_mult

    # Risk gate checks MARGIN (the at-risk dollars), not notional — otherwise
    # max_position_usdt would constantly trip on leveraged sizing.
    approval = risk.check(action=action, symbol=symbol, notional_usdt=margin)
    if not approval.ok:
        log.warning("executor_risk_veto", decision_id=decision_id, reason=approval.reason)
        notifications.risk_veto(symbol, action, approval.reason)
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
        if atr is not None:
            targets.set_targets_on_open(symbol, fill_price, atr, action)
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
        resp: dict[str, Any] = _live_client().create_order(
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
    if atr is not None:
        targets.set_targets_on_open(symbol, avg_price, atr, action)
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
