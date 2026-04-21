from __future__ import annotations

from sqlmodel import select

from app.db import get_session
from app.models import Decision, Position, Trade
from app.services import executor


def _make_decision(action: str = "buy") -> int:
    with get_session() as s:
        d = Decision(
            source="scheduler",
            symbol="BTC/USDT",
            timeframe="15m",
            snapshot_json="{}",
            action=action,
            size_pct=0.1,
            stop_loss=0.0,
            take_profit=0.0,
            confidence=0.8,
            reasoning="test",
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def test_dry_run_records_trade_without_calling_exchange(monkeypatch):
    # Stub equity so notional > 0, and make sure create_order is NOT called.
    from app.exchange import binance_client

    monkeypatch.setattr(binance_client, "quote_equity_usdt", lambda: 1000.0)

    def _boom(*a, **kw):
        raise AssertionError("create_order must NOT be called in dry-run mode")

    monkeypatch.setattr(binance_client, "create_order", _boom)

    decision_id = _make_decision("buy")
    result = executor.execute(
        decision_id=decision_id,
        symbol="BTC/USDT",
        action="buy",
        size_pct=0.05,  # 50 USDT @ cap
        last_price=60_000.0,
    )
    assert result.ok
    assert result.status == "dry"

    with get_session() as s:
        trades = s.exec(select(Trade).where(Trade.decision_id == decision_id)).all()
        assert len(trades) == 1
        assert trades[0].status == "dry"
        pos = s.get(Position, "BTC/USDT")
        assert pos is not None
        assert pos.qty > 0


def test_idempotent_replay_same_decision(monkeypatch):
    from app.exchange import binance_client

    monkeypatch.setattr(binance_client, "quote_equity_usdt", lambda: 1000.0)
    monkeypatch.setattr(binance_client, "create_order", lambda **kw: {})

    decision_id = _make_decision("buy")
    r1 = executor.execute(decision_id, "BTC/USDT", "buy", 0.05, 60_000.0)
    r2 = executor.execute(decision_id, "BTC/USDT", "buy", 0.05, 60_000.0)
    assert r1.ok and r2.ok
    assert r1.trade_id == r2.trade_id


def test_hold_noop():
    r = executor.execute(1, "BTC/USDT", "hold", 0.0, 60_000.0)
    assert r.ok
    assert r.status == "hold"
