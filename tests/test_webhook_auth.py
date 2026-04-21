from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_webhook_rejects_bad_secret():
    with TestClient(app) as client:
        r = client.post(
            "/tv/webhook",
            json={"secret": "WRONG", "symbol": "BTC/USDT", "price": 60000.0},
        )
    assert r.status_code == 401


def test_webhook_accepts_good_secret():
    with TestClient(app) as client:
        r = client.post(
            "/tv/webhook",
            json={
                "secret": "unit-test-secret",
                "symbol": "BTC/USDT",
                "price": 60000.0,
                "alert": "ema_bull_cross",
                "tf": "15m",
            },
        )
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert body["queued"] is True


def test_status_endpoint_reports_safety_flags():
    with TestClient(app) as client:
        r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["testnet"] is True
    assert body["live_trading"] is False
    assert body["real_orders_enabled"] is False
