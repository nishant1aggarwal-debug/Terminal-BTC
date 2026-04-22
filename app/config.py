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
    paper_starting_equity_usdt: float = 1_000.0

    # TradingView
    tradingview_webhook_secret: str = "change-me"

    # Trading params — 16 high-volume USDT pairs swept every tick.
    trade_symbols: str = (
        "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,ADA/USDT,AVAX/USDT,LINK/USDT,"
        "BNB/USDT,TON/USDT,TRX/USDT,LTC/USDT,DOT/USDT,MATIC/USDT,NEAR/USDT,APT/USDT"
    )
    trade_market: str = "spot"  # "spot" | "futures"
    trade_timeframe: str = "15m"
    poll_interval_sec: int = 300

    # Risk
    max_position_usdt: float = 50.0
    max_daily_loss_usdt: float = 25.0
    max_open_positions: int = 5
    # Hard filter: refuses anything outside this list even if TradingView sends it.
    # Covers the 16 actively traded + 9 extra high-volume alts (ATOM, UNI, FIL, ARB, OP,
    # SUI, SEI, INJ, HBAR) you can promote by adding them to TRADE_SYMBOLS.
    symbol_allowlist: str = (
        "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,ADA/USDT,AVAX/USDT,LINK/USDT,"
        "BNB/USDT,TON/USDT,TRX/USDT,LTC/USDT,DOT/USDT,MATIC/USDT,NEAR/USDT,APT/USDT,"
        "ATOM/USDT,UNI/USDT,FIL/USDT,ARB/USDT,OP/USDT,SUI/USDT,SEI/USDT,INJ/USDT,HBAR/USDT"
    )

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
