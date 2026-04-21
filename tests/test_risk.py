from __future__ import annotations

from app.services import risk


def test_hold_always_ok():
    a = risk.check(action="hold", symbol="BTC/USDT", notional_usdt=0)
    assert a.ok


def test_symbol_allowlist():
    a = risk.check(action="buy", symbol="DOGE/USDT", notional_usdt=10)
    assert not a.ok
    assert "symbol_not_allowed" in a.reason


def test_per_trade_cap():
    a = risk.check(action="buy", symbol="BTC/USDT", notional_usdt=10_000)
    assert not a.ok
    assert "per_trade_cap" in a.reason


def test_kill_switch_blocks():
    risk.set_kill_switch(True, "manual")
    try:
        a = risk.check(action="buy", symbol="BTC/USDT", notional_usdt=10)
        assert not a.ok
        assert "kill_switch" in a.reason
    finally:
        risk.set_kill_switch(False, "")


def test_approved_within_limits():
    a = risk.check(action="buy", symbol="BTC/USDT", notional_usdt=10)
    assert a.ok
    assert a.reason == "approved"
