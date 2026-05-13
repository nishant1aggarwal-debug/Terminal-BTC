from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

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


def _peak_equity(current: float) -> float:
    """Ratchet-up peak equity persisted as a MacroIndicator row."""
    from datetime import datetime, timezone
    from app.models import MacroIndicator
    with get_session() as s:
        row = s.get(MacroIndicator, "peak_equity")
        peak = row.value if row is not None else 0.0
        if current > peak:
            peak = current
            if row is None:
                row = MacroIndicator(name="peak_equity", value=peak, source="internal",
                                     classification="tracked")
            else:
                row.value = peak
                row.fetched_at = datetime.now(timezone.utc)
            s.add(row)
            s.commit()
    return peak


def kelly_fraction(min_trades: int = 50) -> dict[str, Any] | None:
    """Compute half-Kelly sizing fraction from the last ``min_trades`` ClosedTrades.

    Returns None when there's not enough history — caller falls back to the
    confidence+ADX-scaled sizing. Half-Kelly (0.5 * Kelly) is the conservative
    choice to survive the variance inherent in a finite sample.

    Formula: Kelly f* = (p*b - q) / b
      p = win rate, q = 1-p, b = avg_win / abs(avg_loss)
    """
    from app.models import ClosedTrade
    from sqlmodel import desc as _desc
    with get_session() as s:
        rows = s.exec(
            select(ClosedTrade).order_by(_desc(ClosedTrade.closed_at)).limit(min_trades)
        ).all()
    if len(rows) < min_trades:
        return None
    wins = [t.net_pnl_usdt for t in rows if t.net_pnl_usdt > 0]
    losses = [t.net_pnl_usdt for t in rows if t.net_pnl_usdt < 0]
    if not wins or not losses:
        return None
    p = len(wins) / len(rows)
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    if avg_loss == 0:
        return None
    b = avg_win / avg_loss
    full_kelly = (p * b - (1 - p)) / b
    half_kelly = max(0.0, full_kelly) * 0.5
    # Cap at 8% of equity — if Kelly says more, trust less.
    fraction = min(0.08, half_kelly)
    return {
        "sample_size": len(rows),
        "win_rate": p,
        "avg_win_loss_ratio": b,
        "full_kelly": full_kelly,
        "half_kelly": half_kelly,
        "fraction": fraction,
    }


def drawdown_state() -> dict[str, float | bool]:
    """Returns {peak, current, dd_pct, multiplier_active, multiplier}.

    Applied by signal.py to downsize every new trade when we're in drawdown.
    Hysteresis: triggers at ``dd_trigger_pct`` below peak, releases at
    ``dd_release_pct`` below peak — so we don't flap on small bounces.
    """
    # Lazy import to avoid executor ↔ risk circular at import time.
    from app.services import executor
    settings = get_settings()
    try:
        current = executor._paper_equity_usdt()
    except Exception:
        current = settings.paper_starting_equity_usdt
    peak = _peak_equity(current)
    dd_pct = ((peak - current) / peak) if peak > 0 else 0.0

    # Hysteresis: once triggered, stay active until we recover above release line.
    # Use the presence of a MacroIndicator row as the active flag.
    from app.models import MacroIndicator
    with get_session() as s:
        flag = s.get(MacroIndicator, "dd_active")
        was_active = flag is not None and flag.value > 0
    if not was_active and dd_pct >= settings.dd_trigger_pct:
        with get_session() as s:
            row = MacroIndicator(name="dd_active", value=1.0, source="internal",
                                 classification="triggered")
            s.add(row)
            s.commit()
        was_active = True
    elif was_active and dd_pct <= settings.dd_release_pct:
        with get_session() as s:
            row = s.get(MacroIndicator, "dd_active")
            if row is not None:
                s.delete(row)
                s.commit()
        was_active = False

    multiplier = settings.dd_size_mult if was_active else 1.0
    return {
        "peak_equity_usdt": peak,
        "current_equity_usdt": current,
        "dd_pct": dd_pct,
        "multiplier_active": was_active,
        "multiplier": multiplier,
    }


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
    # The 'other' bucket holds every symbol not in the hardcoded sector map.
    # Those are unrelated by design (MEXC auto-discovered memes / micro-caps),
    # NOT a correlation cluster. Capping it at 3 was vetoing the bulk of the
    # tradable universe. Skip the cap for 'other'.
    if sec != "other":
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
