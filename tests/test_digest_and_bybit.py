"""Daily digest renderer + Bybit-vs-Binance routing tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services import email_digest, executor


def test_digest_html_contains_pnl():
    data = {
        "date": "2026-04-23",
        "starting_equity_usdt": 5000.0,
        "realized_pnl_usdt": 42.50,
        "open_positions": [{"symbol": "BTC/USDT", "side": "long",
                            "qty": 0.001, "avg_entry": 78000.0}],
        "closed_today_count": 5,
        "wins": 3,
        "losses": 2,
        "win_rate_pct": 60.0,
        "top_signals": [{"symbol": "ETH/USDT", "action": "buy",
                         "confidence": 0.72, "reasoning": "trend up"}],
    }
    html = email_digest.render_digest_html(data)
    assert "+$42.50" in html
    assert "BTC/USDT" in html
    assert "long" in html
    assert "60.0%" in html
    text = email_digest.render_digest_text(data)
    assert "+$42.50" in text
    assert "BTC/USDT" in text


def test_digest_skips_when_smtp_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/dig.db")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    import importlib, app.db
    importlib.reload(app.db)
    app.db.init_db()
    result = email_digest.send_digest()
    assert result["ok"] is False
    assert result["reason"] == "smtp_not_configured"
    get_settings.cache_clear()


def test_executor_uses_bybit_client_when_configured(monkeypatch):
    """When TRADE_EXCHANGE=bybit, _live_client returns the bybit module."""
    monkeypatch.setenv("TRADE_EXCHANGE", "bybit")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.exchange import bybit_client
    assert executor._live_client() is bybit_client
    get_settings.cache_clear()


def test_executor_uses_binance_client_when_configured(monkeypatch):
    monkeypatch.setenv("TRADE_EXCHANGE", "binance")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.exchange import binance_client
    assert executor._live_client() is binance_client
    get_settings.cache_clear()
