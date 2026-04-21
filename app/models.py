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
    raw_response: Optional[str] = None


class Position(SQLModel, table=True):
    symbol: str = Field(primary_key=True)
    qty: float = 0.0
    avg_entry: float = 0.0
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
