"""Multi-target exit manager.

Every open ``Position`` carries a plan: a stop-loss, TP1, TP2, and a trailing
stop that arms once TP1 is hit. On every tick this module walks the open
positions and checks whether the latest price has crossed any level. Hits
get a partial or full close via the executor, and each emits a notification
so the user sees ``TP1 hit — SOL/USDT +$12.40`` without having to poll trades.

Design choices:
  * Targets are stored on ``Position`` at open time (1.5×ATR SL, 1.5×ATR TP1,
    2.5×ATR TP2). This keeps the monitor branchless — just compare to stored
    prices.
  * TP1 exits 50% of the original position. Trailing stop then arms at entry
    (breakeven) and ratchets up (long) / down (short) with the high/low
    watermark. TP2 closes the remaining 50%.
  * Exits go through the normal ``executor._update_position`` path so realized
    PnL, ClosedTrade row, and DailyPnL accounting all run exactly once per fill.
"""
from __future__ import annotations

from typing import Any

from sqlmodel import select

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import Position
from app.services import notifications

log = get_logger(__name__)


def compute_levels(entry_price: float, atr: float, side: str) -> tuple[float, float, float]:
    """Single source of truth for SL / TP1 / TP2 calculation.

    Strategy:
      * SL  — entry ± 1.5 × ATR  (tight ATR-anchored stop)
      * TP2 — max(2.5 × ATR, entry × MIN_TP2_PCT)  (the meaningful move target)
      * TP1 — half of TP2 distance from entry (first-half partial close)

    The MIN_TP2_PCT floor matters because with 10x leverage we need a
    sufficiently large spot move to outpace commissions + funding fees.
    Default 5% spot ≈ 50% return on margin at 10x.

    Returns (sl, tp1, tp2) prices already oriented for the side.
    """
    settings = get_settings()
    if atr <= 0 or entry_price <= 0:
        return 0.0, 0.0, 0.0
    sl_dist = 1.5 * atr
    tp2_dist = max(2.5 * atr, entry_price * settings.min_tp2_pct)
    tp1_dist = tp2_dist * 0.5
    if side == "buy":
        return entry_price - sl_dist, entry_price + tp1_dist, entry_price + tp2_dist
    return entry_price + sl_dist, entry_price - tp1_dist, entry_price - tp2_dist


def set_targets_on_open(
    symbol: str, entry_price: float, atr: float, side: str,
) -> None:
    """Write SL/TP1/TP2 onto the Position after a fresh open.

    Called from the executor when a new position is opened (qty went 0 → non-zero
    in the same direction). No-op if the position already has targets (adds keep
    the original plan).
    """
    if atr <= 0:
        return
    with get_session() as s:
        pos = s.get(Position, symbol)
        if pos is None or abs(pos.qty) < 1e-9:
            return
        if pos.sl_price is not None:
            return  # Already planned; an add shouldn't reset the stop.
        sl, tp1, tp2 = compute_levels(entry_price, atr, side)
        pos.sl_price = sl
        if get_settings().exit_mode == "runner":
            # Runner: NO fixed take-profit. The initial 1.5xATR stop caps the
            # immediate downside; from there the Chandelier trail (armed at
            # entry via trailing_high_water) rides the move and is the only
            # profit-side exit. Leaving tp1/tp2 None makes check_and_exit
            # skip the fixed-target branches entirely.
            pos.tp1_price = None
            pos.tp2_price = None
        else:
            pos.tp1_price = tp1
            pos.tp2_price = tp2
        pos.trailing_high_water = entry_price
        if pos.initial_qty == 0.0:
            pos.initial_qty = abs(pos.qty)
        s.add(pos)
        s.commit()


def _update_trailing(
    pos: Position, price: float, runner: bool = False
) -> tuple[float | None, bool]:
    """Chandelier Exit trailing stop — ATR-based, 22-period by default.

    Long:  trailing = max(highest_high_last_N, high_water) − K × ATR
    Short: trailing = min(lowest_low_last_N, low_water) + K × ATR
    Never moves the stop backward (long: stop only ratchets up; short: only down).

    Arming:
      * targets mode — only after TP1 hits (we've banked half).
      * runner  mode — armed at entry (trailing_high_water is seeded on open).
        The clamp also differs: targets mode locks to breakeven once armed;
        runner mode clamps to the INITIAL hard stop instead, so a fresh
        position isn't shaken out at breakeven on the first wiggle — it
        gets the full 1.5xATR room until the Chandelier rises above it.
    """
    armed = pos.trailing_high_water is not None and (runner or pos.tp1_hit)
    if not armed:
        return None, False

    from app.config import get_settings
    from app.services.market_data import get_recent_extremes

    settings = get_settings()
    extremes = get_recent_extremes(
        pos.symbol,
        timeframe=settings.trade_timeframe,
        period=settings.chandelier_period,
    )
    # Floor the trailing stop. targets mode locks to breakeven (we already
    # banked half at TP1, so don't give it back). runner mode floors to the
    # initial hard stop so the position keeps its full 1.5xATR breathing
    # room until the Chandelier organically rises above it — that's what
    # lets a winner actually run instead of getting scratched at breakeven.
    floor = pos.sl_price if (runner and pos.sl_price is not None) else pos.avg_entry

    if extremes is None or extremes["atr"] <= 0:
        # Fall back to a floor-locked stop if we can't fetch extremes.
        if pos.qty > 0:
            trailing_stop = max(floor, pos.trailing_high_water - 2 * pos.avg_entry * 0.005)
            triggered = price <= trailing_stop
        else:
            trailing_stop = min(floor, pos.trailing_high_water + 2 * pos.avg_entry * 0.005)
            triggered = price >= trailing_stop
        return trailing_stop, triggered

    atr_dist = settings.chandelier_mult * extremes["atr"]

    if pos.qty > 0:  # long
        high_water = max(pos.trailing_high_water, extremes["high"], price)
        pos.trailing_high_water = high_water
        raw_stop = high_water - atr_dist
        # Ratchet: stop can only move up, never down.
        trailing_stop = max(raw_stop, floor)
        triggered = price <= trailing_stop
    else:  # short
        low_water = min(pos.trailing_high_water, extremes["low"], price)
        pos.trailing_high_water = low_water
        raw_stop = low_water + atr_dist
        trailing_stop = min(raw_stop, floor)
        triggered = price >= trailing_stop
    return trailing_stop, triggered


def check_and_exit(symbol: str, price: float) -> dict[str, Any] | None:
    """Evaluate every level for ``symbol``. If one hits, execute the exit leg.

    Returns a summary dict (kind, qty, pnl) if anything fired, otherwise None.
    The caller (scheduler tick) doesn't need to do anything — notifications and
    DB updates happen inside.
    """
    # Local import to avoid circular executor ↔ targets dependency at import time.
    from app.services import executor

    with get_session() as s:
        pos = s.get(Position, symbol)
    if pos is None or abs(pos.qty) < 1e-9 or pos.sl_price is None:
        return None

    is_long = pos.qty > 0
    qty_open = abs(pos.qty)

    # Determine which level fires. Order matters: SL first (worst case), then
    # TP2 (full close), then TP1 (partial), then trailing.
    fire_kind: str | None = None
    close_qty = 0.0

    if is_long:
        if price <= pos.sl_price:
            fire_kind, close_qty = "SL", qty_open
        elif pos.tp2_price is not None and price >= pos.tp2_price:
            fire_kind, close_qty = "TP2", qty_open
        elif not pos.tp1_hit and pos.tp1_price is not None and price >= pos.tp1_price:
            fire_kind, close_qty = "TP1", qty_open * 0.5
    else:  # short
        if price >= pos.sl_price:
            fire_kind, close_qty = "SL", qty_open
        elif pos.tp2_price is not None and price <= pos.tp2_price:
            fire_kind, close_qty = "TP2", qty_open
        elif not pos.tp1_hit and pos.tp1_price is not None and price <= pos.tp1_price:
            fire_kind, close_qty = "TP1", qty_open * 0.5

    # Trailing stop. targets mode: only after TP1. runner mode: armed from
    # entry — it IS the profit-side exit (there's no TP1/TP2).
    runner = get_settings().exit_mode == "runner"
    if fire_kind is None and (runner or pos.tp1_hit):
        trailing_stop, triggered = _update_trailing(pos, price, runner=runner)
        if triggered:
            fire_kind, close_qty = "TRAIL", qty_open
        elif trailing_stop is not None:
            # Persist the updated high-water mark even if not triggered.
            with get_session() as s:
                fresh = s.get(Position, symbol)
                fresh.trailing_high_water = pos.trailing_high_water
                s.add(fresh)
                s.commit()

    if fire_kind is None or close_qty <= 1e-9:
        return None

    # Size close to an integer-ish amount. Executor takes `size_pct` of equity,
    # so we bypass that and call _update_position directly with the exact qty.
    # We reuse executor's accounting by synthesizing a tiny "target exit" decision.
    exit_side = "sell" if is_long else "buy"

    # Emit an exit trade via the normal executor path. We fake a decision_id
    # using a negative-ish unique value so the Trade row still has a reference.
    # Easiest: insert a minimal Decision row and use that id.
    from app.models import Decision
    import json as _json
    with get_session() as s:
        d = Decision(
            source="target",
            symbol=symbol,
            timeframe="exit",
            snapshot_json=_json.dumps({"last_close": price, "trigger": fire_kind}),
            action=exit_side,
            size_pct=0.0,
            reasoning=f"[target] {fire_kind} hit @ {price:.4f}",
            confidence=1.0,
        )
        s.add(d); s.commit(); s.refresh(d)
        decision_id = d.id

    # Directly call the position updater — bypass size calc since we have exact qty.
    entry_commission = executor._commission(close_qty * price)
    realized = executor._update_position(
        symbol=symbol,
        side=exit_side,
        amount=close_qty,
        fill_price=price,
        entry_commission=entry_commission,
        decision_id=decision_id,
        confidence=1.0,
    )

    # After the exit, update target flags on the (now partially/fully closed) position.
    with get_session() as s:
        pos2 = s.get(Position, symbol)
        if pos2 is not None:
            if fire_kind == "TP1":
                pos2.tp1_hit = True
                # Arm trailing: move SL to breakeven so trailing logic kicks in.
                pos2.sl_price = pos2.avg_entry
            elif fire_kind == "TP2":
                pos2.tp2_hit = True
            if abs(pos2.qty) < 1e-9:
                # Fully closed — clear plan.
                pos2.sl_price = None
                pos2.tp1_price = None
                pos2.tp2_price = None
                pos2.trailing_high_water = None
                pos2.tp1_hit = False
                pos2.tp2_hit = False
                pos2.initial_qty = 0.0
            s.add(pos2); s.commit()

    notifications.target_hit(symbol, fire_kind, price, close_qty, realized)
    log.info("target_fired", symbol=symbol, kind=fire_kind, qty=close_qty, pnl=realized)
    return {"kind": fire_kind, "qty": close_qty, "pnl": realized, "price": price}
