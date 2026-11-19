"""Bound model-visible tool text while preserving its outcome and JSON data."""

from dataclasses import dataclass, replace
from typing import Literal

from ..domain import ToolOutput, ToolResultContext


@dataclass(frozen=True, slots=True)
class TruncateToolResults:
    max_chars: int
    keep: Literal["head", "tail", "head_tail"] = "head_tail"

    def __post_init__(self) -> None:
        if type(self.max_chars) is not int or self.max_chars < 64:
            raise ValueError("max_chars must be an integer >= 64")
        if self.keep not in ("head", "tail", "head_tail"):
            raise ValueError("unknown truncation mode")

    async def transform(
        self, context: ToolResultContext, output: ToolOutput
    ) -> ToolOutput:
        text = output.content
        if len(text) <= self.max_chars:
            return output
        omitted = len(text)
        while True:
            marker = f"\n[... truncated {omitted} characters ...]\n"
            budget = max(0, self.max_chars - len(marker))
            actual = len(text) - budget
            if actual == omitted:
                break
            omitted = actual
        head = (
            budget if self.keep == "head" else 0 if self.keep == "tail" else budget // 2
        )
        tail = budget - head
        content = text[:head] + marker + (text[-tail:] if tail else "")
        return replace(output, content=content)
