from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from sqlmodel import select

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import DailyPnL

log = get_logger(__name__)

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "system_trader.md"

# Rough pricing (USD / 1M tokens) for claude-opus-4-x. Used purely for daily cost cap.
# These are conservative defaults; override at deploy time if needed.
_OPUS_INPUT_PER_MTOK = 15.0
_OPUS_OUTPUT_PER_MTOK = 75.0

TOOL_SUBMIT_DECISION: dict[str, Any] = {
    "name": "submit_decision",
    "description": "Submit a single trading decision for the current market snapshot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
            "size_pct": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 0.25,
                "description": "Fraction of quote equity. Use 0 for hold.",
            },
            "stop_loss": {"type": "number", "minimum": 0.0},
            "take_profit": {"type": "number", "minimum": 0.0},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reasoning": {"type": "string", "maxLength": 500},
        },
        "required": ["action", "size_pct", "stop_loss", "take_profit", "confidence", "reasoning"],
    },
}


@dataclass
class ClaudeDecision:
    action: str
    size_pct: float
    stop_loss: float
    take_profit: float
    confidence: float
    reasoning: str
    usd_cost: float
    raw_usage: dict[str, int]


class ClaudeCostCapExceeded(Exception):
    pass


@lru_cache(maxsize=1)
def _client() -> Anthropic:
    settings = get_settings()
    return Anthropic(api_key=settings.anthropic_api_key)


@lru_cache(maxsize=1)
def _system_prompt_text() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _estimate_cost_usd(usage: dict[str, int]) -> float:
    in_tok = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get(
        "cache_creation_input_tokens", 0
    )
    out_tok = usage.get("output_tokens", 0)
    return (in_tok / 1_000_000.0) * _OPUS_INPUT_PER_MTOK + (out_tok / 1_000_000.0) * _OPUS_OUTPUT_PER_MTOK


def _today_spend_usd() -> float:
    with get_session() as s:
        row = s.get(DailyPnL, date.today())
        return row.claude_usd_spent if row else 0.0


def _bump_spend(usd: float) -> None:
    with get_session() as s:
        today = date.today()
        row = s.get(DailyPnL, today)
        if row is None:
            row = DailyPnL(day=today)
        row.claude_usd_spent += usd
        s.add(row)
        s.commit()


def generate_decision(snapshot: dict[str, Any], tv_alert: dict[str, Any] | None = None,
                      position: dict[str, Any] | None = None) -> ClaudeDecision:
    settings = get_settings()

    spent = _today_spend_usd()
    if spent >= settings.daily_claude_usd_cap:
        raise ClaudeCostCapExceeded(
            f"Daily Claude spend cap reached: ${spent:.4f} >= ${settings.daily_claude_usd_cap:.2f}"
        )

    user_payload = {"snapshot": snapshot, "tv_alert": tv_alert, "position": position}

    system_blocks = [
        {
            "type": "text",
            "text": _system_prompt_text(),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    resp = _client().messages.create(
        model=settings.claude_model,
        max_tokens=settings.claude_max_tokens,
        system=system_blocks,
        tools=[TOOL_SUBMIT_DECISION],
        tool_choice={"type": "tool", "name": "submit_decision"},
        messages=[{"role": "user", "content": json.dumps(user_payload)}],
    )

    tool_use = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError("Claude did not return the submit_decision tool call")

    decision_args: dict[str, Any] = dict(tool_use.input)
    usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
    cost = _estimate_cost_usd(usage)
    _bump_spend(cost)

    log.info(
        "claude_decision",
        action=decision_args["action"],
        size_pct=decision_args["size_pct"],
        confidence=decision_args["confidence"],
        usd_cost=round(cost, 6),
        usage=usage,
    )

    return ClaudeDecision(
        action=decision_args["action"],
        size_pct=float(decision_args["size_pct"]),
        stop_loss=float(decision_args["stop_loss"]),
        take_profit=float(decision_args["take_profit"]),
        confidence=float(decision_args["confidence"]),
        reasoning=str(decision_args["reasoning"]),
        usd_cost=cost,
        raw_usage=usage,
    )
