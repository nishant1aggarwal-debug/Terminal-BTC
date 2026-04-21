from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlmodel import select

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import DailyPnL, KillSwitch, Position

log = get_logger(__name__)


@dataclass
class Approval:
    ok: bool
    reason: str = ""


def _kill_switch_on() -> tuple[bool, str]:
    with get_session() as s:
        row = s.get(KillSwitch, 1)
        if row and row.enabled:
            return True, row.reason or "kill switch engaged"
    return False, ""


def set_kill_switch(enabled: bool, reason: str = "") -> None:
    with get_session() as s:
        row = s.get(KillSwitch, 1)
        if row is None:
            row = KillSwitch(id=1)
        row.enabled = enabled
        row.reason = reason
        s.add(row)
        s.commit()
    log.warning("kill_switch_changed", enabled=enabled, reason=reason)


def _open_positions_count() -> int:
    with get_session() as s:
        rows = s.exec(select(Position)).all()
        return sum(1 for r in rows if abs(r.qty) > 1e-9)


def _today_realized_loss_usdt() -> float:
    with get_session() as s:
        row = s.get(DailyPnL, date.today())
        if row is None:
            return 0.0
        pnl = row.realized_usdt + row.unrealized_usdt
        return max(0.0, -pnl)  # positive number = loss magnitude


def check(action: str, symbol: str, notional_usdt: float) -> Approval:
    settings = get_settings()

    on, reason = _kill_switch_on()
    if on:
        return Approval(False, f"kill_switch: {reason}")

    if symbol not in settings.allowed_symbols:
        return Approval(False, f"symbol_not_allowed: {symbol}")

    if action == "hold":
        return Approval(True, "hold")

    if action not in {"buy", "sell"}:
        return Approval(False, f"invalid_action: {action}")

    if notional_usdt > settings.max_position_usdt:
        return Approval(
            False, f"per_trade_cap: {notional_usdt:.2f} > {settings.max_position_usdt:.2f}"
        )

    if _today_realized_loss_usdt() >= settings.max_daily_loss_usdt:
        return Approval(False, "daily_loss_cap_reached")

    if _open_positions_count() >= settings.max_open_positions:
        return Approval(False, f"max_open_positions: {settings.max_open_positions}")

    return Approval(True, "approved")
