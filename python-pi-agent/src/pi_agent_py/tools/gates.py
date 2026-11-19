"""Dispatch trusted tools under declared side-effect and allowlist policies."""

from __future__ import annotations

from collections.abc import Iterable

from ..domain import JSONObject, ToolExecutionContext, ToolOutput, ToolSideEffects
from ..errors import ToolExecutionDeniedError
from ..ports import Tool


class DeclaredSideEffectGate:
    """In-process dispatcher gated by declared effect and tool allowlists.

    This class is intentionally named a gate: it trusts tool declarations and
    does not isolate Python handlers from the host process.
    """

    def __init__(
        self,
        *,
        allowed_side_effects: Iterable[ToolSideEffects] = ("none",),
        allowed_tools: Iterable[str] | None = None,
    ) -> None:
        effects = frozenset(allowed_side_effects)
        valid_effects = {"none", "read", "write", "external"}
        if not effects:
            raise ValueError("allowed_side_effects must not be empty")
        if not effects <= valid_effects:
            raise ValueError("allowed_side_effects contains an unknown effect")
        tools = None if allowed_tools is None else frozenset(allowed_tools)
        if tools is not None and any(
            not isinstance(name, str) or not name.strip() for name in tools
        ):
            raise ValueError("allowed_tools must contain only non-empty names")
        self._allowed_side_effects = effects
        self._allowed_tools = tools

    async def execute(
        self,
        tool: Tool,
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        definition = context.definition
        if definition.requires_approval and (
            context.approval is None or not context.approval.approved
        ):
            raise ToolExecutionDeniedError(
                f'tool "{definition.name}" requires an approved decision'
            )
        if definition.side_effects not in self._allowed_side_effects:
            raise ToolExecutionDeniedError(
                f'tool "{definition.name}" declares disallowed '
                f'effect "{definition.side_effects}"'
            )
        if (
            self._allowed_tools is not None
            and definition.name not in self._allowed_tools
        ):
            raise ToolExecutionDeniedError(
                f'tool "{definition.name}" is not in the execution allowlist'
            )
        return await tool.execute(arguments, context)


class TrustedToolExecutionGate(DeclaredSideEffectGate):
    """Explicitly permissive dispatcher for already-trusted Python tools."""

    def __init__(self) -> None:
        super().__init__(
            allowed_side_effects=("none", "read", "write", "external")
        )
