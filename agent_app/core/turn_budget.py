"""Per-turn limits for interactive runs, so a looping model pauses for the user."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TurnBudget:
    max_model_calls: int
    max_tool_calls: int
    max_tokens: int
    model_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0

    def record_model_call(self, usage, estimated_input: int) -> None:
        """Count reported tokens, or the request estimate when the provider reports none."""
        self.model_calls += 1
        reported = (getattr(usage, "input_tokens", None) or 0) + (getattr(usage, "output_tokens", None) or 0)
        self.tokens += reported or max(0, estimated_input)

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def exceeded(self) -> bool:
        return (self.model_calls >= self.max_model_calls or self.tool_calls >= self.max_tool_calls
                or self.tokens >= self.max_tokens)

    def summary(self) -> str:
        return f"本轮已调用模型 {self.model_calls} 次、工具 {self.tool_calls} 次、约 {self.tokens} tokens"

    def reset(self) -> None:
        self.model_calls = self.tool_calls = self.tokens = 0
