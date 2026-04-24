"""Drawdown circuit breaker tests — hysteresis + signal-path integration."""
from __future__ import annotations

import pytest

from app.services import risk


@pytest.fixture
def _in_memory_db(monkeypatch, tmp_path):
    """Isolate each test to its own SQLite file so fixtures don't leak."""
    db_path = tmp_path / "dd.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    get_settings.cache_clear()
    # Re-import db + models so the new engine is built against our DATABASE_URL.
    import importlib
    import app.db
    importlib.reload(app.db)
    app.db.init_db()
    yield
    get_settings.cache_clear()


def test_peak_ratchets_up_and_triggers_on_dd(_in_memory_db, monkeypatch):
    # Sim equity trajectory: 5000 → 5400 → 5400 (peak locked) → 4800 (11.1% DD → triggers).
    equities = iter([5000.0, 5400.0, 5400.0, 4800.0])

    def fake_equity():
        return next(equities)

    monkeypatch.setattr("app.services.executor._paper_equity_usdt", fake_equity)

    state = risk.drawdown_state(); assert state["peak_equity_usdt"] == 5000
    state = risk.drawdown_state(); assert state["peak_equity_usdt"] == 5400
    state = risk.drawdown_state(); assert state["multiplier_active"] is False
    state = risk.drawdown_state()
    assert state["multiplier_active"] is True
    assert state["multiplier"] == 0.5
    assert state["dd_pct"] > 0.10


def test_release_after_recovery(_in_memory_db, monkeypatch):
    # Push equity up, then down past the trigger, then back up past the release.
    equities = iter([5000.0, 6000.0, 5200.0, 5800.0])

    def fake_equity():
        return next(equities)

    monkeypatch.setattr("app.services.executor._paper_equity_usdt", fake_equity)

    risk.drawdown_state()  # peak 5000
    risk.drawdown_state()  # peak 6000
    dd = risk.drawdown_state()  # 5200 → 13.3% drawdown → active
    assert dd["multiplier_active"] is True

    dd = risk.drawdown_state()  # 5800 → 3.3% drawdown, below release threshold
    assert dd["multiplier_active"] is False
    assert dd["multiplier"] == 1.0
