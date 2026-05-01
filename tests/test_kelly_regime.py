"""Kelly-criterion and regime-classifier tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.services import regime, risk


@pytest.fixture
def _in_memory_db(monkeypatch, tmp_path):
    db_path = tmp_path / "kelly.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    get_settings.cache_clear()
    import importlib
    import app.db
    importlib.reload(app.db)
    app.db.init_db()
    yield
    get_settings.cache_clear()


def _seed_closed_trades(wins: int, losses: int, avg_win: float, avg_loss: float):
    """Insert a synthetic ClosedTrade history with known win-rate & avg P/L."""
    from app.db import get_session
    from app.models import ClosedTrade
    base = datetime.now(timezone.utc) - timedelta(hours=wins + losses)
    with get_session() as s:
        for i in range(wins):
            s.add(ClosedTrade(
                closed_at=base + timedelta(minutes=i), symbol="BTC/USDT", side="long",
                qty=1.0, entry_price=100.0, exit_price=100.0 + avg_win,
                entry_ts=base + timedelta(minutes=i),
                gross_pnl_usdt=avg_win, net_pnl_usdt=avg_win, pnl_pct=avg_win,
            ))
        for i in range(losses):
            s.add(ClosedTrade(
                closed_at=base + timedelta(minutes=wins + i), symbol="ETH/USDT", side="short",
                qty=1.0, entry_price=100.0, exit_price=100.0 - avg_loss,
                entry_ts=base + timedelta(minutes=wins + i),
                gross_pnl_usdt=-avg_loss, net_pnl_usdt=-avg_loss, pnl_pct=-avg_loss,
            ))
        s.commit()


def test_kelly_returns_none_below_min_trades(_in_memory_db):
    _seed_closed_trades(wins=20, losses=10, avg_win=2.0, avg_loss=1.0)
    assert risk.kelly_fraction(min_trades=50) is None


def test_kelly_known_win_rate_and_ratio(_in_memory_db):
    """30 wins (avg +$2) / 20 losses (avg -$1) → p=0.6, b=2.0 → full Kelly = 0.4."""
    _seed_closed_trades(wins=30, losses=20, avg_win=2.0, avg_loss=1.0)
    k = risk.kelly_fraction(min_trades=50)
    assert k is not None
    assert k["sample_size"] == 50
    assert abs(k["win_rate"] - 0.6) < 0.01
    assert abs(k["avg_win_loss_ratio"] - 2.0) < 0.01
    # Full Kelly: (0.6 * 2 - 0.4) / 2 = 0.4
    assert abs(k["full_kelly"] - 0.4) < 0.01
    # Half-Kelly = 0.2; capped at 8%
    assert k["fraction"] == 0.08


def test_kelly_bad_strategy_returns_zero(_in_memory_db):
    """Loser strategy: 10 wins / 40 losses → negative Kelly → fraction=0."""
    _seed_closed_trades(wins=10, losses=40, avg_win=1.0, avg_loss=1.0)
    k = risk.kelly_fraction(min_trades=50)
    assert k is not None
    assert k["full_kelly"] < 0
    assert k["fraction"] == 0.0


def test_regime_chop_when_emas_tangled():
    """When EMA20=EMA50=EMA200 (no trend), regime = chop regardless of ADX."""
    fake_ohlcv = [[1, 100, 100, 100, 100, 1000]] * 250
    with patch("app.exchange.data_source.fetch_ohlcv", return_value=fake_ohlcv):
        # Tangled EMAs at flat price; ADX should be near 0
        result = regime.classify_regime()
    # All EMAs equal → not bullish-stack, not bearish-stack → chop
    assert result["regime"] == "chop"


def test_regime_bull_when_uptrending_and_high_adx(_in_memory_db):
    """Synthetic strong uptrend: bullish stack + high ADX + neutral F&G."""
    # Generate a clean uptrend so EMA20>50>200 and ADX is high.
    candles = []
    base_price = 100.0
    for i in range(300):
        price = base_price + i * 0.5
        candles.append([i * 60_000, price - 0.1, price + 0.5, price - 0.5, price, 1000])
    with patch("app.exchange.data_source.fetch_ohlcv", return_value=candles):
        result = regime.classify_regime()
    assert result["regime"] == "bull"
    assert result["ema20"] > result["ema50"] > result["ema200"]
