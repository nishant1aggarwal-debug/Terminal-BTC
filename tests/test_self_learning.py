"""Phase A — per-indicator + per-regime self-learning.

Covers the contract end-to-end:
  * rules_signal returns per-indicator contributions + dominant_indicator + regime
  * auditor's weight_* overrides actually shrink the corresponding indicator
  * regime-scoped overrides only fire when the current regime matches
  * indicator + regime stats aggregations bin correctly
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.db import get_session
from app.models import (
    ClosedTrade,
    MacroIndicator,
    StrategyOverride,
)
from app.services import auditor, rules_signal


def _bull_snapshot() -> dict:
    """Synthetic snapshot that fires a strong long: EMA stack + MACD + RSI agree."""
    return {
        "symbol": "BTC/USDT", "last_close": 100.0, "atr_14": 1.0,
        "spread_bps": 5.0, "adx_14": 30.0,
        "rsi_14": 60.0,
        "macd": 0.5, "macd_signal": 0.3, "macd_hist": 0.2,
        "ema_20": 102.0, "ema_50": 101.0, "ema_200": 99.0,
        "bb_upper": 105.0, "bb_middle": 100.0, "bb_lower": 95.0, "bb_pct": 0.75,
        "stoch_rsi_k": 35.0, "stoch_rsi_d": 30.0,
    }


def _set_regime(name: str) -> None:
    with get_session() as s:
        row = s.get(MacroIndicator, "regime")
        if row is None:
            row = MacroIndicator(name="regime", value=0.0)
        row.value = {"bull": 1.0, "bear": -1.0, "chop": 0.0}[name]
        row.classification = name
        row.source = "test"
        s.add(row)
        s.commit()


def test_contributions_and_dominant_indicator_returned():
    d = rules_signal.generate_decision(_bull_snapshot())
    assert d.action == "buy"
    # All five indicators contribute (non-zero), and EMA stack should dominate at +0.30.
    assert set(d.contributions.keys()) == {"ema", "macd", "rsi", "stoch_rsi", "bb"}
    assert d.dominant_indicator == "ema"
    assert d.regime in {"bull", "bear", "chop"}


def test_weight_override_halves_indicator_contribution():
    """A weight_ema=0.5 override should halve the EMA contribution to 0.15."""
    now = datetime.now(timezone.utc)
    with get_session() as s:
        s.add(StrategyOverride(
            symbol="BTC/USDT", param_key="weight_ema", param_value="0.5",
            reason="test", created_at=now, expires_at=now + timedelta(hours=1),
            source="test",
        ))
        s.commit()
    d = rules_signal.generate_decision(_bull_snapshot())
    assert abs(d.contributions["ema"] - 0.15) < 1e-6
    # Other indicators are unchanged (default weight 1.0).
    assert abs(d.contributions["rsi"] - 0.15) < 1e-6


def test_regime_scoped_override_only_applies_in_matching_regime():
    """disable_long with regime=bear should NOT fire in a bull regime."""
    now = datetime.now(timezone.utc)
    with get_session() as s:
        s.add(StrategyOverride(
            symbol="BTC/USDT", param_key="disable_long", param_value="true",
            reason="bear-only block", regime="bear",
            created_at=now, expires_at=now + timedelta(hours=1),
            source="test",
        ))
        s.commit()
    _set_regime("bull")
    d = rules_signal.generate_decision(_bull_snapshot())
    assert d.action == "buy"  # bear-scoped override doesn't bite in bull regime
    _set_regime("bear")
    d = rules_signal.generate_decision(_bull_snapshot())
    assert d.action == "hold"  # now it does


def test_per_indicator_and_per_regime_stats_bin_correctly():
    """Auditor's aggregations correctly group trades by indicator and (regime, side)."""
    now = datetime.now(timezone.utc)
    with get_session() as s:
        # 3 losing trades dominated by EMA (in bull regime), 3 winning trades by RSI
        for i in range(3):
            s.add(ClosedTrade(
                closed_at=now, symbol="BTC/USDT", side="long", qty=1.0,
                entry_price=100.0, exit_price=99.0, entry_ts=now,
                gross_pnl_usdt=-1.0, net_pnl_usdt=-1.0, pnl_pct=-1.0,
                hold_seconds=60, entry_contributions_json=json.dumps({"ema": 0.30}),
                entry_dominant_indicator="ema", entry_regime="bull",
            ))
        for i in range(3):
            s.add(ClosedTrade(
                closed_at=now, symbol="BTC/USDT", side="long", qty=1.0,
                entry_price=100.0, exit_price=102.0, entry_ts=now,
                gross_pnl_usdt=2.0, net_pnl_usdt=2.0, pnl_pct=2.0,
                hold_seconds=60, entry_contributions_json=json.dumps({"rsi": 0.15}),
                entry_dominant_indicator="rsi", entry_regime="bull",
            ))
        s.commit()
        trades = s.exec(select(ClosedTrade)).all()

    ind_stats = auditor._per_indicator_stats(list(trades))
    reg_stats = auditor._per_regime_stats(list(trades))

    assert "BTC/USDT|ema" in ind_stats
    assert ind_stats["BTC/USDT|ema"]["trades"] == 3
    assert ind_stats["BTC/USDT|ema"]["win_rate_pct"] == 0.0  # all losers
    assert ind_stats["BTC/USDT|rsi"]["win_rate_pct"] == 100.0  # all winners

    assert "BTC/USDT|bull|long" in reg_stats
    assert reg_stats["BTC/USDT|bull|long"]["trades"] == 6  # 3 wins + 3 losses


def test_local_proposals_produce_weight_and_regime_overrides():
    """End-to-end: feed losing-indicator stats into _local_proposals and verify
    a weight_ema=0.5 plus a regime-scoped disable_long are emitted.
    """
    indicator_stats = {
        "BTC/USDT|ema": {"symbol": "BTC/USDT", "indicator": "ema", "trades": 6,
                          "wins": 1, "win_rate_pct": 16.67, "pnl_usdt": -10.0},
    }
    regime_stats = {
        "BTC/USDT|bull|long": {"symbol": "BTC/USDT", "regime": "bull", "side": "long",
                                "trades": 6, "wins": 1, "win_rate_pct": 16.67,
                                "pnl_usdt": -10.0},
    }
    props = auditor._local_proposals(
        stats_by_symbol={}, backtest_by_symbol={},
        indicator_stats=indicator_stats, regime_stats=regime_stats,
    )
    keys = {(p.param_key, p.regime) for p in props}
    assert ("weight_ema", None) in keys           # global indicator weight cut
    assert ("disable_long", "bull") in keys       # bull-scoped direction disable
