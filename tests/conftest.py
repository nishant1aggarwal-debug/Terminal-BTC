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
    from app.db import engine, init_db
    from sqlmodel import SQLModel

    get_settings.cache_clear()
    SQLModel.metadata.drop_all(engine)
    init_db()
    yield
