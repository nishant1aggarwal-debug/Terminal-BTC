"""Notifications — user-facing trade alerts that fire on every meaningful event.

Every notification is a row in the ``Notification`` table. The dashboard polls
``/api/notifications`` and renders them as a top-of-page feed plus (if the
user grants permission) a browser push notification — so the user can stay
productive in other tabs and still catch every signal the system fires.

Kinds (what ``kind`` means):
  * ``SIGNAL`` — a buy/sell decision crossed the confidence threshold. No
    position action yet; this is the "heads up".
  * ``OPEN``   — executor opened or scaled into a position.
  * ``ADD``    — a same-direction signal fired while a position was open, so
    executor pyramided in.
  * ``TP1`` / ``TP2`` / ``SL`` / ``TRAIL`` — target hit, partial or full close.
  * ``CLOSE``  — position fully closed (any reason).
  * ``RISK``   — a risk gate blocked a trade (kill switch, per-trade cap, etc.).
  * ``INFO``   — generic status messages (startup, backtest done, etc.).
"""
from __future__ import annotations

from typing import Any

from sqlmodel import desc, select

from app.db import get_session
from app.logging_setup import get_logger
from app.models import Notification

log = get_logger(__name__)

_VALID_KINDS = {
    "SIGNAL", "OPEN", "ADD", "CLOSE", "TP1", "TP2", "SL", "TRAIL", "RISK", "INFO",
}


def fire(
    kind: str,
    symbol: str,
    title: str,
    message: str = "",
    *,
    severity: str = "info",
    action: str | None = None,
    price: float | None = None,
    sl: float | None = None,
    tp1: float | None = None,
    tp2: float | None = None,
    confidence: float | None = None,
    style: str | None = None,
) -> Notification:
    if kind not in _VALID_KINDS:
        log.warning("unknown_notification_kind", kind=kind)
    n = Notification(
        kind=kind, symbol=symbol, title=title, message=message,
        severity=severity, action=action, price=price,
        sl=sl, tp1=tp1, tp2=tp2, confidence=confidence, style=style,
    )
    with get_session() as s:
        s.add(n)
        s.commit()
        s.refresh(n)
    log.info("notification_fired", kind=kind, symbol=symbol, title=title)

    # Fan out to ntfy.sh push (no-op when NTFY_TOPIC isn't set). Best-effort:
    # a phone-push failure must never block the in-DB notification.
    try:
        from app.services import push as _push
        _push.push_event(
            kind=kind, symbol=symbol, title=title, message=message,
            action=action, price=price, sl=sl, tp1=tp1, tp2=tp2,
            confidence=confidence, style=style,
        )
    except Exception as exc:
        log.warning("push_dispatch_failed", error=str(exc))

    return n


def _classify_style(entry: float, sl: float | None, htf_agrees: bool | None) -> str:
    """Tag a fresh signal as SCALP / SWING / LONG based on SL distance + HTF agreement.

    Distance is measured as |entry-sl| / entry. The buckets:
      * SCALP — stop within 0.6% of entry (tight, fast turnover, minutes-to-an-hour hold)
      * SWING — stop 0.6%-1.5% from entry (standard 15m setups, hours-to-a-day hold)
      * LONG  — stop > 1.5% from entry OR HTF strongly agrees (positional, days)
    """
    if entry is None or sl is None or entry <= 0:
        return "SWING"
    dist_pct = abs(entry - sl) / entry * 100.0
    base = "SCALP" if dist_pct < 0.6 else "SWING" if dist_pct < 1.5 else "LONG"
    # Upgrade SWING → LONG when higher-timeframe trend agrees — those setups
    # typically run longer because they're trading with the dominant move.
    if base == "SWING" and htf_agrees is True:
        return "LONG"
    return base


def signal_fired(
    symbol: str, action: str, confidence: float, entry: float,
    sl: float | None, tp1: float | None, tp2: float | None, reasoning: str,
    *, htf_agrees: bool | None = None,
) -> None:
    style = _classify_style(entry, sl, htf_agrees)
    verb = "LONG" if action == "buy" else "SHORT"
    title = f"[{style}] {verb} {symbol}"
    parts = [f"Entry {entry:.4f}"]
    if sl is not None:
        parts.append(f"SL {sl:.4f}")
    if tp1 is not None:
        parts.append(f"TP1 {tp1:.4f}")
    if tp2 is not None:
        parts.append(f"TP2 {tp2:.4f}")
    message = " · ".join(parts) + f" · conf {int(confidence * 100)}% · {reasoning}"
    fire(
        "SIGNAL", symbol, title, message,
        severity="success" if action == "buy" else "warning",
        action=action, price=entry, sl=sl, tp1=tp1, tp2=tp2,
        confidence=confidence, style=style,
    )


def position_opened(symbol: str, side: str, qty: float, entry: float,
                    sl: float | None = None, tp1: float | None = None,
                    tp2: float | None = None) -> None:
    verb = "Opened LONG" if side == "buy" else "Opened SHORT"
    title = f"{verb} {symbol}"
    message = f"qty {qty:.6f} @ {entry:.4f}"
    fire("OPEN", symbol, title, message, severity="success" if side == "buy" else "warning",
         action=side, price=entry, sl=sl, tp1=tp1, tp2=tp2)


def position_added(symbol: str, side: str, qty: float, price: float, add_index: int) -> None:
    verb = "Added to LONG" if side == "buy" else "Added to SHORT"
    title = f"{verb} {symbol} (#{add_index})"
    message = f"+{qty:.6f} @ {price:.4f}"
    fire("ADD", symbol, title, message, severity="success" if side == "buy" else "warning",
         action="add", price=price)


def target_hit(symbol: str, kind: str, price: float, qty: float, pnl: float) -> None:
    title = f"{kind} hit — {symbol}"
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    message = f"exit {qty:.6f} @ {price:.4f} · {pnl_str}"
    sev = "success" if pnl > 0 else "danger" if pnl < 0 else "info"
    fire(kind, symbol, title, message, severity=sev, action="close", price=price)


def position_closed(symbol: str, side: str, qty: float, exit_price: float, pnl: float) -> None:
    title = f"Closed {symbol}"
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    message = f"{side} {qty:.6f} exited @ {exit_price:.4f} · net {pnl_str}"
    fire("CLOSE", symbol, title, message, severity="success" if pnl >= 0 else "danger",
         action="close", price=exit_price)


def risk_veto(symbol: str, action: str, reason: str) -> None:
    fire("RISK", symbol, f"Blocked {action.upper()} {symbol}", reason, severity="warning",
         action=action)


def list_recent(limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
    with get_session() as s:
        q = select(Notification).order_by(desc(Notification.ts)).limit(limit)
        if unread_only:
            q = select(Notification).where(Notification.read == False).order_by(desc(Notification.ts)).limit(limit)  # noqa: E712
        rows = s.exec(q).all()
    return [
        {
            "id": n.id,
            "ts": n.ts.isoformat(),
            "kind": n.kind,
            "severity": n.severity,
            "symbol": n.symbol,
            "title": n.title,
            "message": n.message,
            "action": n.action,
            "price": n.price,
            "sl": n.sl,
            "tp1": n.tp1,
            "tp2": n.tp2,
            "confidence": n.confidence,
            "style": n.style,
            "read": n.read,
        }
        for n in rows
    ]


def mark_all_read() -> int:
    with get_session() as s:
        rows = s.exec(select(Notification).where(Notification.read == False)).all()  # noqa: E712
        count = 0
        for n in rows:
            n.read = True
            s.add(n)
            count += 1
        s.commit()
    return count


def unread_count() -> int:
    with get_session() as s:
        rows = s.exec(select(Notification).where(Notification.read == False)).all()  # noqa: E712
        return len(rows)
