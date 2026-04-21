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

    # Binance (optional — only needed when PAPER_MODE=false)
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

    # Trading params
    trade_symbol: str = "BTC/USDT"
    trade_market: str = "spot"  # "spot" | "futures"
    trade_timeframe: str = "15m"
    poll_interval_sec: int = 300

    # Risk
    max_position_usdt: float = 50.0
    max_daily_loss_usdt: float = 25.0
    max_open_positions: int = 1
    symbol_allowlist: str = "BTC/USDT,ETH/USDT"

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

    @property
    def allowed_symbols(self) -> list[str]:
        return [s.strip() for s in self.symbol_allowlist.split(",") if s.strip()]

    @property
    def real_orders_enabled(self) -> bool:
        """Real orders require: paper_mode=False AND live_trading=True AND testnet=False."""
        return (not self.paper_mode) and self.live_trading and (not self.binance_testnet)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
