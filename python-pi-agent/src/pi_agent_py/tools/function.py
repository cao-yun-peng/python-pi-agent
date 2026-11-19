"""Adapt async Python functions to the Tool port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..domain import JSONObject, ToolDefinition, ToolExecutionContext, ToolOutput


ToolHandler = Callable[
    [JSONObject, ToolExecutionContext],
    Awaitable[ToolOutput],
]


class FunctionTool:
    """Adapts an async Python function to the Tool port."""

    def __init__(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        self._definition = definition
        self._handler = handler

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        return await self._handler(arguments, context)
