"""Macro-regime indicators — currently just Crypto Fear & Greed Index.

Polls https://api.alternative.me/fng/ (free, no key) every
``MACRO_POLL_MIN`` minutes and upserts the latest value into
``MacroIndicator``. Used by the rules engine to dampen confidence when the
market is at an emotional extreme.

  value  0-24   = extreme fear
         25-44  = fear
         45-55  = neutral
         56-74  = greed
         75-100 = extreme greed
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import MacroIndicator

log = get_logger(__name__)


def _fetch_fear_greed() -> dict[str, Any] | None:
    url = get_settings().fear_greed_api_url
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.load(r)
    except Exception as exc:
        log.warning("fear_greed_fetch_failed", error=str(exc))
        return None
    data = payload.get("data") or []
    if not data:
        return None
    row = data[0]
    try:
        return {
            "value": float(row["value"]),
            "classification": row.get("value_classification", ""),
            "raw": json.dumps(row),
        }
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("fear_greed_parse_failed", error=str(exc))
        return None


def refresh_fear_greed() -> dict[str, Any] | None:
    """Fetch Fear & Greed and upsert into MacroIndicator.

    Returns a plain dict (not the ORM row) so callers can safely serialize
    it after the session closes — attached SQLModel instances raise
    DetachedInstanceError when accessed outside their session.
    """
    parsed = _fetch_fear_greed()
    if parsed is None:
        return None
    now = datetime.now(timezone.utc)
    with get_session() as s:
        row = s.get(MacroIndicator, "fear_greed")
        if row is None:
            row = MacroIndicator(name="fear_greed")
        row.value = parsed["value"]
        row.classification = parsed["classification"]
        row.source = "alternative.me"
        row.fetched_at = now
        row.raw = parsed["raw"]
        s.add(row)
        s.commit()
    log.info("fear_greed_updated", value=parsed["value"], classification=parsed["classification"])
    return {
        "name": "fear_greed",
        "value": parsed["value"],
        "classification": parsed["classification"],
        "source": "alternative.me",
        "fetched_at": now.isoformat(),
    }


def get_latest(name: str = "fear_greed") -> MacroIndicator | None:
    with get_session() as s:
        return s.get(MacroIndicator, name)


def confidence_adjustment(action: str, value: float | None) -> float:
    """Returns a multiplier in [0.7, 1.15] to apply to a signal's confidence.

    Contrarian bias: when greed is extreme, long entries get dampened and
    short entries get a small boost (and vice versa).
    """
    if value is None:
        return 1.0
    if action == "buy":
        if value >= 80:  # extreme greed — don't chase longs
            return 0.75
        if value <= 20:  # extreme fear — buy the dip
            return 1.15
    elif action == "sell":
        if value <= 20:
            return 0.75
        if value >= 80:
            return 1.15
    return 1.0
