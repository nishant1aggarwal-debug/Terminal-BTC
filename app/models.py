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
    # Multi-target exit plan stored at open time. Monitored every tick.
    sl_price: Optional[float] = None
    tp1_price: Optional[float] = None
    tp1_hit: bool = False
    tp2_price: Optional[float] = None
    tp2_hit: bool = False
    trailing_high_water: Optional[float] = None  # tracked once TP1 hits; used for trailing SL
    initial_qty: float = 0.0                     # original size, for scaling partial exits
    adds: int = 0                                # how many pyramid adds so far (0, 1, 2, ...)
    updated_at: datetime = Field(default_factory=_utcnow)


class Notification(SQLModel, table=True):
    """User-facing alert — a new row per fire. Drives the dashboard toast feed
    and the optional browser push so the user can copy the call to a real
    exchange without watching the page.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=_utcnow, index=True)
    kind: str = Field(index=True)  # SIGNAL | OPEN | CLOSE | ADD | TP1 | TP2 | SL | TRAIL | RISK | INFO
    severity: str = "info"  # info | success | warning | danger
    symbol: str = Field(index=True)
    title: str
    message: str = ""
    action: Optional[str] = None  # buy | sell | close | add
    price: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    confidence: Optional[float] = None
    style: Optional[str] = None  # "SCALP" | "SWING" | "LONG"  — set on SIGNAL kind
    read: bool = Field(default=False, index=True)


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


class MacroIndicator(SQLModel, table=True):
    """Latest value for a macro/market-regime indicator (fear&greed, DXY, etc.).

    One row per `name` — upserted on every fetch. We keep only the latest so
    the dashboard has an O(1) read.
    """
    name: str = Field(primary_key=True)  # e.g. "fear_greed"
    value: float
    classification: str = ""
    source: str = ""
    fetched_at: datetime = Field(default_factory=_utcnow)
    raw: Optional[str] = None


class NewsEvent(SQLModel, table=True):
    """One crypto news post per row — ingested from CryptoPanic.

    ``sentiment_score`` is a [-1, +1] float derived from CryptoPanic's own
    vote breakdown (positive / negative / important). ``currencies`` is a
    comma-joined list of symbols the post mentions (e.g., "BTC,ETH").
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    external_id: str = Field(index=True, unique=True)
    ts: datetime = Field(default_factory=_utcnow, index=True)
    title: str
    url: str = ""
    source: str = "cryptopanic"
    currencies: str = Field(default="", index=True)
    sentiment_score: float = 0.0
    raw: Optional[str] = None


class BacktestReport(SQLModel, table=True):
    """Result of replaying the rules engine over historical candles for one symbol.

    Includes both in-sample (full window) and out-of-sample (walk-forward)
    metrics so users can spot curve-fitted strategies. A healthy signal has
    IS and OOS within ~20 percentage points; a big gap means the strategy
    over-fits recent data.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    timeframe: str
    generated_at: datetime = Field(default_factory=_utcnow, index=True)
    candles: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    profit_factor: Optional[float] = None  # None = undefined (zero losses, zero wins)
    net_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_hold_minutes: float = 0.0
    # Walk-forward out-of-sample metrics (3 rolling 60/40 train/test windows).
    # NULL until at least one walk-forward run completes.
    is_win_rate_pct: Optional[float] = None
    is_profit_factor: Optional[float] = None
    oos_trades: Optional[int] = None
    oos_win_rate_pct: Optional[float] = None
    oos_profit_factor: Optional[float] = None
    oos_net_pnl_pct: Optional[float] = None
    params_json: str = "{}"  # the strategy params used (for future A/B)
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None


class StrategyOverride(SQLModel, table=True):
    """Per-symbol strategy tweak the auditor writes after reviewing outcomes.

    The rules engine reads any active (non-expired) override for a symbol
    on every tick and applies it BEFORE composing its score. Overrides are
    the learning loop — the system's way of saying "last week I lost money
    shorting ADA in low-ADX regimes, so skip it for 24h".

    Valid param_key values:
      * ``disable_long``   ("true"/"false") — refuse long entries for this symbol
      * ``disable_short``  ("true"/"false") — refuse short entries
      * ``size_multiplier`` (float 0.25..1.75) — scale position size
      * ``confidence_adj`` (float ±0.20) — shift the threshold: +0.1 makes
                                            the engine MORE selective on this
                                            symbol; -0.1 makes it looser
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    param_key: str = Field(index=True)
    param_value: str
    reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    expires_at: datetime = Field(index=True)
    source: str = "local"  # "local" (rule-based) | "claude" | "manual"
    audit_id: Optional[int] = Field(default=None, foreign_key="auditreport.id")
    applied_count: int = 0


class AuditReport(SQLModel, table=True):
    """One row per audit run — whether local or Claude-assisted."""
    id: Optional[int] = Field(default=None, primary_key=True)
    generated_at: datetime = Field(default_factory=_utcnow, index=True)
    source: str = "local"  # "local" | "claude"
    window_trades: int = 0
    summary: str = ""
    overrides_proposed: int = 0
    overrides_stored: int = 0
    claude_usd_cost: float = 0.0
    raw_json: Optional[str] = None
