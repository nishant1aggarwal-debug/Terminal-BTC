from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Decision(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=_utcnow, index=True)
    source: str = Field(index=True)  # "scheduler" | "tv"
    symbol: str = Field(index=True)
    timeframe: str
    snapshot_json: str
    action: str  # "buy" | "sell" | "hold"
    size_pct: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: Optional[float] = None
    reasoning: str = ""
    claude_usd_cost: float = 0.0


class Trade(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    decision_id: int = Field(foreign_key="decision.id", index=True)
    ts: datetime = Field(default_factory=_utcnow, index=True)
    symbol: str
    side: str  # "buy" | "sell"
    type: str = "market"
    amount: float
    price: Optional[float] = None
    client_order_id: str = Field(index=True, unique=True)
    exchange_order_id: Optional[str] = None
    status: str = "pending"  # pending | filled | rejected | dry
    filled_amount: float = 0.0
    avg_price: Optional[float] = None
    commission_usdt: float = 0.0
    slippage_usdt: float = 0.0
    raw_response: Optional[str] = None


class ClosedTrade(SQLModel, table=True):
    """A realized round-trip — entry and exit reconciled into one row.

    Emitted every time a fill brings a Position's qty across zero (either
    all the way flat, or into the opposite direction). Powers win-rate,
    profit-factor, and average-win/loss stats.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    closed_at: datetime = Field(default_factory=_utcnow, index=True)
    symbol: str = Field(index=True)
    side: str  # "long" | "short" (the direction of the position that was closed)
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: datetime
    gross_pnl_usdt: float
    commission_usdt: float = 0.0
    funding_usdt: float = 0.0
    net_pnl_usdt: float = 0.0
    pnl_pct: float = 0.0  # on entry notional
    hold_seconds: int = 0
    entry_decision_id: Optional[int] = None
    exit_decision_id: Optional[int] = None
    entry_confidence: Optional[float] = None


class Position(SQLModel, table=True):
    symbol: str = Field(primary_key=True)
    qty: float = 0.0
    avg_entry: float = 0.0
    opened_at: Optional[datetime] = None  # when qty went from 0 to non-zero
    opened_decision_id: Optional[int] = None
    opened_confidence: Optional[float] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class DailyPnL(SQLModel, table=True):
    day: date = Field(primary_key=True)
    realized_usdt: float = 0.0
    unrealized_usdt: float = 0.0
    claude_usd_spent: float = 0.0
    updated_at: datetime = Field(default_factory=_utcnow)


class KillSwitch(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    enabled: bool = False
    reason: str = ""
    updated_at: datetime = Field(default_factory=_utcnow)
