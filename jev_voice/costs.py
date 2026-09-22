"""Cost accounting for a run. Pure arithmetic over the usage objects the APIs return.

Jev (TypeSafe System One) is charged per input token; output tokens are free
(docs.typesafe.ai/models: $42 per billion = $0.042 per million input tokens for jev-1.13).
The text helper is OpenAI-compatible; set its prices per million tokens in .env
(TEXT_MODEL_PRICE_IN / TEXT_MODEL_PRICE_OUT) or its cost shows as tokens only.
"""
from __future__ import annotations

import os
from typing import Any

JEV_PRICE_IN = float(os.environ.get("JEV_PRICE_PER_MTOK_IN", "0.042"))       # USD per 1M input tokens
JEV_PRICE_OUT = float(os.environ.get("JEV_PRICE_PER_MTOK_OUT", "0"))          # output is free
TEXT_PRICE_IN = os.environ.get("TEXT_MODEL_PRICE_IN")                        # USD per 1M prompt tokens, optional
TEXT_PRICE_OUT = os.environ.get("TEXT_MODEL_PRICE_OUT")                      # USD per 1M completion tokens, optional


def jev_cost(usage: dict[str, Any] | None) -> float:
    u = usage or {}
    return (float(u.get("input_tokens", 0)) * JEV_PRICE_IN + float(u.get("output_tokens", 0)) * JEV_PRICE_OUT) / 1e6


CLAUDE_PRICES = {  # USD per 1M tokens (input, output)
    "claude-fable-5-1": (10.0, 50.0), "claude-fable-5": (10.0, 50.0),
    "claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0), "claude-opus-4-7": (5.0, 25.0), "claude-opus-4-6": (5.0, 25.0),
}


def text_cost(usage: dict[str, Any] | None, model: str | None = None) -> float | None:
    u = usage or {}
    tokens_in = float(u.get("prompt_tokens", u.get("input_tokens", 0)))
    tokens_out = float(u.get("completion_tokens", u.get("output_tokens", 0)))
    if model in CLAUDE_PRICES:
        pin, pout = CLAUDE_PRICES[model]
        return (tokens_in * pin + tokens_out * pout) / 1e6
    if TEXT_PRICE_IN is None and TEXT_PRICE_OUT is None:
        return None
    return (tokens_in * float(TEXT_PRICE_IN or 0) + tokens_out * float(TEXT_PRICE_OUT or 0)) / 1e6


def run_costs(decisions: list[dict[str, Any]], text_calls: list[dict[str, Any]]) -> dict[str, Any]:
    # A text value selected by Jev ("jev:select") is a Jev request, billed like any other.
    jev_calls = [*decisions, *(t for t in text_calls if str(t.get("model", "")).startswith("jev:"))]
    text_calls = [t for t in text_calls if not str(t.get("model", "")).startswith("jev:")]
    jev_in = sum(int((d.get("usage") or {}).get("input_tokens", 0)) for d in jev_calls)
    jev_out = sum(int((d.get("usage") or {}).get("output_tokens", 0)) for d in jev_calls)
    jev_usd = sum(jev_cost(d.get("usage")) for d in jev_calls)
    text_in = sum(int((t.get("usage") or {}).get("prompt_tokens", (t.get("usage") or {}).get("input_tokens", 0))) for t in text_calls)
    text_out = sum(int((t.get("usage") or {}).get("completion_tokens", (t.get("usage") or {}).get("output_tokens", 0))) for t in text_calls)
    text_usd_parts = [text_cost(t.get("usage"), t.get("model")) for t in text_calls]
    text_usd = None if any(p is None for p in text_usd_parts) else sum(text_usd_parts)
    total = jev_usd + (text_usd or 0.0)
    return {
        "jev": {"requests": len(jev_calls), "input_tokens": jev_in, "output_tokens": jev_out, "usd": jev_usd,
                "price_per_mtok_in": JEV_PRICE_IN},
        "text": {"requests": len(text_calls), "prompt_tokens": text_in, "completion_tokens": text_out, "usd": text_usd,
                 "priced": text_usd is not None or not text_calls},
        "total_usd": total,
        "per_decision_usd": (jev_usd / len(decisions)) if decisions else 0.0,
        "last_decision_usd": jev_cost(decisions[-1].get("usage")) if decisions else 0.0,
    }


def usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "$0"
    if value < 0.001:
        return f"${value:.5f}"
    if value < 1:
        return f"${value:.4f}"
    return f"${value:.2f}"
