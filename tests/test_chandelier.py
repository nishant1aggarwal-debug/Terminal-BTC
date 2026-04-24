"""Chandelier Exit trailing-stop math tests."""
from __future__ import annotations

from unittest.mock import patch

from app.models import Position
from app.services import targets


def _pos(side: str, entry: float, water: float) -> Position:
    qty = 1.0 if side == "long" else -1.0
    return Position(
        symbol="BTC/USDT", qty=qty, avg_entry=entry, initial_qty=1.0,
        tp1_hit=True, trailing_high_water=water,
    )


def test_long_chandelier_triggers_below_trail():
    """22-bar high 110, ATR 2, mult 3 → stop = 110 − 6 = 104. Price 103 → triggered."""
    pos = _pos("long", entry=100.0, water=108.0)
    with patch.object(
        targets, "__name__", targets.__name__,
    ):  # ensure module is loaded
        with patch(
            "app.services.market_data.get_recent_extremes",
            return_value={"high": 110.0, "low": 95.0, "atr": 2.0},
        ):
            stop, triggered = targets._update_trailing(pos, price=103.0)
    assert stop == 104.0
    assert triggered is True


def test_long_chandelier_stops_ratchet_up_not_down():
    """After the high hits 120, stop goes to 114. A later bar with high 118 still keeps stop >= 114."""
    pos = _pos("long", entry=100.0, water=120.0)
    with patch(
        "app.services.market_data.get_recent_extremes",
        return_value={"high": 118.0, "low": 95.0, "atr": 2.0},
    ):
        stop, triggered = targets._update_trailing(pos, price=115.0)
    # New raw stop would be 118 - 6 = 112; but ratchet says max(raw, entry) → still 112 (above entry 100).
    # But pos.trailing_high_water ratchets up: max(120, 118, 115) = 120.
    assert pos.trailing_high_water == 120.0
    assert stop == max(120.0 - 6.0, 100.0)  # 114


def test_short_chandelier_triggers_above_trail():
    """22-bar low 80, ATR 2, mult 3 → stop = 80 + 6 = 86. Price 87 → triggered."""
    pos = _pos("short", entry=90.0, water=82.0)
    with patch(
        "app.services.market_data.get_recent_extremes",
        return_value={"high": 95.0, "low": 80.0, "atr": 2.0},
    ):
        stop, triggered = targets._update_trailing(pos, price=87.0)
    assert stop == 86.0
    assert triggered is True


def test_chandelier_inactive_before_tp1():
    """If tp1_hit is False, the function returns (None, False) — no trailing."""
    pos = _pos("long", entry=100.0, water=105.0)
    pos.tp1_hit = False
    stop, triggered = targets._update_trailing(pos, price=103.0)
    assert stop is None
    assert triggered is False


def test_chandelier_falls_back_on_fetch_failure():
    """If get_recent_extremes returns None, we fall back to a breakeven-locked stop."""
    pos = _pos("long", entry=100.0, water=110.0)
    with patch(
        "app.services.market_data.get_recent_extremes",
        return_value=None,
    ):
        stop, triggered = targets._update_trailing(pos, price=99.0)
    # Fallback gives us SOME stop and a sensible triggered flag.
    assert stop is not None
    # At price 99, below entry 100 → the fallback min/max clamping means we
    # get a stop at or near breakeven (100); 99 < 100 → triggered.
    assert triggered is True
