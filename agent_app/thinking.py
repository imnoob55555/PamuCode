"""Thinking levels shared by the REPL, prompt borders and model requests.

Five levels match the built-in budgets of pi's coding agent: ``off`` sends
no ``thinking`` field, the other levels enable Anthropic extended thinking
with an explicit token budget.  The border colors mirror pi's dark theme.
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_THINKING_LEVEL = "off"
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high")
THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}
THINKING_LABELS = {
    "off": "关闭 · 不启用 extended thinking",
    "minimal": "最小 · 1024 tokens",
    "low": "低 · 2048 tokens",
    "medium": "中 · 8192 tokens",
    "high": "高 · 16384 tokens",
}
THINKING_COLORS = {
    "off": "#808080",
    "minimal": "#6e6e6e",
    "low": "#5f87af",
    "medium": "#81a2be",
    "high": "#b294bb",
}
MIN_ANSWER_TOKENS = 1024

_THINKING_BLOCK_TYPES = ("thinking", "redacted_thinking")


def normalize_level(value) -> str | None:
    """Return a known level for ``value``, or ``None`` when unknown."""
    if not isinstance(value, str):
        return None
    level = value.strip().casefold()
    return level if level in THINKING_LEVELS else None


def style_class(level) -> str:
    """Return the prompt_toolkit style class for a thinking level."""
    return f"class:thinking-{normalize_level(level) or DEFAULT_THINKING_LEVEL}"


def plan_request(
    level: str, max_tokens: int, ceiling: int
) -> tuple[dict | None, int]:
    """Return ``(thinking payload, max_tokens)`` for one model request.

    The budget shares the response ceiling with the answer, so the request
    grows by the budget and always keeps ``MIN_ANSWER_TOKENS`` for output.
    ``off`` returns no payload and the unchanged token limit.
    """
    budget = THINKING_BUDGETS.get(normalize_level(level) or "")
    if budget is None:
        return None, max_tokens
    limit = min(max_tokens + budget, max(ceiling, max_tokens))
    budget = min(budget, max(0, limit - MIN_ANSWER_TOKENS))
    if budget <= 0:
        return None, max_tokens
    return {"type": "enabled", "budget_tokens": budget}, limit


def strip_thinking_blocks(messages: Sequence) -> list:
    """Return a copy of ``messages`` without thinking blocks.

    Requests made while thinking is disabled must not include blocks from
    earlier thinking-enabled turns.  Assistant messages left without any
    content are dropped; the stored history is never mutated.
    """
    cleaned = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            cleaned.append(message)
            continue
        blocks = [
            block
            for block in content
            if _block_type(block) not in _THINKING_BLOCK_TYPES
        ]
        if not blocks:
            continue
        updated = dict(message)
        updated["content"] = blocks
        cleaned.append(updated)
    return cleaned


def _block_type(block):
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)
