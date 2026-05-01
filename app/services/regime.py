"""BTC 4h regime classifier — bull / bear / chop.

Every 15 minutes we read BTC 4h candles, compute EMA 20/50/200 + ADX + F&G,
and write a ``MacroIndicator`` row with ``name="regime"`` and
``classification`` in {"bull","bear","chop"}.

Rules engine reads this on every tick and:
  * BULL  — only long entries fire; shorts get demoted to hold.
  * BEAR  — only short entries fire; longs get demoted to hold.
  * CHOP  — both sides fire but size is halved (regime uncertainty = smaller bet).

Why BTC 4h: the whole crypto market correlates with BTC, and 4h is the
de-facto regime timeframe on TradingView. 1h is too noisy; 1d too slow.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator

from app.db import get_session
from app.exchange import data_source
from app.logging_setup import get_logger
from app.models import MacroIndicator

log = get_logger(__name__)


def classify_regime() -> dict[str, Any]:
    """Fetch BTC 4h + F&G and return {regime, reason, ema20, ema50, ema200, adx, fg}.

    Doesn't write — caller wraps this in ``refresh_regime()`` to persist.
    """
    try:
        ohlcv = data_source.fetch_ohlcv("BTC/USDT", timeframe="4h", limit=300)
    except Exception as exc:
        log.warning("regime_fetch_failed", error=str(exc))
        return {"regime": "chop", "reason": f"fetch failed: {exc}"}

    if not ohlcv or len(ohlcv) < 200:
        return {"regime": "chop", "reason": "insufficient history"}

    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    close = df["close"]
    ema20 = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
    ema50 = EMAIndicator(close=close, window=50).ema_indicator().iloc[-1]
    ema200 = EMAIndicator(close=close, window=200).ema_indicator().iloc[-1]
    adx = ADXIndicator(high=df["high"], low=df["low"], close=close, window=14).adx().iloc[-1]

    with get_session() as s:
        fg_row = s.get(MacroIndicator, "fear_greed")
    fg = fg_row.value if fg_row is not None else 50.0

    trending = float(adx) >= 20.0
    bullish_stack = ema20 > ema50 > ema200
    bearish_stack = ema20 < ema50 < ema200

    if trending and bullish_stack and fg >= 40:
        regime = "bull"
        reason = f"4h EMA20>50>200, ADX {adx:.1f}, F&G {fg:.0f}"
    elif trending and bearish_stack and fg <= 60:
        regime = "bear"
        reason = f"4h EMA20<50<200, ADX {adx:.1f}, F&G {fg:.0f}"
    else:
        regime = "chop"
        bits = []
        if not trending: bits.append(f"ADX {adx:.1f}<20")
        if not bullish_stack and not bearish_stack: bits.append("EMAs tangled")
        reason = "chop: " + (", ".join(bits) if bits else "regime unclear")

    return {
        "regime": regime,
        "reason": reason,
        "ema20": float(ema20),
        "ema50": float(ema50),
        "ema200": float(ema200),
        "adx": float(adx),
        "fg": float(fg),
    }


def refresh_regime() -> dict[str, Any] | None:
    result = classify_regime()
    now = datetime.now(timezone.utc)
    with get_session() as s:
        row = s.get(MacroIndicator, "regime")
        if row is None:
            # Numeric value: bull=1, bear=-1, chop=0 (for easy querying).
            row = MacroIndicator(name="regime", value=0.0)
        row.value = {"bull": 1.0, "bear": -1.0, "chop": 0.0}[result["regime"]]
        row.classification = result["regime"]
        row.source = "btc_4h_ema_adx_fg"
        row.fetched_at = now
        import json as _json
        row.raw = _json.dumps(result, default=str)
        s.add(row)
        s.commit()
    log.info("regime_updated", regime=result["regime"], reason=result["reason"])
    return result


def current_regime() -> str:
    """Cheap lookup for the rules engine. Returns 'bull'|'bear'|'chop'."""
    with get_session() as s:
        row = s.get(MacroIndicator, "regime")
    if row is None or not row.classification:
        return "chop"
    return row.classification
