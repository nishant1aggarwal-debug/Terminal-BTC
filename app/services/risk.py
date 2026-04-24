from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlmodel import select

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import DailyPnL, KillSwitch, Position

log = get_logger(__name__)


# Sector clusters: symbols inside the same bucket tend to move together, so
# we cap how many concurrent positions can sit in one cluster. A BTC dump
# takes out every high-beta alt the same day — better to diversify.
_SECTORS: dict[str, str] = {
    # BTC-core
    "BTC/USDT": "btc",
    # ETH/SOL — highest-correlation majors
    "ETH/USDT": "eth_sol", "SOL/USDT": "eth_sol",
    # Large-cap alts
    "XRP/USDT": "large_alt", "ADA/USDT": "large_alt", "AVAX/USDT": "large_alt",
    "LINK/USDT": "large_alt", "LTC/USDT": "large_alt", "BNB/USDT": "large_alt",
    "TRX/USDT": "large_alt", "TON/USDT": "large_alt",
    # L1s / L2s
    "DOT/USDT": "l1_l2", "MATIC/USDT": "l1_l2", "NEAR/USDT": "l1_l2",
    "APT/USDT": "l1_l2", "ATOM/USDT": "l1_l2", "FIL/USDT": "l1_l2",
    "HBAR/USDT": "l1_l2", "ARB/USDT": "l1_l2", "OP/USDT": "l1_l2",
    "SUI/USDT": "l1_l2", "SEI/USDT": "l1_l2", "INJ/USDT": "l1_l2",
    "TIA/USDT": "l1_l2", "ETC/USDT": "l1_l2",
    # DeFi
    "UNI/USDT": "defi", "AAVE/USDT": "defi", "MKR/USDT": "defi",
    "CRV/USDT": "defi", "LDO/USDT": "defi", "COMP/USDT": "defi",
    "SNX/USDT": "defi",
    # Legacy / payments
    "XLM/USDT": "legacy", "ALGO/USDT": "legacy", "XTZ/USDT": "legacy",
    # Metaverse / gaming (highest beta)
    "SAND/USDT": "gaming", "MANA/USDT": "gaming", "AXS/USDT": "gaming",
    "IMX/USDT": "gaming", "GALA/USDT": "gaming", "APE/USDT": "gaming",
    "CHZ/USDT": "gaming",
    # Memes
    "DOGE/USDT": "meme", "SHIB/USDT": "meme", "PEPE/USDT": "meme",
    "WIF/USDT": "meme", "BONK/USDT": "meme", "FLOKI/USDT": "meme",
    "ORDI/USDT": "meme",
    # Infrastructure / oracles
    "RUNE/USDT": "infra", "GRT/USDT": "infra", "RNDR/USDT": "infra",
    "FET/USDT": "infra", "THETA/USDT": "infra",
    "JUP/USDT": "infra", "PYTH/USDT": "infra", "JTO/USDT": "infra",
    "ICP/USDT": "infra", "FLOW/USDT": "infra", "KSM/USDT": "infra",
    "EGLD/USDT": "infra",
}


def sector_for(symbol: str) -> str:
    return _SECTORS.get(symbol, "other")


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


def _sector_counts() -> dict[str, int]:
    """Per-sector count of currently-open positions."""
    counts: dict[str, int] = {}
    with get_session() as s:
        rows = s.exec(select(Position)).all()
    for r in rows:
        if abs(r.qty) <= 1e-9:
            continue
        s_key = sector_for(r.symbol)
        counts[s_key] = counts.get(s_key, 0) + 1
    return counts


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

    # Sector cluster cap: if this symbol's sector already has N open positions,
    # refuse — too much correlated exposure.
    sec = sector_for(symbol)
    counts = _sector_counts()
    if counts.get(sec, 0) >= settings.max_positions_per_sector:
        # Exclude the case where we're adding to an EXISTING position in this
        # sector — pyramid adds don't count as new exposure clusters.
        with get_session() as s:
            pos = s.get(Position, symbol)
        already_open = pos is not None and abs(pos.qty) > 1e-9
        if not already_open:
            return Approval(
                False,
                f"sector_cap_{sec}: {counts[sec]}/{settings.max_positions_per_sector}",
            )

    return Approval(True, "approved")
