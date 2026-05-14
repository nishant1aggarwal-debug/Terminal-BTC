"""Push notifications to your phone via ntfy.sh.

Why ntfy.sh: free, no signup, no API keys. You install the ntfy app on your
phone (iOS or Android), subscribe to a topic name (any secret-ish word), and
this module POSTs to ``https://ntfy.sh/<topic>`` every time the trading
system fires a meaningful event. The ntfy server holds the message just long
enough to push it to your subscribed phones; nothing is stored.

Setup:
  1. Install ntfy: https://docs.ntfy.sh/subscribe/phone/
  2. In the app, tap "Subscribe to topic" and pick anything, e.g.
     ``terminal-btc-<random-string>``.
  3. Set ``NTFY_TOPIC`` env var to that same string.
  4. (Optional) ``NTFY_SERVER`` if you self-host ntfy. Default: https://ntfy.sh.

Without ``NTFY_TOPIC`` set, this module is a no-op — keeps local dev quiet.
"""
from __future__ import annotations

import threading
import time
import urllib.request
from collections import deque
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


# Push hygiene state — kept in-process. Reset on every container boot.
#
# Why this exists: ntfy.sh free tier rate-limits at ~60 pushes/hour per IP.
# Without throttling, a single crash-loop or a hot ADD chain can blow
# through that in seconds — and then EVERY push for the rest of the hour
# returns HTTP 429, including the genuinely important ones.
_PUSH_LOCK = threading.Lock()
_LAST_PUSH_BY_KEY: dict[str, float] = {}      # (kind, symbol) → last sent monotonic ts
_PUSH_TIMESTAMPS: deque[float] = deque()      # rolling window of sent push times
_HOURLY_CAP = 40                              # max pushes per rolling hour (safe under 60)
_DEDUP_SECONDS = 240                          # don't push the same (kind, symbol) for 4 min
# Kinds we never push — they stay in the DB feed but don't fan out to the phone.
# INFO is housekeeping; RISK vetoes spam when a sector cap holds for hours.
# ADD is on the wire only when the underlying SIGNAL push got through (the
# SIGNAL alert itself already says "ADD #N", so a separate ADD push duplicates).
_NOPUSH_KINDS = {"INFO", "RISK", "ADD"}


def _should_send(kind: str, symbol: str) -> tuple[bool, str]:
    """Return (allowed, reason). Reasons are logged when we drop a push."""
    if kind in _NOPUSH_KINDS:
        return False, f"kind_{kind.lower()}_no_push"
    now = time.monotonic()
    with _PUSH_LOCK:
        # Drop pushes older than 1 h from the rolling window.
        cutoff = now - 3600.0
        while _PUSH_TIMESTAMPS and _PUSH_TIMESTAMPS[0] < cutoff:
            _PUSH_TIMESTAMPS.popleft()
        if len(_PUSH_TIMESTAMPS) >= _HOURLY_CAP:
            return False, f"hourly_cap_{_HOURLY_CAP}"
        key = f"{kind}|{symbol}"
        last = _LAST_PUSH_BY_KEY.get(key)
        if last is not None and (now - last) < _DEDUP_SECONDS:
            return False, f"dedup_{int(now - last)}s_ago"
        # Reserve a slot — caller commits on a successful send.
        _LAST_PUSH_BY_KEY[key] = now
        _PUSH_TIMESTAMPS.append(now)
    return True, ""


# Map our notification kinds to ntfy priority + tags so the phone surfaces
# trade signals more loudly than housekeeping events.
#   priority 1 = min, 2 = low, 3 = default, 4 = high, 5 = max (urgent + bypass DND)
_KIND_PRIORITY: dict[str, int] = {
    "SIGNAL": 5,   # NEW signal — copy to exchange ASAP
    "OPEN":   4,
    "ADD":    4,
    "TP1":    4,   # partial win
    "TP2":    5,   # full win — celebrate
    "SL":     5,   # stop hit — wake up
    "TRAIL":  4,
    "CLOSE":  3,
    "RISK":   2,
    "INFO":   2,
}
_KIND_TAGS: dict[str, str] = {
    "SIGNAL": "rotating_light,chart_with_upwards_trend",
    "OPEN":   "rocket",
    "ADD":    "heavy_plus_sign",
    "TP1":    "money_with_wings",
    "TP2":    "moneybag,white_check_mark",
    "SL":     "fire,bangbang",
    "TRAIL":  "chart_with_downwards_trend",
    "CLOSE":  "checkered_flag",
    "RISK":   "warning",
    "INFO":   "information_source",
}


def push_event(
    kind: str,
    symbol: str,
    title: str,
    message: str,
    *,
    action: str | None = None,
    price: float | None = None,
    sl: float | None = None,
    tp1: float | None = None,
    tp2: float | None = None,
    confidence: float | None = None,
    style: str | None = None,
) -> bool:
    """Send one push notification. Returns True on success, False on no-op/error.

    No-op when ``NTFY_TOPIC`` is unset, so it's safe to call from inside
    ``notifications.fire()`` without a flag check on every event.
    """
    settings = get_settings()
    topic = (settings.ntfy_topic or "").strip()
    if not topic:
        return False

    # Hygiene gate — dedup + hourly cap + drop noisy kinds. Without this the
    # ntfy.sh free tier (~60/h per IP) trips a 429 and silently swallows
    # every subsequent push for the rest of the hour, including SIGNAL/SL
    # ones we really want. Run BEFORE composing the body so we don't waste
    # work on a push we're about to drop.
    allowed, reason = _should_send(kind, symbol)
    if not allowed:
        log.info("ntfy_push_skipped", kind=kind, symbol=symbol, reason=reason)
        return False

    server = (settings.ntfy_server or "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"

    # Build the headers ntfy reads — title, priority, tags, optional click URL.
    priority = _KIND_PRIORITY.get(kind, 3)
    tags = _KIND_TAGS.get(kind, "bell")
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": str(priority),
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
    }
    # If the dashboard URL is configured, ntfy will make the notification
    # tappable straight to your live dashboard.
    if settings.dashboard_url:
        headers["Click"] = settings.dashboard_url

    # Build a phone-friendly message body. Structured Entry/SL/TP1/TP2 first,
    # then the reasoning text.
    lines: list[str] = []
    if action:
        lines.append(f"Action: {action.upper()}")
    if price is not None:
        lines.append(f"Entry:  {price}")
    if sl is not None:
        lines.append(f"SL:     {sl}")
    if tp1 is not None:
        lines.append(f"TP1:    {tp1}")
    if tp2 is not None:
        lines.append(f"TP2:    {tp2}")
    if confidence is not None:
        lines.append(f"Conf:   {int(confidence * 100)}%")
    if style:
        lines.append(f"Style:  {style}")
    if message:
        lines.append("")
        lines.append(message)

    body = ("\n".join(lines)).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        # 3 s ceiling — ntfy.sh normally answers in <200 ms; anything beyond
        # 3 s means the upstream is wedged and we'd rather drop the push than
        # block whoever called us. With the to_thread wrapping at the scheduler
        # site this can't stall the event loop anyway, but a tight ceiling
        # keeps total tick time bounded even when many alerts fire at once.
        with urllib.request.urlopen(req, timeout=3) as resp:
            ok = resp.status == 200
    except Exception as exc:
        log.warning("ntfy_push_failed", error=str(exc), topic=topic[:8] + "...")
        return False
    if ok:
        log.info("ntfy_push_sent", kind=kind, symbol=symbol)
    return ok


def test_push() -> dict[str, Any]:
    """Manual ping — for the /control/test-push endpoint to verify setup."""
    settings = get_settings()
    if not settings.ntfy_topic:
        return {"ok": False, "reason": "NTFY_TOPIC not set"}
    sent = push_event(
        "SIGNAL", "TEST/USDT", "Terminal-BTC test push",
        "If you see this on your phone, ntfy.sh is wired correctly.",
        action="buy", price=12345.67, sl=12000.0, tp1=12700.0, tp2=13000.0,
        confidence=0.99, style="TEST",
    )
    return {"ok": sent, "topic_hint": settings.ntfy_topic[:6] + "***"}
