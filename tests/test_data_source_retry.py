"""Tests for the ccxt retry wrapper in data_source."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import ccxt
import pytest

from app.exchange import data_source


def test_retries_then_succeeds(monkeypatch):
    """RateLimitExceeded twice, then success on the third call."""
    monkeypatch.setattr(data_source.time, "sleep", lambda _s: None)  # no real waits
    fake_client = MagicMock()
    fake_client.fetch_ohlcv.side_effect = [
        ccxt.RateLimitExceeded("rate limit"),
        ccxt.DDoSProtection("cloudfront"),
        [[1, 2, 3, 4, 5, 6]],
    ]
    with patch.object(data_source, "get_client", return_value=fake_client):
        out = data_source.fetch_ohlcv("BTC/USDT", "15m", 50)
    assert out == [[1, 2, 3, 4, 5, 6]]
    assert fake_client.fetch_ohlcv.call_count == 3


def test_gives_up_after_max_attempts(monkeypatch):
    """Four consecutive failures — the third retry still errors, propagated."""
    monkeypatch.setattr(data_source.time, "sleep", lambda _s: None)
    fake_client = MagicMock()
    fake_client.fetch_ohlcv.side_effect = ccxt.RateLimitExceeded("forever")
    with patch.object(data_source, "get_client", return_value=fake_client):
        with pytest.raises(ccxt.RateLimitExceeded):
            data_source.fetch_ohlcv("BTC/USDT", "15m", 50)
    assert fake_client.fetch_ohlcv.call_count == 3


def test_non_retryable_errors_propagate_immediately(monkeypatch):
    """A non-network error (e.g., BadSymbol) bypasses retry."""
    monkeypatch.setattr(data_source.time, "sleep", lambda _s: None)
    fake_client = MagicMock()
    fake_client.fetch_ohlcv.side_effect = ccxt.BadSymbol("bogus")
    with patch.object(data_source, "get_client", return_value=fake_client):
        with pytest.raises(ccxt.BadSymbol):
            data_source.fetch_ohlcv("BOGUS/USDT", "15m", 50)
    assert fake_client.fetch_ohlcv.call_count == 1
