from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Anthropic (optional — only needed when SIGNAL_MODE=claude)
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-7"
    claude_max_tokens: int = 1024
    daily_claude_usd_cap: float = 5.0

    # Signal generator: "rules" (default, no keys) | "claude" (requires ANTHROPIC_API_KEY) | "hold"
    signal_mode: str = "rules"

    # Public market data source (no keys required). "bybit" | "binance" | "kraken".
    data_source: str = "bybit"

    # Binance (optional — only needed when PAPER_MODE=false AND you're executing on Binance)
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    # Safety gates
    # PAPER_MODE=true  -> no real orders ever, no API keys required. Uses real public data.
    # To hit an exchange you must BOTH set PAPER_MODE=false AND LIVE_TRADING=true.
    paper_mode: bool = True
    live_trading: bool = False

    # Paper portfolio starting equity (USDT). Only used when PAPER_MODE=true.
    paper_starting_equity_usdt: float = 5_000.0

    # Futures-only (ignored when TRADE_MARKET=spot).
    # Cross vs isolated margin: in paper mode this is informational + surfaced in the UI.
    # In live mode, ccxt applies it to new positions.
    margin_mode: str = "isolated"  # "cross" | "isolated"
    leverage: float = 10.0         # Bybit futures default for this system

    # Per-trade risk cap: fraction of equity risked on stop-loss distance.
    # e.g. risk_per_trade_pct=0.01 + 2% stop-distance -> 50% notional (capped by
    # max_position_usdt). Signals that don't set a stop fall back to size_pct.
    risk_per_trade_pct: float = 0.01  # 1% of equity risked per trade

    # TradingView
    tradingview_webhook_secret: str = "change-me"

    # Trading params — 16 high-volume USDT pairs swept every tick.
    trade_symbols: str = (
        "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,ADA/USDT,AVAX/USDT,LINK/USDT,"
        "BNB/USDT,TON/USDT,TRX/USDT,LTC/USDT,DOT/USDT,MATIC/USDT,NEAR/USDT,APT/USDT"
    )
    trade_market: str = "futures"  # "spot" | "futures"
    trade_timeframe: str = "15m"
    poll_interval_sec: int = 300

    # Realistic paper-trading fees (match real Binance taker rates by default).
    # bps = basis points (1 bp = 0.01%).
    fee_spot_bps: float = 10.0       # 0.10% taker — Binance spot default
    fee_futures_bps: float = 4.0     # 0.04% taker — Binance USDⓈ-M futures default
    slippage_bps: float = 2.0        # 0.02% adverse price slip on market orders
    # Futures funding — only applied when TRADE_MARKET=futures. Binance charges
    # funding every 8h (00:00, 08:00, 16:00 UTC); we approximate with a flat rate.
    funding_rate_8h_bps: float = 1.0  # 0.01% of notional every 8 hours

    # Risk — Bybit-futures-style defaults: 8 concurrent positions, 10x leverage,
    # $500 max notional per position (= ~1 BTC on 10x from $5000 margin × leverage).
    max_position_usdt: float = 500.0
    max_daily_loss_usdt: float = 250.0
    max_open_positions: int = 8

    # Signal threshold: composite multi-indicator confidence must exceed this
    # for a buy/sell to fire. 0.55 is permissive, 0.70 is strict. Lower =
    # more trades.
    min_signal_confidence: float = 0.55
    # Hard filter: refuses anything outside this list even if TradingView sends it.
    # Covers the 16 actively traded + 9 extra high-volume alts (ATOM, UNI, FIL, ARB, OP,
    # SUI, SEI, INJ, HBAR) you can promote by adding them to TRADE_SYMBOLS.
    symbol_allowlist: str = (
        "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,ADA/USDT,AVAX/USDT,LINK/USDT,"
        "BNB/USDT,TON/USDT,TRX/USDT,LTC/USDT,DOT/USDT,MATIC/USDT,NEAR/USDT,APT/USDT,"
        "ATOM/USDT,UNI/USDT,FIL/USDT,ARB/USDT,OP/USDT,SUI/USDT,SEI/USDT,INJ/USDT,HBAR/USDT"
    )

    # Macro context — Fear & Greed pulled every MACRO_POLL_MIN minutes.
    # When F&G is extreme (>80 greed or <20 fear), the rules engine
    # dampens same-direction confidence and boosts contrarian confidence.
    macro_poll_min: int = 15
    fear_greed_api_url: str = "https://api.alternative.me/fng/?limit=1"

    # Backtester — replays rules_signal against historical candles to measure
    # out-of-sample win rate / profit factor before promoting to live.
    backtest_candles: int = 500          # how many bars back per symbol
    backtest_hour_utc: int = 3           # nightly job fires at this UTC hour
    backtest_timeframe: str = ""         # empty = use trade_timeframe

    # Infra
    database_url: str = "sqlite:///data/terminal_btc.db"
    log_level: str = "INFO"

    @field_validator("trade_market")
    @classmethod
    def _check_market(cls, v: str) -> str:
        v = v.lower()
        if v not in {"spot", "futures"}:
            raise ValueError("trade_market must be 'spot' or 'futures'")
        return v

    @field_validator("signal_mode")
    @classmethod
    def _check_signal_mode(cls, v: str) -> str:
        v = v.lower()
        if v not in {"rules", "claude", "hold"}:
            raise ValueError("signal_mode must be 'rules', 'claude', or 'hold'")
        return v

    @field_validator("data_source")
    @classmethod
    def _check_data_source(cls, v: str) -> str:
        v = v.lower()
        if v not in {"bybit", "binance", "kraken"}:
            raise ValueError("data_source must be 'bybit', 'binance', or 'kraken'")
        return v

    @field_validator("margin_mode")
    @classmethod
    def _check_margin_mode(cls, v: str) -> str:
        v = v.lower()
        if v not in {"cross", "isolated"}:
            raise ValueError("margin_mode must be 'cross' or 'isolated'")
        return v

    @field_validator("leverage")
    @classmethod
    def _check_leverage(cls, v: float) -> float:
        if v < 1 or v > 125:
            raise ValueError("leverage must be between 1 and 125")
        return v

    @property
    def allowed_symbols(self) -> list[str]:
        return [s.strip() for s in self.symbol_allowlist.split(",") if s.strip()]

    @property
    def symbols(self) -> list[str]:
        return [s.strip() for s in self.trade_symbols.split(",") if s.strip()]

    @property
    def real_orders_enabled(self) -> bool:
        """Real orders require: paper_mode=False AND live_trading=True AND testnet=False."""
        return (not self.paper_mode) and self.live_trading and (not self.binance_testnet)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
