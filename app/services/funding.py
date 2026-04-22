"""Futures funding fee engine.

Binance USDⓈ-M futures charges funding every 8 hours at 00:00, 08:00, and
16:00 UTC. Longs pay shorts when the rate is positive; shorts pay longs
when it's negative. In paper mode we approximate the realized rate with a
flat per-8h value (configurable via ``FUNDING_RATE_8H_BPS``) — the point
is to make the P&L curve look like a real exchange's, not to predict
funding exactly.

Only active when ``TRADE_MARKET=futures``. Spot pays nothing.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlmodel import select

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import DailyPnL, Position

log = get_logger(__name__)

_FUNDING_HOURS = {0, 8, 16}


def should_charge_now(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now.minute == 0 and now.hour in _FUNDING_HOURS


def charge_funding(now: datetime | None = None) -> float:
    """Apply one 8h funding charge across every open futures position.

    Returns the total funding P&L booked (negative = paid, positive = earned).
    """
    settings = get_settings()
    if settings.trade_market != "futures":
        return 0.0
    now = now or datetime.now(timezone.utc)
    rate = settings.funding_rate_8h_bps / 10_000.0

    total = 0.0
    with get_session() as s:
        positions = s.exec(select(Position)).all()
    for pos in positions:
        if abs(pos.qty) < 1e-9:
            continue
        # Approximation: use last-known avg_entry as the notional marker.
        # A real implementation would use the current mark price.
        notional = abs(pos.qty * pos.avg_entry)
        # Longs pay when rate positive; shorts receive. Match Binance sign.
        charge = -rate * notional if pos.qty > 0 else rate * notional
        total += charge
        log.info(
            "funding_charge",
            symbol=pos.symbol,
            side="long" if pos.qty > 0 else "short",
            notional=notional,
            charge=charge,
        )

    if total != 0.0:
        today = date.today()
        with get_session() as s:
            row = s.get(DailyPnL, today)
            if row is None:
                row = DailyPnL(day=today, realized_usdt=0.0)
            row.realized_usdt += total
            row.updated_at = now
            s.add(row)
            s.commit()

    return total
