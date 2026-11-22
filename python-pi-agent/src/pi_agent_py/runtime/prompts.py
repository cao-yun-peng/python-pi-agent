"""Deterministic, I/O-free rendering shared by standalone and session agents."""

from collections.abc import Sequence

from ..domain import SystemPromptSpec, ToolDefinition


def render_system_prompt(spec: SystemPromptSpec, tools: Sequence[ToolDefinition]) -> str:
    """Preserve the preamble and emit trimmed, stable, unique rule bullets."""
    rules = list(spec.guidelines)
    if spec.include_tool_guidelines:
        for tool in tools:
            rules.extend(tool.prompt_guidelines)
    unique = dict.fromkeys(rule.strip() for rule in rules if rule.strip())
    if not unique:
        return spec.preamble
    return spec.preamble + "\n\n<rules>\n" + "\n".join(
        f"- {rule}" for rule in unique
    ) + "\n</rules>"
