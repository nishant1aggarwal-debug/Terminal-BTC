"""Crypto news & sentiment ingestion — CryptoPanic free tier, no key required.

Polls ``https://cryptopanic.com/api/free/v1/posts/?public=true`` every
``NEWS_POLL_MIN`` minutes and upserts ``NewsEvent`` rows. Each post's
sentiment is derived from the vote breakdown they publish:

    score = (positive - negative + 0.5 * important) /
            max(1, positive + negative + important + 1)

in [-1, +1]. Positive floods (listings, partnerships) push the score
up; fud/hack stories push it down. We decay older events exponentially
when computing the per-symbol sentiment for the signal engine.

Graceful degradation: if the CryptoPanic fetch fails (network / rate
limit / API change), no notifications are fired, the existing rows
stay, and the next scheduler tick retries. Never blocks signals.
"""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import desc, select

from app.db import get_session, iso_utc
from app.logging_setup import get_logger
from app.models import NewsEvent

log = get_logger(__name__)

_ENDPOINT = "https://cryptopanic.com/api/free/v1/posts/?public=true"


def _vote_sentiment(votes: dict[str, Any]) -> float:
    pos = int(votes.get("positive") or 0)
    neg = int(votes.get("negative") or 0)
    imp = int(votes.get("important") or 0)
    total = pos + neg + imp
    if total == 0:
        return 0.0
    score = (pos - neg + 0.5 * imp) / (total + 1)
    return max(-1.0, min(1.0, score))


def _extract_currencies(post: dict[str, Any]) -> list[str]:
    """Return a list of currency codes mentioned in the post (e.g., ['BTC', 'ETH'])."""
    currencies = post.get("currencies") or []
    out: list[str] = []
    for c in currencies:
        code = (c.get("code") or "").upper()
        if code:
            out.append(code)
    return out


def refresh_news(limit: int = 40) -> int:
    """Fetch latest news and upsert. Returns count of NEW rows."""
    try:
        with urllib.request.urlopen(_ENDPOINT, timeout=10) as r:
            payload = json.load(r)
    except Exception as exc:
        log.warning("news_fetch_failed", error=str(exc))
        return 0

    results = payload.get("results") or []
    new_count = 0
    with get_session() as s:
        for post in results[:limit]:
            ext_id = str(post.get("id") or "")
            if not ext_id:
                continue
            existing = s.exec(select(NewsEvent).where(NewsEvent.external_id == ext_id)).first()
            if existing is not None:
                continue
            currencies = _extract_currencies(post)
            sentiment = _vote_sentiment(post.get("votes") or {})
            published = post.get("published_at") or post.get("created_at")
            try:
                ts = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else datetime.now(timezone.utc)
            except (ValueError, AttributeError):
                ts = datetime.now(timezone.utc)
            row = NewsEvent(
                external_id=ext_id,
                ts=ts,
                title=(post.get("title") or "")[:500],
                url=(post.get("url") or "")[:500],
                source="cryptopanic",
                currencies=",".join(currencies),
                sentiment_score=sentiment,
                raw=json.dumps(post, default=str)[:5000],
            )
            s.add(row)
            new_count += 1
        if new_count:
            s.commit()
    log.info("news_updated", new_rows=new_count, total_fetched=len(results))
    return new_count


def symbol_sentiment(symbol: str, hours: int = 3, half_life_minutes: int = 60) -> float:
    """Return the recency-weighted sentiment for a symbol over the last ``hours``.

    Each news item is weighted by ``0.5 ** (age_min / half_life_min)`` — a 1h
    half-life means a headline 2h old counts a quarter as much as a fresh one.
    Output is clamped to [-1, +1].
    """
    code = symbol.split("/")[0].upper()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with get_session() as s:
        rows = s.exec(
            select(NewsEvent)
            .where(NewsEvent.ts >= cutoff)
            .order_by(desc(NewsEvent.ts))
            .limit(500)
        ).all()

    now = datetime.now(timezone.utc)
    numer = 0.0
    denom = 0.0
    for r in rows:
        if code not in (r.currencies or "").split(","):
            continue
        # Postgres returns naive datetimes; attach UTC so subtraction works.
        r_ts = r.ts if r.ts.tzinfo is not None else r.ts.replace(tzinfo=timezone.utc)
        age_min = max(0.0, (now - r_ts).total_seconds() / 60.0)
        weight = 0.5 ** (age_min / max(1, half_life_minutes))
        numer += r.sentiment_score * weight
        denom += weight
    if denom == 0:
        return 0.0
    return max(-1.0, min(1.0, numer / denom))


def recent_news(limit: int = 30) -> list[dict[str, Any]]:
    with get_session() as s:
        rows = s.exec(select(NewsEvent).order_by(desc(NewsEvent.ts)).limit(limit)).all()
    return [
        {
            "id": r.id,
            "ts": iso_utc(r.ts),
            "title": r.title,
            "url": r.url,
            "currencies": r.currencies.split(",") if r.currencies else [],
            "sentiment_score": r.sentiment_score,
            "source": r.source,
        }
        for r in rows
    ]
