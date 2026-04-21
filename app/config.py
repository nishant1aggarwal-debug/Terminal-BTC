from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-7"
    claude_max_tokens: int = 1024
    daily_claude_usd_cap: float = 5.0

    # Binance
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    # Safety gates
    live_trading: bool = False

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

    @property
    def allowed_symbols(self) -> list[str]:
        return [s.strip() for s in self.symbol_allowlist.split(",") if s.strip()]

    @property
    def real_orders_enabled(self) -> bool:
        """Real orders require BOTH flags: live_trading=True AND testnet=False."""
        return self.live_trading and not self.binance_testnet


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
