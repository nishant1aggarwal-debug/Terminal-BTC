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

    # Trading params — wide alt universe. Alts have 2-5x the beta of BTC/ETH,
    # so capturing their moves during macro trends is where the edge lives.
    # Grouped by sector so it's easy to prune:
    #   Majors    — BTC/ETH/SOL/XRP
    #   Top-10    — DOGE/ADA/AVAX/LINK/BNB/TON/TRX/LTC
    #   High-vol  — DOT/MATIC/NEAR/APT/ATOM/FIL/HBAR
    #   L2/new-L1 — ARB/OP/SUI/SEI/INJ/TIA
    #   DeFi blue-chip — UNI/AAVE/MKR/CRV/LDO
    #   Legacy    — XLM/ALGO/XTZ/ETC
    #   Metaverse/gaming (high-beta) — SAND/MANA/AXS
    # 34 pairs total. Kraken doesn't list TRX/MATIC/NEAR/APT in USDT —
    # data_source.filter_supported() drops those at runtime so backtest
    # and tick see ~30 actively tradable symbols there.
    trade_symbols: str = (
        "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,"
        "DOGE/USDT,ADA/USDT,AVAX/USDT,LINK/USDT,BNB/USDT,TON/USDT,TRX/USDT,LTC/USDT,"
        "DOT/USDT,MATIC/USDT,NEAR/USDT,APT/USDT,ATOM/USDT,FIL/USDT,HBAR/USDT,"
        "ARB/USDT,OP/USDT,SUI/USDT,SEI/USDT,INJ/USDT,TIA/USDT,"
        "UNI/USDT,AAVE/USDT,MKR/USDT,CRV/USDT,LDO/USDT,"
        "XLM/USDT,ALGO/USDT,XTZ/USDT,ETC/USDT,"
        "SAND/USDT,MANA/USDT,AXS/USDT"
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
    max_open_positions: int = 15

    # Signal threshold: composite multi-indicator confidence must exceed this
    # for a buy/sell to fire. 0.55 is permissive, 0.70 is strict. Lower =
    # more trades.
    min_signal_confidence: float = 0.55

    # Minimum TP2 distance as a fraction of entry price. With 10x leverage a
    # 5% move = 50% return on margin — comfortably outpaces 0.04% × 2 taker
    # fees + funding. Set lower (0.02) for scalping; higher (0.10) for
    # positional swings only. SL stays ATR-anchored; only TP2 (and TP1 = ½ TP2)
    # use this floor.
    min_tp2_pct: float = 0.05
    # Hard filter: refuses anything outside this list even if TradingView sends it.
    # Superset of TRADE_SYMBOLS plus meme/retail pairs you can opt into by adding
    # to TRADE_SYMBOLS. 50+ pairs covering essentially every high-volume USDT market.
    symbol_allowlist: str = (
        "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,"
        "DOGE/USDT,ADA/USDT,AVAX/USDT,LINK/USDT,BNB/USDT,TON/USDT,TRX/USDT,LTC/USDT,"
        "DOT/USDT,MATIC/USDT,NEAR/USDT,APT/USDT,ATOM/USDT,FIL/USDT,HBAR/USDT,"
        "ARB/USDT,OP/USDT,SUI/USDT,SEI/USDT,INJ/USDT,TIA/USDT,"
        "UNI/USDT,AAVE/USDT,MKR/USDT,CRV/USDT,LDO/USDT,COMP/USDT,SNX/USDT,"
        "XLM/USDT,ALGO/USDT,XTZ/USDT,ETC/USDT,ICP/USDT,FLOW/USDT,KSM/USDT,EGLD/USDT,"
        "SAND/USDT,MANA/USDT,AXS/USDT,IMX/USDT,GALA/USDT,APE/USDT,CHZ/USDT,"
        "RUNE/USDT,GRT/USDT,RNDR/USDT,FET/USDT,THETA/USDT,"
        "SHIB/USDT,PEPE/USDT,WIF/USDT,BONK/USDT,FLOKI/USDT,JUP/USDT,PYTH/USDT,JTO/USDT,ORDI/USDT"
    )

    # Auto-discover the trade universe at startup from the data source's listed
    # USDT pairs. Set true when DATA_SOURCE is a wide-universe exchange (mexc,
    # bitget, gateio) and you want 50-200+ pairs instead of the hardcoded 14.
    auto_discover_symbols: bool = False
    # 25 keeps Render free-tier CPU happy. 60 saturated the event loop during
    # ticks and caused /healthz to time out (server_failed loop). Bump back up
    # if you upgrade to Render Starter or run locally.
    auto_discover_top_n: int = 25

    # Macro context — Fear & Greed pulled every MACRO_POLL_MIN minutes.
    # When F&G is extreme (>80 greed or <20 fear), the rules engine
    # dampens same-direction confidence and boosts contrarian confidence.
    macro_poll_min: int = 15
    fear_greed_api_url: str = "https://api.alternative.me/fng/?limit=1"

    # News & sentiment — CryptoPanic free tier, no key required.
    news_poll_min: int = 15
    news_sentiment_window_hours: int = 3
    news_sentiment_max_adj: float = 0.10  # max confidence multiplier impact

    # Backtester — replays rules_signal against historical candles to measure
    # out-of-sample win rate / profit factor before promoting to live.
    backtest_candles: int = 500          # how many bars back per symbol
    backtest_hour_utc: int = 3           # nightly job fires at this UTC hour
    backtest_timeframe: str = ""         # empty = use trade_timeframe

    # Strategy auditor (the self-improving loop). Runs nightly, reads recent
    # closed trades + backtest + macro state, and writes per-symbol
    # StrategyOverride rows ("disable_long", "size_multiplier", etc.). The
    # rules engine applies active overrides on every tick, so the system
    # learns from its own results without human input.
    auditor_hour_utc: int = 4                  # cron hour (after backtest at 03:00)
    auditor_window_trades: int = 50            # inspect last N closed trades per symbol
    auditor_max_overrides_per_run: int = 12    # safety cap so a bad run can't flood
    auditor_override_hours: int = 24           # how long each override stays active
    # Rule-based thresholds (local auditor, no Claude needed):
    auditor_loss_win_rate_pct: float = 30.0    # < this → tighten (disable or shrink)
    auditor_win_win_rate_pct: float = 65.0     # > this → loosen (boost size)
    auditor_min_trades: int = 5                # need at least N trades to act
    # Claude augmentation (optional). When ANTHROPIC_API_KEY is set, the
    # auditor also asks Claude for suggestions beyond the local rules.
    auditor_use_claude: bool = True            # honoured only if key is present

    # Walk-forward parameter optimizer (Phase B of the self-learning loop).
    # Runs weekly, picks the best (confidence_adj, size_multiplier) per symbol
    # by replaying the rules engine on OOS windows and scoring PF × sqrt(trades).
    # Winners are written as StrategyOverride rows (source="optimizer") and
    # live for ``optimizer_override_hours`` (default 1 week = 168h).
    optimizer_day_of_week: str = "sun"   # APScheduler cron day_of_week token
    optimizer_hour_utc: int = 5          # 05:15 UTC = after auditor at 04:00
    optimizer_max_symbols: int = 8       # top-N symbols by recent backtest
    optimizer_override_hours: int = 168  # 1 week — refresh on the next run

    # Multi-timeframe confirmation. Signal fires only when the 1h trend agrees
    # with the 15m setup — dramatically cuts bad entries in whipsawing markets.
    htf_confirmation: bool = True
    htf_timeframe: str = "1h"

    # Sector cluster cap: limits how many concurrent positions can sit in the
    # same high-correlation bucket. Prevents one BTC dump from taking out 6
    # correlated alts at once.
    max_positions_per_sector: int = 3

    # Drawdown circuit breaker — halves position size when equity falls too
    # far below its all-time peak, releases once recovered. Keeps losing
    # streaks from compounding at full size.
    dd_trigger_pct: float = 0.10      # halve size when equity < peak * (1 - 0.10)
    dd_release_pct: float = 0.05      # release multiplier once equity >= peak * (1 - 0.05)
    dd_size_mult: float = 0.5         # size multiplier when breaker is active

    # Chandelier Exit (ATR-based trailing stop — standard on TradingView).
    # Replaces the crude "50% of favorable move" trailing stop once TP1 hits.
    chandelier_period: int = 22       # how many bars back for the high/low
    chandelier_mult: float = 3.0      # N × ATR distance from the high/low

    # Dashboard authentication — leave both empty for open mode (local dev).
    # Set BOTH in production so /ui/, /api/*, /control/* require basic auth.
    # Healthchecks (/healthz, /readyz) stay public regardless.
    dashboard_user: str = ""
    dashboard_pass: str = ""
    # Public URL for the dashboard — used by push notifications so taps on
    # phone notifications open the live dashboard. Leave blank to omit.
    dashboard_url: str = "https://terminal-btc.onrender.com/ui/"

    # Phone push notifications via ntfy.sh — install the ntfy app, subscribe
    # to whatever topic name you put in NTFY_TOPIC, and every SIGNAL / OPEN /
    # TP1 / TP2 / SL / CLOSE fires a push to your phone. Leave blank to disable.
    # NTFY_SERVER defaults to the public ntfy.sh service — change only if you
    # self-host (some users on https://github.com/binwiederhier/ntfy).
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"

    # Daily digest email — sends an HTML P&L summary at DIGEST_HOUR_UTC if all
    # SMTP fields are set. Any missing field → job no-ops (logged, no error).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    digest_from: str = ""
    digest_to: str = ""
    digest_hour_utc: int = 23  # 23:55 UTC default
    digest_minute_utc: int = 55

    # Live execution exchange selector. "binance" routes orders through
    # binance_client; "bybit" through bybit_client. Paper mode ignores this.
    trade_exchange: str = "bybit"
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = False

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
        # Must mirror data_source._SUPPORTED. Exchanges added here become valid
        # DATA_SOURCE env values; pydantic rejects unknown ones at startup.
        allowed = {"kraken", "mexc", "bitget", "gateio", "okx", "htx",
                   "bybit", "binance"}
        if v not in allowed:
            raise ValueError(f"data_source must be one of {sorted(allowed)}")
        return v

    @field_validator("trade_exchange")
    @classmethod
    def _check_trade_exchange(cls, v: str) -> str:
        v = v.lower()
        if v not in {"binance", "bybit"}:
            raise ValueError("trade_exchange must be 'binance' or 'bybit'")
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
