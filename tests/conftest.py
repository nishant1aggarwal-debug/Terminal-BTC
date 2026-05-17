from __future__ import annotations

import os
import tempfile

import pytest

# Before importing the app, point it at a throwaway SQLite DB per test session.
_tmpdir = tempfile.mkdtemp(prefix="terminal_btc_tests_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmpdir}/test.db")
os.environ.setdefault("PAPER_MODE", "true")
os.environ.setdefault("LIVE_TRADING", "false")
os.environ.setdefault("SIGNAL_MODE", "rules")
os.environ.setdefault("TRADINGVIEW_WEBHOOK_SECRET", "unit-test-secret")
os.environ.setdefault("SYMBOL_ALLOWLIST", "BTC/USDT,ETH/USDT")


@pytest.fixture(autouse=True)
def _reset_db():
    from app.config import get_settings
    from app.db import engine, get_session, init_db
    from sqlmodel import SQLModel

    get_settings.cache_clear()
    SQLModel.metadata.drop_all(engine)
    init_db()
    # Seed a trending (bull) regime. In production the regime job populates
    # this every 15 min; without a row regime.current_regime() defaults to
    # "chop", which the block_entries_in_chop gate would treat as "no fresh
    # entries" — coupling every signal test to the regime gate. Tests that
    # specifically exercise regime behaviour set their own row, which runs
    # after this fixture and overrides it.
    from app.models import MacroIndicator
    with get_session() as s:
        s.add(MacroIndicator(name="regime", value=1.0,
                              classification="bull", source="test"))
        s.commit()
    yield
