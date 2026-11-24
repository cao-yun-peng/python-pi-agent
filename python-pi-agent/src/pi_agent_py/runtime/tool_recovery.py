"""Shared, pure unknown-outcome compensation; never retry a tool here."""

from ..domain import Message, ToolCall


def interrupted_tool_result(call: ToolCall) -> Message:
    return Message(
        role="tool",
        content=(
            "Tool execution did not produce a durable result because the previous "
            "agent run was interrupted. The external side-effect outcome is unknown. "
            "Do not assume the call failed or retry it automatically."
        ),
        tool_call_id=call.id,
        tool_name=call.name,
        is_error=True,
        structured_content={
            "status": "interrupted",
            "outcome": "unknown",
            "recovered": True,
        },
    )
