"""Daily P&L digest email — sent at 23:55 UTC if SMTP is configured.

Pulls today's stats from the same sources the dashboard uses (overview,
positions, closed-trades, signals) and renders an HTML+plain-text email
the user can review without opening the dashboard.

If any SMTP env var is missing the job no-ops cleanly — perfect for
deployments that haven't wired email yet.
"""
from __future__ import annotations

import smtplib
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from sqlmodel import desc, select

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import ClosedTrade, DailyPnL, Decision, Position

log = get_logger(__name__)


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _gather() -> dict[str, Any]:
    """Pull all the bits the digest renders from."""
    settings = get_settings()
    today = _today_utc()
    today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)

    with get_session() as s:
        pnl = s.get(DailyPnL, today)
        positions = s.exec(select(Position)).all()
        closed_today = s.exec(
            select(ClosedTrade)
            .where(ClosedTrade.closed_at >= today_start)
            .order_by(desc(ClosedTrade.closed_at))
        ).all()
        signals_today = s.exec(
            select(Decision)
            .where(Decision.ts >= today_start)
            .where(Decision.action != "hold")
            .order_by(desc(Decision.confidence))
            .limit(5)
        ).all()

    open_positions = [
        {"symbol": p.symbol, "side": "long" if p.qty > 0 else "short",
         "qty": p.qty, "avg_entry": p.avg_entry}
        for p in positions if abs(p.qty) > 1e-9
    ]
    realized = pnl.realized_usdt if pnl else 0.0
    wins = sum(1 for t in closed_today if t.net_pnl_usdt > 0)
    losses = sum(1 for t in closed_today if t.net_pnl_usdt < 0)
    return {
        "date": today.isoformat(),
        "starting_equity_usdt": settings.paper_starting_equity_usdt,
        "realized_pnl_usdt": realized,
        "open_positions": open_positions,
        "closed_today_count": len(closed_today),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (wins / len(closed_today) * 100.0) if closed_today else 0.0,
        "top_signals": [
            {"symbol": d.symbol, "action": d.action,
             "confidence": d.confidence, "reasoning": d.reasoning}
            for d in signals_today
        ],
    }


def render_digest_html(data: dict[str, Any]) -> str:
    pnl = data["realized_pnl_usdt"]
    pnl_color = "#4ade80" if pnl >= 0 else "#f87171"
    pnl_sign = "+" if pnl >= 0 else ""

    pos_rows = "".join(
        f"<tr><td>{p['symbol']}</td><td>{p['side']}</td>"
        f"<td>{p['qty']:.6f}</td><td>${p['avg_entry']:.4f}</td></tr>"
        for p in data["open_positions"]
    ) or "<tr><td colspan=4 style='color:#888;text-align:center'>flat</td></tr>"

    sig_rows = "".join(
        f"<tr><td>{s['symbol']}</td><td>{s['action'].upper()}</td>"
        f"<td>{(s['confidence'] or 0) * 100:.0f}%</td>"
        f"<td style='color:#888'>{(s['reasoning'] or '')[:80]}</td></tr>"
        for s in data["top_signals"]
    ) or "<tr><td colspan=4 style='color:#888;text-align:center'>no actionable signals today</td></tr>"

    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,sans-serif;background:#0b0e14;color:#e5e9f0;padding:20px">
  <h2 style="margin:0 0 4px 0">Terminal-BTC daily digest — {data['date']}</h2>
  <p style="color:#7d8699;margin:0 0 20px 0">Auto-trading paper P&amp;L summary.</p>
  <div style="display:flex;gap:12px;margin-bottom:24px">
    <div style="background:#131823;border:1px solid #232a3a;border-radius:8px;padding:14px 18px;flex:1">
      <div style="color:#7d8699;font-size:11px;text-transform:uppercase">Realized P&amp;L today</div>
      <div style="font-size:24px;font-weight:600;color:{pnl_color}">{pnl_sign}${pnl:.2f}</div>
    </div>
    <div style="background:#131823;border:1px solid #232a3a;border-radius:8px;padding:14px 18px;flex:1">
      <div style="color:#7d8699;font-size:11px;text-transform:uppercase">Trades closed</div>
      <div style="font-size:24px;font-weight:600">{data['closed_today_count']}</div>
      <div style="color:#7d8699;font-size:11px">{data['wins']}W · {data['losses']}L · {data['win_rate_pct']:.1f}% win rate</div>
    </div>
    <div style="background:#131823;border:1px solid #232a3a;border-radius:8px;padding:14px 18px;flex:1">
      <div style="color:#7d8699;font-size:11px;text-transform:uppercase">Open positions</div>
      <div style="font-size:24px;font-weight:600">{len(data['open_positions'])}</div>
    </div>
  </div>
  <h3 style="margin:24px 0 8px 0">Open positions</h3>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#7d8699"><th align=left>Symbol</th><th align=left>Side</th><th align=left>Qty</th><th align=left>Entry</th></tr></thead>
    <tbody>{pos_rows}</tbody>
  </table>
  <h3 style="margin:24px 0 8px 0">Top signals today</h3>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#7d8699"><th align=left>Symbol</th><th align=left>Side</th><th align=left>Conf</th><th align=left>Why</th></tr></thead>
    <tbody>{sig_rows}</tbody>
  </table>
</body></html>"""


def render_digest_text(data: dict[str, Any]) -> str:
    """Plain-text fallback for clients that don't render HTML."""
    pnl = data["realized_pnl_usdt"]
    lines = [
        f"Terminal-BTC daily digest — {data['date']}",
        "",
        f"Realized P&L:    {'+' if pnl >= 0 else ''}${pnl:.2f}",
        f"Trades closed:    {data['closed_today_count']} ({data['wins']}W · {data['losses']}L · {data['win_rate_pct']:.1f}% WR)",
        f"Open positions:   {len(data['open_positions'])}",
        "",
        "Open positions:",
    ]
    for p in data["open_positions"]:
        lines.append(f"  {p['symbol']:<10} {p['side']:<5} {p['qty']:.6f} @ ${p['avg_entry']:.4f}")
    lines.append("")
    lines.append("Top signals today:")
    for s in data["top_signals"]:
        lines.append(f"  {s['symbol']:<10} {s['action'].upper():<5} conf {(s['confidence'] or 0) * 100:.0f}%")
    return "\n".join(lines)


def send_digest() -> dict[str, Any]:
    settings = get_settings()
    required = (settings.smtp_host, settings.smtp_user, settings.smtp_pass,
                settings.digest_from, settings.digest_to)
    if not all(required):
        log.info("digest_skip_unconfigured")
        return {"ok": False, "reason": "smtp_not_configured"}

    data = _gather()
    msg = EmailMessage()
    msg["Subject"] = f"Terminal-BTC digest {data['date']} — {'+' if data['realized_pnl_usdt'] >= 0 else ''}${data['realized_pnl_usdt']:.2f}"
    msg["From"] = settings.digest_from
    msg["To"] = settings.digest_to
    msg.set_content(render_digest_text(data))
    msg.add_alternative(render_digest_html(data), subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_pass)
            smtp.send_message(msg)
    except Exception as exc:
        log.error("digest_send_failed", error=str(exc))
        return {"ok": False, "reason": str(exc)}

    log.info("digest_sent", to=settings.digest_to, pnl=data["realized_pnl_usdt"])
    return {"ok": True, "sent_to": settings.digest_to, **data}
